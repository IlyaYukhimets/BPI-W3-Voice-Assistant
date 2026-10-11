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
    import asr_ws
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

    # --- нормализация сегмента перед ASR (и защита тишины) ------------------
    # Замерено: одно слово-имя с микрофона платы при фиксированном гейне
    # читается «Эстро», после нормализации по пику — «Австра» (уже узнаётся).
    quiet = struct.pack("<4h", 10, -12, 8, -9)
    eq(asr_ws.normalize_pcm16(quiet), quiet,
       "тишина не раздувается: ниже порога сегмент не трогаем")
    loud = asr_ws.normalize_pcm16(struct.pack("<3h", 1000, -2000, 500))
    vals = struct.unpack("<3h", loud)
    true(abs(abs(vals[1]) - int(0.7 * 32767)) <= 1,
         "пик приведён к 0.7 от шкалы int16")
    true(abs(vals[0]) < abs(vals[1]), "пропорции сигнала сохранены")

    # --- гейн PCM: масштаб с насыщением ---------------------------------- -
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
    import asr_ws
    import assistant

    true("Home Assistant" not in assistant.DEFAULT_TEMPLATE
         and "шаблон" not in assistant.DEFAULT_TEMPLATE,
         "в ответе нет служебной приписки про шаблон и Home Assistant")
    true(assistant.make_reply("включить свет").startswith("Принято:"),
         "ответ начинается с подтверждения")
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

    # --- заглушка запроса в HA --------------------------------------------
    req = assistant.stub_intent("включить свет")
    eq(req["method"], "POST", "метод будущего запроса — POST")
    true(req["path"].startswith("/api/services/"),
         "путь ведёт в services-API Home Assistant")
    eq(req["json"]["text"], "включить свет", "текст кладётся в тело запроса")


def test_trim_cue():
    """Обрезка файла сигнала: моно, нужная длина, приглушённые края."""
    import numpy as np
    import assistant

    rate = 16000
    wave = np.sin(2 * np.pi * 440 * np.arange(rate) / rate).astype("float32")
    stereo = np.stack([wave, wave], axis=1)          # чужые записи часто стерео

    cut = assistant.trim_cue(stereo, rate, 0.4)
    eq(cut.ndim, 1, "стерео сведено в моно: колонка одна")
    eq(len(cut), int(rate * 0.4), "запись обрезана до заданной длины")
    true(abs(float(cut[0])) < 0.01 and abs(float(cut[-1])) < 0.01,
         "края приглушены — иначе на обрезке слышен щелчок")
    eq(round(float(np.max(np.abs(cut))), 2), assistant.PEAK_LEVEL,
       "громкость выровнена по встроенным сигналам")

    quiet = np.full(rate // 10, 0.1, dtype="float32")
    kept = assistant.trim_cue(quiet, rate, 5.0)
    eq(len(kept), len(quiet), "короткая запись остаётся целиком")
    eq(round(float(np.max(np.abs(kept))), 2), 0.1,
       "тихий сигнал не подтягивается вверх: иначе он станет громким")


def test_cue_files():
    """Свои сигналы: список для панели, поиск по имени, проверка на опечатку."""
    import tempfile
    import assistant

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("alice.ogg", "alice.wav", "beep.flac", "заметка.txt"):
            with open(os.path.join(tmp, name), "wb") as handle:
                handle.write(b"x")
        found = [os.path.basename(path) for path in assistant.cue_files(tmp)]
        eq(found, ["alice.ogg", "beep.flac"],
           "один сигнал — один пункт списка (ogg вместо wav), тексты не в счёт")
        true(assistant.cue_is_file("alice.ogg"),
             "файл отличается от встроенного варианта")
        true(not assistant.cue_is_file("bell"),
             "встроенный вариант файлом не считается")
        true(assistant.cue_source(os.path.join(tmp, "beep.flac")) is not None,
             "путь к своему файлу принимается")
        true(assistant.cue_ok("bell"), "встроенный вариант годится")
        true(not assistant.cue_ok(os.path.join(tmp, "нет-такого.ogg")),
             "несуществующий файл не годится — цикл скажет об этом при старте")

        # Файл должен быть ЗВУКОМ: у контейнера с видео расширение .ogg, и он попал
        # бы в список сигналов, а играть из него нечего (так и было: close.ogg с
        # видео Theora внутри).
        import types
        fake = types.ModuleType("soundfile")
        def _info(path, duration=1.5):
            if os.path.basename(path) == "video.ogg":
                raise RuntimeError("File contains data in an unimplemented format")
            return types.SimpleNamespace(duration=duration)
        fake.info = _info
        with open(os.path.join(tmp, "video.ogg"), "wb") as handle:
            handle.write(b"OggS theora")
        real = sys.modules.get("soundfile")
        sys.modules["soundfile"] = fake
        try:
            true(assistant.cue_ok(os.path.join(tmp, "beep.flac")),
                 "существующий звуковой файл годится")
            true(not assistant.cue_ok(os.path.join(tmp, "video.ogg")),
                 "контейнер с видео не годится: панель скажет об этом, а не промолчит")
        finally:
            if real is None:
                sys.modules.pop("soundfile", None)
            else:
                sys.modules["soundfile"] = real


def test_cue_file_seconds():
    """Длину файла берём из заголовка: длинную запись играем обрезанной."""
    import tempfile
    import types
    import assistant

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "долгая.ogg")
        with open(path, "wb") as handle:
            handle.write(b"x")
        fake = types.ModuleType("soundfile")
        fake.info = lambda p: types.SimpleNamespace(duration=2.5)
        real = sys.modules.get("soundfile")
        sys.modules["soundfile"] = fake
        try:
            eq(assistant.tone_seconds(path, seconds=0.7), 0.7,
               "длина сигнала-файла ограничена обрезкой")
            eq(assistant.tone_seconds(path, seconds=5.0), 2.5,
               "если запись короче отведённого — играем её целиком")
        finally:
            if real is None:
                sys.modules.pop("soundfile", None)
            else:
                sys.modules["soundfile"] = real


