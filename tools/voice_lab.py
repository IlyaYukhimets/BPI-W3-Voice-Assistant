#!/usr/bin/env python3
"""
voice_lab.py — стенд подбора голоса и интонации для ассистента на BPI-W3.

ЗАЧЕМ. Настройка голоса состоит из перебора: другой темп, другой тон, другой
голос — и решение принимается НА СЛУХ человеком, а не метрикой. Руками это
превращается в десятки однотипных запусков с потерей файлов. Стенд делает
рутину машинной: синтезирует набор вариантов на ОДНОМ тексте, выравнивает
громкость, пишет OGG для прослушивания и манифест JSON с замерами.

ПОЧЕМУ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ say.sh. say.sh озвучивает реплику ассистента
(продакшен). Стенд измеряет и сравнивает — задача другая. Общее ядро у них
одно (scripts/tts_core.py), поэтому стенд меряет ровно тот же звук, который
потом играет ассистент.

КОМАНДЫ
  list      голоса, профили, ступени темпа (без синтеза, быстро)
  check     что Silero вырежет из текста + расстановка ударений (без синтеза)
  palette   палитра вариантов одного текста  -> OGG-файлы + manifest.json
  compare   один текст разными голосами      -> OGG-файлы + метрики
  tune      лестница темпа с ЗАМЕРОМ факта   -> подбор нужного ускорения
  question  сравнение pitch-контуров (не распознаёт вопросительность)
  say       озвучить один вариант в файл или на динамик

ПРИМЕРЫ
  # что можно и что нельзя менять
  python3 tools/voice_lab.py list

  # палитра на нужном тексте (появится ./lab/out/*.ogg — их и слушать)
  python3 tools/voice_lab.py palette -t "Хорошо, включаю свет в гостиной. Нужно ли мне сделать что-то *ещё*?"

  # сравнить голоса на одном тексте, честно по громкости
  python3 tools/voice_lab.py compare -v baya,kseniya,xenia -t "Добрый вечер!"

  # узнать, какой rate даёт реальные -5% (а не номинальные)
  python3 tools/voice_lab.py tune -t "Хорошо, включаю свет в гостиной." --target -5

  # почему вопрос звучит как утверждение
  python3 tools/voice_lab.py question -t "Включить свет в спальне?"

  # проверить, не потеряется ли смысл (цифры/латиница)
  python3 tools/voice_lab.py check -t "Напомни через 10 минут, открой YouTube в 19:45"
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

try:
    import tts_core as core
except ImportError:  # скрипт лежит рядом с tts_core.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tts_core as core

DEFAULT_TEXT = "Хорошо, включаю свет в гостиной. Нужно ли мне сделать что-то *ещё*?"

# Палитра рецептов для команды palette. Ключи — короткие, чтобы имена файлов
# были читаемыми: их видно в плеере и на телефоне.
PALETTE = [
    ("00-baseline", "neutral", None, None, "как есть — точка отсчёта"),
    ("01-speedy", "neutral", "110%", None, "+10% темпа (номинал)"),
    ("02-emotive", "neutral", "110%", "high", "быстрее и выше тоном"),
    ("03-strong", "neutral", "115%", "x-high", "ещё энергичнее"),
    ("04-warm", "warm", None, None, "1-я фраза ниже, пауза, 2-я выше"),
    ("05-lively", "lively", None, None, "бодро, фразы раздельно"),
    ("06-calm", "calm", None, None, "медленнее и ниже"),
    ("07-alert", "alert", None, None, "тревожная реплика"),
    ("08-question", "question", None, None, "эвристика низкого старта и высокого хвоста"),
    ("09-emphasis", "emphasis", None, None, "акцент на первом слове"),
]


def _log(msg: str) -> None:
    print(msg, flush=True)


def _manifest(out_dir: str, rows: list[dict], meta: dict | None = None) -> str:
    """Пишет manifest.json: без него через день непонятно, что за файлы и
    какие у них параметры. Кладём рядом с аудио."""
    p = os.path.join(out_dir, "manifest.json")
    payload = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "meta": meta or {}, "items": rows}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return p


# --------------------------------------------------------------------------
# Команды без синтеза
# --------------------------------------------------------------------------

def cmd_list(_args) -> int:
    _log("ГОЛОСА (все 5 встроенных; свои добавить нельзя)")
    for name, desc in core.VOICES.items():
        mark = "  <- по умолчанию" if name == core.DEFAULT_VOICE else ""
        _log(f"  {name:<9} {desc}{mark}")

    _log("\nПРОФИЛИ ИНТОНАЦИИ (--profile)")
    for name, desc in core.PROFILES.items():
        _log(f"  {name:<10} {desc}")

    _log("\nСТУПЕНИ ТЕМПА (--rate); ФАКТИЧЕСКИЙ эффект НЕ равен номиналу:")
    _log("  105% -> -0.6% длительности, 108% -> -4.4%, 110% -> -6.5%")
    _log("  точную цифру для своего текста даёт команда: tune --target -5")

    _log("\nПАРАМЕТРЫ apply_tts (работают, по умолчанию True):")
    _log("  put_accent, put_yo, put_stress_homo, put_yo_homo, stress_single_vowel")
    _log("  intensity — МЁРТВЫЙ параметр (1/3/6 дают 0% разницы), не использовать")

    _log("\nSSML: что поддержано:")
    _log("  ДА  : prosody(rate,pitch), break(time), <s>, <p>")
    _log("  НЕТ : emphasis, say-as, sub, voice  (ValueError)")
    _log("  по справке Silero pitch: x-low/low/medium/high/x-high")
    _log("  по справке Silero rate: x-slow/slow/medium/fast/x-fast")
    _log("  проценты в рецептах проекта проверены отдельно, но не перечислены")
    _log("  в официальной SSML-справке")
    _log("  ручное ударение: знак + ПЕРЕД гласной -> хор+ошо, замк+и")
    return 0


def cmd_check(args) -> int:
    text = args.text or DEFAULT_TEXT
    _log(f"ТЕКСТ: {text}")
    _log("")

    # 1. что было бы вырезано БЕЗ нормализации (показываем риск)
    bad_before = core.check_text(text)

    # 2. нормализация
    norm = core.normalize_text(text)
    _log("ПОСЛЕ НОРМАЛИЗАЦИИ (именно это пойдёт в синтез):")
    _log(f"  {norm}")
    _log("")

    # 3. что осталось опасного ПОСЛЕ нормализации — это и есть вердикт
    bad_after = core.check_text(norm)

    if bad_before:
        _log(f"БЕЗ нормализации модель потеряла бы {len(bad_before)} типов символов "
             f"(всего {sum(n for _, n in bad_before)} шт.):")
        groups = {"цифры": "", "латиница": "", "прочее": ""}
        for ch, _n in bad_before:
            if ch.isdigit():
                groups["цифры"] += ch
            elif ch.isalpha() and ch.isascii():
                groups["латиница"] += ch
            else:
                groups["прочее"] += ch
        for k, v in groups.items():
            if v:
                _log(f"  {k}: {v}")
    else:
        _log("В исходном тексте опасных символов не было.")

    _log("")
    if not bad_after:
        _log("РЕЗУЛЬТАТ: безопасно. После нормализации терять нечего — "
             "цифры и латиница переписаны словами.")
        _log("БЕЗ нормализации смысл был бы ИСКАЖЁН: 'через 10 минут' звучит как "
             "'через минуту', 'YouTube' исчезает совсем.")
    else:
        _log("РЕЗУЛЬТАТ: ОСТАЛСЯ РИСК — эти символы модель всё ещё вырежет:")
        for ch, n in bad_after:
            shown = "пробел" if ch == " " else repr(ch)
            _log(f"  {shown:<8} x{n}")
        _log("Допиши их в LATIN/normalize_text() или замени словами вручную.")

    # 4. ударения
    _log("")
    _log("УДАРЕНИЯ (silero-stress):")
    try:
        import silero_stress  # noqa: F401
        have = True
    except ImportError:
        have = False
    if not have:
        _log("  библиотека не установлена — ударения расставит сама модель")
        _log("  при синтезе (put_accent/put_stress_homo=True по умолчанию).")
        _log("  Установка: uv pip install --python <venv> silero-stress")
    else:
        acc, pairs = core.accentuate(norm)
        _log(f"  {acc}")
        if pairs:
            _log("  РАЗОБРАНО ПО КОНТЕКСТУ:")
            for src, dst in pairs:
                _log(f"    {src} -> {dst}")
        else:
            _log("  (расхождений с исходным нет)")
        _log("")
        _log("ВНИМАНИЕ: на короткой фразе без контекста омографы разбираются")
        _log("не всегда верно ('все замки закрыты' -> 'замк+и' вместо 'зАмки').")
        _log("Если смысл критичен — ставь ударение знаком + вручную.")
    return 0


# --------------------------------------------------------------------------
# Команды с синтезом
# --------------------------------------------------------------------------

def _load(args):
    if getattr(args, "quiet", False):
        _log("[i] загрузка модели...")
    t0 = time.time()
    # Через build_model, а не напрямую: внутри выставляются потоки ПОСЛЕ
    # загрузки (модель иначе затирает их на 1) — см. core.resolve_threads.
    m = core.build_model(getattr(args, "model", None),
                         threads=getattr(args, "threads", None))
    if getattr(args, "quiet", False):
        _log(f"[i] модель за {time.time()-t0:.1f} с, потоков {core.torch_threads()}")
    return m


def _emit(model, rows, out_dir, args, text, tag, profile, rate, pitch, note):
    """Синтез одного варианта -> WAV+OGG, метрики в строку манифеста."""
    ssml = core.build_ssml(text, profile=profile, rate=rate, pitch=pitch)
    t0 = time.time()
    mono = core.synth(model, ssml, voice=args.voice)
    gen = time.time() - t0
    dur = len(mono) / core.SR
    if getattr(args, "level", True):
        mono = core.rms_normalize(mono)
    path = os.path.join(out_dir, f"{args.voice}-{tag}.wav")
    made = core.write_audio(mono, path, ogg=True)
    rep = core.intonation_report(mono)
    contour, why = core.judge_question(rep) if rep else ("-", "")
    row = {
        "tag": tag, "voice": args.voice, "profile": profile,
        "rate": rate, "pitch": pitch, "note": note,
        "duration_s": round(dur, 3), "gen_s": round(gen, 2),
        "rtf": round(gen / dur, 3) if dur else None,
        # Пишем в манифест: замер должен сам сообщать, при скольких потоках он снят.
        "threads": core.torch_threads(),
        "files": made, "ssml": ssml,
        "f0_median_hz": round(rep["median"]) if rep else None,
        "f0_start_hz": round(rep["start"]) if rep else None,
        "f0_peak_hz": round(rep["peak"]) if rep else None,
        "peak_pos": round(rep["peak_pos"], 3) if rep else None,
        "tail_gain": round(rep["tail_gain"], 3) if rep else None,
        "intonation": contour, "why": why,
    }
    return row


def cmd_human(args) -> int:
    """Сравнить речевые обороты для вопроса на ОДНОМ тексте.

    Проблема, которую это решает: Silero не имеет вопросительной интонации,
    и на короткой фразе pitch даёт скачущую высоту, а фраза всё равно
    слышится утверждением. Оборот делает вопрос вопросом ЛЕКСИЧЕСКИ.
    Решение принимается НА СЛУХ: синтезируем все обороты подряд, выравниваем
    громкость и кладём рядом метку голосом, которого нет среди кандидатов.
    """
    text = args.text or DEFAULT_TEXT
    out_dir = os.path.join(args.out, f"{args.voice}-human")
    os.makedirs(out_dir, exist_ok=True)
    _log(f"ТЕКСТ: {text}")
    _log(f"ГОЛОС: {args.voice}   выход: {out_dir}")
    if args.normalize:
        text = core.normalize_text(text)
    _log("")

    model = _load(args)
    # метка голосом aidar — мужской, его нет среди женских кандидатов,
    # поэтому метку не спутать с образцом
    marker_voice = "aidar" if args.voice != "aidar" else "xenia"

    rows = []
    _log(f"{'оборот':<9} {'длит':>7} {'старт':>7} {'хвост':>7} {'пик':>6}  фраза")
    _log("-" * 84)
    variants = [(None, text)] + [(st, core.humanize_question(text, style=st))
                                 for st in core.QUESTION_TURNS]
    for st, t in variants:
        try:
            # метка голосом marker_voice -> отдельный файл
            # ВАЖНО: метка только кириллицей — латиница («Вариант li.») валит
            # разбор SSML у этой модели (см. QUESTION_TURN_LABELS).
            if args.marker:
                label = core.QUESTION_TURN_LABELS.get(st, "как есть")
                mssml = core.build_ssml(f"Вариант {label}.", profile="calm")
                mmono = core.rms_normalize(core.synth(model, mssml, voice=marker_voice))
                core.write_audio(mmono, os.path.join(out_dir, f"00-{st or 'base'}-mark.wav"),
                                 ogg=True)
            row = _emit(model, rows, out_dir, args, t, st or "base", "warm",
                        None, None, core.QUESTION_TURNS.get(st, "как есть"))
            row["turn"] = st or "base"
            row["original"] = text
            rows.append(row)
            _log(f"{st or 'base':<9} {row['duration_s']:6.2f}s "
                 f"{str(row['f0_start_hz']):>6}Гц {str(row['tail_gain']):>7} "
                 f"{str(row['peak_pos']):>6}  {t}")
        except Exception as e:
            _log(f"{st or 'base':<9} ОШИБКА: {type(e).__name__}: {str(e)[:44]}")

    p = _manifest(out_dir, rows, {"kind": "human", "voice": args.voice,
                                 "text": text, "marker_voice": marker_voice})
    _log("")
    _log(f"готово: {len(rows)} вариантов в {out_dir}, потоков {core.torch_threads()}")
    _log(f"манифест: {p}")
    _log("Слушать *.ogg по порядку. Метка (голос "
         f"{marker_voice}) называет оборот перед каждым образцом.")
    return 0


def cmd_palette(args) -> int:
    text = args.text or DEFAULT_TEXT
    out_dir = os.path.join(args.out, f"{args.voice}-palette")
    os.makedirs(out_dir, exist_ok=True)
    _log(f"ТЕКСТ: {text}")
    _log(f"ГОЛОС: {args.voice}   выход: {out_dir}")
    if args.normalize:
        text = core.normalize_text(text)
        _log(f"НОРМАЛИЗОВАНО: {text}")
    if args.accent:
        text, pairs = core.accentuate(text)
        if pairs:
            _log(f"УДАРЕНИЯ: {text}")
    _log("")

    model = _load(args)
    rows = []
    _log(f"{'вариант':<16} {'длит':>7} {'пик F0':>7} {'интонация':<13} примечание")
    _log("-" * 78)
    for tag, profile, rate, pitch, note in PALETTE:
        try:
            row = _emit(model, rows, out_dir, args, text, tag, profile, rate, pitch, note)
            rows.append(row)
            _log(f"{tag:<16} {row['duration_s']:6.2f}s {str(row['f0_peak_hz']):>6}Гц "
                 f"{row['intonation']:<13} {note}")
        except Exception as e:
            _log(f"{tag:<16} ОШИБКА: {type(e).__name__}: {str(e)[:50]}")

    p = _manifest(out_dir, rows, {"kind": "palette", "voice": args.voice, "text": text})
    _log("")
    _log(f"готово: {len(rows)} вариантов в {out_dir}, потоков {core.torch_threads()}")
    _log(f"манифест: {p}")
    _log("Слушать: файлы *.ogg (WAV — для дальнейшей обработки).")
    return 0


def cmd_compare(args) -> int:
    text = args.text or DEFAULT_TEXT
    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    bad = [v for v in voices if v not in core.VOICES]
    if bad:
        _log(f"неизвестные голоса: {bad}; есть: {list(core.VOICES)}")
        return 2
    out_dir = os.path.join(args.out, "compare")
    os.makedirs(out_dir, exist_ok=True)

    if args.normalize:
        text = core.normalize_text(text)
    if args.accent:
        text, _ = core.accentuate(text)

    _log(f"ТЕКСТ: {text}")
    _log(f"ГОЛОСА: {', '.join(voices)}")
    _log("Громкость выравнивается по RMS — иначе выиграет самый громкий голос,")
    _log("а не самый приятный. Настройки у всех одинаковые.")
    _log("")

    model = _load(args)
    rows = []
    _log(f"{'голос':<10} {'длит':>7} {'медиана F0':>11} {'пик F0':>7} {'интонация':<13}")
    _log("-" * 56)
    for v in voices:
        sub = argparse.Namespace(**vars(args))
        sub.voice = v
        row = _emit(model, rows, out_dir, sub, text, "cmp",
                    args.profile, args.rate, args.pitch, f"голос {v}")
        rows.append(row)
        _log(f"{v:<10} {row['duration_s']:6.2f}s {str(row['f0_median_hz']):>9}Гц "
             f"{str(row['f0_peak_hz']):>6}Гц {row['intonation']:<13}")

    p = _manifest(out_dir, rows, {"kind": "compare", "voices": voices, "text": text})
    _log("")
    _log(f"манифест: {p}")
    _log("Файлы: " + ", ".join(os.path.basename(r["files"][1]) for r in rows
                               if len(r["files"]) > 1))
    return 0


def cmd_tune(args) -> int:
    """Замер ФАКТИЧЕСКОГО эффекта темпа. Нужен всегда, когда хочется сказать
    «ускорил на N%»: номинал и факт расходятся в разы."""
    text = args.text or "Хорошо, включаю свет в гостиной. Нужно ли мне сделать что-то *ещё*?"
    if args.normalize:
        text = core.normalize_text(text)
    if args.accent:
        text, _ = core.accentuate(text)
    out_dir = os.path.join(args.out, f"{args.voice}-tune")
    os.makedirs(out_dir, exist_ok=True)

    steps = args.rates.split(",") if args.rates else core.RATE_STEPS
    _log(f"ТЕКСТ: {text}")
    _log(f"ГОЛОС: {args.voice}   профиль: {args.profile}")
    _log(f"Мерим на ЭТОМ тексте: эффект темпа зависит от длины фразы.")
    _log("")

    model = _load(args)
    rows = []
    base = None
    _log(f"{'rate':<8} {'длит':>8} {'факт к 100%':>12} {'пик F0':>7}")
    _log("-" * 40)
    for r in steps:
        ssml = core.build_ssml(text, profile=args.profile,
                               rate=("100%" if r == "100%" else r),
                               pitch=args.pitch)
        mono = core.synth(model, ssml, voice=args.voice)
        dur = len(mono) / core.SR
        if base is None:
            base = dur
            note = ""
        else:
            note = f"{(dur/base-1)*100:+6.2f}%"
        rep = core.intonation_report(mono)
        path = os.path.join(out_dir, f"{args.voice}-rate-{r.replace('%','pct')}.wav")
        core.write_audio(mono, path, ogg=True)
        rows.append({"rate": r, "duration_s": round(dur, 3),
                     "delta_pct": round((dur / base - 1) * 100, 2) if base else 0.0,
                     "f0_peak_hz": round(rep["peak"]) if rep else None,
                     "files": os.path.basename(path)})
        _log(f"{r:<8} {dur:7.3f}s {note:>12} "
             f"{str(round(rep['peak']) if rep else '-'):>6}Гц")

    target = args.target
    if target is not None and base:
        want = base * (1 + target / 100)
        best = min(rows, key=lambda x: abs(x["duration_s"] - want))
        _log("")
        _log(f"ЦЕЛЬ {target:+g}% -> нужно {want:.3f}s")
        _log(f"БЛИЖАЙШАЯ СТУПЕНЬ: rate={best['rate']} -> {best['duration_s']:.3f}s "
             f"({best['delta_pct']:+.2f}%)")
        _log("Если промах большой — между ступенями НЕТ промежуточных значений:")
        _log("темп квантуется кадрами, поэтому вариант только выбирать ближайший.")

    p = _manifest(out_dir, rows, {"kind": "tune", "text": text, "voice": args.voice,
                                 "target_pct": target})
    _log("")
    _log(f"манифест: {p}")
    return 0


def cmd_question(args) -> int:
    """Сравнивает варианты высоты тона; метрики не распознают вопросительность."""
    text = args.text or "Включить свет в спальне?"
    out_dir = os.path.join(args.out, f"{args.voice}-question")
    os.makedirs(out_dir, exist_ok=True)
    if not text.rstrip().endswith(("?", "!")):
        text = text.rstrip(".") + "?"
    if args.normalize:
        text = core.normalize_text(text)

    _log(f"ВОПРОС: {text}")
    _log(f"ГОЛОС: {args.voice}")
    _log("")
    _log("Метрики показывают только контур высоты тона, не тип высказывания.")
    _log("Сравнивай звучание на слух: Silero не предоставляет режима «вопрос».")
    _log("")

    model = _load(args)
    variants = [
        ("whole-high", "neutral", "108%", "high",
         "вся фраза на high — сравнить с вариантами без настройки"),
        ("whole-low", "neutral", "108%", "low", "вся фраза низким тоном"),
        ("plain", "neutral", None, None, "без настроек тона"),
        ("tail-high", "question", None, None,
         "низкий старт + высокий хвост (эвристика SSML)"),
        ("warm", "warm", None, None, "профиль warm (первая фраза ниже, дальше выше)"),
    ]
    rows = []
    _log(f"{'вариант':<11} {'старт':>7} {'пик':>7} {'пик_поз':>8} {'хвост':>7} "
         f"{'контур':<18} пояснение")
    _log("-" * 100)
    for tag, profile, rate, pitch, note in variants:
        ssml = core.build_ssml(text, profile=profile, rate=rate, pitch=pitch)
        mono = core.synth(model, ssml, voice=args.voice)
        rep = core.intonation_report(mono)
        contour, why = core.judge_question(rep) if rep else ("не измерить", "")
        path = os.path.join(out_dir, f"{args.voice}-q-{tag}.wav")
        core.write_audio(mono, path, ogg=True)
        rows.append({"tag": tag, "profile": profile, "rate": rate, "pitch": pitch,
                     "note": note, "contour": contour, "why": why, "ssml": ssml,
                     "f0_start_hz": round(rep["start"]) if rep else None,
                     "f0_peak_hz": round(rep["peak"]) if rep else None,
                     "peak_pos": round(rep["peak_pos"], 3) if rep else None,
                     "tail_gain": round(rep["tail_gain"], 3) if rep else None,
                     "files": [os.path.basename(path)]})
        if rep:
            _log(f"{tag:<11} {rep['start']:6.0f}Гц {rep['peak']:6.0f}Гц "
                 f"{rep['peak_pos']:8.2f} {rep['tail_gain']:7.2f} "
                 f"{contour:<18} {note}")
        else:
            _log(f"{tag:<11} не измерить   {note}")

    p = _manifest(out_dir, rows, {"kind": "question", "text": text, "voice": args.voice})
    _log("")
    _log("Сравнивай на слух файлы: " + ", ".join(
        os.path.basename(r["files"][0]) for r in rows))
    _log(f"манифест: {p}")
    return 0


def cmd_say(args) -> int:
    text = args.text or DEFAULT_TEXT
    if args.normalize:
        text = core.normalize_text(text)
    if getattr(args, "human", None):
        text = core.humanize_question(text, style=args.human)
    if args.accent:
        text, pairs = core.accentuate(text)
        if pairs and not args.quiet:
            _log(f"[i] ударения: {text}")
    ssml = core.build_ssml(text, profile=args.profile, rate=args.rate, pitch=args.pitch)

    model = _load(args)
    mono = core.synth(model, ssml, voice=args.voice)
    dur = len(mono) / core.SR

    if args.out:
        os.makedirs(args.out, exist_ok=True) if not args.out.endswith(".wav") else None
        path = args.out if args.out.endswith(".wav") else os.path.join(
            args.out, f"{args.voice}-say.wav")
        made = core.write_audio(mono, path, ogg=True)
        _log(f"сохранено: {', '.join(made)} ({dur:.2f}s)")
        return 0

    path = f"/tmp/voice_lab_{os.getpid()}.wav"
    core.write_audio(mono, path, ogg=False)
    rc = core.play(path, device=args.device)
    try:
        os.unlink(path)
    except OSError:
        pass
    if rc == 0:
        _log(f"проиграно на {args.device} ({dur:.2f}s)")
    elif rc == 4:
        _log("нет aplay (apt install alsa-utils)")
    else:
        _log(f"aplay не смог вывести на {args.device} — проверь устройство/колонку")
    return rc


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Стенд подбора голоса и интонации (Silero v5_5_ru)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("ПРИМЕРЫ")[1] if "ПРИМЕРЫ" in __doc__ else None)
    ap.add_argument("-V", "--version", action="version", version="voice_lab 1.0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, with_text=True):
        if with_text:
            p.add_argument("-t", "--text", help="текст для синтеза")
        p.add_argument("-v", "--voice", default=core.DEFAULT_VOICE,
                       choices=list(core.VOICES))
        p.add_argument("-o", "--out", default="./lab",
                       help="каталог для файлов (по умолчанию ./lab)")
        # Короткие флаги те же, что в scripts/say.sh (-p профиль, -P тон):
        # разнобой в интерфейсе между стендом и продакшен-скриптом сбивает.
        p.add_argument("-p", "--profile", default="warm", choices=list(core.PROFILES))
        p.add_argument("-r", "--rate", help="SSML rate, напр. 108%%")
        p.add_argument("-P", "--pitch", help="SSML pitch: high, low, +5%%")
        p.add_argument("--model", help="путь к модели (по умолчанию ~/models/tts/...)")
        # -j как «jobs»: -t в стенде занят текстом.
        p.add_argument("-j", "--threads", type=int, default=None,
                       help="потоков на синтез (по умолчанию 4 — замеренный оптимум; или TTS_THREADS)")
        p.add_argument("--normalize", action="store_true", default=True,
                       help="переписать цифры/латиницу словами (по умолчанию ВКЛ)")
        p.add_argument("--no-normalize", dest="normalize", action="store_false")
        p.add_argument("-a", "--accent", action="store_true",
                       help="расставить ударения через silero-stress")
        p.add_argument("--no-level", dest="level", action="store_false", default=True,
                       help="не выравнивать громкость по RMS")
        p.add_argument("-q", "--quiet", action="store_true")

    p = sub.add_parser("list", help="голоса, профили, что можно менять")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="что Silero вырежет + ударения (без синтеза)")
    p.add_argument("-t", "--text")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("palette", help="палитра вариантов одного текста")
    common(p)
    p.set_defaults(func=cmd_palette)

    p = sub.add_parser("compare", help="один текст разными голосами")
    common(p)
    p.add_argument("--voices", default="baya,kseniya,xenia",
                   help="голоса через запятую")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("tune", help="замер фактического эффекта темпа")
    common(p)
    p.add_argument("--rates", help="своя сетка через запятую, напр. 104%%,108%%,110%%")
    p.add_argument("--target", type=float,
                   help="целевое изменение длительности в %%, напр. -5")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("question", help="сравнить pitch-контуры; не распознаёт вопрос")
    common(p)
    p.set_defaults(func=cmd_question)

    p = sub.add_parser("human", help="сравнить речевые обороты для вопроса")
    common(p)
    p.add_argument("--marker", action="store_true", default=True,
                   help="озвучить метку оборота чужим голосом (по умолчанию ВКЛ)")
    p.add_argument("--no-marker", dest="marker", action="store_false")
    p.set_defaults(func=cmd_human)

    p = sub.add_parser("say", help="озвучить один вариант")
    common(p)
    p.add_argument("-d", "--device", default=core.DEFAULT_DEVICE)
    p.add_argument("--human", default=None, choices=list(core.QUESTION_TURNS),
                   metavar="STYLE", help="переформулировать вопрос: " +
                   " | ".join(core.QUESTION_TURNS))
    p.set_defaults(func=cmd_say)

    args = ap.parse_args()
    if args.cmd == "check":
        return args.func(args)
    if getattr(args, "out", None):
        args.out = os.path.expanduser(args.out)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        _log(f"ОШИБКА: {e}")
        _log("Модель качается отдельно, см. docs/09-tts.md")
        return 1
    except KeyboardInterrupt:
        _log("прервано")
        return 130


if __name__ == "__main__":
    sys.exit(main())
