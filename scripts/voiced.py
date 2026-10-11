#!/usr/bin/env python3
"""
voiced.py — резидентный триггер-цикл ассистента: микрофон -> VAD -> сегмент -> ASR -> текст.

ЗАЧЕМ ОТДЕЛЬНЫЙ ЦИКЛ, А НЕ ГОТОВЫЙ sherpa-onnx-vad-alsa:
проверено на живых прогонах — сегменты sherpa начинаются ровно на пересечении
порога VAD (temp_start_ = current_sample_, без пре-ролла), из-за чего срезается
начало фразы: «Включить свет в коридоре» -> «Свет в коридоре», «Напомни через
десять минут выключить плиту» -> «Десять минут выключить прибор». Для голосовых
команд это фатально: теряется глагол. Здесь держим кольцевой буфер
--pre-roll секунд ДО пересечения порога, и сегмент начинается раньше.

Распознавание выполняет РЕЗИДЕНТНЫЙ ASR-сервер (asrd, порт 6006) — тот же
процесс, что и в listen-and-recognize.sh; сегмент посылается по websocket,
текст печатается в stdout. Сервер недоступен -> сегмент сохраняется в spool,
цикл продолжается.

Формат строк вывода — «N: текст» (как у sherpa-vad-alsa), чтобы скрипты-клиенты
не зависели от того, кто делает распознавание.

Грабли, учтённые в коде: у микрофона единственный ALSA-сабдивайс — arecord
здесь и есть единственный потребитель устройства; vad-alsa/sherpa-скрипты
параллельно запущены быть не могут (ALSA вернёт busy).
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import struct
import subprocess
import sys
import time
import wave
from collections import deque
from dataclasses import dataclass

# Соседние скрипты репозитория. Путь добавляем ЗДЕСЬ, а не в main(): иначе
# вспомогательные функции уровня модуля (recognize_segment) падают с
# NameError — было ровно так после рефакторинга.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_ws      # noqa: E402
import assistant   # noqa: E402

WINDOW_SAMPLES = 512
"""Окно silero VAD на 16 кГц (32 мс) — как в sherpa (window_size=512)."""


# ---------------------------------------------------------------------------
# Машина состояний речи. Единственная сложная логика демона — потому вынесена
# и тестируется отдельно: вместо модели подставляется любой источник
# вероятностей (см. tools/test_tts_core.py::test_voiced_gate).
# ---------------------------------------------------------------------------
@dataclass
class GateConfig:
    threshold: float = 0.5         # ниже вероятности = тишина
    neg_offset: float = 0.15       # neg_threshold = threshold - offset (как в sherpa)
    min_speech: float = 0.25       # с: сколько подряд речи до решения «это речь»
    min_silence: float = 0.6       # с: сколько подряд тишины до закрытия сегмента
    max_speech: float = 20.0       # с: вынужденный разрез длинной реплики
    pre_roll: float = 0.4          # с: аудио ДО пересечения порога попадает в сегмент
    sample_rate: int = 16000


class SpeechGate:
    """Кормится окнами PCM-байтов; выдаёт SpeechSegment на закрытии реплики.

    Семантика повторяет sherpa (threshold/neg_threshold/min_*), отличие одно:
    при решении «это речь» сегмент открывается с откатом на pre_roll секунд
    назад по кольцевому буферу — компенсация задержки срабатывания VAD.
    Одно окно попадает в сегмент ровно один раз: либо из кольцевого буфера
    (окно срабатывания и предыдущие), либо прямым добавлением.
    """

    def __init__(self, cfg: GateConfig, prob_fn=None):
        self.cfg = cfg
        self.prob_fn = prob_fn or (lambda window: 0.0)
        self.window_bytes = WINDOW_SAMPLES * 2
        # Кольцевой буфер пре-ролла и текущий сегмент хранят СЫРЫЕ PCM-байты окон.
        self._ring = deque(maxlen=max(1, int(cfg.pre_roll * cfg.sample_rate
                                         // WINDOW_SAMPLES)))
        # Параллельно кольцу — «было ли окно речью»: по этому счётчику сегмент
        # сообщает длину РЕЧИ (не файла), что и различает одно позванное слово
        # от длинной фразы. Длина файла не годится: в сегменте всегда есть
        # пре-ролл, и она почти не зависит от сказанного.
        self._hot = deque(maxlen=self._ring.maxlen)
        self._piece = bytearray()
        self._triggered = False
        self._samples_seen = 0       # глобальные часы: сэмплов принято всего
        self._cand_at = None         # сэмпл-кандидат: первое пересечение порога
        self._speech_run = 0         # сэмплов речи внутри открытого сегмента
        self._silence_run = 0        # сэмплов подряд при prob <= neg_threshold
        self._counter = 0            # номер сегмента в строке отчёта «N: текст»

    def reset(self) -> None:
        """После закрытия сегмента: кольцевой буфер сохраняем (последние окна
        тишины станут пре-роллом следующей реплики), счётчики сбрасываем."""
        self._piece.clear()
        self._triggered = False
        self._cand_at = None
        self._speech_run = 0
        self._silence_run = 0

    def flush(self) -> None:
        """Забыть всё накопленное (после собственной озвучки).

        Пока играет ответ, микрофон слышит динамик: и текущий сегмент, и
        кольцо пре-ролла заполнены нашим же голосом. Их надо выбросить, иначе
        следующий сегмент начнётся с эха реплики. Номер сегмента НЕ сбрасываем:
        нумерация строк в журнале остаётся непрерывной.
        """
        self._ring.clear()
        self._hot.clear()
        self._piece.clear()
        self._triggered = False
        self._cand_at = None
        self._speech_run = 0
        self._silence_run = 0

    def feed(self, window: bytes) -> dict | None:
        """Одно окно (WINDOW_SAMPLES int16). Возврат при закрытии сегмента:
        {"index", "pcm": bytes, "samples": int}.
        """
        if len(window) != self.window_bytes:
            raise ValueError(f"ожидается окно {self.window_bytes} байт, получено {len(window)}")
        prob = float(self.prob_fn(window))
        cfg = self.cfg
        neg = max(0.0, cfg.threshold - cfg.neg_offset)
        self._ring.append(window)
        self._hot.append(prob > cfg.threshold)
        self._samples_seen += WINDOW_SAMPLES

        if not self._triggered:
            # Семантика sherpa: кандидат открывается на первом пересечении
            # порога и ЖИВЁТ, пока вероятности не ушли в шум (<= neg). Окна в
            # полосе (neg..threshold) кандидата НЕ сбрасывают — «Напомни» даёт
            # слабые окна подряд, и последовательная логика теряет начало
            # фразы (проверено на плате: без этого фраза не триггерилась).
            if prob > cfg.threshold:
                if self._cand_at is None:
                    self._cand_at = self._samples_seen - WINDOW_SAMPLES
                now = self._samples_seen
                if now - self._cand_at >= int(cfg.min_speech * cfg.sample_rate):
                    self._triggered = True
                    # Буфер содержит пре-ролл И текущее окно: высыпаем целиком.
                    for w in self._ring:
                        self._piece.extend(w)
                    # Речь в пре-ролле тоже считаем: именно она — «позвали».
                    self._speech_run = sum(self._hot) * WINDOW_SAMPLES
                    self._ring.clear()
                    self._silence_run = 0
                    return None
            elif prob <= neg:
                self._cand_at = None
            return None

        # --- сегмент открыт: окно пришло впервые, добавляем один раз --------
        self._piece.extend(window)
        if prob > cfg.threshold:
            self._speech_run += WINDOW_SAMPLES

        samples = len(self._piece) // 2
        if prob <= neg:
            self._silence_run += WINDOW_SAMPLES
            if self._silence_run >= int(cfg.min_silence * cfg.sample_rate):
                return self._close(trim_tail=self._silence_run)
        else:
            self._silence_run = 0
            if samples >= int(cfg.max_speech * cfg.sample_rate):
                # Вынужденный разрез длинной реплики: ASR получит её частями.
                return self._close(trim_tail=0)
        return None

    def _close(self, trim_tail: int) -> dict:
        # Хвостовую тишину подрезаем до последнего «неслышного» окна: ASR
        # всё равно её скушает, но в spool и в отчёт попадает honest-длина.
        tail_windows = trim_tail // WINDOW_SAMPLES if trim_tail else 0
        end = len(self._piece) - tail_windows * self.window_bytes
        pcm = bytes(self._piece[:max(0, end)])
        speech = self._speech_run
        self._counter += 1
        self.reset()
        return {"index": self._counter - 1, "pcm": pcm,
                "samples": len(pcm) // 2,
                # Длина РЕЧИ (секунды) — по ней отличаем короткое «позвали»
                # от длинной фразы, когда ASR расслышал имя иначе.
                "speech": speech / 16000.0}


# ---------------------------------------------------------------------------
# Адаптер silero VAD (onnxruntime).
# ---------------------------------------------------------------------------
class SileroVad:
    """silero VAD через onnxruntime. У модели на плате СТАРЫЙ LSTM-интерфейс
    (входы x/h/c, состояния [2,1,64], выход prob/new_h/new_c) — у свежих
    файлов он другой (input/state/sr, состояние [2,1,128]). Адаптер выбирает
    режим по сигнатуре сессии, чтобы voiced не зависел от того, какой именно
    silero_vad.onnx лежит на машине.
    """

    def __init__(self, model_path: str, threads: int = 1):
        import numpy as np
        import onnxruntime as ort
        self._np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self._sess = ort.InferenceSession(model_path, sess_options=opts,
                                          providers=["CPUExecutionProvider"])
        self._in = {i.name: i for i in self._sess.get_inputs()}
        self._mode = "input" if "input" in self._in else "x"
        if self._mode == "input":
            self._state = self._zeros("state")
            self._sr = np.array([16000], dtype=np.int64)
            self._feed_names = ("input", "state", "sr")
            self._out_names = ["output", "state"]
        else:
            self._h = self._zeros("h")
            self._c = self._zeros("c")
            self._feed_names = ("x", "h", "c")
            self._out_names = ["prob", "new_h", "new_c"]

    def _zeros(self, in_name: str):
        """Нулевое состояние по onnx-сигнатуре; динамические размерности (=0,
        строки) заменяются на 1 — у этих моделей фиксированные батчи."""
        shape = [d if isinstance(d, int) and d > 0 else 1
                 for d in self._in[in_name].shape]
        return self._np.zeros(shape, dtype=self._np.float32)

    def prob(self, window: bytes) -> float:
        """Окно int16 -> вероятность речи. int16 -> float32 /32768 (шкала [-1, 1])."""
        x = self._np.frombuffer(window, dtype=self._np.int16)
        x = (x.astype(self._np.float32) / 32768.0).reshape(1, -1)
        if self._mode == "input":
            feed = {"input": x, "state": self._state, "sr": self._sr}
            out, self._state = self._sess.run(self._out_names, feed)
            return float(out[0][0])
        feed = {"x": x, "h": self._h, "c": self._c}
        prob, self._h, self._c = self._sess.run(self._out_names, feed)
        return float(prob[0][0])


# Насколько поздно после своей реплики ловить эхо динамика. Хвост колонки и
# отражение звука живут доли секунды; всё, что позже этого окна, — уже не эхо,
# а речь человека (иначе повтор своей же команды молча теряется).
ECHO_WINDOW = 2.5


def peak_db(pcm: bytes) -> float:
    """Пиковый уровень сегмента: сразу видно, был ли звук вообще."""
    n = len(pcm) // 2
    if not n:
        return -100.0
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    peak = max((abs(v) for v in samples), default=0)
    return 20.0 * math.log10(max(1, peak) / 32767.0)


def bandpass_file(path: str) -> bytes | None:
    """Речевая полоса 120-7500 Гц + нормализация (sox). None, если не вышло.

    Нужна как ВТОРАЯ попытка: микрофон платы даёт SNR 1-9 дБ, и ASR иногда
    читает сегмент в пустоту. На фильтрованной версии та же фраза читается
    («Эсто, включи свет в коридоре» вместо мусора) — замерено на сегментах
    пользователя.
    """
    out = "/tmp/_voiced_bp.wav"
    try:
        rc = subprocess.run(["sox", path, out, "highpass", "120", "lowpass",
                             "7500", "norm", "-3"],
                            capture_output=True, timeout=5).returncode
        if rc != 0:
            return None
        with wave.open(out, "rb") as w:
            return w.readframes(w.getnframes())
    except (OSError, subprocess.SubprocessError, wave.Error):
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def recognize_segment(path: str, pcm: bytes, addr: str, port: int):
    """Распознать сегмент -> (текст, секунд).

    Пустой ответ ASR — это «не расслышал», а не «ничего не было»: по нему
    нельзя молча выбрасывать фразу (именно так терялись команды после сигнала —
    в журнале не оставалось ни строки). Поэтому сначала исходный вариант,
    затем полоса 300-7000 Гц.
    """
    def run(raw: bytes) -> str:
        return asyncio.run(asr_ws.recognize(
            asr_ws.samples_message(16000, asr_ws.pcm16_to_float32(raw)),
            addr, port))

    t0 = time.monotonic()
    # Сначала полоса 120-7500 Гц: шум микрофона широкополосный (в 3-8 кГц
    # почти треть энергии), и фильтр даёт +4.6 дБ SNR — замерено на записи
    # через динамик (7.0 -> 11.6 дБ).
    bp = bandpass_file(path)
    if bp:
        text = run(asr_ws.normalize_pcm16(bp))
        if text.strip():
            return text, time.monotonic() - t0
    # Вторая попытка — исходный сегмент: если фильтр что-то испортил, вернёмся
    # к нему. Раньше порядок был обратный, и пустой ответ фильтра означал, что
    # фраза потеряна целиком.
    text = run(asr_ws.normalize_pcm16(pcm))
    if text.strip() and bp:
        print(f"[asr] на полосе пусто, на исходном: «{text}»", flush=True)
    return text, time.monotonic() - t0


def write_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    """PCM-байты int16 -> WAV без внешних зависимостей (struct, не soundfile)."""
    chunk = len(pcm)
    byte_rate = sample_rate * 2
    with open(path, "wb") as fh:
        fh.write(b"RIFF" + struct.pack("<I", 36 + chunk) + b"WAVE")
        fh.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                       byte_rate, 2, 16))
        fh.write(b"data" + struct.pack("<I", chunk) + pcm)


# ---------------------------------------------------------------------------
# Демон
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Резидентный триггер-цикл: VAD на микрофоне -> ASR-сервер -> текст")
    ap.add_argument("--device", default="plughw:5,0",
                    help="ALSA-устройство захвата (см. arecord -l)")
    ap.add_argument("--vad-model", default=os.path.expanduser("~/models/silero_vad.onnx"))
    ap.add_argument("--spool-dir", default=os.path.expanduser("~/.assistant/segments"))
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--min-speech", type=float, default=0.15,
                    help="сколько речи подряд нужно, чтобы открыть сегмент (бодрствуя)")
    ap.add_argument("--min-speech-sleep", type=float, default=0.15,
                    help="то же для состояния сна: имя — одно короткое слово, и "
                         "замеры показали 0.22 с речи у «Джарвиса» против порога "
                         "0.25 с — имени не хватало одного окна")
    ap.add_argument("--min-silence", type=float, default=0.5,
                    help="тишина, закрывающая сегмент бодрствуя: от неё зависит "
                         "скорость ответа на команду")
    ap.add_argument("--min-silence-sleep", type=float, default=0.4,
                    help="тишина, закрывающая сегмент во сне: от неё напрямую "
                         "зависит задержка сигнала после имени (0.4 с против "
                         "0.6 с — слышно как «отвечает быстрее»)")
    ap.add_argument("--max-speech", type=float, default=20.0)
    ap.add_argument("--pre-roll", type=float, default=2.0,
                    help="секунд аудио до пересечения порога включать в сегмент "
                         "(задержка срабатывания VAD на слабых начальных словах "
                         "достигает ~0.9 с — меньше, чем она, не ставить; запас "
                         "нужен и для имени ассистента, которое произносят тихо)")
    ap.add_argument("--gain", type=float, default=4.0,
                    help="программный гейн микрофона до VAD/ASR (1..8); "
                         "микрофон платы отдаёт тихий сигнал")
    ap.add_argument("--no-asr", action="store_true",
                    help="только писать сегменты в spool (для отладки)")
    ap.add_argument("--no-wake", action="store_true",
                    help="без имени: отвечать на любую распознанную фразу")
    ap.add_argument("--wake-word", default=None,
                    help="имя ассистента для пробуждения (список через запятую; "
                         "по умолчанию — из assistant.DEFAULT_WAKE_WORD)")
    ap.add_argument("--wake-similarity", type=float, default=None,
                    help="строгость узнавания имени (0..1). По умолчанию 0.6 — "
                         "подобрано по живым логам; больше = строже, меньше = "
                         "откликается на похожие слова")
    ap.add_argument("--awake-timeout", type=float, default=12.0,
                    help="сколько секунд ждать команду после имени, пока снова "
                         "не заснуть (как у колонок)")
    ap.add_argument("--timeout-phrase", default="",
                    help="что сказать, засыпая по таймауту (по умолчанию молча)")
    ap.add_argument("--no-wake-beep", action="store_true",
                    help="не играть короткий сигнал пробуждения")
    ap.add_argument("--ignore-without-name", action="store_true",
                    help="отвечать ТОЛЬКО когда расслышано имя. По умолчанию "
                         "фраза без имени тоже выполняется: имя часто теряется "
                         "на слабом микрофоне, а команда распознаётся хорошо")
    ap.add_argument("--instant-call", action="store_true",
                    help="сигнал сразу при закрытии сегмента, НЕ проверяя имя "
                         "через ASR. Быстрее на ~0.25 с, но откликается на любой "
                         "короткий звук — по умолчанию выключено")
    ap.add_argument("--call-min-speech", type=float, default=0.3,
                    help="меньше этого (с) речь — шум, а не оклик: щелчки и "
                         "скрипы дают 0.15-0.26 с")
    ap.add_argument("--call-max-speech", type=float, default=1.0,
                    help="сколько речи (с) считать коротким обращением: если "
                         "ASR расслышал имя иначе, но речи было не больше этого — "
                         "всё равно откликаемся (слабый микрофон читает имя "
                         "по-разному: «Астя», «Аста», «Прости»). 0 отключает")
    ap.add_argument("--no-reply", action="store_true",
                    help="не отвечать голосом: только распознавать и писать в журнал")
    ap.add_argument("--reply-template", default=None,
                    help="шаблон ответа, {text} — распознанный текст "
                         "(по умолчанию — шаблон-заглушка из assistant.py)")
    ap.add_argument("--reply-voice", default=None, help="голос для озвучки ответа")
    ap.add_argument("--asr-addr", default=os.environ.get("ASR_ADDR", "127.0.0.1"))
    ap.add_argument("--asr-port", type=int, default=int(os.environ.get("ASR_PORT", "6006")))
    ap.add_argument("--keep", type=int, default=20,
                    help="сколько последних сегментов держать в spool")
    args = ap.parse_args()

    for path in (args.vad_model,):
        if not os.path.exists(path):
            print(f"voiced: нет модели VAD: {path}", file=sys.stderr)
            return 2
    os.makedirs(args.spool_dir, exist_ok=True)

    vad = SileroVad(args.vad_model)
    gate = SpeechGate(GateConfig(threshold=args.threshold,
                                 min_speech=args.min_speech,
                                 min_silence=args.min_silence,
                                 max_speech=args.max_speech,
                                 pre_roll=args.pre_roll),
                      prob_fn=vad.prob)

    # arecord — единственный владелец микрофона; сырое 16k int16 по stdout.
    # stderr глушим: во время озвучки ответа мы намеренно не читаем поток, и
    # ALSA пишет туда «overrun!!! (at least NNN ms long)» — это шум, а не
    # ошибка. О настоящем отказе сообщит код возврата процесса (см. выход из
    # цикла ниже): раньше это было видно только по тишине в журнале.
    arecord = subprocess.Popen(
        ["arecord", "-q", "-D", args.device, "-f", "S16_LE",
         "-r", "16000", "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    print(f"[voiced] слушаю {args.device}; VAD {args.vad_model}", flush=True)

    import asyncio
    # asr_ws и assistant импортированы на уровне модуля (см. шапку).

    template = args.reply_template or assistant.DEFAULT_TEMPLATE
    # Имя ассистента: без --no-wake отвечаем ТОЛЬКО после него. Это лечит
    # «съеденные» фразы: команда идёт после пробуждения, её запись начинается
    # уже на бодрствующем gate — пре-ролл на неё больше не влияет.
    wake = None
    if not args.no_wake:
        wake = assistant.WakeState(words=args.wake_word,
                                   timeout=args.awake_timeout,
                                   similarity=args.wake_similarity)
        print(f"[voiced] имя для пробуждения: {args.wake_word or assistant.DEFAULT_WAKE_WORD}; "
              f"ожидание команды {args.awake_timeout:g} с; "
              f"min_speech сна {args.min_speech_sleep:g} с", flush=True)

    # Профиль VAD зависит от состояния: во сне нужна чуткость к одному короткому
    # слову (имени) и БЫСТРОЕ закрытие сегмента, бодрствуя — обычные пороги.
    # Конфиг читается gate'ом на каждом окне, поэтому достаточно менять поля.
    def tune_vad(asleep: bool) -> None:
        gate.cfg.min_speech = (args.min_speech_sleep if asleep
                               else args.min_speech)
        gate.cfg.min_silence = (args.min_silence_sleep if asleep
                                else args.min_silence)

    tune_vad(True)

    pending = b""
    muted_until = 0.0      # до этого момента окна не кормятся в VAD (своя озвучка)
    was_muted = False
    last_reply = None      # предыдущий ответ: по нему ловим эхо динамика
    reply_ended_at = 0.0   # когда ответ доиграл (эхо ловим ТОЛЬКО сразу после)

    def speak(text: str) -> None:
        """Озвучить и заглушить прослушку на время речи (+ хвост колонки)."""
        nonlocal muted_until, was_muted, last_reply, reply_ended_at
        muted_until = time.monotonic() + 30.0   # глухо на всю озвучку
        rc = assistant.synth_and_play(text, voice=args.reply_voice)
        muted_until = time.monotonic() + 0.8    # + хвост колонки
        was_muted = True
        last_reply = text
        reply_ended_at = time.monotonic()
        if rc != 0:
            print(f"[reply] озвучка вернула код {rc} (колонка?)", flush=True)

    try:
        while True:
            chunk = arecord.stdout.read(WINDOW_SAMPLES * 2 * 16)
            if not chunk:
                break
            if wake is not None and wake.tick():
                # Имя было, команды за отведённое время не прозвучало — спим.
                print(f"[wake] команды не дождался за {args.awake_timeout:g} с, "
                      f"засыпаю", flush=True)
                tune_vad(True)
                if args.timeout_phrase:
                    speak(args.timeout_phrase)
            if time.monotonic() < muted_until:
                # Играет наш ответ: читаем поток (чтобы не копить буфер ALSA),
                # но в VAD не отдаём — иначе ассистент услышит сам себя.
                pending = b""
                was_muted = True
                continue
            if was_muted:
                # Вернулись из мьюта: сливаем накопленный хвост и забываем
                # всё, что gate успел набрать из эха собственной реплики.
                pending = b""
                gate.flush()
                was_muted = False
            pending += chunk
            while len(pending) >= WINDOW_SAMPLES * 2:
                window, pending = pending[:WINDOW_SAMPLES * 2], pending[WINDOW_SAMPLES * 2:]
                if args.gain != 1.0:
                    window = asr_ws.gain_pcm16(window, args.gain)
                got = gate.feed(window)
                if got is None:
                    continue
                seg_closed = time.monotonic()
                dur = got["samples"] / 16000.0
                speech_s = got.get("speech", 0.0)
                path = os.path.join(args.spool_dir, f"seg-{got['index']}-{dur:.3f}s.wav")
                write_wav(path, got["pcm"], 16000)
                # Удаление старых сегментов: spool не должен расти бесконечно.
                old = sorted(os.listdir(args.spool_dir))
                for name in old[:-args.keep] if args.keep > 0 else []:
                    try:
                        os.unlink(os.path.join(args.spool_dir, name))
                    except OSError:
                        pass
                # Каждый закрытый сегмент виден в журнале, с уровнем и длиной
                # речи: без этого «команда не дошла» выглядело как полная
                # тишина, и отличить «не было звука» от «ASR не расслышал»
                # было нельзя.
                print(f"[seg] {got['index']}: {dur:.2f}с, речь {speech_s:.2f}с, "
                      f"пик {peak_db(got['pcm']):.0f} дБ", flush=True)
                if args.no_asr:
                    print(f"{got['index']}: {dur:.2f}с -> {path}", flush=True)
                    continue

                # БЫСТРЫЙ ОКЛИК (--instant-call): сигнал сразу при закрытии
                # сегмента, без проверки имени через ASR. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН:
                # на живой эксплуатации это оказалось хуже — сигнал звучал на
                # любой короткий звук (щелчок, скрип, обрывок речи 0.26 с), а
                # ассистент без проверки не знает, позвали его или нет.
                # Экономит всего ~0.25 с (столько занимает ASR), поэтому
                # правильнее проверять имя, а скорость брать из min_silence.
                if (args.instant_call and wake is not None and not wake.awake
                        and assistant.is_short_call(speech_s, args.call_max_speech,
                                                    args.call_min_speech)):
                    print(f"[wake?] короткое обращение ({speech_s:.2f} с речи) — "
                          f"сигнал сразу, без распознавания "
                          f"({(time.monotonic() - seg_closed) * 1000:.0f} мс после "
                          f"закрытия сегмента)", flush=True)
                    wake.wake_up()
                    tune_vad(False)
                    if not args.no_wake_beep:
                        rc = assistant.beep()
                        if rc != 0:
                            print(f"[wake] сигнал не проигрался (aplay rc={rc})",
                                  flush=True)
                    # Распознавание — ПОСЛЕ сигнала: оклик уже отработан, но
                    # если в этой же короткой фразе была и команда («Астра,
                    # стоп»), её нельзя потерять. Задержку отклика это не
                    # возвращает: сигнал уже прозвучал.
                    try:
                        text, _ = recognize_segment(path, got["pcm"],
                                                    args.asr_addr, args.asr_port)
                    except OSError:
                        continue
                    found, rest = assistant.match_wake(text, args.wake_word,
                                                       args.wake_similarity)
                    if found and rest.strip():
                        print(f"[wake] имя и команда одной фразой: {text}",
                              flush=True)
                        payload = rest
                    else:
                        print(f"[wake?] ASR для справки услышал: «{text}»",
                              flush=True)
                        continue
                else:
                    try:
                        text, asr_s = recognize_segment(path, got["pcm"],
                                                        args.asr_addr,
                                                        args.asr_port)
                    except OSError as exc:
                        print(f"{got['index']}: {dur:.2f}с -> сервер недоступен "
                              f"({exc}); сегмент: {path}", flush=True)
                        continue
                    if not text.strip():
                        # «Пусто» — это НЕ «тишина»: сегмент был, звук был
                        # (в строке [seg] выше видно уровень), а разобрать не
                        # удалось. Говорим об этом прямо: окно ожидания ещё
                        # открыто, фразу можно повторить.
                        print(f"[asr] {got['index']}: не расслышал ({asr_s:.2f} с, "
                              f"пик {peak_db(got['pcm']):.0f} дБ) — повторите",
                              flush=True)
                        continue
                    if (assistant.is_echo(text, last_reply)
                            and time.monotonic() - reply_ended_at < ECHO_WINDOW):
                        # Хвост колонки попал в сегмент: отвечать нельзя — цикл.
                        # Окно ограничено по времени НАМЕРЕННО: реплика содержит
                        # текст команды, и без границы повтор той же команды
                        # через полминуты тоже считался бы эхом (проверено).
                        print(f"[echo] пропускаю свою же реплику: {text}",
                              flush=True)
                        continue
                    if wake is None:
                        kind, payload, by_name = "command", text, False
                    else:
                        found, rest = assistant.match_wake(
                            text, args.wake_word, args.wake_similarity)
                        if found and rest.strip():
                            # Имя и команда одной фразой: сигнал не нужен,
                            # отвечаем сразу (для пользователя это самый
                            # быстрый путь).
                            kind, payload, by_name = "command", rest, True
                        elif found:
                            # Прозвучало только имя: сигнал «слушаю» и ждём.
                            kind, payload, by_name = "call", text, True
                        else:
                            # Имя — удобство, а не турникет: на этом микрофоне
                            # оно почти всегда расслышано иначе («Поставь»,
                            # «Астя»), и строгая проверка выбрасывала верно
                            # распознанные команды («Включи свет в коридоре.»
                            # уходило в [sleep]).
                            kind = assistant.classify_utterance(
                                False, speech_s, args.call_min_speech,
                                args.call_max_speech, args.ignore_without_name)
                            payload, by_name = text, False
                    if kind == "call":
                        print(f"[wake] {'услышал имя' if by_name else 'короткое обращение'}"
                              f" ({speech_s:.2f} с речи, ASR: «{text}»)", flush=True)
                        wake.wake_up()
                        tune_vad(False)
                        if not args.no_wake_beep:
                            rc = assistant.beep()
                            if rc != 0:
                                print(f"[wake] сигнал не проигрался (aplay rc={rc})",
                                      flush=True)
                        continue
                    if kind == "ignore":
                        print(f"[sleep] не имя ({text}; речи {speech_s:.2f} с, "
                              f"ASR {asr_s:.2f} с) — флаг --ignore-without-name",
                              flush=True)
                        continue
                    if by_name:
                        print(f"[wake] услышал имя: {text}", flush=True)
                    elif not wake.awake:
                        # Команда без имени: говорим об этом в журнале, чтобы
                        # поведение не выглядело случайным.
                        print(f"[команда без имени] {text}", flush=True)
                    tune_vad(True)

                # --- команда: заглушка запроса в HA -> шаблон -> озвучка ------
                text = payload
                tune_vad(True)           # после команды снова сон
                # Время ASR показываем в строке: по нему видно, сколько именно
                # занимает распознавание (пользователь спрашивал про задержку).
                print(f"{got['index']}: {text}  [ASR {asr_s:.2f} с]", flush=True)
                if args.no_reply:
                    continue
                reply = assistant.make_reply(text, template)
                assistant.stub_intent(text)
                print(f"[reply] {reply}", flush=True)
                speak(reply)
    except KeyboardInterrupt:
        print("[voiced] остановлен клавиатурой", file=sys.stderr)
    finally:
        arecord.terminate()
        # Почему цикл кончился: код arecord отделяет «микрофон отвалился»
        # от «нас останавливают» (terminate -> -15).
        rc = arecord.wait()
        if rc not in (-15, 0):
            print(f"[voiced] arecord завершился с кодом {rc} — микрофон "
                  f"{args.device} недоступен или занят?", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())