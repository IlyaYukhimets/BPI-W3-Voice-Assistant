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

## Озвучивание (TTS)

### Плата перезагружается каждые ~5 минут при нагрузке

**Причина — не софт.** При `torch.set_num_threads(8)` все ядра RK3588 дают
просадку питания ниже порога → аппаратный сброс.

Признак, отличающий аппаратный сброс от перезагрузки по инициативе ОС:

```bash
sudo journalctl -b -1 --no-pager -o short | grep -iE "systemd-shutdown|Power off"
# пусто => аппаратный сброс, а не выключение через ОС
journalctl --list-boots | tail -5    # загрузки идут очередью
```

Сопутствующий симптом: недописанные WAV-файлы обрезаны —
`wave.Error: fmt chunk and/or data chunk missing`. Размер при этом может
совпадать с ожидаемым, поэтому проверять не размер, а целостность контейнера:
`d.rfind(b'PK\x05\x06')` для ZIP/torch.package, чтение заголовка для WAV.

**Решение:** `torch.set_num_threads(2)`. RTF 0.13–0.18 сохраняется, потери нет —
узкое место не в потоках. В долгих задачах логировать `uptime` в каждой строке
прогресса: сразу видно, когда отсчёт начался заново.

### Юнит `inactive` без ошибки, лог обрывается на середине

`systemd --user` убивает юниты при выходе последней SSH-сессии.

```bash
sudo loginctl enable-linger ubuntu
loginctl show-user ubuntu | grep Linger
```

Дополнительно: `--collect` стирает журнал юнита после выхода — писать вывод
в файл через `--property=StandardOutput=file:...`.

### `ensurepip is not available` при создании venv

`python3-venv` на плате не установлен. Создавать venv через `uv`:

```bash
uv venv ~/models/tts-venv --python 3.10
```

### `TypeError: save_wav() got an unexpected keyword argument 'audio'`

API Silero v5 отличается от примеров для v3/v4. Правильно:

```python
m.save_wav(text=..., speaker=..., audio_path=..., sample_rate=...)
```

Для numpy-обработки — `audio = m.apply_tts(text=..., speaker=..., sample_rate=SR)`,
затем `.detach().cpu().numpy()`.

### `PytorchStreamReader failed reading zip archive`

Модель `v5_5_ru.pt` обрезана. Причина — `curl` на самой плате рвётся при разрыве
SSH-сессии и оставляет неполный файл, при этом размер может совпасть с ожидаемым
(если параллельно шла другая закачка в тот же путь).

Проверка целостности:

```bash
md5sum ~/models/tts/v5_5_ru.pt    # сверить с источником
python3 -c "
d=open('/home/ubuntu/models/tts/v5_5_ru.pt','rb').read()
print('EOCD:', d.rfind(b'PK\x05\x06'), 'из', len(d))"   # -1 => обрезан
```

Лечение: качать на рабочей машине и копировать `scp` по LAN (за секунды),
предварительно убив зависшие `curl` на плате.

### `Slave PCM not usable` / `Broken configuration for this PCM`

Моно-файл на ES8316. Кодек принимает **только стерео** — дублировать канал:

```python
import numpy as np
stereo = np.column_stack([mono, mono]).astype(np.float32)
```

Если колонка выключена или физически отключена, `aplay` вернёт ошибку вывода,
но это не ошибка синтеза — аудио сгенерировано корректно.

### `Failed to set sample rate to 16000` при живом слушании

Использовать `plughw:5,0` вместо `hw:5,0` — ресемплинг делает ALSA.
С `hw:` устройство отдаёт 11025 Гц, и VAD-пайплайн падает.

---

## Системные

### Held packages were changed

**Решение:**

```bash
sudo apt-mark unhold linux-headers-$(uname -r)
sudo apt upgrade -y
sudo apt-mark hold linux-headers-$(uname -r)
```

