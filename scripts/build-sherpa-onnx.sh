#!/bin/bash
# Сборка sherpa-onnx с поддержкой RKNN для RK3588
set -e

SHERPA_VERSION="${1:-v1.13.8}"
WORKDIR="${HOME}/sherpa-onnx"

# 1. Клонируем
if [ ! -d "$WORKDIR" ]; then
  git clone https://github.com/k2-fsa/sherpa-onnx.git "$WORKDIR"
fi
cd "$WORKDIR"
git checkout "$SHERPA_VERSION"

# 2. Указываем путь к заголовкам RKNN
RKNN_HEADERS="$HOME/rknn-toolkit2/rknpu2/runtime/Linux/librknn_api/include"
if [ ! -d "$RKNN_HEADERS" ]; then
  echo "Сначала скачайте rknn-toolkit2:"
  echo "  git clone --depth 1 https://github.com/airockchip/rknn-toolkit2.git ~/rknn-toolkit2"
  exit 1
fi
export CPLUS_INCLUDE_PATH="$RKNN_HEADERS:$CPLUS_INCLUDE_PATH"

# 3. Конфигурируем и собираем
mkdir -p build-rknn && cd build-rknn
cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=ON \
  -DSHERPA_ONNX_ENABLE_RKNN=ON \
  -DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF \
  -DCMAKE_INSTALL_PREFIX=./install \
  ..
make -j$(nproc)
make install

echo ""
echo "Готово: $WORKDIR/build-rknn/install"
