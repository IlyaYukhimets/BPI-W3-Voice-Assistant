#!/usr/bin/env python3
"""
tts_core.py — общее ядро синтеза русской речи (Silero v5_5_ru).

Зачем отдельный модуль: одну и ту же логику используют
  * scripts/tts_say.py  — продакшен-CLI ассистента (озвучить реплику);
  * tools/voice_lab.py  — стенд подбора голоса и интонации.
Дублировать загрузку модели, нормализацию текста и сборку SSML в двух местах
нельзя: разъедется, и стенд начнёт мерить не то, что играет ассистент.

Три вещи, о которые спотыкаешься при работе с Silero (все проверены на BPI-W3):
  1) модель МОЛЧА удаляет любые символы вне своего алфавита — цифры, латиницу,
     эмодзи. Поэтому нужна normalize_text() ДО синтеза и check_text() ПОСЛЕ,
     чтобы увидеть, что пропало;
  2) `intensity` в apply_tts — мёртвый параметр (1/3/6 дают 0% разницы);
  3) темп в процентах квантуется: rate="105%" даёт -0.6%, а "108%" уже -4.4%.
     Ставить проценты из головы бессмысленно — см. tools/voice_lab.py.
"""
from __future__ import annotations

import os
import re

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------

SR = 48000
"""Частота дискретизации. 48000 — нативная для модели; ES8316 держит и 44100."""

THREADS_DEFAULT = 4
"""Число потоков torch по умолчанию — ЗАМЕРЕННЫЙ оптимум для BPI-W3, не «все ядра».

Замерено на плате (baya/warm, медиана из 5 прогонов после прогрева):

    фраза «Включить ли свет?» (1.2 с аудио)    3 фразы (~6 с аудио)
    1 поток   0.215 с   RTF 0.177              0.655 с   RTF 0.108
    2 потока  0.173 с   RTF 0.142  -20%        0.645 с   RTF 0.106   -1%
    4 потока  0.159 с   RTF 0.132  -26%  <-    0.608 с   RTF 0.100   -7%  <-
    8 потоков 0.212 с   RTF 0.175   -1%        0.610 с   RTF 0.100   -7%

Выводы, которые отсюда следуют:
  * больше потоков НЕ значит быстрее: 8 хуже 4 (на короткой фразе — до уровня
    одного потока). Короткий синтез (~0.2 с) слишком мал, чтобы окупить
    синхронизацию потоков;
  * 4 потока выигрывают в обоих случаях, поэтому по умолчанию ставим 4;
  * потолок выигрыша невелик (26% на коротком, 7% на длинном) — узкое место
    не в потоках, а в самой модели.

Переопределяется аргументом threads или переменной TTS_THREADS.
"""

THREADS_ENV = "TTS_THREADS"
"""Имя переменной окружения для переопределения числа потоков."""


def resolve_threads(threads: int | None = None) -> int:
    """Сколько потоков отдать torch. Приоритет: аргумент -> TTS_THREADS -> THREADS_DEFAULT.

    Отдельная функция появилась из-за ПОРЯДКА применения: пакет модели Silero
    при загрузке сам вызывает set_num_threads(1), поэтому своё значение надо
    ставить ПОСЛЕ load_pickle, иначе оно молча затирается. Проверено на плате:
        set_num_threads(4) -> 4
        PackageImporter(...) -> 4
        load_pickle(...) -> 1   <-- вот здесь значение теряется
    """
    if threads is not None:
        if threads < 1:
            raise ValueError(f"потоков должно быть >= 1, получено {threads}")
        return threads

    raw = (os.environ.get(THREADS_ENV) or "").strip()
    if not raw:
        return THREADS_DEFAULT

    try:
        n = int(raw)
    except ValueError:
        raise ValueError(
            f"{THREADS_ENV} должно быть целым числом, получено {raw!r}"
        ) from None
    if n < 1:
        raise ValueError(f"{THREADS_ENV} должно быть >= 1, получено {n}")
    return n


def torch_threads() -> int | None:
    """Фактическое число потоков torch (None, если torch ещё не импортирован).

    Нужна, чтобы видеть РЕАЛЬНОЕ значение, а не предполагаемое: заявленное
    число и рантайм расходятся, и это уже приводило к неверным выводам.
    """
    try:
        import torch
    except ImportError:
        return None
    return torch.get_num_threads()


