# Troubleshooting — грабли, на которые мы наступили

## Запись / звук

### Звук есть, но речь не распознаётся

**Симптом:** RMS < 0.02, `Midline amplitude` > 0.1, спектрограмма — сплошной шум.

**Причина:** низкочастотный гул сервера или DC offset.

**Решение:**

```bash
sox raw.wav ready.wav highpass 300 lowpass 7000 rate -v 16000 norm -3
```

### GTCRN / DPDFNet не помогают

**Симптом:** после denoiser ASR выдаёт мусор.

**Причина:** нейросети на уже чистом сигнале работают как усилитель ошибок,
задавливают форманты речи.

**Решение:** использовать **только sox** (highpass 300 / lowpass 7000).
Нейросети — только если микрофон переедет в шумное место (улица, кухня).

## VAD

### VAD возвращает 44 байта (пустой WAV)

**Причина 1:** VAD не видит тишину после речи → не может «закрыть» сегмент.

**Решение:** добавить тишину в конец:

```bash
sox in.wav out.wav pad 0 2
```

**Причина 2:** скачана модель Silero VAD v5/v6 вместо v4.

**Решение:** скачивать из релизов `k2-fsa/sherpa-onnx`, а не из `snakers4/silero-vad`.

### NPU VAD: unsupport Log op

**Причина:** модель `silero-vad-v4-rk3588.rknn` использует операцию `Log`,
не поддерживаемую драйвером 0.9.2.

**Решение:** VAD на CPU (RTF < 0.1, хватает с запасом).

## sherpa-onnx

### RKNN build падает на rknn_api.h

**Решение:**

```bash
export CPLUS_INCLUDE_PATH=$HOME/rknn-toolkit2/rknpu2/runtime/Linux/librknn_api/include:$CPLUS_INCLUDE_PATH
```

### Invalid option --provider

**Правильные флаги:**

- Для VAD: `--vad-provider=cpu|rknn|cuda|coreml|openvino`
- Для ASR: `--provider=cpu|rknn|cuda|...`

Флаг `--provider` работает только у ASR-утилит.

## ASR GigaAM

### Модель загружается, но текст пустой

**Причина 1:** неправильный `--feat-dim`. У GigaAM — **64**, не 80.

**Причина 2:** отсутствует `--model-type=nemo_transducer`.

**Причина 3:** скачан неполный `tokens.txt` (196 байт вместо 13354).

### Путает слова (кухне → кнотке/кулке)

**Причина:** плохое качество записи после обработки.

**Решение:** использовать sox-фильтры вместо нейросетевых denoiser'ов.

## NPU

### can't request region for resource

**Причина:** драйвер NPU встроен в ядро, нельзя заменить `.ko`.

**Проверка:**

```bash
zcat /proc/config.gz | grep -i rknpu
```

Ожидаемо: `CONFIG_ROCKCHIP_RKNPU=y`.

**Решение:** обновление версии требует пересборки ядра.
Для sherpa-onnx 0.9.2 достаточно.

### rknn_init failed

**Причины:**

1. `librknnrt.so` несовместима с драйвером → использовать 1.5.2 при драйвере 0.9.2.
2. Модель сконвертирована под другую версию runtime.

## Системные

### Held packages were changed

**Решение:**

```bash
sudo apt-mark unhold linux-headers-$(uname -r)
sudo apt upgrade -y
sudo apt-mark hold linux-headers-$(uname -r)
```

