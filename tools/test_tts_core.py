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
import json
import os
import re
import struct
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


def fail(what: str) -> None:
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


def test_humanize_question():
    # значение по умолчанию — выбранный на слух оборот «ли»
    eq(core.humanize_question("Включить свет?"), "Включить ли свет?",
       "по умолчанию применяется оборот «ли»")
    # «ли» ставится после первого слова
    eq(core.humanize_question("Включить свет?", style="li"), "Включить ли свет?",
       "оборот «ли» после первого слова")
    # однословный вопрос: «ли» не встаёт, уходим на хвостик
    eq(core.humanize_question("Готово?", style="li"), "Готово, да?",
       "однословный вопрос не получает «ли»")
    eq(core.humanize_question("Включить свет?", style="maybe"), "Может, включить свет?",
       "зачин «Может,» и понижение регистра")
    eq(core.humanize_question("Готово?", style="tag"), "Готово, да?",
       "хвостик «, да?»")
    eq(core.humanize_question("Ты уверен?", style="verno"), "Ты уверен, верно?",
       "хвостик «, верно?»")
    # утверждения не трогаем
    eq(core.humanize_question("Включаю свет.", style="maybe"), "Включаю свет.",
       "утверждение остаётся без изменений")
    # идемпотентность: повторный прогон ничего не добавляет
    once = core.humanize_question("Включить свет?", style="tag")
    eq(core.humanize_question(once, style="tag"), once,
       "повторный вызов не дублирует оборот")
    true(core.humanize_question("Включить ли свет?", style="li") == "Включить ли свет?",
         "уже готовый оборот не удваивается")
    # смешанный текст: вопрос переформулирован, ответ — нет
    eq(core.humanize_question("Свет включён. Нужно ещё?", style="maybe"),
       "Свет включён. Может, нужно ещё?",
       "в смешанном тексте трогается только вопрос")
    # неизвестный стиль — явная ошибка, а не молчаливое игнорирование
    try:
        core.humanize_question("Готово?", style="nope")
        true(False, "неизвестный оборот должен падать")
    except ValueError:
        true(True, "неизвестный оборот -> ValueError")


def test_question_split():
    # Теги SSML разделяют сегменты, поэтому при «очистке» ставим ПРОБЕЛ:
    # иначе «Нужно</prosody><prosody>ещё?» склеится в одно слово и счёт врёт.
    def plain_words(ssml: str) -> list[str]:
        return re.sub(r"<[^>]+>", " ", ssml).split()

    # однословный вопрос НЕ дублируется в профилях (регрессия)
    for profile in ("question", "warm", "lively"):
        s = core.build_ssml("Готово?", profile=profile)
        eq(len(plain_words(s)), 1,
           f"{profile}: однословный вопрос произносится один раз")
    # двухсловный — делится на низкое тело и высокий хвост
    for profile in ("question", "warm", "lively"):
        s = core.build_ssml("Нужно ещё?", profile=profile)
        true('pitch="low"' in s and 'pitch="high"' in s,
             f"{profile}: короткий вопрос разделён на тело и хвост")
        eq(len(plain_words(s)), 2,
           f"{profile}: двухсловный вопрос не дублируется")
        eq(plain_words(s), ["Нужно", "ещё?"],
           f"{profile}: тело и хвост сохранены в правильном порядке")
    # тело и хвост не теряются и не повторяются
    eq(core._q_tail("Включить свет?"), ("Включить", "свет?"), "деление на тело и хвост")
    eq(core._q_tail("Готово?"), ("Готово?", ""), "одно слово -> тела нет")


def test_accusative_detection():
    true(core._looks_accusative("через 5", 6), "«через» -> винительный")
    true(core._looks_accusative("за 5", 3), "«за» -> винительный")
    true(not core._looks_accusative("осталось 5", 10), "«осталось» -> не винительный")