VOICES = {
    "aidar": "мужской, ровный, нейтральный",
    "baya": "женский, тёплый, ниже тесситура — ВЫБРАН основным",
    "kseniya": "женский, середина, чёткая дикция",
    "eugene": "мужской, ниже тоном",
    "xenia": "женский, выше, легче",
}

FEMALE_VOICES = ("baya", "kseniya", "xenia")

DEFAULT_VOICE = "baya"
DEFAULT_DEVICE = "plughw:3,0"
"""ES8316 (card 3). Принимает ТОЛЬКО стерео — моно молча не заиграет."""

# Путь к модели. Переменная окружения TTS_MODEL нужна для запуска в
# контейнере: модель лежит в примонтированном томе, а не в домашнем каталоге.
# Приоритет: явный аргумент --model > TTS_MODEL > путь по умолчанию.
DEFAULT_MODEL = os.environ.get("TTS_MODEL") or "~/models/tts/silero_v5_5_ru_ok.pt"

# Алфавит модели: всё, что вне него, Silero вырезает без предупреждения.
# Полный набор берётся из самой модели (get_alphabet), это лишь запасной вариант.
FALLBACK_ALPHABET = (
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "_~|!+,-.:;?–… "
)

# Символ ручного ударения: ставится ПЕРЕД гласной — "хор+ошо", "замк+и".
STRESS_MARK = "+"


# --------------------------------------------------------------------------
# Загрузка модели
# --------------------------------------------------------------------------

def build_model(path: str | None = None, threads: int | None = None):
    """Загружает модель Silero v5_5_ru. Требует torch — импорт внутри функции,
    чтобы модуль можно было импортировать без torch (для справок о профилях).

    Порядок важен: пакет модели при загрузке сам выставляет set_num_threads(1),
    поэтому потоки задаём ПОСЛЕ load_pickle. Поставленное до — затирается.
    """
    import torch

    p = os.path.expanduser(path or DEFAULT_MODEL)
    if not os.path.exists(p):
        raise FileNotFoundError(f"нет модели: {p}")
    m = torch.package.PackageImporter(p).load_pickle("tts_models", "model")
    torch.set_num_threads(resolve_threads(threads))
    m.to(torch.device("cpu"))
    return m


def get_alphabet(model) -> set:
    """Алфавит модели. Пробуем известные атрибуты, иначе запасной набор.
    Нужен, чтобы предупредить о символах, которые будут молча вырезаны."""
    for attr in ("symbols", "alphabet", "chars", "vocab"):
        v = getattr(model, attr, None)
        if isinstance(v, (list, tuple, str)) and 20 < len(v) < 5000:
            return set(v)
    return set(FALLBACK_ALPHABET)


# --------------------------------------------------------------------------
# Нормализация текста: цифры, латиница, сокращения
# --------------------------------------------------------------------------
# ЗАЧЕМ: Silero читает только кириллицу. "через 10 минут" превращается в
# "через минуту" (цифра вырезана, смысл потерян!), "YouTube и Telegram" —
# в "Открой и", "19:45" — в "Сейчас". Обход через SSML/lang НЕ помогает,
# единственное решение — переписать текст словами ДО синтеза.

_UNITS = {
    # ключ: (им.ед, вин.ед, 2-4, 5+, род)   род: m | f | n
    "минута": ("минута", "минуту", "минуты", "минут", "f"),
    "секунда": ("секунда", "секунду", "секунды", "секунд", "f"),
    "час": ("час", "час", "часа", "часов", "m"),
    "день": ("день", "день", "дня", "дней", "m"),
    "неделя": ("неделя", "неделю", "недели", "недель", "f"),
    "градус": ("градус", "градус", "градуса", "градусов", "m"),
    "процент": ("процент", "процент", "процента", "процентов", "m"),
    "рубль": ("рубль", "рубль", "рубля", "рублей", "m"),
    "штука": ("штука", "штуку", "штуки", "штук", "f"),
    "комната": ("комната", "комнату", "комнаты", "комнат", "f"),
    "яблоко": ("яблоко", "яблоко", "яблока", "яблок", "n"),
}