def test_voiced_constants():
    """Имена, к которым обращается цикл, должны существовать у своих модулей.

    Живая проверка после опечатки: voiced считал длину сигнала через
    kws.SAMPLE_RATE, а эти константы объявлены в самом voiced — служба падала на
    AttributeError сразу после первого срабатывания имени.
    """
    import kws
    import voiced

    import inspect

    eq(voiced.FRAME_BYTES, voiced.FRAME * 2, "кадр 16-битный: байтов вдвое больше")
    eq(round(voiced.FRAME / voiced.SAMPLE_RATE * 1000), 32,
       "кадр 32 мс — по нему считаются и сигнал, и запись")
    default = inspect.signature(
        kws.KwsSpotter.__init__).parameters["sample_rate"].default
    eq(default, voiced.SAMPLE_RATE,
       "KWS слушает с той же частотой, что и микрофон: иначе Vosk читает мусор")


def test_wake_tones():
    """Сигналы: короткие, без щелчков по краям, без клиппинга и различимые."""
    import assistant

    shapes = {}
    for kind in assistant.WAKE_TONES:
        samples = assistant.wake_tone(kind)
        true(len(samples) > 0, f"«{kind}»: сигнал не пустой")
        seconds = assistant.tone_seconds(kind)
        true(0.05 < seconds <= 0.5,
             f"«{kind}»: {seconds:.2f} с — короткая отбивка, а не мелодия")
        peak = max(abs(float(x)) for x in samples)
        true(peak <= 0.5, f"«{kind}»: пик {peak:.2f} — без клиппинга в колонке")
        # Резкий старт и обрез ноты слышны как щелчок: проверяем края.
        true(abs(float(samples[0])) < 0.01 and abs(float(samples[-1])) < 0.01,
             f"«{kind}»: края на нуле — иначе колонка щёлкает")
        shapes[kind] = tuple(round(float(x), 3) for x in samples[:400])

    eq(len(set(shapes.values())), len(assistant.WAKE_TONES),
       "варианты действительно разные, а не один и тот же тон")
    true(assistant.DEFAULT_WAKE_TONE in assistant.WAKE_TONES,
         "сигнал по умолчанию есть среди вариантов")
    eq(sorted(assistant.WAKE_TONE_LABELS), sorted(assistant.WAKE_TONES),
       "у каждого варианта есть подпись для списка в панели")
    true(all(assistant.WAKE_TONE_LABELS.values()),
         "подписи не пустые — иначе в списке будут пустые строки")

    # Колокольчик собран из частичных тонов со своим затуханием у каждого, и они
    # НЕ по гармоникам, как у синусоиды: иначе звук «пластмассовый».
    true(len(assistant.BELL_PARTIALS) >= 4, "у колокольчика несколько частичных тонов")
    ratios = [r for r, _, _ in assistant.BELL_PARTIALS]
    true(any(abs(r - round(r)) > 0.1 for r in ratios),
         "частичные тоны не по гармоникам — иначе это синтезатор, а не колокол")
    decays = [d for _, _, d in assistant.BELL_PARTIALS]
    eq(decays, sorted(decays, reverse=True),
       "высокие частичные затухают быстрее низких — так звучит удар")

    raised = False
    try:
        assistant.wake_tone("барабан")
    except ValueError:
        raised = True
    true(raised, "неизвестный сигнал — ошибка, а не тихая подмена")


