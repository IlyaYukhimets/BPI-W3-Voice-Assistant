#!/usr/bin/env python3
"""voiced.py — резидентный цикл ассистента.

СХЕМА: микрофон -> Vosk-KWS (сторож имени) -> сигнал -> запись команды ->
GigaAM -> ответ голосом.

ПОЧЕМУ ТАК, А НЕ ЧЕРЕЗ VAD (см. docs/13-kws.md — там замеры):

1. Имя сторожит Vosk в режиме грамматики. На записи, где свободное
   распознавание имени давало «Остров», грамматика дала «астра»; на команде не
   сработала; на шуме комнаты — тишина. Прежняя попытка узнать имя по тексту
   ASR (нечёткое сравнение, порог, эвристика «короткого оклика») упиралась в то,
   что микрофон платы расслышивает одно короткое слово почти всегда иначе.

2. VAD (silero) больше не нужен. Точка старта команды известна от KWS: имя
   прозвучало, сигнал отыграл. Конец фразы ловит энергетический детектор
   (CommandRecorder) — одно сравнение громкости кадра с шумовым полом.
   Вместе с VAD ушли пре-ролл 2 с, профили сна и бодрствования, пороги
   min_speech/min_silence и машина состояний с таймаутом 12 с.

3. Команду по-прежнему распознаёт GigaAM: на том же звуке Vosk свободно не
   читает ничего, а GigaAM читает фразу («Включи свет в коридоре.» дословно).
   Роли разные: имя — задача с крошечным словарём (нужна устойчивость),
   команда — открытый словарь (нужно качество модели).

Строки вывода сохранены как раньше: «N: текст» для распознанного, [asr],
[reply], [seg] для диагностики.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import select
import struct
import subprocess
import sys
import time
import wave
from collections import deque

# Соседние скрипты репозитория. Путь добавляем на уровне модуля, иначе
# вспомогательные функции падают с NameError (уже наступали на это).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_ws      # noqa: E402
import assistant   # noqa: E402
import kws         # noqa: E402

SAMPLE_RATE = 16000
FRAME = 512                      # 32 мс: один кадр для KWS и детектора
FRAME_BYTES = FRAME * 2


# ---------------------------------------------------------------------------
# Диагностика и обработка звука сегмента.
# ---------------------------------------------------------------------------
def peak_db(pcm: bytes) -> float:
    """Пиковый уровень сегмента: сразу видно, был ли звук вообще."""
    n = len(pcm) // 2
    if not n:
        return -100.0
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    peak = max((abs(v) for v in samples), default=0)
    return 20.0 * math.log10(max(1, peak) / 32767.0)


def frame_rms(pcm: bytes) -> float:
    """Громкость кадра — основа детектора конца фразы."""
    n = len(pcm) // 2
    if not n:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    return math.sqrt(sum(s * s for s in samples) / n)


def bandpass_file(path: str) -> bytes | None:
    """Речевая полоса 120-7500 Гц + нормализация (sox). None, если не вышло.

    Шум микрофона широкополосный (в 3-8 кГц почти треть энергии), фильтр даёт
    +4.6 дБ отношения речи к шуму — замерено на записи через микрофон.
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
    """Распознать сегмент -> (текст, секунд). Пусто = «не расслышал».

    Сначала полоса 120-7500 Гц, затем исходный сегмент: порядок такой, потому
    что на фильтрованной версии той же фразы ASR читает больше.
    """
    def run(raw: bytes) -> str:
        return asyncio.run(asr_ws.recognize(
            asr_ws.samples_message(SAMPLE_RATE, asr_ws.pcm16_to_float32(raw)),
            addr, port))

    t0 = time.monotonic()
    bp = bandpass_file(path)
    if bp:
        text = run(asr_ws.normalize_pcm16(bp))
        if text.strip():
            return text, time.monotonic() - t0
    text = run(asr_ws.normalize_pcm16(pcm))
    if text.strip() and bp:
        print(f"[asr] на полосе пусто, на исходном: «{text}»", flush=True)
    return text, time.monotonic() - t0


def write_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)


