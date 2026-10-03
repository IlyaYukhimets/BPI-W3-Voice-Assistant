# VAD и ASR модели

## VAD — Silero VAD v4

### Скачивание

**Только из релизов `k2-fsa/sherpa-onnx`**, не из `snakers4/silero-vad`:

```bash
cd ~/models
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
# ~629 КБ
```

### Использование (CPU)

```bash
~/sherpa-onnx/build-rknn/install/bin/sherpa-onnx-vad \
  --vad-provider=cpu \
  --silero-vad-model=/home/ubuntu/models/silero_vad.onnx \
  input.wav output.wav
```

**Обратите внимание:** флаг `--vad-provider`, а не `--provider`. Оба существуют, но для VAD — именно `--vad-provider`.

### Признак работы

- `output.wav` > 44 байт (значит, VAD нашёл речь)
- `output.wav` < `input.wav` по размеру (вырезаны паузы)

### Грабли

**44 байта на выходе.** Причины:
1. VAD не видит тишину после речи → не может «закрыть» сегмент. Решение: `sox in.wav out.wav pad 0 2` (добавить тишину в конец).
2. Скачана Silero VAD v5/v6 вместо v4. Решение: скачать из релизов `k2-fsa/sherpa-onnx`.

### VAD на NPU — не работает

Модель `silero-vad-v4-rk3588.rknn` использует операцию `Log`, не поддерживаемую драйвером 0.9.2. Ошибка: `unsupport Log op in current`.

**VAD на CPU — RTF < 0.1**, хватает с запасом. NPU для VAD не нужен.

---

## ASR — GigaAM v3

### Скачивание

Модель от SberDevices, ONNX-версия из репозитория `csukuangfj`:

```bash
cd ~/models
mkdir -p gigaam-v3 && cd gigaam-v3

wget https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/resolve/main/encoder.int8.onnx
wget https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/resolve/main/decoder.onnx
wget https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/resolve/main/joiner.onnx
wget https://huggingface.co/csukuangfj/sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16/resolve/main/tokens.txt
```

Ожидаемые размеры:
- `encoder.int8.onnx` ~225 МБ
- `decoder.onnx` ~3.3 МБ
- `joiner.onnx` ~1.4 МБ
- `tokens.txt` ~13 КБ (если 196 байт — файл битый)

### Использование

```bash
~/sherpa-onnx/build-rknn/install/bin/sherpa-onnx-offline \
  --tokens=./gigaam-v3/tokens.txt \
  --encoder=./gigaam-v3/encoder.int8.onnx \
  --decoder=./gigaam-v3/decoder.onnx \
  --joiner=./gigaam-v3/joiner.onnx \
  --feat-dim=64 \
  --model-type=nemo_transducer \
  input.wav
```

### Критичные параметры

| Параметр | Значение | Почему |
|---|---|---|
| `--feat-dim` | **64** | GigaAM обучена на 64 мел-бинах, не 80 |
| `--model-type` | **nemo_transducer** | Иначе sherpa-onnx использует неверный путь декодирования |
| `input.wav` | 16 кГц, mono | После sox-обработки |

### Если текст пустой

Проверьте:
1. `--feat-dim=64` установлен
2. `--model-type=nemo_transducer` установлен
3. `tokens.txt` весит 13 КБ, не 196 байт
4. WAV имеет речь (проверить через `sox input.wav -n stat`, RMS > 0.02)

### Эталонный тестовый файл

```bash
wget https://huggingface.co/csukuangfj/tmp-files/resolve/main/GigaAM/example.wav -O gigaam_official_example.wav
```

На нём модель должна дать: «Ничьих не требуя похвал, Счастлив уж я надеждой сладкой...». Если на эталоне работает, а на вашей записи нет — проблема в записи, не в модели.

---

## TTS — не сделано

Кандидаты (проверять экспериментально):
- **Piper** (`vits-piper-ru_RU-*`) — быстрый, но интонации ужасные (проверено)
- **Silero TTS** — лучше по интонациям, есть ONNX-экспорт
- **Supertonic** — новое поколение, есть русский

Вывод через `aplay -D plughw:3,0` (ES8316, card 3).

Утилита: `sherpa-onnx-offline-tts`.

---

## Где искать готовые модели

- **Hugging Face:** `csukuangfj/sherpa-onnx-*` — официальные ONNX-модели
- **GitHub Releases:** `k2-fsa/sherpa-onnx` → asr-models, tts-models
- **RKNN-модели:** `csukuangfj/sherpa-onnx-rknn-models` (большинство китайские)
