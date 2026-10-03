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

**Стенд не требует BPI-W3** — работает на обычном ПК. Режимы `list` и `check`
вообще не используют torch, остальные требуют установки `requirements.txt`.

```bash
# один раз: окружение и модель
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
curl -L -o silero_v5_5_ru_ok.pt https://models.silero.ai/models/tts/ru/v5_5_ru.pt

# что вообще можно менять (без зависимостей)
python3 tools/voice_lab.py list

# что модель вырежет из текста: цифры, латиница (без зависимостей)
python3 tools/voice_lab.py check -t "Напомни через 10 минут"

# с синтезом (нужна модель)
python3 tools/voice_lab.py palette --model ./silero_v5_5_ru_ok.pt -t "текст"
python3 tools/voice_lab.py compare --voices baya,kseniya,xenia -t "текст"
python3 tools/voice_lab.py tune --target -5 -t "текст"
python3 tools/voice_lab.py question -t "Включить свет в спальне?"
```

Тесты нормализации и SSML (без torch):

```bash
python3 tools/test_tts_core.py      # 58 проверок
```

Полное руководство, включая запуск в Docker — [docs/10-voice-tuning.md](docs/10-voice-tuning.md).

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
