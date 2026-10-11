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


# Набор сигналов пробуждения.
#
# Прежний «tone» — чистая синусоида: звучит как пищалка измерительного прибора.
# Остальные собраны как настоящий инструмент: у колокольчика частичные тоны стоят
# НЕ по гармоникам (1, 2, 2.76, 4.07, 5.43 — так устроен колокол), и высокие
# затухают быстрее низких. Именно поэтому он звучит колокольчиком, а не
# синтезатором: у синусоиды обертонов нет вовсе, а у гармоник 2-4-8 звук
# «пластмассовый». Атака короткая (иначе щелчок), спад в конце обязателен.
WAKE_TONES = ("bell", "doorbell", "chime", "marimba", "pop", "tone")
DEFAULT_WAKE_TONE = "bell"
PEAK_LEVEL = 0.35            # общая громкость сигнала: выше — уже слышно клиппинг
WAKE_TONE_LABELS = {
    "bell": "колокольчик (один удар)",
    "doorbell": "динь-дон (две ноты вниз)",
    "chime": "две ноты вверх",
    "marimba": "маримба (деревянный)",
    "pop": "мягкий щелчок",
    "tone": "прежний тон 880 Гц",
}
# (отношение частоты, громкость, доля от длительности на затухание)
BELL_PARTIALS = ((1.0, 1.00, 1.00), (2.0, 0.50, 0.60), (2.76, 0.28, 0.45),
                 (4.07, 0.16, 0.30), (5.43, 0.09, 0.20))
WOOD_PARTIALS = ((1.0, 1.00, 1.00), (4.0, 0.32, 0.45), (9.2, 0.10, 0.25))


def _struck_note(freq: float, dur: float, amp: float, sample_rate: int,
                  partials=BELL_PARTIALS, attack: float = 0.004):
    """Удар по инструменту: сумма частичных тонов со своим затуханием у каждого."""
    import numpy as np

    count = max(1, int(sample_rate * dur))
    t = np.arange(count, dtype=np.float32) / sample_rate
    wave = np.zeros(count, dtype=np.float32)
    for ratio, level, decay in partials:
        wave += level * np.exp(-t / (decay * dur)) * np.sin(2 * np.pi * freq * ratio * t)
    env = np.minimum(t / attack, 1.0) * np.clip((dur - t) / 0.008, 0.0, 1.0)
    return wave * env * amp


def _soft_pop(dur: float, amp: float, sample_rate: int):
    """Короткий «щелчок» интерфейса: тон падает по частоте и быстро гаснет."""
    import numpy as np

    count = max(1, int(sample_rate * dur))
    t = np.arange(count, dtype=np.float32) / sample_rate
    freq = 1150.0 - 560.0 * (t / dur)
    phase = 2 * np.pi * np.cumsum(freq) / sample_rate
    env = np.minimum(t / 0.004, 1.0) * np.exp(-t / (0.35 * dur))
    env = env * np.clip((dur - t) / 0.006, 0.0, 1.0)
    return np.sin(phase) * env * amp