# Стем -> ключ единицы. Матчим СТЕМ, а не точную форму: в речи единица
# встречается в любом падеже ("минуту", "минуты", "минутам"), и перечислять
# все формы в регулярке — прямой путь к пропускам.
_UNIT_STEMS = (
    ("минут", "минута"), ("мин", "минута"),
    ("секунд", "секунда"), ("сек", "секунда"),
    ("час", "час"),
    ("дн", "день"), ("день", "день"), ("дня", "день"), ("дней", "день"),
    ("недел", "неделя"),
    ("градус", "градус"),
    ("процент", "процент"),
    ("рубл", "рубль"),
    ("штук", "штука"),
    ("комнат", "комната"),
    ("яблок", "яблоко"),
)

# Предлоги, после которых идёт ВИНИТЕЛЬНЫЙ падеж ("через одну минуту"),
# а не именительный ("одна минута"). Только на них опираемся — полноценный
# морфоанализ здесь избыточен.
_ACC_PREPS = ("через", "за", "спустя", "на", "про", "в течение", "по прошествии")

LATIN = {
    "youtube": "ютуб", "telegram": "телеграм", "whatsapp": "вотсап",
    "wifi": "вай-фай", "wi-fi": "вай-фай", "wi fi": "вай-фай",
    "bluetooth": "блютус", "google": "гугл", "chrome": "хром",
    "spotify": "спотифай", "android": "андроид", "iphone": "айфон",
    "usb": "ю-эс-би", "hdmi": "эйч-ди-эм-ай", "ok": "окей", "okay": "окей",
    "home assistant": "хоум ассистент", "homeassistant": "хоум ассистент",
    "http": "эйч-ти-ти-пи", "ip": "ай-пи", "api": "эй-пи-ай",
    "cpu": "си-пи-ю", "gpu": "джи-пи-ю", "ssd": "эс-эс-ди",
    "led": "эл-и-ди", "tv": "телевизор", "pc": "компьютер",
}

_MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _plural_index(n: int) -> str:
    """Какая форма нужна: 'one' | 'few' | 'many'. Русское правило:
    1, 21, 31 -> one; 2-4, 22-24 -> few; 5-20, 11-14, 25-30 -> many."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return "many"
    n %= 10
    if n == 1:
        return "one"
    if 2 <= n <= 4:
        return "few"
    return "many"


_NUM_FIX = {
    # num2words отдаёт мужской род и именительный падеж; правим хвост
    # числительного под род и падеж: "один" -> "одну", "два" -> "две".
    ("f", True): {"один": "одну", "два": "две"},
    ("f", False): {"один": "одна", "два": "две"},
    ("n", True): {"один": "одно", "два": "два"},
    ("n", False): {"один": "одно", "два": "два"},
    ("m", True): {},   # "через один час" — совпадает с именительным
    ("m", False): {},
}


def num_word(n: int, gender: str = "m", accusative: bool = False) -> str:
    """Число словами с согласованием по роду и падежу.

    Зачем не просто num2words: библиотека всегда даёт мужской род и
    именительный падеж, поэтому "1 минута" превращается в "один минута"
    (ошибка, слышимая носителем). Здесь хвост числительного правится:
      "одна минута" / "через одну минуту" / "две минуты" / "одно яблоко".
    """
    from num2words import num2words

    w = num2words(int(n), lang="ru")
    head, _, last = w.rpartition(" ")
    fix = _NUM_FIX.get((gender, accusative), {})
    if last in fix:
        return f"{head} {fix[last]}".strip()
    return w


def unit_phrase(n: int, unit_key: str, accusative: bool = False) -> str:
    """'10 минут', '1 минуту', '2 часа' — числительное + существительное
    в согласованном числе, роде и падеже."""
    n = int(n)
    nom1, acc1, few, many, gender = _UNITS[unit_key]
    idx = _plural_index(n)
    if idx == "one":
        noun = acc1 if accusative else nom1
    elif idx == "few":
        noun = few
    else:
        noun = many
    return f"{num_word(n, gender, accusative)} {noun}"


def _looks_accusative(text: str, match_start: int) -> bool:
    """Есть ли перед числом предлог, требующий винительного падежа."""
    head = text[max(0, match_start - 18):match_start].lower()
    return any(head.rstrip().endswith(p) for p in _ACC_PREPS)


def normalize_text(text: str) -> str:
    """Переписать текст так, чтобы Silero его не потерял.

    Что делает:
      * время 19:45 -> "девятнадцать сорок пять", 19:05 -> "... ноль пять";
      * число + единица ("10 минут") -> "десять минут" с верным числом,
        родом и падежом ("через 1 минуту" -> "через одну минуту");
      * температура -3° / -3 °C -> "минус три градуса";
      * проценты 50% -> "пятьдесят процентов";
      * латиница по словарю: YouTube -> ютуб;
      * схлопывает лишние пробелы.

    Чего НЕ делает: не транскрибирует произвольную латиницу вне словаря
    (нельзя угадать чтение) и не трогает пунктуацию — её модель понимает.
    """
    from num2words import num2words

    if not text:
        return ""

    out = text

    # --- время HH:MM -------------------------------------------------------
    def _time(mt):
        h, mi = int(mt.group(1)), int(mt.group(2))
        if h > 23 or mi > 59:
            return mt.group(0)
        # минуты 01-09 читаются с "ноль": 19:05 -> "девятнадцать ноль пять"
        mm = f"ноль {num_word(mi)}" if 0 < mi < 10 else num_word(mi)
        return f"{num_word(h)} {mm}"

    out = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", _time, out)

    # --- температура: -3°, -3 °C, +5 C ------------------------------------
    def _temp(mt):
        sign = (mt.group(1) or "").strip()
        raw = mt.group(2).replace(",", ".")
        prefix = "минус " if sign in ("-", "−") else ("плюс " if sign == "+" else "")
        n = int(float(raw))
        return f"{prefix}{unit_phrase(n, 'градус')}"

    out = re.sub(r"([+\-−]?)\s?(\d+(?:[.,]\d+)?)\s?(?:°\s?[CF]?|градус\w*)", _temp, out)

    # --- проценты ----------------------------------------------------------
    out = re.sub(r"\b(\d+)\s?%", lambda mt: unit_phrase(int(mt.group(1)), "процент"), out)

    # --- число + единица измерения ----------------------------------------
    stem_alt = "|".join(s for s, _ in _UNIT_STEMS)
    stem_map = dict(_UNIT_STEMS)

    def _with_unit(mt):
        v = int(mt.group(1))
        stem = mt.group(2).lower()
        key = stem_map.get(stem)
        if not key:
            return num_word(v)
        return unit_phrase(v, key, accusative=_looks_accusative(out, mt.start()))

    # ГРАБЛЯ: раньше шаблон заканчивался на `\.?` и съедал точку в конце
    # предложения ("минут." -> "минут"). Из-за этого ломалась разбивка по
    # фразам, а от неё зависит интонация вопроса. Точку НЕ трогаем.
    out = re.sub(rf"\b(\d+)\s?({stem_alt})[а-яё]*\b", _with_unit, out)

    # --- оставшиеся числа (год, количество) -------------------------------
    out = re.sub(r"\b(\d{1,12})\b", lambda mt: num2words(int(mt.group(1)), lang="ru"), out)

    # --- латиница --------------------------------------------------------
    for lat, rus in sorted(LATIN.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(lat)}\b", rus, out, flags=re.IGNORECASE)
    # остаточные слова из латиницы: прочитать побуквенно нельзя, но и терять
    # нельзя — транслитерируем (см. check_text: он покажет, что было под угрозой).
    out = re.sub(r"\b[A-Za-z][A-Za-z0-9\-]{1,}\b",
                 lambda mt: _translit(mt.group(0)), out)

    # --- чистка ----------------------------------------------------------
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([,.;:?!])", r"\1", out)
    return out.strip()


_TR = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
       "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
       "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
       "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
       "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре",
       "5": "пять", "6": "шесть", "7": "семь", "8": "восемь", "9": "девять",
       "-": "", "_": " "}


def _translit(w: str) -> str:
    return "".join(_TR.get(c.lower(), "") for c in w)


def check_text(text: str, alphabet: set | None = None) -> list[tuple[str, int]]:
    """Возвращает символы, которые модель МОЛЧА вырежет: [(символ, сколько раз)].

    Смысл проверки: Silero не ругается на цифры и латиницу, а просто
    удаляет их. Без этой проверки ответ ассистента звучит правдоподобно,
    но с потерянным смыслом ("через 10 минут" -> "через минуту").
    """
    alpha = alphabet or set(FALLBACK_ALPHABET)
    bad: dict[str, int] = {}
    for ch in text:
        if ch not in alpha:
            bad[ch] = bad.get(ch, 0) + 1
    return sorted(bad.items(), key=lambda kv: -kv[1])


def accentuate(text: str, homographs: bool = True):
    """Расставить ударения и «ё» через silero-stress (если установлен).

    Возвращает (текст_с_ударениями, пометки) — пометки это список
    (слово_без, слово_с), чтобы было видно, что именно решено по контексту.
    Если библиотеки нет — возвращает текст как есть (не падаем: она опциональна).
    """
    try:
        import silero_stress
    except ImportError:
        return text, None

    acc = silero_stress.load_accentor()
    fn = acc.homosolver if homographs else acc.accentor
    out = fn(text)

    # что изменилось: пары по словам
    src = re.findall(r"[\w\-]+", text)
    dst = re.findall(r"[\w\-]+", out)
    pairs = [(s, d) for s, d in zip(src, dst) if s != d]
    return out, (pairs or None)


# --------------------------------------------------------------------------
# SSML: профили интонации
# --------------------------------------------------------------------------
# ПОДДЕРЖИВАЕТСЯ моделью: prosody(rate, pitch), break(time/strength), <s>, <p>.
# НЕ ПОДДЕРЖИВАЕТСЯ (ValueError: not enough values to unpack):
#   emphasis, say-as, sub, voice.
#
# Параметры функции apply_tts работают и по умолчанию True:
#   put_accent, put_yo, put_stress_homo, put_yo_homo, stress_single_vowel.
#
# Silero v5 предоставляет SSML для пауз и локальной настройки темпа/высоты
# тона, но не отдельный режим вопросительной интонации. Профили question,
# warm и lively ниже используют pitch как эвристику по знакам препинания;
# они не гарантируют, что реплика будет восприниматься как вопрос.

def _split_sentences(text: str) -> list[str]:
    """Фразы с сохранением конечного знака. Нужно, чтобы понять,
    где вопрос (для него своя интонация)."""
    parts = re.findall(r"[^.!?…]+[.!?…]*", text)
    return [p.strip() for p in parts if p.strip()]


def _q_tail(sentence: str, tail_words: int = 2) -> tuple[str, str]:
    """Делит вопрос на тело и хвост, оставляя тело хотя бы из одного слова."""
    words = sentence.split()
    if len(words) < 2:
        return sentence, ""
    count = min(tail_words, len(words) - 1)
    return " ".join(words[:-count]), " ".join(words[-count:])


# --------------------------------------------------------------------------
# Речевые обороты для вопросов («человечная» формулировка)
# --------------------------------------------------------------------------
# Silero не имеет режима вопросительной интонации, и на КОРОТКОЙ фразе
# восходящий контур не складывается: pitch-эвристика даёт скачущую высоту, а
# фраза всё равно слышится утверждением. Обходной путь — сделать вопрос
# вопросом ЛЕКСИЧЕСКИ: добавить частицу или хвостик, свойственные русской
# вопросительной речи. Замеры (голос baya, профиль warm, старт/хвост F0):
#
#   «Включить свет?»          старт 305 Гц, пик в НАЧАЛЕ (0.03) → утверждение
#   «Включить ли свет?»       старт 200 Гц, пик 0.65, хвост +36 %  → вопрос
#   «Может, включить свет?»   старт 201 Гц, пик 0.74, хвост +31 %  → вопрос
#   «Включить свет, да?»      старт 197 Гц, пик 0.97, хвост +10 %  → вопрос
#   «Готово?»                 старт 225 Гц, пик 0.60
#   «Готово, да?»             старт 197 Гц, пик 0.97 (пик в САМОМ конце)
#
# То есть оборот снижает стартовую высоту и даёт восходящей дуге место —
# ровно то, чего не хватало. Это работа с ТЕКСТОМ, а не с SSML.
QUESTION_TURNS = {
    "li":    "ВЫБРАН основным: частица «ли» после первого слова («Включить ли свет?»)",
    "verno": "для фраз-запросов подтверждения: хвостик «…, верно?»",
    "maybe": "зачин «Может, …?»: «Может, включить свет?»",
    "tag":   "хвостик «…, да?»: «Включить свет, да?»",
}

# Метки для озвучивания. ТОЛЬКО КИРИЛЛИЦА: латиница в SSML у этой модели
# не просто вырезается — на ней падает разбор (ValueError: Failed to parse
# SSML). Проверено: метка «Вариант li.» ломала синтез, а «как есть» — нет.
QUESTION_TURN_LABELS = {
    "li":    "с частицей ли",
    "maybe": "с зачином может",
    "tag":   "с хвостиком да",
    "verno": "с хвостиком верно",
}


def humanize_question(text: str, style: str = "li") -> str:
    """Переформулирует вопрос, добавляя оборот, характерный для вопроса.

    Утверждения не трогает: работает только с фразами, оканчивающимися на «?».
    Идемпотентна: если оборот уже есть, фраза возвращается как есть.

    ВНИМАНИЕ, это осознанное действие, а не «тихая» правка: оборот меняет
    оттенок смысла («Может, …?» — предположение, «…, верно?» — запрос
    подтверждения). Применять там, где ассистент спрашивает владельца.

    Ограничения:
      * «ли» требует минимум два слова («Готово ли?» звучит канцелярски) —
        для однословного вопроса вместо неё подставляется хвостик «, да?»;
      * зачин «Может,» понижает регистр первого слова (для имён собственных
        в начале вопроса используйте `tag`/`verno`).
    """
    if style not in QUESTION_TURNS:
        raise ValueError(f"неизвестный оборот: {style} (есть {list(QUESTION_TURNS)})")

    out = []
    for s in _split_sentences(text):
        if not s.endswith("?"):
            out.append(s)
            continue

        body = s[:-1].strip().rstrip("…").strip()      # без «?» и многоточия
        words = body.split()
        if not words:
            out.append(s)
            continue

        # идемпотентность: не добавляем оборот второй раз
        if re.search(r",\s*(да|верно|правда)\?$", s) or re.search(r"\bли\b", body):
            out.append(s)
            continue

        if style == "li":
            if len(words) >= 2:
                out.append(" ".join([words[0], "ли"] + words[1:]) + "?")
            else:
                out.append(body + ", да?")
        elif style == "maybe":
            first = words[0][0].lower() + words[0][1:]
            out.append("Может, " + " ".join([first] + words[1:]) + "?")
        elif style == "tag":
            out.append(body + ", да?")
        elif style == "verno":
            out.append(body + ", верно?")
    return " ".join(out)


PROFILES = {
    "neutral": "как есть, без настроек — точка отсчёта для сравнения",
    "warm": "ВЫБРАН: 1-я фраза ниже, дальше выше, пауза 350 мс, темп 108%",
    "lively": "бодро: темп 110%, тон выше, фразы раздельно",
    "alert": "тревога: первая фраза громче тоном, короткая пауза 250 мс",
    "calm": "спокойно: темп 90%, тон ниже",
    "question": "эвристика: низкий старт + высокий хвост; не режим вопроса",
    "emphasis": "акцент на первом слове за счёт темпа и паузы",
}

# Проверенные ступени темпа (факт по замерам, а не номинал параметра).
RATE_STEPS = ["100%", "104%", "105%", "108%", "110%", "115%", "120%", "slow", "fast"]


def build_ssml(text: str, profile: str = "warm", rate: str | None = None,
               pitch: str | None = None, split: bool = True) -> str:
    """Собирает SSML по профилю.

    rate/pitch — принудительное переопределение (если заданы, профиль их не
    трогает). split=False отключает разбивку по фразам — полезно для замера
    «чистого» эффекта одного тега.
    """
    if profile not in PROFILES:
        raise ValueError(f"неизвестный профиль: {profile} (есть {list(PROFILES)})")

    sentences = _split_sentences(text) if split else [text.strip()]
    if not sentences:
        return "<speak></speak>"

    def seg(body: str, rate_: str | None, pitch_: str | None) -> str:
        b = body
        a = []
        if rate_:
            a.append(f'rate="{rate_}"')
        if pitch_:
            a.append(f'pitch="{pitch_}"')
        if not a:
            return b
        return f"<prosody {' '.join(a)}>{b}</prosody>"

    p = profile

    if p == "neutral":
        body = " ".join(seg(s, rate, pitch) for s in sentences)
        return f"<speak>{body}</speak>"

    if p == "calm":
        body = " ".join(seg(s, rate or "90%", pitch or "low") for s in sentences)
        return f"<speak>{body}</speak>"

    if p == "lively":
        r = rate or "110%"
        parts = []
        for s in sentences:
            if s.endswith("?") and not pitch:
                h, t = _q_tail(s)
                # Однословный вопрос: _q_tail отдаёт ВСЮ фразу как «тело» и
                # пустой хвост. Делить нечего — озвучиваем фразу ОДИН раз.
                # (Раньше h добавлялся по `if h`, и ветка else добавляла ту же
                # фразу целиком — текст произносился дважды.)
                if h and t:
                    inner = seg(h, r, "low") + seg(t, r, "high")
                else:
                    inner = seg(s, r, pitch)
                parts.append(inner)
            else:
                parts.append(seg(s, r, pitch or "high"))
        return "<speak>" + "".join(parts) + "</speak>"

    if p == "alert":
        parts = []
        for i, s in enumerate(sentences):
            parts.append(seg(s, rate or "108%", pitch or ("high" if i == 0 else "low")))
            if i < len(sentences) - 1:
                parts.append('<break time="250ms"/>')
        return "<speak>" + "".join(parts) + "</speak>"

    if p == "question":
        r = rate or "108%"
        parts = []
        for i, s in enumerate(sentences):
            if s.endswith("?"):
                h, t = _q_tail(s)
                # Эвристика по пунктуации; модель не предоставляет режим ИК-3.
                if h and t:
                    parts.append(seg(h, r, "low") + seg(t, r, "high"))
                else:
                    parts.append(seg(s, r, pitch))
            else:
                parts.append(seg(s, r, pitch or "low"))
            # пауза ТОЛЬКО между фразами — иначе лишний <break> в конце
            # делает XML невалидным. Здесь же была ошибка: rstrip() принимает
            # НАБОР символов, а не подстроку, и портил закрывающий тег.
            if i < len(sentences) - 1:
                parts.append('<break time="350ms"/>')
        return "<speak>" + "".join(parts) + "</speak>"

    if p == "emphasis":
        parts = []
        for s in sentences:
            m = re.match(r"^(\S+)(.*)$", s)
            if m:
                parts.append(seg(m.group(1), rate or "90%", "low") +
                             '<break time="200ms"/>' +
                             seg(m.group(2), rate or "112%", "high"))
            else:
                parts.append(seg(s, rate, pitch))
        return "<speak>" + "".join(parts) + "</speak>"

    # --- warm: победивший рецепт -----------------------------------------
    # 1-я фраза ниже тоном, следующие выше, пауза 350 мс, темп 108%.
    # Эвристика по пунктуации; SSML не задаёт семантический тип фразы.
    r = rate or "108%"
    parts = []
    for i, s in enumerate(sentences):
        if s.endswith("?") and not pitch:
            h, t = _q_tail(s)
            if h and t:
                parts.append(seg(h, r, "low") + seg(t, r, "high"))
            else:
                parts.append(seg(s, r, pitch))
        else:
            parts.append(seg(s, r, pitch or ("low" if i == 0 else "high")))
        if i < len(sentences) - 1:
            parts.append('<break time="350ms"/>')
    return "<speak>" + "".join(parts) + "</speak>"


# --------------------------------------------------------------------------
# Синтез и запись
# --------------------------------------------------------------------------

def synth(model, ssml: str, voice: str = DEFAULT_VOICE):
    """SSML -> моно numpy float32 [-1,1]."""
    import numpy as np

    a = model.apply_tts(ssml_text=ssml, speaker=voice, sample_rate=SR)
    if hasattr(a, "detach"):
        a = a.detach().cpu().numpy()
    return np.asarray(a, dtype=np.float32)


def to_stereo(mono):
    """ES8316 молча не играет моно — дублируем канал."""
    import numpy as np

    return np.column_stack([mono, mono]).astype(np.float32)


def rms_normalize(mono, target: float = 0.12):
    """Выравнивание громкости. Без него при сравнении голосов побеждает
    тот, кто просто громче, а не тот, кто лучше звучит."""
    import numpy as np

    r = float(np.sqrt(np.mean(mono ** 2))) or 1.0
    y = mono * (target / r)
    pk = float(np.max(np.abs(y))) if len(y) else 0.0
    if pk > 0.98:
        y = y * (0.98 / pk)
    return y.astype(np.float32)


def write_audio(mono, path: str, ogg: bool = True) -> list[str]:
    """Пишет стерео WAV (PCM_16) и, если просили, рядом OGG/Vorbis для
    прослушивания на телефоне. Возвращает список созданных файлов."""
    import soundfile as sf

    made = []
    base, ext = os.path.splitext(path)
    wav = path if ext.lower() == ".wav" else base + ".wav"
    sf.write(wav, to_stereo(mono), SR, subtype="PCM_16")
    made.append(wav)
    if ogg:
        o = base + ".ogg"
        sf.write(o, to_stereo(mono), SR, format="OGG", subtype="VORBIS")
        made.append(o)
    return made


def play(path: str, device: str = DEFAULT_DEVICE, timeout: int = 180) -> int:
    """Проигрывание через aplay. Колонка может быть выключена — это не ошибка
    синтеза, поэтому возвращаем код: 0 ок, 3 ошибка ALSA, 4 нет aplay."""
    import subprocess

    try:
        subprocess.run(["aplay", "-q", "-D", device, path],
                       check=True, capture_output=True, timeout=timeout)
        return 0
    except FileNotFoundError:
        return 4
    except Exception:
        return 3


# --------------------------------------------------------------------------
# Анализ интонации (для стенда: проверка «звучит ли это как вопрос»)
# --------------------------------------------------------------------------

def f0_track(mono, sr: int = SR, fmin: int = 70, fmax: int = 420,
             win: float = 0.04, hop: float = 0.01):
    """Оценка основного тона F0 по кадрам (автокорреляция).
    Возвращает (времена_сек, частоты_Гц) для сравнения pitch-контуров."""
    import numpy as np

    x = np.asarray(mono, dtype=np.float64)
    fr, hp = int(win * sr), int(hop * sr)
    lo, hi = int(sr / fmax), int(sr / fmin)
    ts, fs = [], []
    for i in range(0, max(0, len(x) - fr), hp):
        s = x[i:i + fr]
        if np.sqrt(np.mean(s ** 2)) < 0.01:
            continue
        s = s - s.mean()
        ac = np.correlate(s, s, mode="full")[len(s) - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        w = ac[lo:hi]
        if not len(w):
            continue
        lag = int(np.argmax(w)) + lo
        if ac[lag] > 0.35:
            ts.append(i / sr)
            fs.append(sr / lag)
    return np.array(ts), np.array(fs)


def intonation_report(mono, sr: int = SR) -> dict | None:
    """Метрики высоты тона. Они описывают pitch-контур, не тип высказывания:

      * peak_pos — положение максимума F0;
      * tail_gain — отношение высоты тона в хвосте к началу;
      * start_hz — высота тона в начале.

    Эти метрики не определяют, звучит ли фраза вопросительно.
    """
    import numpy as np

    ts, fs = f0_track(mono, sr)
    if len(fs) < 12:
        return None
    k = np.ones(3) / 3
    f = np.convolve(fs, k, mode="same")
    n = len(f)
    dur = float(ts[-1]) if len(ts) else 0.0
    peak_i = int(np.argmax(f))
    n_tail = max(3, int(n * 0.4))
    tail = f[-n_tail:]
    head = f[: max(2, int(n * 0.35))]
    return {
        "n": n,
        "dur": dur,
        "median": float(np.median(f)),
        "start": float(f[: max(2, int(n * 0.1))].mean()),
        "peak": float(f[peak_i]),
        "peak_pos": peak_i / max(1, n - 1),
        "end": float(tail.mean()),
        "tail_gain": float(tail.mean() / head.mean()) if head.mean() else 0.0,
        "end_vs_peak": float(tail.mean() / f[peak_i]) if f[peak_i] else 0.0,
        "range": float(np.percentile(f, 95) - np.percentile(f, 5)),
    }


def judge_question(rep: dict) -> tuple[str, str]:
    """Описывает измеряемое направление pitch-контура, не распознаёт вопрос."""
    if rep is None:
        return "не измерить", "мало вокализованных кадров"
    gain, start = rep["tail_gain"], rep["start"]

    # На коротких фразах даже направление контура может быть нестабильным.
    if rep.get("dur", 0) < 1.5:
        contour, why = _judge_core(gain, start)
        return "НЕЯСНО", (f"фраза короткая ({rep['dur']:.1f} с) — контур F0 "
                          f"ненадёжен. Формально: {contour.lower()}, {why}")
    return _judge_core(gain, start)


def _judge_core(gain: float, start: float) -> tuple[str, str]:
    """Классифицирует только направление изменения F0, не вопросительность."""
    if gain >= 1.03:
        return "ХВОСТ ВЫШЕ", f"хвост выше начала на {(gain-1)*100:+.0f}% (начало {start:.0f} Гц)"
    if gain < 0.97:
        return "ХВОСТ НИЖЕ", f"хвост ниже начала на {(1-gain)*100:.0f}%"
    return "БЕЗ ЯВНОГО ПОДЪЁМА", f"отношение хвоста к началу {gain:.2f}"