# ---------------------------------------------------------------- потоки
def test_resolve_threads():
    """Потоки: аргумент -> TTS_THREADS -> все ядра; мусор падает понятно.

    Тест нужен потому, что раньше значение молча не применялось: модель Silero
    затирала его при загрузке, и код «настраивал» то, что не работало.
    """
    env = core.THREADS_ENV
    saved = os.environ.get(env)
    try:
        os.environ[env] = "3"
        eq(core.resolve_threads(5), 5, "аргумент важнее переменной окружения")
        eq(core.resolve_threads(), 3, "TTS_THREADS учитывается")

        # Пробельная строка = переменная не задана (иначе int("") упал бы).
        os.environ[env] = "   "
        eq(core.resolve_threads(), core.THREADS_DEFAULT, "пробелы трактуются как «не задано»")

        os.environ.pop(env, None)
        eq(core.resolve_threads(), core.THREADS_DEFAULT,
           "по умолчанию — замеренный оптимум, а не все ядра")
        true(core.THREADS_DEFAULT >= 1, "THREADS_DEFAULT — положительное число")

        for bad in ("abc", "0", "-1", "2.5"):
            os.environ[env] = bad
            err = None
            try:
                core.resolve_threads()
            except ValueError as e:
                err = e
            true(err is not None and env in str(err),
                 f"{bad!r} в TTS_THREADS -> ValueError с упоминанием {env}")
    finally:
        if saved is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = saved

    for bad in (0, -2):
        err = None
        try:
            core.resolve_threads(bad)
        except ValueError as e:
            err = e
        true(err is not None, f"resolve_threads({bad}) -> ValueError")


# ---------------------------------------------------------------- воркер и ASR
def test_worker_protocol():
    """Протоколы резидентных служб: путь сокета, строка запроса, разбор ответа.

    Клиент и сервер пользуются ОДНОЙ сборкой запроса (core.worker_request), так
    что тест фиксирует не реализацию, а договорённость между сторонами.
    """
    saved = os.environ.get(core.WORKER_SOCKET_ENV)
    os.environ.pop(core.WORKER_SOCKET_ENV, None)
    try:
        eq(core.worker_socket_path(), os.path.expanduser("~/.assistant/ttsd.sock"),
           "путь сокета по умолчанию")
        os.environ[core.WORKER_SOCKET_ENV] = "/tmp/alt-ttsd.sock"
        eq(core.worker_socket_path(), "/tmp/alt-ttsd.sock",
           "TTSD_SOCKET переопределяет путь")
    finally:
        if saved is None:
            os.environ.pop(core.WORKER_SOCKET_ENV, None)
        else:
            os.environ[core.WORKER_SOCKET_ENV] = saved

    # Запрос: ровно одна строка, JSON валиден и содержит голос+SSML.
    line = core.worker_request("baya", "<speak>привет</speak>")
    true(line.endswith(b"\n"), "запрос к воркеру кончается \\n")
    eq(len(line.decode("utf-8").splitlines()), 1, "запрос — ровно одна строка")
    eq(json.loads(line.decode("utf-8")),
       {"voice": "baya", "ssml": "<speak>привет</speak>"}, "JSON запроса")

    # ASR: фрейм протокола sherpa и разбор его ответа.
    import asr_ws
    msg = asr_ws.samples_message(16000, b"\x00" * 8)
    eq(msg[:8], (16000).to_bytes(4, "little") + (8).to_bytes(4, "little"),
       "заголовок ASR-фрейма: rate (LE) и длина данных в байтах")
    eq(len(msg), 16, "фрейм = 8 байт заголовка + данные")
    # int16 -> float32 для протокола: масштаб [-1, 1] и размер x2 на сэмпл.
    f32 = asr_ws.pcm16_to_float32(struct.pack("<3h", 16384, 0, -16384))
    eq(len(f32), 12, "float32: 4 байта на сэмпл")
    vals = struct.unpack("<3f", f32)
    eq(round(vals[0], 3), 0.5, "int16 верхняя половина -> ~0.5 float")
    eq(vals[1], 0.0, "нуль остаётся нулём")
    eq(round(vals[2], 3), -0.5, "нижняя половина -> ~-0.5 float")
    eq(asr_ws.parse_result('{"text": "Включить свет"}'), "Включить свет",
       "JSON-ответ ASR-сервера -> текст")
    eq(asr_ws.parse_result("Просто текст"), "Просто текст",
       "текстовый ответ ASR-сервера проходит без правок")