def test_beep_plays_once():
    """Сигнал — это «написать тон и проиграть один раз»: ни повторов, ни цикла."""
    import assistant
    import tts_core

    written, played = [], []
    real_write, real_play = tts_core.write_audio, tts_core.play
    # Подменяем и запись, и проигрыш: звуковая библиотека в тестовой среде не
    # нужна (в CI её не ставим), а обвязку сигнала проверить надо.
    tts_core.write_audio = lambda samples, path, **kw: written.append((samples, path))
    tts_core.play = lambda path, **kw: (played.append((path, kw)), 0)[1]
    try:
        eq(assistant.beep(device="plughw:7,0"), 0, "код проигрывателя отдаётся дальше")
    finally:
        tts_core.write_audio, tts_core.play = real_write, real_play

    eq(len(written), 1, "тон записывается один раз")
    samples, path = written[0]
    true(path.endswith("wake.wav"), "сигнал пишется в свой файл")
    true(len(samples) > 0 and max(abs(x) for x in samples) > 0,
         "тон не пустой — иначе сигнала не слышно")
    eq(len(played), 1, "ровно одно обращение к проигрывателю, без повторов")
    eq(played[0][0], path, "играется именно записанный тон")
    eq(played[0][1].get("device"), "plughw:7,0", "устройство берётся из аргумента")


def test_kws_grammar():
    """Грамматика KWS: список наших форм + [unk] для всего остального."""
    import json

    import kws

    grammar = json.loads(kws.build_grammar(["астра", "астры"]))
    eq(grammar, ["астра", "астры", "[unk]"],
       "грамматика = формы имени + [unk] последним")
    true("[unk]" in kws.build_grammar(kws.DEFAULT_PHRASES),
         "по умолчанию [unk] тоже включён: посторонняя речь не считается именем")
    try:
        kws.build_grammar(["", "   "])
        true(False, "пустой список фраз должен отвергаться")
    except ValueError:
        true(True, "пустой список фраз отвергается с ошибкой")
    eq(kws.allowed_words(["астра", "эй астра"]), {"астра", "эй"},
       "слова грамматики: по ним проверяем срабатывание")


