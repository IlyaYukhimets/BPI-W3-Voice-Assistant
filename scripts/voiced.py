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
import os
import struct
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

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
        self._piece = bytearray()
        self._triggered = False
        self._samples_seen = 0       # глобальные часы: сэмплов принято всего
        self._cand_at = None         # сэмпл-кандидат: первое пересечение порога
        self._silence_run = 0        # сэмплов подряд при prob <= neg_threshold
        self._counter = 0            # номер сегмента в строке отчёта «N: текст»

    def reset(self) -> None:
        """После закрытия сегмента: кольцевой буфер сохраняем (последние окна
        тишины станут пре-роллом следующей реплики), счётчики сбрасываем."""
        self._piece.clear()
        self._triggered = False
        self._cand_at = None
        self._silence_run = 0

    def flush(self) -> None:
        """Забыть всё накопленное (после собственной озвучки).

        Пока играет ответ, микрофон слышит динамик: и текущий сегмент, и
        кольцо пре-ролла заполнены нашим же голосом. Их надо выбросить, иначе
        следующий сегмент начнётся с эха реплики. Номер сегмента НЕ сбрасываем:
        нумерация строк в журнале остаётся непрерывной.
        """
        self._ring.clear()
        self._piece.clear()
        self._triggered = False
        self._cand_at = None
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
                    self._ring.clear()
                    self._silence_run = 0
                    return None
            elif prob <= neg:
                self._cand_at = None
            return None

        # --- сегмент открыт: окно пришло впервые, добавляем один раз --------
        self._piece.extend(window)

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
        self._counter += 1
        self.reset()
        return {"index": self._counter - 1, "pcm": pcm,
                "samples": len(pcm) // 2}


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
    ap.add_argument("--min-speech", type=float, default=0.25)
    ap.add_argument("--min-silence", type=float, default=0.6)
    ap.add_argument("--max-speech", type=float, default=20.0)
    ap.add_argument("--pre-roll", type=float, default=1.5,
                    help="секунд аудио до пересечения порога включать в сегмент "
                         "(задержка срабатывания VAD на слабых начальных словах "
                         "достигает ~0.9 с — меньше, чем она, не ставить)")
    ap.add_argument("--gain", type=float, default=4.0,
                    help="программный гейн микрофона до VAD/ASR (1..8); "
                         "микрофон платы отдаёт тихий сигнал")
    ap.add_argument("--no-asr", action="store_true",
                    help="только писать сегменты в spool (для отладки)")
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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import asr_ws
    import assistant

    template = args.reply_template or assistant.DEFAULT_TEMPLATE
    pending = b""
    muted_until = 0.0      # до этого момента окна не кормятся в VAD (своя озвучка)
    was_muted = False
    last_reply = None      # предыдущий ответ: по нему ловим эхо динамика
    try:
        while True:
            chunk = arecord.stdout.read(WINDOW_SAMPLES * 2 * 16)
            if not chunk:
                break
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
                dur = got["samples"] / 16000.0
                path = os.path.join(args.spool_dir, f"seg-{got['index']}-{dur:.3f}s.wav")
                write_wav(path, got["pcm"], 16000)
                # Удаление старых сегментов: spool не должен расти бесконечно.
                old = sorted(os.listdir(args.spool_dir))
                for name in old[:-args.keep] if args.keep > 0 else []:
                    try:
                        os.unlink(os.path.join(args.spool_dir, name))
                    except OSError:
                        pass
                if args.no_asr:
                    print(f"{got['index']}: {dur:.2f}с -> {path}", flush=True)
                    continue
                try:
                    text = asyncio.run(asr_ws.recognize(
                        asr_ws.samples_message(16000,
                                               asr_ws.pcm16_to_float32(got["pcm"])),
                        args.asr_addr, args.asr_port))
                except OSError as exc:
                    print(f"{got['index']}: {dur:.2f}с -> сервер недоступен ({exc}); "
                          f"сегмент: {path}", flush=True)
                    continue
                print(f"{got['index']}: {text}", flush=True)
                if args.no_reply or not text.strip():
                    continue
                if assistant.is_echo(text, last_reply):
                    # Хвост колонки попал в сегмент: отвечать нельзя — это цикл.
                    print("[voiced] это эхо собственной реплики, пропускаю",
                          flush=True)
                    continue
                # --- ответ: заглушка запроса в HA -> шаблон -> озвучка ---------
                reply = assistant.make_reply(text, template)
                assistant.stub_intent(text)
                print(f"[reply] {reply}", flush=True)
                muted_until = time.monotonic() + 30.0   # глухо на всю озвучку
                rc = assistant.synth_and_play(reply, voice=args.reply_voice)
                muted_until = time.monotonic() + 0.8    # + хвост колонки
                was_muted = True
                last_reply = reply
                if rc != 0:
                    print(f"[reply] озвучка вернула код {rc} (колонка?)", flush=True)
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