def test_mono_from_bytes():
    """Ответ воркера должен превращаться в перезаписываемый float32-массив."""
    try:
        import numpy as np
    except ImportError:
        # В bare-окружении (CI test job гоняет тесты только с num2words) эти
        # проверки невозможны. Пропускаем С ВИДИМОЙ отметкой: пустой пропуск
        # выглядел бы зелёным при мёртвых проверках на реальном numpy.
        print("пропущено: numpy отсутствует, mono_from_bytes не проверяется")
        return
    import numpy as np

    raw = np.array([0.5, -0.5, 0.25], dtype=np.float32).tobytes()
    arr = core.mono_from_bytes(raw)
    eq(arr.dtype, np.float32, "тип данных")
    eq(arr.tolist(), [0.5, -0.5, 0.25], "значения на месте")
    true(arr.flags.writeable,
         "массив перезаписываем (иначе rms_normalize упадёт позже)")
    try:
        core.mono_from_bytes(b"\x00\x00\x00")  # не кратно 4
    except ValueError as e:
        true("не кратен 4" in str(e), "ошибка про кратность 4 байтам")
    else:
        fail("mono_from_bytes(3 байта) должен давать ValueError")


def test_worker_framing():
    """Кадрирование ответа воркера: статус режется по ПЕРВОМУ \\n.

    Регрессия реального сбоя: сервер пишет статус и payload ПОДРЯД, а
    float32-аудио содержит байт 0x0a. Клиент, читавший «до \\n», под
    нагрузкой получал статус+payload одним чанком и трактовал это то как
    мусорный JSON, то как «воркер закрыл соединение» — рабочий ответ
    выбрасывался, реплика шла холодным путём.
    """
    import socket

    payload = bytes([0x0A, 0x00, 0xFF, 0x0A]) * 3   # 0x0a внутри аудио
    status = b'{"ok": true, "samples": 3, "gen_s": 0.02, "threads": 4}\n'

    # Случай 1: статус и payload пришли одним чанком (худший вариант).
    pair1, pair2 = socket.socketpair()
    try:
        pair2.sendall(status + payload)
        resp, tail = core.read_worker_reply(pair1)
        eq(resp, {"ok": True, "samples": 3, "gen_s": 0.02, "threads": 4},
           "статус разобран даже при склеенном чанке")
        eq(tail, payload, "payload получен целиком и без искажений")
    finally:
        pair1.close()
        pair2.close()

    # Случай 2: статус и payload разнесены по чанкам (обычный вариант).
    pair1, pair2 = socket.socketpair()
    try:
        pair2.sendall(status[:20])
        pair2.sendall(status[20:] + payload)
        resp, tail = core.read_worker_reply(pair1)
        eq(resp["ok"], True, "статус из двух чанков")
        eq(tail, payload, "payload после перенесённого статуса")
    finally:
        pair1.close()
        pair2.close()

    # Случай 3: экранированный "\n" (2 байта) в статусе не рвёт кадрирование —
    # резать надо только НАСТОЯЩИЙ байт 0x0a. Строки собираем программно, чтобы
    # не путать escape-последовательности с реальными байтами.
    pair1, pair2 = socket.socketpair()
    try:
        esc = chr(92) + "n"                       # backslash + 'n', не перевод строки
        wire = b'{"ok": false, "error": "line1' + esc.encode() \
               + b'line2"}' + b"\n" + payload
        pair2.sendall(wire)
        resp, tail = core.read_worker_reply(pair1)
        # json-экскейп \n на проводе декодируется в НАСТОЯЩИЙ перевод строки —
        # и это нормально: кадрирование режет по сырому байту, а не по смыслу.
        eq(resp.get("error"), "line1" + chr(10) + "line2",
           "экранированный перенос внутри JSON-строки не обрывает статус")
        eq(tail, payload, "payload после статуса с экранированным переносом")
    finally:
        pair1.close()
        pair2.close()

    # Случай 4: соединение закрыто до конца статуса -> ValueError (фолбэк).
    pair1, pair2 = socket.socketpair()
    try:
        pair2.close()
        try:
            core.read_worker_reply(pair1)
        except ValueError as e:
            true("закрыто до конца" in str(e), "EOF до \\n -> ValueError")
        else:
            fail("обрыв соединения должен давать ValueError")
    finally:
        pair1.close()


