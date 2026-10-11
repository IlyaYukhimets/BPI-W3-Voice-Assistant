"""Отчёт по сохранённым сегментам: что услышал микрофон и понял ли ASR.

Зачем: при слабом микрофоне бесполезно смотреть на текст распознавания — надо
видеть УРОВНИ. Скрипт берёт последние сегменты из spool (их пишет voiced), для
каждого считает:

  - длину и длину РЕЧИ (по silero, тем же порогом, что в цикле);
  - пик и уровень речи в дБFS;
  - уровень ШУМА комнаты (по кадрам тишины) и отношение речи к шуму;
  - что услышал ASR на исходном сегменте и на речевой полосе.

Микрофон не занимается, поэтому отчёт можно снимать при работающем voiced:
скажи фразу и сразу запусти. Ориентир: для надёжного распознавания нужно
отношение речи к шуму от 15 дБ; на 5-8 дБ ASR теряет окончания и путает слова.

Запуск:
    ~/models/tts-venv/bin/python scripts/segment-report.py --last 8
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_ws  # noqa: E402

WINDOW = 512
DEFAULT_SPOOL = os.path.expanduser("~/.assistant/segments")


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())


def frame_rms(pcm: bytes) -> np.ndarray:
    x = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    n = len(x) // (WINDOW * 4)
    if n == 0:
        return np.array([1.0])
    return np.sqrt(np.mean(x[:n * WINDOW * 4].reshape(n, WINDOW * 4) ** 2, axis=1))


def db(x: float) -> float:
    return 20.0 * np.log10(max(1e-9, x) / 32768.0)


def stats(pcm: bytes) -> dict:
    x = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype=np.int16)
    rms = frame_rms(pcm)
    # Речь — громкие кадры, шум — тихие: сегмент всегда несёт пре-ролл тишины,
    # поэтому шумовой пол оценивается по нему же.
    speech = float(np.percentile(rms, 90))
    noise = float(np.percentile(rms, 20))
    return {
        "samples": len(x),
        "peak_db": db(float(np.abs(x).max()) if len(x) else 0.0),
        "speech_db": db(speech),
        "noise_db": db(noise),
        "snr_db": db(speech) - db(noise),
    }


def bandpass(path: str, pcm: bytes) -> bytes | None:
    out = "/tmp/_rep_bp.wav"
    try:
        rc = subprocess.run(["sox", path, out, "highpass", "120", "lowpass",
                             "7500", "norm", "-3"],
                            capture_output=True, timeout=5).returncode
        if rc != 0:
            return None
        return read_wav(out)
    except (OSError, subprocess.SubprocessError, wave.Error):
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def asr(pcm: bytes, addr: str, port: int) -> str:
    return asyncio.run(asr_ws.recognize(
        asr_ws.samples_message(16000, asr_ws.pcm16_to_float32(
            asr_ws.normalize_pcm16(pcm))), addr, port))


def verdict(sn: float) -> str:
    if sn >= 15.0:
        return "нормально, распознавание надёжное"
    if sn >= 10.0:
        return "терпимо, окончания могут теряться"
    if sn >= 6.0:
        return "плохо: слова путаются, часть теряется"
    return "очень плохо: речь тонет в шуме — распознавание случайно"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=DEFAULT_SPOOL)
    ap.add_argument("--last", type=int, default=8, help="сколько сегментов взять")
    ap.add_argument("--no-asr", action="store_true", help="только уровни")
    ap.add_argument("--asr-addr", default="127.0.0.1")
    ap.add_argument("--asr-port", type=int, default=6006)
    args = ap.parse_args()

    files = sorted((os.path.join(args.dir, f) for f in os.listdir(args.dir)
                    if f.endswith(".wav")), key=os.path.getmtime)[-args.last:]
    if not files:
        print(f"сегментов нет в {args.dir}: скажи фразу ассистенту и повтори")
        return 2

    print("  файл                          длит  пик   шум   речь  речь/шум   вердикт")
    for path in files:
        pcm = read_wav(path)
        st = stats(pcm)
        name = os.path.basename(path)
        print(f"  {name:<28} {st['samples'] / 32000:4.2f} "
              f"{st['peak_db']:5.1f} {st['noise_db']:5.1f} {st['speech_db']:5.1f} "
              f"{st['snr_db']:7.1f}   {verdict(st['snr_db'])}", flush=True)
        if args.no_asr:
            continue
        text = asr(pcm, args.asr_addr, args.asr_port)
        print(f"      ASR исходный: «{text}»", flush=True)
        bp = bandpass(path, pcm)
        if bp:
            text_bp = asr(bp, args.asr_addr, args.asr_port)
            print(f"      ASR полоса:   «{text_bp}»", flush=True)

    print("\n  Что делать при плохом отношении речи к шуму:")
    print("   - говорить ближе к микрофону: у нормального микрофона 20-30 см дают")
    print("     +15-20 дБ слышимости речи;")
    print("   - убрать источник шума рядом с микрофоном (кулер, техника);")
    print("   - при возможности заменить микрофон: десктопный кардиоидный USB")
    print("     на порядок слышит речь лучше этого USB-адаптера.")
    print("   Держать микрофон в руке НЕ нужно — нужно поставить его там, где ты")
    print("   говоришь, и проверить этот отчёт.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
