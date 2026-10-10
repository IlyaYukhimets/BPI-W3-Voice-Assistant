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