def test_voiced_gate():
    """Машина состояний триггер-цикла (SpeechGate) БЕЗ модели: вероятности
    речи подставляются как callable.

    Регрессия sherpa-vad-alsa: сегменты там начинаются прямо на пересечении
    порога, из-за чего срезается НАЧАЛО фразы (проверено: «Включить свет» ->
    «Свет в коридоре»). Здесь сегмент обязан содержать пре-ролл — аудио до
    пересечения, взятое из кольцевого буфера. Каждое окно помечаем своим
    содержимым и сверяем его попадание в сегмент.
    """
    import voiced

    cfg = voiced.GateConfig(threshold=0.5, min_speech=0.25, min_silence=0.3,
                            pre_roll=0.4, sample_rate=16000)
    gate = voiced.SpeechGate(cfg, prob_fn=lambda w: PROBS[0])
    wb = voiced.WINDOW_SAMPLES * 2
    w = b"\x00\x00" * voiced.WINDOW_SAMPLES   # «безопасное» содержимое окна

    # --- тишина до и после: сегмента быть не должно -----------------------
    for i in range(20):
        PROBS[0] = 0.0
        true(gate.feed(w) is None, "тишина не порождает сегмент")

    # --- речь (0.4 с) -> пауза: один сегмент, с пре-роллом и без хвоста ----
    # Каждое окно помечаем своим номером в первых 2 байтах; prob задаётся
    # В МОМЕНТ кормления (prob_fn читается на каждом feed).
    feed_list = (
        [(0.0, struct.pack("<h", i) + w[2:]) for i in range(12)]      # тишина 0.384 с
        + [(1.0, struct.pack("<h", i) + w[2:]) for i in range(100, 114)]  # речь 0.448 с
        + [(0.0, struct.pack("<h", i) + w[2:]) for i in range(40, 60)])   # тишина 0.64 с

    got = None
    for prob, cur in feed_list:
        PROBS[0] = prob
        r = gate.feed(cur)
        if r is not None:
            got = r
    true(got is not None, "сегмент закрылся после паузы")
    eq(got["index"], 0, "порядковый номер сегмента")

    samples = got["samples"]
    true(samples > 0, "сегмент не пустой")
    pcm = got["pcm"]
    # Пре-ролл: последние окна тишины ПЕРЕД началом речи обязаны быть внутри.
    head_first = struct.unpack("<h", pcm[0:2])[0]
    true(0 < head_first != 100, "сегмент начинается ДО начала речи (пре-ролл)")
    # Хвост: последние окна тишины подрезаются — последний помеченный номер
    # не из хвостовой тишины (40..59).
    tail_last = struct.unpack("<h", pcm[-2:])[0]
    true(not 40 <= tail_last < 59, "хвостовая тишина подрезана")

    # Один разговорный «блок» -> ровно один сегмент...
    got2 = None
    PROBS[0] = 0.0
    for i in range(30):
        true(gate.feed(w) is None, "после сегмента фолс-триггеров нет")
    # ...и следующя речь даёт следующий индекс.
    for i in range(10):
        PROBS[0] = 1.0
        got3 = gate.feed(w)
    for i in range(30):
        PROBS[0] = 0.0
        r = gate.feed(w)
        if r is not None:
            got2 = r
    true(got3 is None and got2 is not None and got2["index"] == 1,
         "вторые реплика/индекс: 1")

    # --- короткий скачок вероятности < min_speech: НЕ сегмент -------------
    gate2 = voiced.SpeechGate(cfg, prob_fn=lambda x: PROBS[1])
    for i in range(40):
        PROBS[1] = 0.0
        true(gate2.feed(w) is None, "фолс-триггер 1")
    for i in range(5):                        # 0.16 с < min_speech 0.25
        PROBS[1] = 1.0
        true(gate2.feed(w) is None, "короткий скачок не открывает сегмент")
    for i in range(30):
        PROBS[1] = 0.0
        true(gate2.feed(w) is None, "после скачка сегмента нет")

    # --- ограничение длинной реплики: разрез по max_speech ----------------
    cfg2 = voiced.GateConfig(threshold=0.5, min_speech=0.1, min_silence=0.5,
                             max_speech=1.0, pre_roll=0.2, sample_rate=16000)
    gate3 = voiced.SpeechGate(cfg2, prob_fn=lambda x: PROBS[2])
    closed = []
    PROBS[2] = 0.0
    for i in range(60):
        r = gate3.feed(w)
        if r:
            closed.append(r)
    PROBS[2] = 1.0
    for i in range(200):                      # ~6.4 с непрерывной речи
        r = gate3.feed(w)
        if r:
            closed.append(r)
    true(len(closed) >= 2, "реплика длиннее max_speech разрезается (кусков >= 2)")
    if len(closed) >= 2:
        eq(closed[1]["index"], closed[0]["index"] + 1,
           "индексы сегментов идут подряд")
        for r in closed:
            true(r["samples"] <= 1.5 * 16000, "кусок не сильно больше max_speech")

    # --- РЕГРЕССИЯ sherpa-семантики: слабые окна НЕ сбрасывают кандидата ---
    # Реальный механизм «Напомни»: первое слово даёт вероятности ниже порога
    # (проверено пробником на плате: 0.2-0.4 при пороге 0.5). Кандидат,
    # открытый на одном сильном окне, должен дожить до min_speech, собирая
    # полосовые окна; последовательная логика его теряла.
    cfg4 = voiced.GateConfig(threshold=0.5, min_speech=0.25, min_silence=0.3,
                             pre_roll=0.4, sample_rate=16000)
    gate4 = voiced.SpeechGate(cfg4, prob_fn=lambda x: PROBS[3])
    PROBS[3] = 0.0
    for i in range(20):
        gate4.feed(w)
    # 1 сильное окно открывает кандидата...
    PROBS[3] = 0.7
    gate4.feed(w)
    # 7 полосовых окон (0.4; выше neg 0.35, ниже порога; 0.224 с)
    PROBS[3] = 0.4
    for i in range(7):
        gate4.feed(w)
    # ещё 8 сильных: всего кандидата >= 0.25 с -> сегмент открывается
    PROBS[3] = 0.7
    for i in range(8):
        gate4.feed(w)
    true(gate4._triggered, "кандидат дожил до min_speech через полосовые окна")
    # закрытие на тишине для последующих фаз
    PROBS[3] = 0.0
    got5 = None
    for i in range(30):
        r = gate4.feed(w)
        if r is not None:
            got5 = r
    true(got5 is not None, "сегмент со слабым началом закрылся")

    # --- гейн PCM: масштаб с насыщением ---------------------------------- -
    import asr_ws
    src = struct.pack("<3h", 1000, -1000, 30000)
    out = struct.unpack("<3h", asr_ws.gain_pcm16(src, 4.0))
    eq(out[0], 4000, "гейн x4 масштабирует сигнал")
    eq(out[1], -4000, "гейн симметричен")
    eq(out[2], 32767, "насыщение по верхней границе int16")