def test_kws_spotter():
    """Сторож имени: срабатывание только по ИТОГОВОМУ тексту, без модели и звука."""
    import json

    import kws

    class FakeRec:
        """Заглушка: итоговый текст появляется на заданном кадре (at)."""

        def __init__(self, text, at=3):
            self.text, self.at, self.fed = text, at, 0

        def AcceptWaveform(self, pcm):
            self.fed += 1
            return self.fed == self.at

        def PartialResult(self):
            # Частичные результаты нарочно «шумные»: сторож обязан их игнорировать.
            return json.dumps({"partial": "астра"})

        def FinalResult(self):
            return json.dumps({"text": self.text})

    made = []

    def factory(model, rate, grammar):
        made.append((rate, grammar))
        return FakeRec("астра")

    spot = kws.KwsSpotter(phrases=("астра", "астры"), factory=factory)
    spot.load()
    # Ложное срабатывание по partial — та самая ошибка живого прогона
    # (команда дала «имя»), поэтому три кадра до итога молчим.
    true(not spot.feed(b"\x00\x00" * 512), "partial не считается срабатыванием")
    true(not spot.feed(b"\x00\x00" * 512), "partial не считается срабатыванием")
    true(spot.feed(b"\x00\x00" * 512), "итоговый «астра» — срабатывание")
    eq(spot.hits, 1, "счётчик срабатываний ведётся")
    eq(len(made), 1, "распознаватель создаётся один раз")
    eq(json.loads(made[0][1])[-1], "[unk]", "грамматика передана в распознаватель")

    # Посторонняя фраза: итог от грамматики приходит как [unk].
    made.clear()
    spot2 = kws.KwsSpotter(phrases=("астра",),
                           factory=lambda m, r, g: FakeRec("[unk]"))
    for _ in range(4):
        true(not spot2.feed(b"\x00\x00" * 512),
             "чужая фраза (итог [unk]) имя не поднимает")

    # Имя с продолжением фразы в одном итоге («астра включи свет») — срабатываем.
    spot3 = kws.KwsSpotter(phrases=("астра",),
                           factory=lambda m, r, g: FakeRec("астра [unk]"))
    for _ in range(3):
        spot3.feed(b"\x00\x00" * 512)
    eq(spot3.hits, 1, "имя внутри итоговой фразы тоже считается срабатыванием")

    # Имя из пре-ролла попадает в текст команды — снимаем его.
    eq(kws.strip_name("Астра. Свет в коридоре."), "Свет в коридоре.",
       "имя снимается с начала команды")
    eq(kws.strip_name("астра включи свет"), "включи свет",
       "имя без пунктуации тоже снимается")
    eq(kws.strip_name("включи свет"), "включи свет",
       "без имени текст не меняется")
    eq(kws.strip_name("Астра"), "", "одно имя даёт пустую команду")

    spot.reset()
    spot.feed(b"\x00\x00" * 512)
    eq(len(made) + 1, 2, "после reset распознаватель создаётся заново")