def wake_tone(kind: str = DEFAULT_WAKE_TONE, sample_rate: int = 16000):
    """Сигнал пробуждения: короткая отбивка длиной до 0.5 с.

    Смысл сигнала — сказать «я слушаю, говори» без слов: пока ассистент говорит,
    микрофон заглушён, и словесный отклик съел бы начало команды. Поэтому звук
    короткий и не похож на речь.
    """
    import numpy as np

    def note(freq, dur, amp, partials=BELL_PARTIALS):
        return _struck_note(freq, dur, amp, sample_rate, partials)

    if kind == "bell":                       # один удар колокольчика, до-мажор
        samples = note(1046.5, 0.28, 0.26)
    elif kind == "doorbell":                 # динь-дон: две ноты вниз, как у колонок
        samples = np.concatenate([note(880.0, 0.17, 0.25),
                                  note(659.3, 0.28, 0.27)])
    elif kind == "chime":                    # две ноты вверх
        samples = np.concatenate([note(784.0, 0.16, 0.24),
                                 note(1046.5, 0.24, 0.26)])
    elif kind == "marimba":                  # деревянный: частичные по 1, 4, 9.2
        samples = note(1046.5, 0.30, 0.26, WOOD_PARTIALS)
    elif kind == "pop":
        samples = _soft_pop(0.09, 0.30, sample_rate)
    elif kind == "tone":                     # прежний вариант, для сравнения
        count = max(1, int(sample_rate * 0.12))
        t = np.arange(count, dtype=np.float32) / sample_rate
        rise = np.minimum(t / 0.01, 1.0)
        fall = np.minimum((0.12 - t) / 0.02, 1.0)
        env = np.clip(np.minimum(rise, fall), 0.0, 1.0)
        samples = np.sin(2 * np.pi * 880.0 * t) * env
    else:
        raise ValueError(f"неизвестный сигнал: {kind} (есть: {', '.join(WAKE_TONES)})")

    # Одинаковая громкость у всех вариантов: сумма частичных тонов у колокольчика
    # даёт пик выше, чем у одиночной синусоиды, и без выравнивания один вариант
    # звучал бы заметно громче другого.
    peak = float(np.max(np.abs(samples))) or 1.0
    return (samples * (PEAK_LEVEL / peak)).astype(np.float32)


# Свои сигналы: сюда кладутся файлы (например, озвучка «Слушаю» из make-cue.py).
# Сигналом может быть и встроенная отбивка, и готовый файл — цикл не различает.
CUE_DIR = os.path.expanduser("~/.assistant/cues")
CUE_SUFFIXES = (".ogg", ".wav", ".flac", ".oga")
CUE_SECONDS = 0.7            # до какой длины обрезать файл сигнала


def cue_is_file(value: str) -> bool:
    """`--wake-tone` — это встроенный вариант или файл (путь либо имя из каталога)? """
    return value not in WAKE_TONES


