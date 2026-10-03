# TTS — синтез русской речи (Silero v5_5_ru)

Проверено на BPI-W3: **RTF 0.13–0.18** (в 5–7 раз быстрее реального времени),
5 голосов, вывод на ES8316.

Настройка голоса, темпа и интонации — [10-voice-tuning.md](10-voice-tuning.md).
Ниже — установка и базовое использование.

## Почему Silero, а не Piper

Piper-голоса для русского (`ru_RU-irina/ruslan/dmitri/denis`) звучат заметно хуже.
Silero v5_5_ru даёт:

- **автоматические ударения** (`put_accent`) — правильно читает «замки»/«замки»;
- **омографы** (`put_stress_homo`) — различает по контексту;
- **букву ё** (`put_yo`);
- **вопросительную интонацию** — вопрос звучит как вопрос, но её легко
  сломать обернув всю фразу в `pitch="high"` (см. `docs/10-voice-tuning.md`);
- **SSML** — управление темпом, паузами;
- 5 голосов: `aidar`, `baya`, `kseniya`, `eugene`, `xenia`.

Важно: **TTS Сбера (GigaTTS) в открытом доступе нет** — только SaluteSpeech API.
Открыт только ASR-стек (GigaAM). Не путать: GigaAM — распознавание, GigaTTS — синтез.

## Установка

### 1. Отдельный venv через uv

`python3-venv` на плате не установлен (нет `ensurepip`), обычный `python3 -m venv`
падает с «ensurepip is not available». `uv` на плате уже есть.

```bash
uv venv ~/models/tts-venv --python 3.10
```

### 2. torch — ТОЛЬКО CPU-колёса

```bash
uv pip install --python ~/models/tts-venv/bin/python numpy
uv pip install --python ~/models/tts-venv/bin/python torch torchaudio \
  --index-url https://download.pytorch.org/whl/cpu
uv pip install --python ~/models/tts-venv/bin/python silero omegaconf soundfile
# num2words ПИНОМ на 0.5.14: версии 0.5.15 и 0.5.16 содержали вредоносный код
# (PYSEC-2025-72, GHSA-jxr6-qrxx-2ph2) и были удалены с PyPI. Без пина
# автообновление поставит скомпрометированную версию.
uv pip install --python ~/models/tts-venv/bin/python 'num2words==0.5.14'
# опционально: расстановка ударений и омографов
uv pip install --python ~/models/tts-venv/bin/python silero-stress
```

Без `--index-url .../whl/cpu` uv ставит `torch 2.14.1+cu130` и тянет ~2 ГБ
`nvidia-*` библиотек, бесполезных на ARM. Проверка после установки:

```bash
~/models/tts-venv/bin/python -c "import torch; print(torch.__version__)"
# ожидаем: 2.14.1+cpu   (не +cu130)
```

### 3. Модель (139 МБ)

```bash
# Качать НЕ на плате: curl рвётся при разрыве SSH и оставляет обрезанный файл
# (размер совпадает, а ZIP внутри побит). Качать на рабочей машине и копировать:
scp v5_5_ru.pt ubuntu@192.168.0.171:/home/ubuntu/models/tts/
```

Источник: `https://models.silero.ai/models/tts/ru/v5_5_ru.pt`

Проверка целостности перед использованием:

```bash
md5sum ~/models/tts/v5_5_ru.pt          # сверять с копией-источником
python3 -c "
d=open('/home/ubuntu/models/tts/v5_5_ru.pt','rb').read()
print('EOCD:', d.rfind(b'PK\x05\x06'), 'из', len(d))"   # должен быть не -1
```

## Использование

### Скрипт say.sh (рекомендуется)

```bash
./scripts/say.sh "Привет! Я готов."                    # синтез + вывод на динамик
./scripts/say.sh -v xenia -s 1.15 "Другой голос"       # голос и темп
./scripts/say.sh -p alert "Внимание! Протечка."        # профиль интонации
./scripts/say.sh -o /tmp/out.wav "Сохранить в файл"    # без воспроизведения
echo "Текст из пайпа" | ./scripts/say.sh               # чтение из stdin
./scripts/say.sh --check "Напомни через 10 минут"      # проверить риски, без синтеза
```

Профили (`-p`): `neutral`, `warm` (по умолчанию), `lively`, `alert`, `calm`,
`question`, `emphasis`. Что каждый делает — [10-voice-tuning.md](10-voice-tuning.md).

Цифры и латиница по умолчанию **переписываются словами** — Silero молча
вырезает всё вне кириллицы, и «через 10 минут» звучит как «через минуту».
Отключить можно `-n`, но тогда смысл будет теряться.

Коды возврата: `0` — успех, `1` — нет venv/бэкенда, `2` — пустой текст,
`3` — ошибка вывода ALSA, `4` — нет `aplay`.

### Стенд подбора голоса (tools/voice_lab.py)

```bash
python3 tools/voice_lab.py list                   # что можно менять
python3 tools/voice_lab.py check -t "текст"       # ударения + цифры/латиница
python3 tools/voice_lab.py palette -t "текст"     # палитра рецептов -> OGG
python3 tools/voice_lab.py compare --voices baya,kseniya,xenia -t "текст"
python3 tools/voice_lab.py tune --target -5 -t "текст"
python3 tools/voice_lab.py question -t "текст?"
```

