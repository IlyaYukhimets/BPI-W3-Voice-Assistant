"""assistant.py — тексты ответов, сигнал и озвучка.

Имя ассистента и его распознавание переехали в scripts/kws.py (Vosk с
грамматикой): на этом микрофоне узнать имя по тексту ASR не удавалось —
одно короткое слово расслышивается как «Поставь», «Астя», «Остров».
Здесь остаётся то, что не зависит от того, как имя было услышано.
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
