#!/usr/bin/env python3
"""
TTS-бэкенд для say.sh: текст -> Silero v5_5_ru -> стерео WAV -> динамик ES8316.

Почему отдельный процесс, а не инлайн в bash: модель Silero требует torch и
грузится ~1.5 с; держать её в bash невозможно, поэтому вся работа с моделью здесь.

КРИТИЧНО: torch.set_num_threads() НЕ поднимать выше 2. При 8 потоках (все ядра
RK3588) плата уходит в аппаратный сброс — просадка питания. RTF при 2 потоках
всё равно 0.13-0.18, то есть быстрее реального времени.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import soundfile as sf
import torch

MODEL = os.path.expanduser("~/models/tts/silero_v5_5_ru_ok.pt")
SR = 48000
TORCH_THREADS = 2  # предохранитель по питанию — см. docstring выше
DEFAULT_VOICE = "baya"
DEFAULT_DEVICE = "plughw:3,0"  # ES8316: принимает ТОЛЬКО стерео
VOICES = ["aidar", "baya", "kseniya", "eugene", "xenia"]


def build_model():
    torch.set_num_threads(TORCH_THREADS)
    m = torch.package.PackageImporter(MODEL).load_pickle("tts_models", "model")
    m.to(torch.device("cpu"))
    return m


def synth(model, text, voice, speed):
    """Возвращает numpy-массив моно float32. speed применяем через SSML prosody."""
    if abs(speed - 1.0) > 0.01:
        # Silero v5 умеет SSML; темп задаётся в процентах.
        pct = int(round(speed * 100))
        ssml = f'<speak><prosody rate="{pct}%">{text}</prosody></speak>'
        audio = model.apply_tts(ssml_text=ssml, speaker=voice, sample_rate=SR)
    else:
        audio = model.apply_tts(text=text, speaker=voice, sample_rate=SR)
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    return np.asarray(audio, dtype=np.float32)


def to_stereo(mono):
    """ES8316 молча не играет моно — дублируем канал."""
    return np.column_stack([mono, mono]).astype(np.float32)


def main():
    ap = argparse.ArgumentParser(description="Синтез русской речи (Silero v5_5_ru)")
    ap.add_argument("text", nargs="?", help="текст; если пусто, читается stdin")
    ap.add_argument("-v", "--voice", default=DEFAULT_VOICE, choices=VOICES)
    ap.add_argument("-s", "--speed", type=float, default=1.0, help="темп (1.0 = норма)")
    ap.add_argument("-o", "--output", help="сохранить WAV и не воспроизводить")
    ap.add_argument("-d", "--device", default=DEFAULT_DEVICE,
                    help="ALSA-устройство вывода (по умолчанию ES8316)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    text = args.text if args.text else sys.stdin.read()
    text = text.strip()
    if not text:
        print("say: пустой текст", file=sys.stderr)
        return 2

    t0 = time.time()
    model = build_model()
    load_s = time.time() - t0

    t0 = time.time()
    mono = synth(model, text, args.voice, args.speed)
    gen_s = time.time() - t0

    dur = len(mono) / SR
    rtf = gen_s / dur if dur else 0

    # Файл стерео: пишем во временный, если не просили сохранить.
    tmp = None
    if args.output:
        path = os.path.expanduser(args.output)
    else:
        fd, path = tempfile.mkstemp(prefix="say_", suffix=".wav")
        os.close(fd)
        tmp = path

    sf.write(path, to_stereo(mono), SR, subtype="PCM_16")

    if not args.quiet:
        print(f"[say] {args.voice} | {dur:.2f}s аудио | синтез {gen_s:.2f}s "
              f"| RTF {rtf:.3f} | модель {load_s:.2f}s", file=sys.stderr)

    rc = 0
    if not args.output:
        # Вывод на ES8316. Колонка может быть выключена/отключена — это не ошибка
        # синтеза, поэтому отделяем код возврата aplay от нашего.
        try:
            subprocess.run(["aplay", "-q", "-D", args.device, path],
                           check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as e:
            rc = 3
            print(f"[say] aplay не смог вывести на {args.device}: "
                  f"{e.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        except FileNotFoundError:
            rc = 4
            print("[say] aplay не найден (apt install alsa-utils)", file=sys.stderr)
        finally:
            if tmp:
                os.unlink(tmp)

    return rc


if __name__ == "__main__":
    sys.exit(main())
