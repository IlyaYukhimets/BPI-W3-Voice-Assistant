"""Слой ответа ассистента: распознанный текст -> реплика -> озвучка.

ПОЧЕМУ ЗАГЛУШКА. Сейчас ассистент отвечает шаблоном, а место реального
вызова Home Assistant явно отмечено точкой `stub_intent`: она печатает,
какой HTTP-запрос будет отправлен, и ничего не отправляет. Когда появится
долгоживущий токен HA, меняется только эта функция — озвучка, защита от
эха и журнал остаются как есть.

Озвучка идёт через резидентный TTS-воркер (tts_say.daemon_synth), то есть
без загрузки torch в процесс прослушки: ответ звучит за ~0.3 с синтеза.
Если воркера нет — честный фолбэк через say.sh (он сам умеет холодный
путь), только медленнее.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Ответ по умолчанию. Признаётся вслух, поэтому без канцелярита: коротко,
# понятно и честно — что услышано и что слой HA ещё не подключён.
DEFAULT_TEMPLATE = ("Принято: {text}. Отвечаю шаблоном, "
                    "запрос в Home Assistant будет на последнем этапе.")

# Имя ассистента. «Астра» — редкое в бытовой речи слово, и акустически
# удачное: начинается с открытой гласной, поэтому VAD ловит начало раньше,
# чем на согласной (у «Джарвиса» тихая «дж» — замерено: слово давало всего
# 7 окон выше порога при нужных 8). Список имён — через запятую.
DEFAULT_WAKE_WORD = "астра"

# Порог схожести для нечёткого узнавания имени. 0.75 (как было) отклонял
# реальные попытки: ASR с живым голосом выдал «Астя, включи свет в коридоре»
# (схожесть 0.667) — и команда терялась целиком. 0.6 принимает весь живой
# разброс «аст*», но по-прежнему отвергает «прости», «поставь», «эста».
WAKE_SIMILARITY = 0.6


def _norm_words(text: str) -> list[str]:
    """Слова в нижнем регистре без пунктуации; ё приводится к е."""
    out, cur = [], []
    for ch in text.lower().replace("ё", "е"):
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def match_wake(text: str, words=None, threshold: float | None = None) -> tuple[bool, str]:
    """Ищет имя ассистента во фразе: (найдено, остаток-команда).

    Сравнение НЕЧЁТКОЕ (difflib). Причина: VAD может срезать начало слова, а
    микрофон платы слабый и шумный — ASR слышит «Астры», «Астро», «Аста»,
    «Астя». Порог 0.6 подобран по живым логам: «астя» (0.667) и «асти» (0.667)
    узнаются, а «прости» (0.36), «поставь» (0.33), «эста» (0.44) — нет.
    Жёсткое равенство теряло бы почти все попытки позвать ассистента.
    Пары соседних слов проверяются тоже: ASR иногда делит имя надвое.
    """
    if threshold is None:
        threshold = WAKE_SIMILARITY
    if words is None:
        words = DEFAULT_WAKE_WORD
    if isinstance(words, str):
        words = words.split(",")
    # _norm_words возвращает СПИСОК слов: имя может быть и составным
    # («эй астра») — тогда достаточно совпадения с любым его словом.
    targets = [tok for w in words for tok in _norm_words(w)]
    tokens = _norm_words(text)
    if not targets or not tokens:
        return False, ""

    from difflib import SequenceMatcher

    def onset_ok(target: str, cand: str) -> bool:
        """Если имя начинается с гласной, кандидат тоже должен ею начинаться.

        Порог схожести пришлось опустить до 0.6 (живые «астя»/«асти»), и тогда
        под него подпадают «мастер» (0.73) и «страна» (0.73). Но «астра»
        начинается с открытой гласной: VAD может срезать это начало, а вот
        подменить его согласной ASR не может. Проверка отсекает мусорные
        совпадения, не теряя реальных.
        """
        vowels = "аеёиоуыэюя"
        return not (target[:1] in vowels) or cand[:1] in vowels

    cands = [(i, i + 1, tok) for i, tok in enumerate(tokens)]
    cands += [(i, i + 2, tokens[i] + tokens[i + 1])
              for i in range(len(tokens) - 1)]
    for start, end, cand in cands:
        if len(cand) < 4:
            continue
        for target in targets:
            if not onset_ok(target, cand):
                continue
            if SequenceMatcher(None, cand, target).ratio() >= threshold:
                rest = " ".join(tokens[:start] + tokens[end:])
                return True, rest
    return False, ""


class WakeState:
    """Сон ассистента по имени с таймаутом ожидания команды.

    Алгоритм (как у колонок): пока не прозвучало имя — ассистент молчит и на
    распознанный текст не отвечает. Имя услышано -> «бодрствует» timeout
    секунд и ждёт команду. Команда пришла -> уходит наверх, состояние снова
    сон. Тишина дольше таймаута -> сон (в журнале видно, что заснул).

    Имя и команда одним вдохом («джарвис, включи свет») -> сразу команда без
    ожидания: будить и держать таймаут тут незачем.

    Часы инъектируются (clock): тесты проверяют таймаут без реального ожидания.
    """

    def __init__(self, words=None, timeout: float = 12.0, clock=None,
                 similarity: float | None = None):
        import time
        self.words = words
        self.timeout = timeout
        self.similarity = similarity      # None -> WAKE_SIMILARITY
        self._clock = clock or time.monotonic
        self._awake_until = 0.0

    @property
    def awake(self) -> bool:
        return self._clock() < self._awake_until

    def on_text(self, text: str) -> tuple[str, str]:
        """Распознанный текст -> ("wake"|"command"|"ignore", нагрузка)."""
        found, rest = match_wake(text, self.words, self.similarity)
        if found and rest.strip():
            self._awake_until = 0.0
            return "command", rest
        if found:
            self._awake_until = self._clock() + self.timeout
            return "wake", ""
        if self.awake:
            self._awake_until = 0.0
            return "command", text
        return "ignore", ""

    def wake_up(self) -> None:
        """Разбудить без совпадения по имени.

        Нужно, когда ASR расслышал короткое слово-имя иначе («Прости»,
        «Поставь»): ассистент всё равно должен откликнуться на короткое
        обращение — иначе со слабым микрофоном он не отзывается вовсе.
        """
        self._awake_until = self._clock() + self.timeout

    def tick(self) -> bool:
        """True ровно один раз — в момент засыпания по таймауту."""
        if self._awake_until and self._clock() >= self._awake_until:
            self._awake_until = 0.0
            return True
        return False


def wake_tone(sample_rate: int = 16000, freq: float = 880.0,
              dur: float = 0.12, amp: float = 0.35):
    """Нарастание/спад по краям тона: резкий старт даёт щелчок в колонке."""
    import numpy as np
    t = np.arange(int(sample_rate * dur), dtype=np.float32) / sample_rate
    rise = np.minimum(t / 0.01, 1.0)
    fall = np.minimum((dur - t) / 0.02, 1.0)
    env = np.clip(np.minimum(rise, fall), 0.0, 1.0)
    return (np.sin(2 * np.pi * freq * t) * env * amp).astype(np.float32)


def beep(device: str | None = None) -> int:
    """Короткий сигнал пробуждения — ТОН, а не слово.

    Говорить «слушаю» нельзя: пока ассистент говорит, микрофон заглушён, и
    начало команды потерялось бы. 0.12 с тона в сегмент VAD не попадут
    (min_speech 0.25 с), поэтому мьют для сигнала не нужен.
    """
    import tts_core as core

    path = os.path.join(os.path.expanduser("~/.assistant"), "wake.wav")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    core.write_audio(wake_tone(), path, ogg=False)
    return core.play(path, device=device or core.DEFAULT_DEVICE, timeout=10)


def classify_utterance(found_name: bool, speech_s: float, call_min: float,
                       call_max: float, ignore_without_name: bool = False) -> str:
    """Что делать с распознанной фразой: "call" | "command" | "ignore".

    Во сне имя почти всегда расслышано иначе («Поставь», «Астя»), а сама команда
    распознаётся хорошо. Поэтому имя — удобство (сигнал «слушаю»), а не турникет:
    по умолчанию фраза без имени всё равно выполняется. Флаг
    --ignore-without-name возвращает строгое поведение (отвечать только по имени).
    """
    if found_name:
        return "command"
    if call_min <= speech_s <= call_max:
        return "call"
    return "ignore" if ignore_without_name else "command"


def is_short_call(speech_s: float, limit: float, floor: float = 0.3) -> bool:
    """Короткая речь в сегменте = оклик, а не фраза.

    Нижняя граница обязательна: щелчок, скрип стула или обрывок звука дают
    0.15-0.26 с «речи», и без неё ассистент откликался на шум (проверено на
    живой эксплуатации — «Страшно.» на 0.26 с). Настоящий оклик — 0.38-0.74 с.
    Верхняя граница отделяет оклик от короткой команды (1.22-1.54 с).
    """
    return floor <= speech_s <= limit


def make_reply(text: str, template: str = DEFAULT_TEMPLATE) -> str:
    """Реплика ассистента на распознанный текст.

    Подстановка через replace, а не str.format: распознанный текст может
    содержать фигурные скобки (они приходят от ASR как есть), и .format
    на них падал бы. Шаблон тоже может содержать любые скобки.

    Концевая пунктуация распознанного текста снимается: ASR всегда
    заканчивает фразу точкой, а шаблон ставит её сам — иначе в ответе
    звучит «коридоре..» (слышно как лишняя пауза).
    """
    return template.replace("{text}", text.strip().rstrip(".,!?;:").strip())


def stub_intent(text: str) -> dict:
    """ЗАГЛУШКА вызова Home Assistant. Возвращает то, что уйдёт в API.

    Здесь и только здесь появится реальный HTTP-запрос (POST /api/services/...
    с долгоживущим токеном). Пока — печать формы будущего запроса: она видна
    в журнале рядом с ответом, поэтому «путь до HA» виден уже сейчас.
    """
    request = {
        "method": "POST",
        "path": "/api/services/<domain>/<service>",  # определится интентом
        "json": {"text": text.strip()},
        "token": "нужен долгоживущий токен HA",
    }
    print(f"[intent] заглушка, HTTP-запрос НЕ отправляется: "
          f"{request['method']} {request['path']} {request['json']}", flush=True)
    return request


def _words(s: str) -> set:
    """Слова для сравнения: без регистра и знаков препинания."""
    out, cur = set(), []
    for ch in s.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.add("".join(cur))
            cur = []
    if cur:
        out.add("".join(cur))
    return out


def is_echo(text: str, last_reply: str | None, overlap: float = 0.7) -> bool:
    """Похоже ли распознанное на нашу же озвученную реплику.

    Защита от самопрослушивания: микрофон и динамик на одной плате, и даже
    с паузой на время речи хвост колонки может попасть в сегмент. Реплика
    узнаётся по пересечению слов: если >= overlap слов распознанного есть
    в предыдущем ответе — это эхо, отвечать на него нельзя (иначе цикл).
    """
    if not last_reply:
        return False
    said, reply = _words(text), _words(last_reply)
    if not said:
        return False
    return len(said & reply) / len(said) >= overlap


def synth_and_play(text: str, voice: str | None = None, device: str | None = None,
                   quiet: bool = True) -> int:
    """Озвучить текст резидентым воркером; при отказе — честный фолбэк say.sh.

    Возвращает код возврата aplay (0 — ок). Текст нормализуется так же, как
    в say.sh: цифры и латиница иначе молча вырезаются моделью.
    """
    import tts_core as core

    voice = voice or core.DEFAULT_VOICE
    device = device or core.DEFAULT_DEVICE
    text = core.normalize_text(text)
    ssml = core.build_ssml(text, profile="warm")

    import tts_say
    got = tts_say.daemon_synth(ssml, voice)
    if got is None:
        # Воркера нет — say.sh сам уйдёт холодным путём (модель в своём
        # процессе). Это заметно медленнее, но ответ будет произнесён.
        print("[reply] воркер недоступен, озвучиваю через say.sh (медленно)",
              file=sys.stderr)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "say.sh")
        cmd = [script, "-q", text]
        if device != core.DEFAULT_DEVICE:
            cmd += ["-d", device]
        return subprocess.call(cmd)

    import tempfile
    raw, info = got
    mono = core.mono_from_bytes(raw)
    fd, path = tempfile.mkstemp(prefix="reply_", suffix=".wav")
    os.close(fd)
    try:
        core.write_audio(mono, path, ogg=False)
        if not quiet:
            print(f"[reply] синтез {float(info.get('gen_s') or 0):.2f}с, "
                  f"аудио {len(mono) / core.SR:.2f}с", file=sys.stderr)
        return core.play(path, device=device)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ответ ассистента на распознанный текст (шаблон-заглушка)")
    ap.add_argument("--once", metavar="ТЕКСТ",
                    help="обработать одну фразу как распознанную и выйти")
    ap.add_argument("-t", "--template", default=DEFAULT_TEMPLATE,
                    help="шаблон ответа; {text} подставляется распознанным текстом")
    ap.add_argument("-v", "--voice", default=None, help="голос синтеза")
    ap.add_argument("-d", "--device", default=None, help="ALSA-устройство вывода")
    ap.add_argument("--no-speak", action="store_true",
                    help="не озвучивать, только напечатать ответ")
    args = ap.parse_args()

    if not args.once:
        print("assistant: нужен --once \"текст\" (полный цикл — voiced.py)",
              file=sys.stderr)
        return 2

    text = args.once.strip()
    reply = make_reply(text, args.template)
    stub_intent(text)
    print(f"[reply] {reply}")
    if args.no_speak:
        return 0
    rc = synth_and_play(reply, voice=args.voice, device=args.device, quiet=False)
    return 0 if rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())
