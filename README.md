# BPI-W3 Voice Assistant

Локальный голосовой ассистент для Home Assistant на Banana Pi BPI-W3 (RK3588).

## Что умеет

- 🎤 Запись с USB-микрофона (48 кГц)
- 🧹 Очистка сигнала через sox (highpass/lowpass + нормализация)
- 🧠 Распознавание русской речи (GigaAM v3, ONNX, int8)
- ⚡ NPU RK3588 доступен (драйвер 0.9.2, librknnrt 1.5.2)
- 🔊 Вывод звука через ES8316 (3.5 мм JACK)
- 🗣 Голосовые ответы (Silero v5_5_ru, 5 голосов, RTF 0.13–0.18)
- 🎚 Нормализация текста (цифры/латиница → словами, иначе Silero их вырезает)
- 🎛 Стенд подбора голоса и интонации (`tools/voice_lab.py`)

## Стек

| Компонент | Технология |
|---|---|
| Плата | Banana Pi BPI-W3 (RK3588, 8 ГБ) |
| ОС | Ubuntu 22.04.3 LTS (ядро 5.10.160-rockchip) |
| Аудио выход | ES8316 (card 3) |
| Аудио вход | USB-микрофон (card 5) |
| VAD | Silero VAD v4 (CPU) |
| ASR | GigaAM v3 (ONNX int8) |
| Фреймворк | sherpa-onnx v1.13.8 (собран из исходников) |
| TTS | Silero v5_5_ru (torch CPU) |
| NPU | RKNPU 0.9.2 + librknnrt 1.5.2 |

## Документация

1. [Железо](docs/01-hardware.md)
2. [Установка ОС](docs/02-os-install.md)
3. [Настройка звука](docs/03-audio-setup.md)
4. [Настройка NPU](docs/04-npu-setup.md)
5. [Сборка sherpa-onnx](docs/05-sherpa-onnx-build.md)
6. [VAD и ASR модели](docs/06-vad-asr-models.md)
7. [Обработка аудио](docs/07-audio-processing.md)
8. [Troubleshooting](docs/08-troubleshooting.md)
9. [Синтез речи (TTS)](docs/09-tts.md)
10. [Настройка голоса и интонации](docs/10-voice-tuning.md)

## Быстрый старт

```bash
# Установка зависимостей
bash scripts/install-deps.sh

# Озвучить текст (синтез → динамик ES8316)
./scripts/say.sh "Привет! Я готов."

# Тревожная реплика другой интонацией
./scripts/say.sh -p alert "Внимание! Датчик протечки сработал."

# Проверить, не потеряет ли модель смысл (цифры, латиница)
./scripts/say.sh --check "Напомни через 10 минут"

# Скрипт ассистента (запись 7 секунд → распознавание)
./scripts/listen-and-recognize.sh
```

## Подбор голоса

`tools/voice_lab.py` — стенд для подбора: синтезирует варианты на одном
тексте, выравнивает громкость, сохраняет OGG для прослушивания и пишет
`manifest.json` с замерами.

**Стенд не требует BPI-W3.** Проще всего — готовым образом (ничего ставить
не нужно, кроме Docker):

```bash
# модель (145 МБ, один раз)
docker run --rm -v "$PWD/models:/models" \
  ghcr.io/ilyayukhimets/bpi-w3-voice-assistant fetch-model

# что вообще можно менять
docker run --rm ghcr.io/ilyayukhimets/bpi-w3-voice-assistant list

# что модель вырежет из текста: цифры, латиница
docker run --rm ghcr.io/ilyayukhimets/bpi-w3-voice-assistant \
  check -t "Напомни через 10 минут"

# синтез: результат в ./lab
docker run --rm -v "$PWD/models:/models" -v "$PWD/lab:/app/lab" \
  ghcr.io/ilyayukhimets/bpi-w3-voice-assistant \
  palette -t "Хорошо, включаю свет в гостиной. Нужно что-то ещё?"
```

Или через Compose — с возможностью убрать всё одной командой:

```bash
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml run --rm lab list
docker compose -f docker/docker-compose.yml down --rmi local -v   # удалить всё
```

Или без Docker, напрямую (быстрее для итераций):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
python3 tools/fetch_model.py
python3 tools/voice_lab.py palette --model ./silero_v5_5_ru_ok.pt -t "текст"
```

Команды стенда:

```bash
python3 tools/voice_lab.py list                    # что можно менять (torch не нужен)
python3 tools/voice_lab.py check -t "…"            # ударения + цифры/латиница (torch не нужен)
python3 tools/voice_lab.py palette -t "…"          # палитра рецептов
python3 tools/voice_lab.py compare --voices baya,kseniya,xenia -t "…"
python3 tools/voice_lab.py tune --target -5 -t "…"
python3 tools/voice_lab.py question -t "…?"
```

Тесты нормализации и SSML (без torch):

```bash
python3 tools/test_tts_core.py      # 58 проверок
```

Полное руководство — [docs/10-voice-tuning.md](docs/10-voice-tuning.md).

## Проверенные версии

```
Ядро:          5.10.160-rockchip
RKNPU driver:  0.9.2
librknnrt:     1.5.2
sherpa-onnx:   1.13.8 (собран из исходников)
GigaAM:        v3 (encoder.int8.onnx)
GigaAM punct:  v3 + пунктуация (трансдьюсер, 1025 токенов)
Silero TTS:    v5_5_ru (torch 2.14.1+cpu)
Silero VAD:    v4 (CPU)
```

## Лицензия

MIT
