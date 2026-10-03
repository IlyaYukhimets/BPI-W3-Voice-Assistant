#!/bin/bash
# say.sh — озвучить текст голосом ассистента (Silero v5_5_ru -> ES8316).
#
# Использование:
#   ./say.sh "Привет, я готов."
#   echo "Текст из пайпа" | ./say.sh
#   ./say.sh -v xenia -s 1.15 "Быстрее и женским голосом"
#   ./say.sh -o /tmp/out.wav "Сохранить без воспроизведения"
#
# Голоса: aidar, baya, kseniya, eugene, xenia (по умолчанию baya).
#
# Замечание по железу: плата BPI-W3 отдаёт звук на кодек ES8316 (card 3),
# который принимает ТОЛЬКО стерео — конвертация внутри tts_say.py.
# Если колонка физически выключена или отключена, скрипт всё равно синтезирует
# и вернёт 0; ошибка вывода (код 3) означает проблему с ALSA/устройством.

set -uo pipefail

VENV="$HOME/models/tts-venv/bin/python"
BACKEND="$(dirname "$(readlink -f "$0")")/tts_say.py"

if [ ! -x "$VENV" ]; then
    echo "say: нет python в $VENV (см. docs/09-tts.md)" >&2
    exit 1
fi
if [ ! -f "$BACKEND" ]; then
    echo "say: не найден $BACKEND" >&2
    exit 1
fi

exec "$VENV" "$BACKEND" "$@"