def test_command_recorder():
    """Детектор конца команды: старт по трём громким кадрам, конец по тишине."""
    import voiced

    frame = b"\x00\x00" * voiced.FRAME
    floor = 100.0

    # Пре-ролл: сегмент обязан начинаться с кадров, снятых ДО сигнала —
    # ASR нужен «прогрев», иначе теряется первое слово команды.
    pre = [b"\x01\x00" * voiced.FRAME, b"\x02\x00" * voiced.FRAME]
    rec = voiced.CommandRecorder(floor, end_silence=0.16, wait_speech=1.0,
                                 pre_roll=pre)
    for _ in range(3):
        rec.feed(frame, floor * 3)
    true(rec.pcm[:len(pre[0]) * 2] == b"".join(pre),
         "сегмент начинается с пре-ролла (прогрев ASR)")

    rec = voiced.CommandRecorder(floor, end_silence=0.16, wait_speech=0.16)
    true(rec.feed(frame, floor) is None, "тишина: ждём начало команды")
    got = None
    for _ in range(20):
        got = rec.feed(frame, floor) or got
    true(got is not None and got["status"] == "silence",
         "если команда не началась — статус silence, цикл вернётся к сторожу")
    if got:
        true("max_rms" in got and got["max_rms"] > 0,
             "в отчёте есть максимум громкости: по нему видно запас до порога")

    # Одиночный громкий кадр старт НЕ открывает: ровно так запись открывалась
    # на хвосте сигнала после имени, и команда терялась.
    rec = voiced.CommandRecorder(floor, end_silence=0.16, wait_speech=1.0)
    rec.feed(frame, floor * 3)
    true(not rec.started, "один громкий кадр (хвост сигнала) запись не открывает")

    rec = voiced.CommandRecorder(floor, end_silence=0.16, wait_speech=1.0)
    for _ in range(3):
        rec.feed(frame, floor * 3)
    true(rec.started, "три громких кадра подряд открывают запись")
    got = None
    for _ in range(10):
        got = rec.feed(frame, floor) or got
    true(got is not None and got["status"] == "ok", "пауза закрывает команду")
    if got:
        true(got["speech_s"] > 0.0, "длина речи посчитана")
        true(abs(got["dur_s"] - len(got["pcm"]) / 2 / 16000) < 1e-6,
             "длина сегмента = длине записанного аудио")

    rec = voiced.CommandRecorder(floor, end_silence=5.0, max_speech=0.32,
                                 wait_speech=1.0)
    for _ in range(3):
        rec.feed(frame, floor * 3)
    got = None
    for _ in range(30):
        got = rec.feed(frame, floor * 3) or got
    true(got is not None and got["status"] == "long",
         "длинная речь обрезается по --max-command")

    rec = voiced.CommandRecorder(floor, end_silence=0.32, wait_speech=1.0)
    for _ in range(3):
        rec.feed(frame, floor * 3)
    got = rec.feed(frame, floor * 1.5)
    true(got is None, "громкость между порогами не закрывает команду (гистерезис)")

    # Шумная комната: пол высокий, но команда закрывается по падению
    # относительно ПИКА речи — иначе запись шла до --max-command (было на живом
    # прогоне: сегмент 15 с, «речь 15 с»).
    loud_floor = 3000.0
    rec = voiced.CommandRecorder(loud_floor, end_silence=0.16, wait_speech=1.0)
    for _ in range(3):
        rec.feed(frame, loud_floor * 3)
    got = None
    for _ in range(12):
        got = rec.feed(frame, loud_floor * 0.5) or got
    true(got is not None and got["status"] == "ok",
         "в шумной комнате команда закрывается по падению от пика речи")


