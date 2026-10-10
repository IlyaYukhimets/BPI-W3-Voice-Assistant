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

# Резидентный ASR: сервер держит модели в памяти и экономит ~2 с на реплику
# (каждый запуск sherpa-onnx-offline платит их за загрузку — см. docs/09-tts.md).
# Адрес/порт берутся из окружения; сервер может быть выключен — тогда холодный путь.
ASR_ADDR="${ASR_ADDR:-127.0.0.1}"
ASR_PORT="${ASR_PORT:-6006}"
SCRIPTS="$(dirname "$(readlink -f "$0")")"

mkdir -p "$WORK"
cd "$WORK"

echo "[1/3] Запись ${DURATION} сек..."
arecord -D "$MIC" -f S16_LE -r 48000 -c 1 -d "$DURATION" raw.wav 2>/dev/null

echo "[2/3] Фильтрация sox..."
sox raw.wav ready.wav highpass 300 lowpass 7000 rate -v 16000 norm -3

echo "[3/3] Распознавание ($(basename "$MODEL_DIR"))..."

# Резидентный сервер держит модели в памяти; холодный путь грузит их заново
# (это ~2 с на запуск sherpa-onnx-offline — единственный крупный вклад во время отклика).
TEXT=""
SERVER_OK=0
if timeout 2 bash -c "exec 3<>/dev/tcp/$ASR_ADDR/$ASR_PORT" 2>/dev/null; then
    PYBIN="$HOME/models/tts-venv/bin/python"
    [ -x "$PYBIN" ] || PYBIN=python3
    echo "    (резидентный ASR-сервер, порт $ASR_PORT)"
    # Успех с ПУСТЫМ текстом — законный результат (у микрофона была тишина);
    # уйти холодным путём надо только если сервер НЕ ОТВЕТИЛ, поэтому статус
    # проверяем по коду возврата, а не по пустоте вывода.
    if TEXT=$("$PYBIN" "$SCRIPTS/asr_ws.py" --addr "$ASR_ADDR" --port "$ASR_PORT" \
              ready.wav 2>/dev/null); then
        SERVER_OK=1
    else
        echo "    (!) сервер не ответил, уходим холодным путём"
    fi
else
    echo "    (сервера нет — холодный путь, ~2 с)"
fi

if [ "$SERVER_OK" = 0 ]; then
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
fi

echo "$TEXT" > "$WORK/last_text.txt"
echo ""
echo "РАСПОЗНАНО: $TEXT"

# Опционально: озвучить ответ, если есть say.sh и передан второй аргумент.
if [ -n "${2:-}" ]; then
    SAY="$(dirname "$(readlink -f "$0")")/say.sh"
    [ -x "$SAY" ] && "$SAY" "$2"
fi
