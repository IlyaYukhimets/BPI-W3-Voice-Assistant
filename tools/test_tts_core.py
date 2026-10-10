#!/usr/bin/env python3
"""
Тесты нормализации текста и сборки SSML (scripts/tts_core.py).

ЗАЧЕМ. Нормализация — самое неблагодарное место в проекте: ошибка в ней не
ломает запуск, а тихо портит смысл реплики («через 1 минуту» -> «через один
минуту»), причём услышать это можно только на слух. Поэтому каждая форма
зафиксирована тестом.

Запуск (torch НЕ нужен — тесты работают с текстом и SSML):
    python3 tools/test_tts_core.py

Зависимость: num2words==0.5.14 (см. docs/10-voice-tuning.md про 0.5.15/0.5.16).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import tts_core as core  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def eq(got, want, what: str) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{what}\n    получено: {got!r}\n    ожидалось: {want!r}")


def true(cond, what: str) -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(what)


# ---------------------------------------------------------------- время
def test_time():
    eq(core.normalize_text("Сейчас 19:45"), "Сейчас девятнадцать сорок пять", "время 19:45")
    eq(core.normalize_text("в 19:05"), "в девятнадцать ноль пять", "время с ведущим нулём")
    eq(core.normalize_text("в 3:05"), "в три ноль пять", "время 3:05")
    eq(core.normalize_text("00:30"), "ноль тридцать", "полночь")


# ------------------------------------------------------- числа и единицы
def test_numbers_units():
    eq(core.normalize_text("через 1 минуту"), "через одну минуту", "вин. падеж, женский род")
    eq(core.normalize_text("через 2 минуты"), "через две минуты", "две (не два) минуты")
    eq(core.normalize_text("через 21 минуту"), "через двадцать одну минуту",
       "составное числительное + падеж")
    eq(core.normalize_text("1 минута"), "одна минута", "им. падеж, женский род")
    eq(core.normalize_text("22 минуты"), "двадцать две минуты", "22 -> две")
    eq(core.normalize_text("5 минут"), "пять минут", "родительный мн.")
    eq(core.normalize_text("1 час"), "один час", "мужской род")
    eq(core.normalize_text("2 часа"), "два часа", "два часа")
    eq(core.normalize_text("1 яблоко"), "одно яблоко", "средний род")
    eq(core.normalize_text("11 минут"), "одиннадцать минут", "11 -> many, не one")
    eq(core.normalize_text("12 минут"), "двенадцать минут", "12 -> many")
    eq(core.normalize_text("14 минут"), "четырнадцать минут", "14 -> many")
    eq(core.normalize_text("21 минута"), "двадцать одна минута", "21 без предлога -> одна")


def test_temperature_percent():
    eq(core.normalize_text("-3 градуса"), "минус три градуса", "минус")
    eq(core.normalize_text("-3°C"), "минус три градуса", "градусы с °C")
    eq(core.normalize_text("+5 °C"), "плюс пять градусов", "плюс")
    eq(core.normalize_text("скидка 50%"), "скидка пятьдесят процентов", "проценты")
    eq(core.normalize_text("1%"), "один процент", "один процент")
    eq(core.normalize_text("2%"), "два процента", "два процента")
    eq(core.normalize_text("5%"), "пять процентов", "пять процентов")


# ------------------------------------------------------------- латиница
def test_latin():
    eq(core.normalize_text("Открой YouTube"), "Открой ютуб", "YouTube по словарю")
    eq(core.normalize_text("проверь Telegram"), "проверь телеграм", "Telegram")
    eq(core.normalize_text("включи Wi-Fi"), "включи вай-фай", "Wi-Fi")
    eq(core.normalize_text("Home Assistant"), "хоум ассистент", "Home Assistant")
    true("ютуб" in core.normalize_text("открой youtube").lower(), "регистр не важен")


# ------------------------------------- главный сценарий: раньше терялся смысл
def test_real_world():
    """Раньше эти фразы звучали с потерянным смыслом (см. docs/09-tts.md)."""
    got = core.normalize_text("Включи свет через 10 минут.")
    eq(got, "Включи свет через десять минут.", "«через 10 минут» больше не «через минуту»")
    true("десять" in got, "числительное на месте, смысл сохранён")

    got = core.normalize_text("Открой YouTube и Telegram.")
    true("ютуб" in got and "телеграм" in got, "YouTube и Telegram не исчезают")

    got = core.normalize_text("Сейчас 19:45.")
    true("девятнадцать" in got, "время не превращается в «Сейчас.»")

    full = core.normalize_text(
        "Напомни через 10 минут, открой YouTube в 19:45. На улице -3°C, скидка 50%.")
    for must in ("десять минут", "ютуб", "девятнадцать сорок пять",
                 "минус три градуса", "пятьдесят процентов"):
        true(must in full, f"в комплексной фразе есть «{must}»")


# ------------------------------------------------- проверка опасных символов
def test_check_text():
    bad = dict(core.check_text("через 10 минут"))
    true("1" in bad and "0" in bad, "цифры определяются как опасные")

    clean = core.normalize_text("через 10 минут")
    eq(core.check_text(clean), [], "после нормализации опасных символов нет")

    eq(core.check_text("Привет, как дела?"), [],
       "кириллица и пунктуация безопасны")


# ------------------------------------------------------------ SSML/профили
def test_ssml():
    for name in core.PROFILES:
        s = core.build_ssml("Включить свет в спальне?", profile=name)
        true(s.startswith("<speak>") and s.endswith("</speak>"),
             f"профиль {name}: корневой <speak> на месте")

    # ГЛАВНОЕ: вопрос не должен быть обёрнут целиком в pitch="high".
    # Иначе старт у верхней границы диапазона и вопрос звучит утверждением.
    s = core.build_ssml("Включить свет в спальне?", profile="question")
    true('pitch="low"' in s, "профиль question: есть низкий старт (запас по высоте)")
    true(s.count('pitch="high"') >= 1, "профиль question: есть высокий хвост")

    # Вопрос из двух слов делится на низкое тело и высокий хвост; одно слово
    # не повышается целиком, потому что ему не из чего строить контур.
    for profile in ("question", "warm", "lively"):
        s = core.build_ssml("Нужно ещё?", profile=profile)
        true('pitch="low"' in s and 'pitch="high"' in s,
             f"{profile}: короткий вопрос разделён на тело и хвост")
        s = core.build_ssml("Готово?", profile=profile)
        true('pitch="high"' not in s,
             f"{profile}: однословный вопрос не повышен целиком")

    # темп: warm использует проверенную ступень 108%
    s = core.build_ssml("Привет. Как дела?", profile="warm")
    true('rate="108%"' in s, "профиль warm: темп 108% (замеренная ступень)")
    true('<break time="350ms"/>' in s, "профиль warm: пауза между фразами")

    # ручное переопределение
    s = core.build_ssml("Тест", profile="neutral", rate="115%", pitch="low")
    true('rate="115%"' in s and 'pitch="low"' in s, "rate/pitch перекрывают профиль")

    # неизвестный профиль должен падать понятно, а не молча
    try:
        core.build_ssml("Тест", profile="nope")
        FAILED.append("неизвестный профиль не вызвал ошибку")
    except ValueError:
        true(True, "неизвестный профиль -> ValueError")


def test_sentence_split():
    s = core._split_sentences("Первое. Второе! Третье?")
    eq(s, ["Первое.", "Второе!", "Третье?"], "разбивка по фразам с сохранением знака")


def test_pitch_contour_labels():
    rising = {"dur": 2.0, "tail_gain": 1.2, "start": 200, "peak_pos": 0.8}
    falling = {"dur": 2.0, "tail_gain": 0.8, "start": 200, "peak_pos": 0.2}
    short = {"dur": 1.0, "tail_gain": 1.2, "start": 200, "peak_pos": 0.8}
    eq(core.judge_question(rising)[0], "ХВОСТ ВЫШЕ",
       "pitch-метрика описывает подъём, а не вопрос")
    eq(core.judge_question(falling)[0], "ХВОСТ НИЖЕ",
       "pitch-метрика описывает падение, а не утверждение")
    eq(core.judge_question(short)[0], "НЕЯСНО",
       "короткий pitch-контур помечается ненадёжным")


def test_accusative_detection():
    true(core._looks_accusative("через 5", 6), "«через» -> винительный")
    true(core._looks_accusative("за 5", 3), "«за» -> винительный")
    true(not core._looks_accusative("осталось 5", 10), "«осталось» -> не винительный")


def main() -> int:
    for fn in (test_time, test_numbers_units, test_temperature_percent, test_latin,
               test_real_world, test_check_text, test_ssml,
               test_sentence_split, test_pitch_contour_labels,
               test_accusative_detection):
        fn()

    print(f"пройдено проверок: {PASSED}")
    if FAILED:
        print(f"\nОШИБОК: {len(FAILED)}\n")
        for f in FAILED:
            print(f"  ✗ {f}")
        return 1
    print("все тесты пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