PROBS = [0.0, 0.0, 0.0, 0.0]


def test_assistant_reply():
    """Слой ответа: шаблон вместо str.format, узнавание эха, форма будущего запроса.

    Подстановка через replace — не украшение: распознанный текст приходит от
    ASR как есть и легко содержит фигурные скобки, на которых str.format
    падает. Эхо-защита — условие выживания цикла: микрофон и колонка на одной
    плате, и ответ ассистента обязан быть узнан как свой.
    """
    import assistant

    eq(assistant.make_reply("включить свет"),
       assistant.DEFAULT_TEMPLATE.replace("{text}", "включить свет"),
       "шаблон по умолчанию подставляет распознанный текст")
    # Фигурные скобки и в тексте, и в шаблоне — не повод падать.
    eq(assistant.make_reply("скажи {привет}", "Ответ: {text}"),
       "Ответ: скажи {привет}", "скобки в тексте безопасны")
    eq(assistant.make_reply("да", "{{json}}: {text}"),
       "{{json}}: да", "скобки в шаблоне безопасны")
    eq(assistant.make_reply("  пробелы  ", "{text}"), "пробелы",
       "края текста обрезаются")
    # ASR заканчивает фразу точкой: в шаблоне она уже есть, и без обрезки
    # в ответе звучит «коридоре..» — двойная пунктуация слышна как пауза.
    eq(assistant.make_reply("включить свет.", "{text}"), "включить свет",
       "концевая точка распознанного текста снимается")
    eq(assistant.make_reply("это вопрос?", "{text}!"), "это вопрос!",
       "знак вопроса из ASR не удваивается")
    true("{text}" in assistant.DEFAULT_TEMPLATE,
         "шаблон по умолчанию содержит метку {text}")

    # --- эхо собственной реплики ------------------------------------------
    reply = "Принято: включить свет в коридоре. Отвечаю шаблоном."
    true(assistant.is_echo(reply, reply), "точное совпадение — эхо")
    true(assistant.is_echo("принято включить свет", reply),
         "эхо узнаётся без регистра и пунктуации")
    true(not assistant.is_echo("какая погода", reply),
         "другая фраза эхом не считается")
    true(not assistant.is_echo(reply, None), "без предыдущего ответа эха нет")

    # --- заглушка запроса в HA --------------------------------------------
    req = assistant.stub_intent("включить свет")
    eq(req["method"], "POST", "метод будущего запроса — POST")
    true(req["path"].startswith("/api/services/"),
         "путь ведёт в services-API Home Assistant")
    eq(req["json"]["text"], "включить свет", "текст кладётся в тело запроса")


