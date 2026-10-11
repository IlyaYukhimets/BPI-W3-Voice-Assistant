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

# Ответ по умолчанию. Произносится вслух, поэтому без канцелярита: принято —
# и всё. Про шаблон и будущий запрос в Home Assistant в ответе не говорим: это
# служебная кухня, а не ответ человеку (и она устареет, когда HA подключат).
DEFAULT_TEMPLATE = "Принято: {text}."


# Варианты сигнала пробуждения. Исходный — чистый тон 880 Гц: одна синусоида без
# обертонов, звучит как пищалка измерительного прибора. Остальные — короткие
# отбивки: мягкая атака (резкий старт даёт щелчок в колонке), затухание и тихие
# гармоники, отсюда «колокольчик» вместо сигнала таймера.
WAKE_TONES = ("chime", "blip", "soft", "tone")
DEFAULT_WAKE_TONE = "chime"


def wake_tone(kind: str = DEFAULT_WAKE_TONE, sample_rate: int = 16000):
    """Сигнал пробуждения: короткая отбивка длиной 0.1-0.2 с.

    Варианты: chime — две ноты вверх (по умолчанию), blip — один скользящий
    звук, soft — одна мягкая нота пониже, tone — прежний тон 880 Гц (оставлен,
    чтобы было с чем сравнить и чтобы вернуть прежнее поведение одной строкой).
    """
    import numpy as np

    def note(freq: float, dur: float, amp: float, attack: float = 0.006,
             decay: float = 0.05, harmonics=((2, 0.22), (3, 0.08))):
        count = max(1, int(sample_rate * dur))
        t = np.arange(count, dtype=np.float32) / sample_rate
        env = np.minimum(t / attack, 1.0) * np.exp(-t / decay)
        # Спад в самом конце обязателен: обрез ноты слышен как щелчок.
        env = env * np.clip((dur - t) / 0.006, 0.0, 1.0)
        wave = np.sin(2 * np.pi * freq * t)
        for mult, level in harmonics:      # тихие обертоны — «колокольчику»
            wave = wave + level * np.sin(2 * np.pi * freq * mult * t)
        return (wave * env * amp).astype(np.float32)

    def glide(start: float, stop: float, dur: float, amp: float):
        count = max(1, int(sample_rate * dur))
        t = np.arange(count, dtype=np.float32) / sample_rate
        freq = start + (stop - start) * (t / dur)
        phase = 2 * np.pi * np.cumsum(freq) / sample_rate
        env = np.minimum(t / 0.01, 1.0) * np.exp(-t / 0.045)
        env = env * np.clip((dur - t) / 0.006, 0.0, 1.0)
        return np.sin(phase) * env * amp

    if kind == "tone":
        count = max(1, int(sample_rate * 0.12))
        t = np.arange(count, dtype=np.float32) / sample_rate
        rise = np.minimum(t / 0.01, 1.0)
        fall = np.minimum((0.12 - t) / 0.02, 1.0)
        env = np.clip(np.minimum(rise, fall), 0.0, 1.0)
        return (np.sin(2 * np.pi * 880.0 * t) * env * 0.35).astype(np.float32)
    if kind == "blip":
        return glide(700.0, 1050.0, 0.10, 0.30).astype(np.float32)
    if kind == "soft":
        return note(660.0, 0.16, 0.26, decay=0.09).astype(np.float32)
    if kind != "chime":
        raise ValueError(f"неизвестный сигнал: {kind} (есть: {', '.join(WAKE_TONES)})")
    return np.concatenate([note(784.0, 0.055, 0.26),
                           note(1174.7, 0.115, 0.30, decay=0.07)]).astype(np.float32)


def tone_seconds(kind: str = DEFAULT_WAKE_TONE, sample_rate: int = 16000) -> float:
    """Длительность сигнала: по ней цикл выкидывает звук сигнала из записи.

    Считаем из самого сигнала, а не держим в цикле второе магическое число:
    иначе длина отбивки и число выбрасываемых кадров надо согласовывать руками,
    а рассогласование кладёт тон внутрь записи команды.
    """
    return len(wake_tone(kind, sample_rate)) / sample_rate


def beep(device: str | None = None, kind: str = DEFAULT_WAKE_TONE) -> int:
    """Короткий сигнал пробуждения — ТОН, а не слово.

    Говорить «слушаю» нельзя: пока ассистент говорит, микрофон заглушён, и
    начало команды потерялось бы. Сам сигнал в запись не попадает: цикл ждёт
    его окончания и выбрасывает ровно его длину (tone_seconds), поэтому мьют для
    сигнала не нужен.

    Попытка ровно одна, повторов нет. Провальный aplay звука не даёт, зато
    занимает время, и отложенный сигнал приходит ровно тогда, когда колонка уже
    играет, — тон ложится ВНУТРЬ команды и распознавание портится (проверено на
    живой системе: с ожиданием фраза с колонки читалась хуже). Бывает это только
    в одном сценарии — когда фразу проигрывает сама колонка, а для проверки
    имени у панели есть отдельная кнопка. Если сигнал не прозвучал, код ошибки
    уходит в журнал.
    """
    import tts_core as core

    path = os.path.join(os.path.expanduser("~/.assistant"), "wake.wav")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    core.write_audio(wake_tone(kind), path, ogg=False)
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
