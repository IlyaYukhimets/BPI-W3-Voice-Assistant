#!/usr/bin/env python3
"""kws.py — детектор имени ассистента на Vosk: грамматика из одного слова.

ПОЧЕМУ VOSK, А НЕ СВОБОДНОЕ РАСПОЗНАВАНИЕ ИМЕНИ.
Микрофон платы отдаёт SNR 1-7 дБ, и свободное распознавание имени (GigaAM или
Vosk без грамматики) на этом звуке ошибается: «Астра» -> «Остров», «Поставь»,
«Астя». Причина в размере задачи: выбирая из тысяч слов, модель хватается за
акустически близкое обычное слово. С грамматикой задача становится «астра или
не астра», и замеры на том же звуке дают «астра».

Замеры (одна и та же запись через микрофон, пик -14.8 дБ):
    GigaAM свободно      -> «Остров»      ошибка
    Vosk свободно        -> «»            пусто
    Vosk с грамматикой   -> «астра»       верно
Ложные срабатывания: шум комнаты 6 с -> пусто, «Стоп.» -> пусто,
команда -> «[unk]» (не имя), 20 сегментов из spool -> 1 срабатывание.

МОДЕЛЬ. Нужна МЛАДШАЯ: грамматика ограничивает словарь, большой модели
(vosk-model-ru-0.42, 1.8 ГБ) для этой роли не требуется. Официальный источник
отдаёт 0.3 КБ/с (сутки), рабочее зеркало:
    https://raw.githubusercontent.com/kercre123/vosk-models/main/vosk-model-small-ru-0.22.zip
(46 МБ, 605 КБ/с с платы).

Формы, которых нет в словаре модели (например «астру»), Vosk молча игнорирует с
warning «Ignoring word missing in vocabulary» — это нормально, не ошибка настроек.
"""
from __future__ import annotations

import json
import os

DEFAULT_MODEL = "~/models/vosk-model-small-ru-0.22"

# Список форм подбирается по тому, как микрофон слышит имя. Грамматика сама
# работает как порог: посторонняя речь уходит в [unk] и не считается именем.
DEFAULT_PHRASES = ("астра", "астры", "эй астра")


def build_grammar(phrases) -> str:
    """JSON-список фраз для KaldiRecognizer (+ [unk] — всё остальное)."""
    items = [p.strip() for p in phrases if p and p.strip()]
    if not items:
        raise ValueError("пустой список фраз для KWS")
    return json.dumps(items + ["[unk]"], ensure_ascii=False)


def allowed_words(phrases) -> set[str]:
    """Слова, которые могут прийти из грамматики (для проверки срабатывания)."""
    words: set[str] = set()
    for phrase in phrases:
        words.update(w.lower() for w in str(phrase).split() if w.strip())
    return words


def strip_name(text: str, phrases=None) -> str:
    """Снять имя с начала распознанной команды.

    Пре-ролл записи захватывает хвост имени (он нужен ASR как «прогрев»
    фронтенда, иначе теряется первое слово команды), поэтому имя иногда
    попадает в текст: «Астра. Включи свет». Для разбора команды оно лишнее.
    """
    words = allowed_words(phrases or DEFAULT_PHRASES)
    tokens = str(text).split()
    while tokens and tokens[0].strip(".,!?").lower() in words:
        tokens.pop(0)
    return " ".join(tokens).strip()


class KwsSpotter:
    """Постоянный сторож имени.

    Модель грузится лениво (при первом feed): тестам и разбору аргументов модель
    не нужна. Фабрика распознавателя инжектируется — в тестах вместо Vosk
    подставляется заглушка, поэтому логика проверяется без модели и без звука.
    """

    def __init__(self, model_dir: str | None = None, phrases=None,
                 sample_rate: int = 16000, factory=None):
        self.model_dir = os.path.expanduser(model_dir or DEFAULT_MODEL)
        self.phrases = tuple(phrases or DEFAULT_PHRASES)
        self.sample_rate = sample_rate
        self._factory = factory or _vosk_factory
        # Своя фабрика (тесты) означает, что модель не нужна: распознаватель
        # подставляется заглушкой, и логика проверяется без Vosk и без звука.
        self._external = factory is not None
        self._model = None
        self._rec = None
        self.frames = 0      # сколько кадров отдано распознавателю
        self.hits = 0        # сколько раз сработало имя

    # --- загрузка -----------------------------------------------------------
    def load(self) -> None:
        """Загрузить модель. Ошибки — с подсказкой, а не голым traceback."""
        if self._model is not None or self._external:
            return
        if not os.path.isdir(self.model_dir):
            raise FileNotFoundError(
                f"нет модели Vosk: {self.model_dir}\n"
                "скачать (официальный источник практически не отдаёт файл):\n"
                "  curl -L -o /tmp/vosk.zip "
                "https://raw.githubusercontent.com/kercre123/vosk-models/main/"
                "vosk-model-small-ru-0.22.zip\n"
                "  python3 -c \"import zipfile;"
                "zipfile.ZipFile('/tmp/vosk.zip').extractall('"
                + os.path.dirname(self.model_dir) + "')\"")
        self._model = self._load_model()

    def _load_model(self):
        try:
            from vosk import Model, SetLogLevel
        except ImportError as exc:                       # pragma: no cover
            raise RuntimeError(
                f"нет пакета vosk в этом интерпретаторе ({exc}); "
                "поставить: uv pip install vosk") from exc
        SetLogLevel(-1)          # без внутреннего лога Kaldi в stdout журнала
        return Model(self.model_dir)

    def _make_recognizer(self) -> None:
        self.load()
        self._rec = self._factory(self._model, self.sample_rate,
                                  build_grammar(self.phrases))

    def reset(self) -> None:
        """Забыть состояние декодера (после своей озвучки — новый сторож)."""
        self._rec = None

    # --- работа -------------------------------------------------------------
    def feed(self, pcm: bytes) -> bool:
        """Отдать очередной кадр. True — имя прозвучало (по ИТОГОВОМУ тексту).

        Частичные результаты (PartialResult) НАМЕРЕННО не используются: в режиме
        грамматики они нестабильны и дорисовывают единственное разрешённое слово
        в посторонней фразе. Живой прогон: команда «Включи свет в коридоре»
        дала ложное срабатывание по partial, а по итогу — [unk]. Цена отказа —
        задержка: Vosk закрывает фразу по паузе после слова (~0.5-0.7 с), и
        сигнал звучит уже после того, как имя точно прозвучало целиком.
        """
        if self._rec is None:
            self._make_recognizer()
        self.frames += 1
        if not self._rec.AcceptWaveform(pcm):
            return False
        if self._is_name(json.loads(self._rec.FinalResult()).get("text", "")):
            self.hits += 1
            return True
        return False

    def _is_name(self, text: str) -> bool:
        """Имя найдено? Грамматика отдаёт только наши слова или [unk]."""
        words = allowed_words(self.phrases)
        return any(w.strip(".,!?").lower() in words
                   for w in str(text).split() if w.strip())


def _vosk_factory(model, sample_rate: int, grammar: str):
    """Распознаватель Vosk с грамматикой (импорт тут: без vosk модуль живёт)."""
    from vosk import KaldiRecognizer
    return KaldiRecognizer(model, sample_rate, grammar)