def test_gate_flush():
    """flush() после своей озвучки: эхо динамика не попадает в сегмент."""
    import voiced

    cfg = voiced.GateConfig(threshold=0.5, min_speech=0.25, min_silence=0.3,
                            pre_roll=0.4, sample_rate=16000)
    gate = voiced.SpeechGate(cfg, prob_fn=lambda x: PROBS[0])
    w = b"\x00\x00" * voiced.WINDOW_SAMPLES
    # Набираем полное кольцо пре-ролла и открытый кандидат.
    PROBS[0] = 0.0
    for i in range(20):
        gate.feed(w)
    PROBS[0] = 0.8
    for i in range(10):          # min_speech 0.25 с = 8 окон -> триггер
        gate.feed(w)
    true(gate._triggered, "перед flush сегмент открыт")
    gate.flush()
    eq(len(gate._piece), 0, "flush очищает текущий сегмент")
    eq(len(gate._ring), 0, "flush очищает кольцо пре-ролла")
    true(gate._cand_at is None, "flush снимает кандидата")
    # После flush тишина не даёт сегмента, а речь даёт — и он целый.
    PROBS[0] = 0.0
    got = None
    for i in range(30):
        r = gate.feed(w)
        if r:
            got = r
    true(got is None, "после flush тишина сегмента не даёт")
    PROBS[0] = 1.0
    for i in range(12):
        gate.feed(w)
    PROBS[0] = 0.0
    for i in range(30):
        r = gate.feed(w)
        if r:
            got = r
    true(got is not None, "после flush речь снова распознаётся")
    if got:
        # Кольцо после flush успело набрать только тишину: сегмент — это
        # 0.13 с догоняющей тишины + речь. Эхо реплики (0.4 с = 6400 сэмплов
        # пре-ролла) дало бы куда больше: 8192 vs ~14600.
        true(got["samples"] <= int(0.55 * 16000),
             "в сегменте нет пре-ролла из эха (кольцо очищено flush)")


def main() -> int:
    for fn in (test_time, test_numbers_units, test_temperature_percent, test_latin,
               test_real_world, test_check_text, test_ssml,
               test_sentence_split, test_pitch_contour_labels,
               test_humanize_question, test_question_split,
               test_accusative_detection, test_resolve_threads,
               test_worker_protocol, test_worker_framing, test_voiced_gate,
               test_assistant_reply, test_gate_flush,
               test_mono_from_bytes):
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