# ---------------------------------------------------------------------------
# Детектор конца команды.
# ---------------------------------------------------------------------------
class CommandRecorder:
    """Запись команды: начало по громкости, конец по тишине.

    VAD здесь не нужен: начало фразы известно (сразу после сигнала), остаётся
    поймать первый громкий кадр и конец по паузе. Пороги берутся от шумового
    пола, который цикл измеряет в тишине. Гистерезис (старт 2.5x, конец 1.8x)
    защищает от дребезга на границе.
    """

    def __init__(self, floor_rms: float, frame: int = FRAME,
                 sample_rate: int = SAMPLE_RATE, start_factor: float = 2.5,
                 start_frames: int = 3, end_factor: float = 1.8,
                 pre_roll: list[bytes] | None = None,
                 end_drop: float = 4.0, end_silence: float = 0.8,
                 wait_speech: float = 3.5, max_speech: float = 15.0):
        self.floor = max(1.0, floor_rms)
        self.frame = frame
        self.rate = sample_rate
        self.start_factor = start_factor
        self.start_frames = max(1, start_frames)
        self.end_factor = end_factor
        self.end_drop = max(1.0, end_drop)
        self.end_silence = end_silence
        self.wait_speech = wait_speech
        self.max_speech = max_speech
        # Пре-ролл — кадры перед сигналом: ASR нужен «прогрев» фронтенда,
        # иначе на резком старте теряется первое слово команды (замерено:
        # сегмент без пре-ролла -> «Свет в коридоре» вместо «Включи свет…»).
        self.pcm = bytearray()
        for frame in (pre_roll or []):
            self.pcm.extend(frame)
        self.history: list[float] = []   # последние громкости: старт по трём кадрам
        self.started = False
        self.waited = 0.0
        self.silence = 0.0
        self.speech_frames = 0
        self.peak = 0.0          # самый громкий кадр команды

    def feed(self, pcm: bytes, rms: float) -> dict | None:
        """None — пишем дальше; dict — готово (status: ok|long|silence)."""
        if not self.started:
            self.history.append(rms)
            del self.history[:-self.start_frames]
            loud = sum(self.history) >= (self.floor * self.start_factor
                                         * self.start_frames)
            if len(self.history) == self.start_frames and loud:
                self.started = True
            else:
                self.waited += self.frame / self.rate
                if self.waited >= self.wait_speech:
                    return {"status": "silence", "pcm": b"", "speech_s": 0.0,
                            "dur_s": 0.0}
                return None
        self.pcm.extend(pcm)
        self.peak = max(self.peak, rms)
        # Тишиной считаем кадр, который заметно тише и пола, и пика речи:
        # порог только от пола не работает, когда комната сама шумная.
        if rms > max(self.floor * self.end_factor, self.peak / self.end_drop):
            self.silence = 0.0
            self.speech_frames += 1
        else:
            self.silence += self.frame / self.rate
            if self.silence >= self.end_silence:
                return self._done("ok")
        if len(self.pcm) // 2 >= self.max_speech * self.rate:
            return self._done("long")
        return None

    def _done(self, status: str) -> dict:
        return {"status": status, "pcm": bytes(self.pcm),
                "speech_s": self.speech_frames * self.frame / self.rate,
                "dur_s": len(self.pcm) / 2 / self.rate}


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Резидентный цикл: Vosk-KWS (имя) -> запись команды -> ASR -> ответ")
    ap.add_argument("--device", default="plughw:5,0",
                    help="ALSA-устройство захвата (см. arecord -l)")
    ap.add_argument("--kws-model", default=kws.DEFAULT_MODEL,
                    help="каталог модели Vosk для сторожа имени")
    ap.add_argument("--kws-phrases", default=",".join(kws.DEFAULT_PHRASES),
                    help="формы имени через запятую (грамматика KWS)")
    ap.add_argument("--spool-dir",
                    default=os.path.expanduser("~/.assistant/segments"),
                    help="куда складывать записанные команды (для segment-report)")
    ap.add_argument("--keep", type=int, default=20,
                    help="сколько сегментов хранить в spool")
    ap.add_argument("--no-wake-beep", action="store_true",
                    help="не играть сигнал после имени")
    ap.add_argument("--beep-tail-frames", type=int, default=6,
                    help="сколько кадров после сигнала выбросить (6 = 0.19 с — "
                         "длительность самого сигнала и его хвост; больше "
                         "начинает съедать начало команды)")
    ap.add_argument("--end-drop", type=float, default=4.0,
                    help="во сколько раз кадр должен быть тише пика речи, "
                         "чтобы считаться паузой (страховка для шумной комнаты)")
    ap.add_argument("--end-silence", type=float, default=0.8,
                    help="пауза, закрывающая команду (с)")
    ap.add_argument("--wait-speech", type=float, default=3.5,
                    help="сколько ждать начала команды после сигнала (с)")
    ap.add_argument("--max-command", type=float, default=15.0,
                    help="максимальная длина команды (с)")
    ap.add_argument("--start-factor", type=float, default=2.5,
                    help="во сколько раз кадр должен быть громче шума, чтобы "
                         "считать его началом команды")
    ap.add_argument("--no-asr", action="store_true",
                    help="только писать сегменты в spool (для отладки)")
    ap.add_argument("--no-reply", action="store_true",
                    help="не отвечать голосом: только распознавать и писать в журнал")
    ap.add_argument("--reply-template", default=None,
                    help="шаблон ответа, {text} — распознанный текст")
    ap.add_argument("--reply-voice", default=None, help="голос для озвучки ответа")
    ap.add_argument("--asr-addr", default="127.0.0.1")
    ap.add_argument("--asr-port", type=int, default=6006)
    args = ap.parse_args()

    phrases = [p.strip() for p in args.kws_phrases.split(",") if p.strip()]
    os.makedirs(args.spool_dir, exist_ok=True)

    # Модель грузим сразу: без неё цикл бессмысленен, и падать лучше на старте
    # (systemd покажет причину в журнале), чем посреди работы.
    spot = kws.KwsSpotter(model_dir=args.kws_model, phrases=phrases)
    try:
        spot.load()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[voiced] сторож имени не поднялся: {exc}", file=sys.stderr)
        return 1

    arecord = subprocess.Popen(
        ["arecord", "-q", "-D", args.device, "-f", "S16_LE",
         "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    pending = bytearray()          # raw-труба отдаёт сколько есть: собираем кадр

    def read_frame():
        """Ровно один кадр или None, если поток кончился."""
        while len(pending) < FRAME_BYTES:
            chunk = arecord.stdout.read(FRAME_BYTES)
            if not chunk:
                return None
            pending.extend(chunk)
        frame = bytes(pending[:FRAME_BYTES])
        del pending[:FRAME_BYTES]
        return frame

    def drain(limit_frames: int = 200) -> int:
        """Выбросить всё, что уже накопилось (для сигнала — точная граница)."""
        fd = arecord.stdout.fileno()
        dropped = 0
        while dropped < limit_frames and select.select([fd], [], [], 0)[0]:
            data = arecord.stdout.read(FRAME_BYTES)
            if not data:
                break
            dropped += len(data) // 2
        pending.clear()
        return dropped
    print(f"[voiced] слушаю {args.device}; KWS {args.kws_model}; "
          f"имя: {', '.join(phrases)}", flush=True)

    muted_until = 0.0        # до этого момента играет наш ответ
    last_reply = None
    recorder: CommandRecorder | None = None
    noise_hist: deque[float] = deque(maxlen=62)   # ~2 с: из них берём пол
    pre_roll: deque[bytes] = deque(maxlen=16)     # 0.5 с перед командой
    floor_rms = 0.0          # шумовой пол (20-й процентиль истории)
    counter = 0

    def speak(text: str) -> None:
        """Озвучить и заглушить прослушку на время речи (+ хвост колонки)."""
        nonlocal muted_until, last_reply
        muted_until = time.monotonic() + 30.0   # глухо на всю озвучку
        rc = assistant.synth_and_play(text, voice=args.reply_voice)
        muted_until = time.monotonic() + 0.8    # + хвост колонки
        last_reply = text
        spot.reset()                            # после своей речи — свежий декодер
        if rc != 0:
            print(f"[reply] озвучка вернула код {rc} (колонка?)", flush=True)

    try:
        while True:
            chunk = read_frame()
            if chunk is None:
                break
            if time.monotonic() < muted_until:
                # Играет наш ответ: читаем поток, чтобы не копить буфер ALSA,
                # но ни сторожу, ни записи его не отдаём.
                continue
            rms = frame_rms(chunk)

            if recorder is None:
                # Шумовой пол — 20-й процентиль громкостей за последние ~2 с.
                # Минимум по одному кадру не годится: одна тишина занижала пол,
                # и дальше речь считалась непрерывной (запись шла до 15 с).
                noise_hist.append(rms)
                ordered = sorted(noise_hist)
                floor_rms = ordered[len(ordered) // 5]
                pre_roll.append(chunk)
                if spot.feed(chunk):
                    print(f"[kws] услышал имя ({spot.frames} кадров прослушано, "
                          f"срабатываний {spot.hits})", flush=True)
                    # Сигнал не должен попасть в запись команды. Порядок важен:
                    # СЛИВАЕМ накопленное ДО сигнала, тогда во время сигнала в
                    # трубе оказывается ровно его собственное аудио — и его
                    # выбрасываем ровно на длину сигнала (--beep-tail-frames).
                    # Слив после сигнала «на всё» съедал начало команды, если
                    # Vosk закрыл фразу позже (замерено: сегмент 1.1 с вместо
                    # 1.5 — терялось «Включи»).
                    drain()
                    if not args.no_wake_beep:
                        rc = assistant.beep()
                        if rc != 0:
                            print(f"[kws] сигнал не проигрался (aplay rc={rc})",
                                  flush=True)
                    for _ in range(max(0, args.beep_tail_frames)):
                        if read_frame() is None:
                            break
                    recorder = CommandRecorder(
                        floor_rms, end_silence=args.end_silence,
                        wait_speech=args.wait_speech,
                        max_speech=args.max_command,
                        start_factor=args.start_factor,
                        pre_roll=list(pre_roll))
                    pre_roll.clear()
                    spot.reset()
                continue

            got = recorder.feed(chunk, rms)
            if got is None:
                continue
            recorder = None
            if got["status"] == "silence":
                print(f"[kws] команды не последовало за {args.wait_speech:g} с",
                      flush=True)
                continue

            counter += 1
            dur = got["dur_s"]
            # В имени сегмента — время: счётчик начинается заново с каждым
            # запуском, и сортировка по имени удаляла свежие сегменты как
            # «старые» (уже наступали: анализ шёл по чужим файлам).
            path = os.path.join(
                args.spool_dir,
                f"seg-{time.strftime('%H%M%S')}-{counter}-{dur:.3f}s.wav")
            write_wav(path, got["pcm"], SAMPLE_RATE)
            # Чистим по ВРЕМЕНИ изменения, а не по имени.
            old = sorted(os.listdir(args.spool_dir),
                         key=lambda n: os.path.getmtime(
                             os.path.join(args.spool_dir, n)))
            for name in (old[:-args.keep] if args.keep > 0 else []):
                try:
                    os.unlink(os.path.join(args.spool_dir, name))
                except OSError:
                    pass
            print(f"[seg] {counter}: {dur:.2f}с, речь {got['speech_s']:.2f}с, "
                  f"пик {peak_db(got['pcm']):.0f} дБ ({got['status']})", flush=True)
            if args.no_asr:
                continue

            text, asr_s = recognize_segment(path, got["pcm"],
                                            args.asr_addr, args.asr_port)
            # Пре-ролл мог принести хвост имени: для команды он лишний.
            text = kws.strip_name(text, phrases)
            if not text.strip():
                print(f"[asr] {counter}: не расслышал ({asr_s:.2f} с, "
                      f"пик {peak_db(got['pcm']):.0f} дБ) — повторите", flush=True)
                continue
            print(f"{counter}: {text}  [ASR {asr_s:.2f} с]", flush=True)
            if args.no_reply:
                continue
            template = (args.reply_template if args.reply_template is not None
                        else assistant.DEFAULT_TEMPLATE)
            reply = assistant.make_reply(text, template)
            assistant.stub_intent(text)
            print(f"[reply] {reply}", flush=True)
            speak(reply)
    except KeyboardInterrupt:
        print("[voiced] остановлен клавиатурой", file=sys.stderr)
    finally:
        arecord.terminate()
        # Код arecord отделяет «микрофон отвалился» от «нас останавливают».
        rc = arecord.wait()
        if rc not in (-15, 0):
            print(f"[voiced] arecord завершился с кодом {rc} — микрофон "
                  f"{args.device} недоступен или занят?", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
