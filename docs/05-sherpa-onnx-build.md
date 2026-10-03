# Сборка sherpa-onnx с поддержкой RKNN

## Зависимости

```bash
sudo apt install -y git cmake build-essential libasound2-dev
```

## Заголовки RKNN

sherpa-onnx нужен `rknn_api.h`. Скачайте rknn-toolkit2:

```bash
cd ~
git clone --depth 1 https://github.com/airockchip/rknn-toolkit2.git
```

Путь к заголовкам: `~/rknn-toolkit2/rknpu2/runtime/Linux/librknn_api/include/rknn_api.h`

## Клонирование sherpa-onnx

```bash
cd ~
git clone https://github.com/k2-fsa/sherpa-onnx.git
cd sherpa-onnx
git checkout v1.13.8    # или актуальный тег
```

## Конфигурация CMake

Ключевые флаги:
- `SHERPA_ONNX_ENABLE_RKNN=ON` — включает NPU
- `SHERPA_ONNX_ENABLE_PORTAUDIO=OFF` — **иначе сборка зависает** на скачивании PortAudio с files.portaudio.com
- `BUILD_SHARED_LIBS=ON` — динамические библиотеки

```bash
export CPLUS_INCLUDE_PATH=$HOME/rknn-toolkit2/rknpu2/runtime/Linux/librknn_api/include:$CPLUS_INCLUDE_PATH

mkdir -p build-rknn && cd build-rknn

cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=ON \
  -DSHERPA_ONNX_ENABLE_RKNN=ON \
  -DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF \
  -DCMAKE_INSTALL_PREFIX=./install \
  ..
```

Признак успеха: `-- Configuring done` и `-- Generating done`.

## Сборка

```bash
make -j8
```

- Время: 15–30 минут на RK3588
- Первая сборка долгая (скачивает onnxruntime, espeak-ng, openfst, eigen и т.д.)
- Если зависнет на скачивании — прервать (`Ctrl+C`) и перезапустить `cmake` (кэш частичный)

## Установка

```bash
make install
sudo ldconfig
```

Проверка:
```bash
ldd ./install/bin/sherpa-onnx | grep -E "rknn|onnxruntime"
# librknnrt.so => /lib/librknnrt.so
# libonnxruntime.so => .../install/lib/libonnxruntime.so
```

## Использование

Все утилиты в `~/sherpa-onnx/build-rknn/install/bin/`:
- `sherpa-onnx` — ASR
- `sherpa-onnx-vad` — VAD
- `sherpa-onnx-offline-tts` — TTS
- `sherpa-onnx-offline-denoiser` — шумоподавление
- `sherpa-onnx-keyword-spotter` — wake-word

Полный список — `ls ~/sherpa-onnx/build-rknn/install/bin/`.

## Грабли

### Ошибка `rknn_api.h: No such file or directory`
Не задан `CPLUS_INCLUDE_PATH` или не скачан `rknn-toolkit2`.

### Зависание на скачивании PortAudio
Флаг `-DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF`. PortAudio нужен только для macOS/Windows.

### Зависание на скачивании eigen (gitlab.com)
GitLab иногда недоступен. Перезапустить `cmake` — часто помогает.

### Ошибки компиляции в RKNN-части
Проверить, что `librknnrt.so` версии 1.5.2 или 1.6.0, и заголовки соответствуют.

### Повторная сборка после обновления кода
```bash
cd ~/sherpa-onnx
git pull
cd build-rknn
make -j8 && make install
```

Без необходимости заново `cmake` не запускать — кэш сохранит скачанные зависимости.
