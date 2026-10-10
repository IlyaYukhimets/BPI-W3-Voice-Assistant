#!/usr/bin/env python3
"""
TTS-бэкенд для say.sh: текст -> Silero v5_5_ru -> стерео WAV -> ES8316.

Почему отдельный процесс, а не инлайн в bash: модель Silero требует torch и
грузится ~1.5 с; держать её в bash невозможно, поэтому вся работа с моделью здесь.

Вся логика (нормализация, SSML, синтез, анализ) живёт в tts_core.py — тот же
модуль использует tools/voice_lab.py. Общее ядро нужно, чтобы стенд подбора
мерил ровно тот звук, который потом играет ассистент.

ПОТОКИ: по умолчанию 4 — замеренный оптимум для BPI-W3 (core.THREADS_DEFAULT),
а не «все ядра»: 8 потоков на короткой фразе работают как один. Ограничить или
поднять можно флагом --threads N или переменной TTS_THREADS. Значение применяется
ПОСЛЕ загрузки модели: пакет Silero при загрузке сам выставляет 1 поток и
затирает всё, что поставлено раньше.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tts_core as core  # noqa: E402

# Профиль по умолчанию — победивший рецепт (см. docs/10-voice-tuning.md):
# baya + warm (первая фраза ниже тоном, дальше выше, пауза 350 мс, темп 108%).
DEFAULT_PROFILE = "warm"


def main() -> int:
    ap = argparse.ArgumentParser(description="Синтез русской речи (Silero v5_5_ru)")
    ap.add_argument("text", nargs="?", help="текст; если пусто, читается stdin")
    ap.add_argument("-v", "--voice", default=core.DEFAULT_VOICE, choices=list(core.VOICES))
    ap.add_argument("-t", "--threads", type=int, default=None,
                    help="потоков на синтез (по умолчанию 4 — замеренный оптимум; или TTS_THREADS)")
    ap.add_argument("-s", "--speed", type=float, default=None,
                    help="темп как множитель (1.0 = норма); переводится в SSML rate")
    ap.add_argument("-p", "--profile", default=DEFAULT_PROFILE, choices=list(core.PROFILES),
                    help="профиль интонации (по умолчанию warm)")
    ap.add_argument("--human", default=None, choices=list(core.QUESTION_TURNS),
                    metavar="STYLE",
                    help="переформулировать вопрос «по-человечески»: " +
                         " | ".join(core.QUESTION_TURNS))
    ap.add_argument("-P", "--pitch", default=None,
                    help="тон: high/low/+5%%/-10%% (перекрывает профиль)")
    ap.add_argument("-o", "--output", help="сохранить WAV и не воспроизводить")
    ap.add_argument("-d", "--device", default=core.DEFAULT_DEVICE,
                    help="ALSA-устройство вывода (по умолчанию ES8316)")
    ap.add_argument("-a", "--accent", action="store_true",
                    help="расставить ударения через silero-stress (если установлен)")
    ap.add_argument("-n", "--no-normalize", action="store_true",
                    help="НЕ переписывать цифры/латиницу словами (по умолчанию переписываем)")
    ap.add_argument("--check", action="store_true",
                    help="показать, что модель может вырезать, и выйти")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    text = args.text if args.text else sys.stdin.read()
    text = text.strip()
    if not text:
        print("say: пустой текст", file=sys.stderr)
        return 2

    # --- предупреждение о символах, которые Silero удалит молча ---
    # Без этой проверки ответ ассистента звучит правдоподобно, но с потерянным
    # смыслом: "через 10 минут" -> "через минуту", "YouTube" -> исчезает.
    missing = core.check_text(text)
    if missing and not args.no_normalize:
        if not args.quiet:
            lst = " ".join(repr(ch) for ch, _ in missing)
            print(f"[say] нормализую: модель вырезала бы {lst}", file=sys.stderr)
        text = core.normalize_text(text)
        if not args.quiet:
            print(f"[say] -> {text}", file=sys.stderr)
    elif missing:
        print(f"[say] ВНИМАНИЕ: --no-normalize, символы будут потеряны: "
              f"{' '.join(repr(ch) for ch, _ in missing)}", file=sys.stderr)

    if args.check:
        for ch, n in core.check_text(text):
            print(f"  {ch!r} x{n}")
        return 0

    if args.accent:
        text, pairs = core.accentuate(text)
        if pairs and not args.quiet:
            for src, dst in pairs:
                print(f"[say] ударение: {src} -> {dst}", file=sys.stderr)

    # --- «человечный» вопрос: оборот вместо одного pitch -------------------
    # Silero не умеет вопросительную интонацию, и на короткой фразе pitch
    # даёт скачущую высоту при том, что фраза звучит утверждением. Оборот
    # («ли», «Может,», «..., да?») делает вопрос вопросом ЛЕКСИЧЕСКИ —
    # см. замеры в tts_core.QUESTION_TURNS.
    if args.human:
        before = text
        text = core.humanize_question(text, style=args.human)
        if not args.quiet and text != before:
            print(f"[say] вопрос -> {text}", file=sys.stderr)

    # --- темп: множитель -> SSML rate -------------------------------------
    rate = None
    if args.speed is not None and abs(args.speed - 1.0) > 0.01:
        # ВАЖНО: номинал не равен факту. rate="105%" даёт всего -0.6%
        # длительности, "108%" уже -4.4%. Ставить проценты из головы
        # бессмысленно — точную ступень под свой текст даёт
        # tools/voice_lab.py tune --target -N.
        rate = f"{int(round(args.speed * 100))}%"

    ssml = core.build_ssml(text, profile=args.profile, rate=rate, pitch=args.pitch)

    t0 = time.time()
    model = core.build_model(threads=args.threads)
    load_s = time.time() - t0
    # Печатаем ФАКТИЧЕСКОЕ число потоков, а не запрошенное: модель при загрузке
    # ставит своё, и расхождение с ожиданием один раз уже увело в сторону.
    nthr = core.torch_threads()

    t0 = time.time()
    mono = core.synth(model, ssml, voice=args.voice)
    gen_s = time.time() - t0

    dur = len(mono) / core.SR
    rtf = gen_s / dur if dur else 0

    if args.output:
        path = os.path.expanduser(args.output)
        made = core.write_audio(mono, path, ogg=False)
        if not args.quiet:
            print(f"[say] {args.voice}/{args.profile} | {dur:.2f}s аудио | "
                  f"синтез {gen_s:.2f}s | RTF {rtf:.3f} | модель {load_s:.2f}s "
                  f"| потоков {nthr} | {made[0]}", file=sys.stderr)
        return 0

    import tempfile

    fd, path = tempfile.mkstemp(prefix="say_", suffix=".wav")
    os.close(fd)
    try:
        core.write_audio(mono, path, ogg=False)
        if not args.quiet:
            print(f"[say] {args.voice}/{args.profile} | {dur:.2f}s аудио | "
                  f"синтез {gen_s:.2f}s | RTF {rtf:.3f} | модель {load_s:.2f}s "
                  f"| потоков {nthr}",
                  file=sys.stderr)
        # Колонка может быть выключена/отключена — это не ошибка синтеза,
        # поэтому код возврата aplay отделён от нашего.
        rc = core.play(path, device=args.device)
        if rc == 4:
            print("[say] aplay не найден (apt install alsa-utils)", file=sys.stderr)
        elif rc == 3:
            print(f"[say] aplay не смог вывести на {args.device}: "
                  f"проверь устройство и что файл стерео", file=sys.stderr)
        return rc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
