#!/bin/sh
# Точка входа образа лаборатории.
#
# Смысл: чтобы не писать каждый раз «python tools/voice_lab.py», команды
# передаются напрямую:
#
#   docker run --rm ... voice-lab list
#   docker run --rm ... voice-lab palette -t "текст"
#   docker run --rm ... voice-lab check -t "через 10 минут"
#
# Есть отдельные псевдокоманды:
#   fetch-model   скачать модель в том /models (с докачкой и проверкой md5)
#   test          прогнать тесты нормализации (torch не нужен)
#   shell         зайти в контейнер
#
# Если команда не распознана, аргументы передаются voice_lab.py как есть —
# так работает любой флаг.

set -eu

MODEL="${TTS_MODEL:-/models/silero_v5_5_ru_ok.pt}"
# Имя для подсказок. В подсказке нельзя использовать $0: там будет
# «entrypoint.sh», что бесполезно пользователю.
SELF="${VOICE_LAB_NAME:-voice-lab}"

# Подкоманды, которые ПРИНИМАЮТ --model (синтез и замеры требуют модель).
takes_model() {
    case "$1" in
        palette|compare|tune|question|say) return 0 ;;
        *) return 1 ;;
    esac
}

# Подкоманды, которые модель НЕ грузят и не должны её требовать.
no_model() {
    case "$1" in
        list|check) return 0 ;;
        *) return 1 ;;
    esac
}

warn_model() {
    if [ ! -f "$MODEL" ]; then
        cat >&2 <<EOF
Модель не найдена: $MODEL

Скачай её (145 МБ, один раз):
  docker run --rm -v "\$PWD/models:/models" $SELF fetch-model

или примонтируй каталог с уже скачанной моделью:
  -v /путь/к/models:/models -e TTS_MODEL=/models/имя_файла.pt
EOF
    fi
}

cmd="${1:-list}"
[ $# -gt 0 ] && shift || true

case "$cmd" in
    fetch-model)
        exec python /app/tools/fetch_model.py -o "$MODEL" "$@"
        ;;
    test)
        exec python /app/tools/test_tts_core.py "$@"
        ;;
    shell)
        exec /bin/sh
        ;;
    list|check)
        # Эти команды не принимают --model: передавать его нельзя, argparse
        # упадёт с «unrecognized arguments».
        exec python /app/tools/voice_lab.py "$cmd" "$@"
        ;;
    palette|compare|tune|question|say)
        warn_model
        exec python /app/tools/voice_lab.py "$cmd" --model "$MODEL" "$@"
        ;;
    -h|--help|help|"")
        exec python /app/tools/voice_lab.py --help
        ;;
    *)
        # Неизвестная команда: возможно, это флаг для say по умолчанию
        # (например, «voice-lab -t "текст"»), а может — системная утилита.
        if command -v "$cmd" >/dev/null 2>&1; then
            exec "$cmd" "$@"
        fi
        warn_model
        exec python /app/tools/voice_lab.py say --model "$MODEL" "$cmd" "$@"
        ;;
esac