def test_panel_parse():
    """Панель: разбор systemctl и журнала — чистые функции, без сервера."""
    import panel

    props = panel.parse_show("ActiveState=active\nSubState=running\n"
                             "MainPID=143486\nNRestarts=2\n")
    eq(props["ActiveState"], "active", "systemctl show разобран в словарь")
    eq(props["MainPID"], "143486", "PID берётся из вывода systemctl")
    eq(panel.rss_mb("0"), None, "нет процесса — нет памяти (без исключения)")

    noisy = ("Oct 11 14:08:51 ubuntu python[143486]: [kws] услышал имя (318 кадров "
             "прослушано, срабатываний 1, громкость имени -26 дБ)\n"
             "Oct 11 14:08:51 ubuntu python[143486]: 2026-10-11 14:08:51 "
             "[W:onnxruntime:Default, device_discovery.cc:164] GPU device "
             "discovery failed\n"
             "Oct 11 14:08:52 ubuntu systemd[812]: Started BPI-W3 voice assistant.\n"
             "Oct 11 14:08:53 ubuntu python[143486]: LOG (VoskAPI:Model():model.cc:122)"
             " Reading\n")
    lines = panel.clean_lines(noisy)
    eq(len(lines), 1, "служебный шум onnxruntime/Vosk/systemd выброшен")
    true(lines[0].startswith("[kws]"), "строка события осталась, префикс снят")

    journal = [
        "[kws] услышал имя (318 кадров прослушано, срабатываний 1, "
        "громкость имени -26 дБ)",
        "[seg] 1: 2.27с, речь 0.35с, пик -20 дБ (ok)",
        "1: Отсвет в коридоре.  [ASR 0.22 с]",
        "[reply] Принято: Отсвет в коридоре.",
        "[kws] команды не последовало за 5 с (шум 394, громче всего 765, "
        "порог 709)",
        "[seg] 2: 1.31с, речь 0.00с, пик -28 дБ (ok)",
        "[asr] 2: не расслышал (0.25 с, пик -28 дБ) — повторите",
    ]
    ev = panel.parse_events(journal)
    eq(ev["kws"]["hits"], 1, "срабатывания имени посчитаны")
    eq(ev["kws"]["frames"], 318, "кадры прослушивания учтены")
    eq(ev["kws"]["name_db"], -26, "громкость имени прочитана")
    eq(ev["kws"]["waited"], 1, "пропуск команды учтён")
    eq(ev["kws"]["last_margin"]["threshold"], 709, "запас до порога показан")
    eq(len(ev["segments"]), 2, "обе фразы попали в список")
    first = ev["segments"][0]
    eq(first["text"], "Отсвет в коридоре.", "текст привязан к своему сегменту")
    eq(first["levels"]["peak"], -20, "уровень пика прочитан из [seg]")
    true(ev["segments"][1].get("failed"), "неудача отмечена как «не расслышал»")
    eq(ev["replies"][-1], "Принято: Отсвет в коридоре.", "ответ попал в список")

    units = [{"name": "asrd", "active": "active", "exec": "--port 6006"},
             {"name": "ttsd", "active": "active", "exec": ""},
             {"name": "voiced", "active": "failed",
              "exec": "--device plughw:5,0 --kws-phrases \"астра,астры\""}]
    steps = panel.pipeline(units, {"hits": 3})
    eq(len(steps), 7, "схема состоит из семи ступеней")
    eq(steps[0]["detail"], "plughw:5,0", "устройство захвата взято из юнита")
    true("астра" in steps[1]["detail"], "формы имени видны в схеме")
    eq(steps[0]["state"], "bad", "упавшая служба красит ступень схемы")

    # ExecStart в юнитах разбит переносами: читать только первую строку —
    # значит потерять все настройки (так схема показывала умолчания).
    unit = ("[Service]\n"
            "ExecStart=%h/venv/bin/python %h/repo/scripts/voiced.py \\\n"
            "  --device plughw:5,0 \\\n"
            '  --kws-phrases "астра,астры,эй астра" \\\n'
            "  --spool-dir %h/.assistant/segments\n"
            "Restart=on-failure\n")
    joined = panel.exec_from_cat(unit)
    true("--device plughw:5,0" in joined, "перенос строки склеен: устройство видно")
    true("эй астра" in joined, "формы имени из ExecStart целиком")
    eq(panel.exec_from_cat("[Service]\nRestart=on-failure\n"), "",
       "без ExecStart пусто, без исключений")

    # Сделать сигнал рабочим: значение меняется на месте, ключа нет — добавляем.
    unit = ("[Service]\n"
            "ExecStart=%h/python %h/voiced.py \\\n"
            "  --device plughw:5,0 \\\n"
            "  --wake-tone chime \\\n"
            "  --spool-dir %h/.assistant/segments\n")
    patched = panel.set_option_in_text(unit, "--wake-tone", "doorbell")
    true("--wake-tone doorbell" in patched, "сигнал в юните заменён на выбранный")
    eq(patched.count("--wake-tone"), 1, "второй такой ключ не появился")
    eq(panel.exec_option(patched, "--wake-tone"), "doorbell",
       "и читается обратно уже новое значение")
    true("--device plughw:5,0" in patched, "остальные ключи не тронуты")

    # Второй сигнал (после тишины): файл и «выключено» — то же значение «-».
    close_unit = panel.set_option_in_text(unit, "--wake-cue-close", "close.ogg")
    eq(panel.exec_option(close_unit, "--wake-cue-close"), "close.ogg",
       "закрывающий сигнал прописан и читается обратно")
    eq(panel.exec_option(panel.set_option_in_text(close_unit, "--wake-cue-close",
                                                  "-"), "--wake-cue-close"), "-",
       "«тишина» — это значение «-», а не пустая строка (иначе argparse упал бы)")
    import assistant as assistant_mod
    true(not assistant_mod.cue_ok("-"),
         "«-» — не файл и не отбивка: это выключенный сигнал")

    without = unit.replace("  --wake-tone chime \\\n", "")
    added = panel.set_option_in_text(without, "--wake-tone", "bell")
    true("--wake-tone bell" in added, "нет ключа — он добавляется в ExecStart")
    eq(panel.exec_option(added, "--wake-tone"), "bell", "и сразу читается обратно")
    true("--spool-dir" in added, "продолжения ExecStart не разорваны")

    # Значения ключей: одно слово (устройство) и список в кавычках (имена).
    eq(panel.exec_option('--device plughw:5,0 --kws-phrases "астра,астры"',
                         "--device"), "plughw:5,0",
       "значение из одного слова берётся до пробела, а не до кавычки")
    eq(panel.exec_option('--device plughw:5,0 --kws-phrases "астра,астры"',
                         "--kws-phrases"), "астра,астры",
       "список в кавычках берётся целиком, вместе с пробелами")
    eq(panel.exec_option("--device plughw:5,0", "--wake-tone", "chime"), "chime",
       "нет ключа — значение по умолчанию")

    # Формы имени берутся из ExecStart: по ним схема и кнопка «сказать имя».
    eq(panel.wake_phrases('--device plughw:5,0 --kws-phrases "астра,астры,эй астра"'),
       ["астра", "астры", "эй астра"], "формы имени читаются из ExecStart юнита")
    eq(panel.wake_phrases("--device plughw:5,0"), ["астра"],
       "без флага остаётся значение по умолчанию")

    # Документы: имя проверяется, иначе панель отдавала бы что угодно с диска.
    true(panel.doc_path("13-kws.md") is not None, "документ по имени находится")
    eq(panel.doc_path("../etc/passwd"), None, "выход из каталога отсекается")
    eq(panel.doc_path("/etc/passwd.md"), None, "абсолютный путь отсекается")
    eq(panel.doc_path("panel.py"), None, "чужое расширение отсекается")
    eq(panel.doc_path(""), None, "пустое имя отсекается")

    # Действия: без файла с ключом их нет вообще.
    eq(panel.check_token("любой", ""), "disabled", "без ключа на плате — выключено")
    eq(panel.check_token("неверный", "секрет"), "denied", "чужой ключ отвергается")
    eq(panel.check_token("секрет", "секрет"), "ok", "свой ключ принимается")
    eq(panel.check_token(None, "секрет"), "denied", "без заголовка — отказ")

    # Неизвестное действие и чужая служба не выполняются.
    true(not panel.do_action({"action": "rm-rf"}, "x")["ok"],
         "неизвестное действие отвергается")
    true(not panel.do_action({"action": "restart", "unit": "ssh"}, "x")["ok"],
         "перезапуск чужой службы отвергается")

    # История уровней: для графика, без повторов при каждом обновлении.
    panel.HISTORY.clear()
    panel.add_history([{"index": 1, "levels": {"peak": -20, "speech": 0.4},
                        "text": "ок"},
                       {"index": 2, "levels": {"peak": -40, "speech": 0.0}}])
    panel.add_history([{"index": 2, "levels": {"peak": -40, "speech": 0.0}}])
    eq(len(panel.HISTORY), 2, "повторное обновление не дублирует точки графика")
    eq(panel.HISTORY[0]["peak"], -20, "уровень попал в историю")
    panel.HISTORY.clear()


def main() -> int:
    for fn in (test_time, test_numbers_units, test_temperature_percent, test_latin,
               test_real_world, test_check_text, test_ssml,
               test_sentence_split, test_pitch_contour_labels,
               test_humanize_question, test_question_split,
               test_accusative_detection, test_resolve_threads,
               test_worker_protocol, test_worker_framing,
               test_assistant_reply, test_mono_from_bytes,
               test_trim_cue, test_cue_files,
               test_cue_file_seconds,
               test_voiced_constants, test_wake_tones, test_beep_plays_once,
               test_kws_grammar, test_kws_spotter,
               test_command_recorder,
               test_panel_parse):
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