Полное руководство — [10-voice-tuning.md](10-voice-tuning.md).

### Напрямую через Python

```python
import torch
m = torch.package.PackageImporter(
    "/home/ubuntu/models/tts/v5_5_ru_ok.pt").load_pickle("tts_models", "model")
m.save_wav(text="Привет!", speaker="baya", audio_path="/tmp/a.wav", sample_rate=48000)
```

## Критичные параметры

| Параметр | Значение | Почему |
|---|---|---|
| `torch.set_num_threads()` | **2** | при 8 плата уходит в аппаратный сброс |
| `sample_rate` | 48000 | нативный для модели; ES8316 держит и 44100 |
| Каналы на выход | **2 (стерео)** | ES8316 молча не играет моно |
| Устройство вывода | `plughw:3,0` | ES8316, card 3 |

### API v5 отличается от примеров в интернете

```python
# Правильно (v5):
m.save_wav(text=..., speaker=..., audio_path=..., sample_rate=...)
m.apply_tts(text=..., speaker=..., sample_rate=48000)   # -> numpy

# НЕ работает (v3/v4 синтаксис):
m.save_wav(audio=..., speaker=...)   # TypeError: unexpected keyword 'audio'
```

Для numpy-обработки: `audio = m.apply_tts(...)`, затем `.detach().cpu().numpy()`.

---

## Ограничение по питанию — главная грабля

**При `torch.set_num_threads(8)` плата уходит в аппаратный сброс.** Все 8 ядер
RK3588 под пиковой нагрузкой дают просадку питания ниже порога. Симптомы:

- плата перезагружается каждые ~5 минут;
- недописанные WAV остаются обрезанными (`fmt chunk and/or data chunk missing`);
- в `journalctl --list-boots` растут загрузки подряд;
- **в логе предыдущей загрузки НЕТ `systemd-shutdown`** — это признак аппаратного
  сброса, а не перезагрузки по инициативе ОС. Проверка:

```bash
journalctl --list-boots | tail -5
sudo journalctl -b -1 --no-pager -o short | grep -iE "systemd-shutdown|Power off"
# пусто => аппаратный сброс
```

**Решение: `torch.set_num_threads(2)`.** RTF при этом 0.13–0.18 — потери
производительности практически нет, потому что узкое место не в числе потоков.

При диагностике долгих задач логировать `uptime` в каждой строке прогресса —
сразу видно, если плата сбросилась и отсчёт начался заново.

## Фоновые задачи требуют linger

`systemd --user` **убивает юниты при выходе последней SSH-сессии**, если linger
не включён. Признак: юнит `inactive` без ошибки, лог обрывается на середине,
плата при этом жива (uptime растёт).

```bash
sudo loginctl enable-linger ubuntu
loginctl show-user ubuntu | grep Linger   # должно быть Linger=yes
```

Запуск с записью вывода в файл (journal при `--collect` может стереться):

```bash
systemd-run --user --unit=tts-test --collect \
  --property=StandardOutput=file:/home/ubuntu/run.log \
  --property=StandardError=append:/home/ubuntu/run.log \
  /home/ubuntu/models/tts-venv/bin/python /home/ubuntu/script.py
```

## Проверка качества без прослушивания

**Замкнутый цикл TTS → ASR.** Синтезированный текст скормить распознавателю и
сравнить. Работает надёжно:

```bash
./scripts/say.sh -o /tmp/check.wav "Привет! Я голосовой ассистент."
~/sherpa-onnx/build-rknn/install/bin/sherpa-onnx-offline \
  --tokens=~/models/gigaam-v3/tokens.txt \
  --encoder=~/models/gigaam-v3/encoder.int8.onnx \
  --decoder=~/models/gigaam-v3/decoder.onnx \
  --joiner=~/models/gigaam-v3/joiner.onnx \
  --model-type=nemo_transducer --feat-dim=64 --num-threads=2 \
  /tmp/check.wav
# ожидаем: "Привет. Я голосовой ассистент."
```

**Акустическая петля** (динамик → микрофон) — end-to-end тест без человека:

```bash
# терминал 1: слушаем микрофон
~/sherpa-onnx/build-rknn/install/bin/sherpa-onnx-vad-alsa-offline-asr \
  --silero-vad-model=~/models/silero_vad.onnx \
  --tokens=~/models/gigaam-v3/tokens.txt \
  --encoder=~/models/gigaam-v3/encoder.int8.onnx \
  --decoder=~/models/gigaam-v3/decoder.onnx \
  --joiner=~/models/gigaam-v3/joiner.onnx \
  --model-type=nemo_transducer --feat-dim=64 --num-threads=2 plughw:5,0
# терминал 2: говорим в колонку
./scripts/say.sh "Проверка акустической петли"
```

Требуется включённая колонка. Микрофон ждёт `plughw:5,0` — ресемплинг в 16 кГц
внутри, `hw:5,0` упадёт с «Failed to set sample rate to 16000».

## Готовые голосовые образцы

`~/models/tts/samples/*.wav` — по 3 фразы на каждый из 5 голосов:

- `*_hello` — «Привет! Я голосовой ассистент. Сейчас три часа дня.»
- `*_question` — «Включить свет в спальне? Скажи да или нет.» (проверка интонации)
- `*_hard` — омографы («замки») и числа (проверка ударений)

Там же `.ogg`-версии для прослушивания на телефоне.
