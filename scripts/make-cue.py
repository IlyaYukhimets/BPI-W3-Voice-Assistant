#!/usr/bin/env python3
"""make-cue.py — сделать сигнал пробуждения из фразы.

ЗАЧЕМ. Отбивка из тонов звучит машинно: как её ни собирай, живой записи она не
заменит. Поэтому сигналом может быть файл, и этот скрипт делает такой файл из
фразы — тем же голосом, что и ответы ассистента («Слушаю», «Да?», «Астра»).

    make-cue.py "Слушаю" --name slushayu
    make-cue.py "Да?" --name da --seconds 0.5 --no-play

Файл ложится в ~/.assistant/cues, откуда его видит панель: блок «Действия» →
список сигналов → «прослушать» / «сделать рабочим». Готовый сигнал сразу
проигрывается, если не указан --no-play.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assistant          # noqa: E402
import tts_core as core   # noqa: E402

TTS_TIMEOUT = 120.0


def make_cue(text: str, name: str, seconds: float, voice: str) -> tuple[str, float]:
    """Синтезировать фразу и положить её в каталог сигналов.

    Обрезка и спады по краям — та же функция, что и при проигрыше: на диске
    сразу лежит готовый сигнал, а не «сырая» озвучка, которую цикл будет чистить
    на каждом срабатывании.
    """
    import tts_say

    text = core.normalize_text(text)
    if not text:
        raise ValueError("после нормализации от фразы ничего не осталось")
    ssml = core.build_ssml(text, profile="warm")
    got = tts_say.daemon_synth(ssml, voice)
    if got is None:
        raise RuntimeError("воркер TTS не отвечает: "
                           "systemctl --user status ttsd")
    raw, _info = got
    samples = assistant.trim_cue(core.mono_from_bytes(raw), core.SR, seconds)

    os.makedirs(assistant.CUE_DIR, exist_ok=True)
    filename = name if name.endswith((".ogg", ".wav")) else name + ".ogg"
    path = os.path.join(assistant.CUE_DIR, filename)
    core.write_audio(samples, path, ogg=True)   # рядом ляжет и wav — для aplay
    return path, len(samples) / core.SR


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("text", help="что произнести: короткая фраза")
    ap.add_argument("--name", default="cue", help="имя файла без расширения")
    ap.add_argument("--seconds", type=float, default=1.2,
                    help="сколько оставить от озвучки (по умолчанию 1.2 с)")
    ap.add_argument("--voice", default=core.DEFAULT_VOICE)
    ap.add_argument("--no-play", action="store_true", help="не проигрывать готовое")
    args = ap.parse_args()

    try:
        path, seconds = make_cue(args.text, args.name, args.seconds, args.voice)
    except (RuntimeError, ValueError) as exc:
        print(f"не получилось: {exc}", file=sys.stderr)
        return 1

    print(f"готово: {path} ({seconds:.2f} с) — и рядом wav для проигрыша")
    if not args.no_play:
        rc = core.play(os.path.splitext(path)[0] + ".wav",
                       device=core.DEFAULT_DEVICE, timeout=30)
        print("  проиграно" if rc == 0 else f"  aplay не справился (rc={rc})")
    print("дальше — в панели: «Действия» → список сигналов → «сделать рабочим»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