def cue_files(directory: str = CUE_DIR) -> list[str]:
    """Файлы сигналов из каталога. Одно имя — один сигнал, формат не дублируем."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    seen, found = set(), []
    for name in names:
        stem, suffix = os.path.splitext(name)
        if suffix.lower() not in CUE_SUFFIXES or stem in seen:
            continue
        seen.add(stem)
        found.append(os.path.join(directory, name))
    return found


def cue_source(value: str) -> str | None:
    """Путь к файлу сигнала: как задан или найденный в каталоге сигналов."""
    if not cue_is_file(value):
        return None
    path = os.path.expanduser(value)
    if os.path.isfile(path):
        return path
    candidate = os.path.join(os.path.expanduser(CUE_DIR), value)
    return candidate if os.path.isfile(candidate) else None


def cue_ok(value: str) -> bool:
    """Годится ли значение `--wake-tone`: встроенный вариант или читаемый звук.

    Проверяем не только наличие файла, но и что он открывается как звук: у
    контейнера с видео расширение `.ogg` и он попал бы в список сигналов, а играть
    из него нечего. Лучше сказать об этом в панели, чем оставить пользователя без
    сигнала и без объяснений.
    """
    if value in WAKE_TONES:
        return True
    source = cue_source(value)
    return source is not None and cue_duration(source) > 0.0


def decode_audio(path: str):
    """Звук файла. Читаем только то, что понимает soundfile: ogg/wav/flac.

    Через sox здесь больше не ходим. Контейнер с видео в каталоге сигналов — это
    ошибка того, кто положил файл, а не рядовой случай, и правится она один раз
    (`sox in.ogg out.ogg`), а не вторым декодером внутри программы. Так чтение
    остаётся одним путём, а ошибка — внятной строкой в журнале.
    """
    import soundfile as sf

    try:
        return sf.read(path, always_2d=True)
    except Exception as exc:              # noqa: BLE001 — причину показываем целиком
        raise ValueError(
            f"{os.path.basename(path)}: не читается как звук ({exc}). Нужен "
            f"ogg/wav/flac; контейнер с видео или другой формат перекодируй: "
            f"sox исходник.ogg новый.ogg") from exc


def cue_duration(path: str) -> float:
    """Длительность файла из заголовка. 0.0 — если файл не открылся как звук."""
    import soundfile as sf

    try:
        return float(sf.info(path).duration)
    except Exception:                     # noqa: BLE001 — файл разберёт decode_audio
        return 0.0                        # и скажет внятно, что с ним не так


def trim_cue(data, rate: int, seconds: float = CUE_SECONDS):
    """Обрезать сигнал до нужной длины и убрать щелчки по краям.

    Чистая функция: на входе массив из soundfile (моно или стерео, любой формат),
    на выходе моно нужной длины. Файлы чужих записей всегда начинаются и
    заканчиваются щелчком, а играем мы в колонку на полной громкости, поэтому
    края приглушаются, а громкость выравнивается по встроенным вариантам.
    """
    import numpy as np

    samples = np.asarray(data, dtype="float32")
    if samples.ndim > 1:                      # колонка одна: стерео сводим в моно
        samples = samples.mean(axis=1)
    keep = min(len(samples), max(1, int(rate * seconds)))
    samples = samples[:keep].copy()
    fade_in = min(int(rate * 0.005), keep)
    fade_out = min(int(rate * 0.03), keep)
    if fade_in:
        samples[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype="float32")
    if fade_out:
        samples[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype="float32")
    peak = float(np.max(np.abs(samples))) or 1.0
    if peak > PEAK_LEVEL:                    # громкость как у встроенных сигналов
        samples = samples * (PEAK_LEVEL / peak)
    return samples


def prepare_cue(value: str = DEFAULT_WAKE_TONE, seconds: float = CUE_SECONDS,
                path: str | None = None) -> float:
    """Готовит сигнал к проигрышу и возвращает его длину в секундах.

    Встроенный вариант синтезируется, файл — читается, обрезается и получает
    спады по краям. Исходник не трогаем: обрезанная копия уходит в
    ~/.assistant/wake.wav, а сам файл остаётся как есть (его можно заменить и
    слушать заново).
    """
    import tts_core as core

    prepared = path or os.path.join(os.path.expanduser("~/.assistant"), "wake.wav")
    os.makedirs(os.path.dirname(prepared), exist_ok=True)
    source = cue_source(value)
    if source is None:
        samples = wake_tone(value)           # на опечатке бросит ValueError
        core.write_audio(samples, prepared, ogg=False)
        return len(samples) / core.SR

    import soundfile as sf

    data, rate = decode_audio(source)
    samples = trim_cue(data, rate, seconds)
    sf.write(prepared, samples, rate, subtype="PCM_16")
    return len(samples) / rate


def tone_seconds(kind: str = DEFAULT_WAKE_TONE, sample_rate: int = 16000,
                 seconds: float = CUE_SECONDS) -> float:
    """Длительность сигнала: по ней цикл выкидывает звук сигнала из записи.

    Считаем из самого сигнала, а не держим в цикле второе магическое число:
    иначе длина отбивки и число выбрасываемых кадров надо согласовывать руками,
    а рассогласование кладёт тон внутрь записи команды.
    """
    source = cue_source(kind)
    if source is None:
        return len(wake_tone(kind, sample_rate)) / sample_rate
    # Длину берём из заголовка, не читая файл целиком: если запись длиннее
    # отведённого, играть будет ровно обрезанную часть.
    return min(cue_duration(source) or seconds, seconds)


def beep(device: str | None = None, kind: str = DEFAULT_WAKE_TONE,
         seconds: float = CUE_SECONDS) -> int:
    """Короткий сигнал пробуждения — ТОН, а не слово.

    Говорить «слушаю» нельзя: пока ассистент говорит, микрофон заглушён, и
    начало команды потерялось бы. Сам сигнал в запись не попадает: цикл ждёт
    его окончания и выбрасывает ровно его длину (tone_seconds), поэтому мьют для
    сигнала не нужен.

    Сигналом может быть встроенная отбивка или готовый файл (озвучка, .ogg):
    файл читается, обрезается до `seconds` и получает спады по краям.

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
    prepare_cue(kind, seconds, path)
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
