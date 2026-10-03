#!/bin/bash
# Голосовой ассистент BPI-W3: запись → фильтры → распознавание
set -e

# === Настройки ===
# Модель с пунктуацией (читаемый текст, тот же RTF). Если её нет — обычная v3.
MODEL_DIR="$HOME/models/gigaam-v3-punct"
[ -d "$MODEL_DIR" ] || MODEL_DIR="$HOME/models/gigaam-v3"
SHERPA="$HOME/sherpa-onnx/build-rknn/install/bin/sherpa-onnx-offline"
MIC="plughw:5,0"
WORK="/tmp/assistant"
DURATION="${1:-7}"

mkdir -p "$WORK"
cd "$WORK"

echo "[1/3] Запись ${DURATION} сек..."
arecord -D "$MIC" -f S16_LE -r 48000 -c 1 -d "$DURATION" raw.wav 2>/dev/null

echo "[2/3] Фильтрация sox..."
sox raw.wav ready.wav highpass 300 lowpass 7000 rate -v 16000 norm -3

echo "[3/3] Распознавание ($(basename "$MODEL_DIR"))..."
RESULT=$("$SHERPA" \
  --tokens="$MODEL_DIR/tokens.txt" \
  --encoder="$MODEL_DIR/encoder.int8.onnx" \
  --decoder="$MODEL_DIR/decoder.onnx" \
  --joiner="$MODEL_DIR/joiner.onnx" \
  --feat-dim=64 \
  --model-type=nemo_transducer \
  --num-threads=2 \
  ready.wav 2>/dev/null | grep '"text"' | tail -1)

TEXT=$(echo "$RESULT" | sed 's/.*"text": "\(.*\)".*/\1/')
echo "$TEXT" > "$WORK/last_text.txt"
echo ""
echo "РАСПОЗНАНО: $TEXT"

# Опционально: озвучить ответ, если есть say.sh и передан второй аргумент.
if [ -n "${2:-}" ]; then
    SAY="$(dirname "$(readlink -f "$0")")/say.sh"
    [ -x "$SAY" ] && "$SAY" "$2"
fi
