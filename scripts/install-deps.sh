#!/bin/bash
# Установка зависимостей для сборки sherpa-onnx и работы ассистента
set -e

sudo apt update
sudo apt install -y \
  build-essential cmake git \
  libasound2-dev \
  sox libsox-fmt-all \
  alsa-utils \
  curl ca-certificates

# uv — современный менеджер пакетов Python
if ! command -v uv &> /dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  source ~/.bashrc
fi

echo "Готово. Для сборки sherpa-onnx: см. docs/05-sherpa-onnx-build.md"
