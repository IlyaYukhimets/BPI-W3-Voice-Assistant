# syntax=docker/dockerfile:1
#
# Образ лаборатории подбора голоса (tools/voice_lab.py).
#
# ЗАЧЕМ. Голый `pip install requirements.txt` требует отдельного venv и знания
# про CPU-колёса torch (иначе приезжает несколько гигабайт nvidia-*, ненужных
# для этой задачи). Образ забирает всю эту возню на себя: на машине нужен
# только Docker.
#
# Модель (145 МБ) в образ НЕ включается — она монтируется томом. Так образ
# остаётся переносимым, а модель качается один раз и переиспользуется.
#
# Сборка:  docker build -f docker/Dockerfile -t voice-lab .
# Запуск:  docker run --rm -v ./models:/models -v ./lab:/app/lab \
#            -e TTS_MODEL=/models/silero_v5_5_ru_ok.pt voice-lab list

FROM python:3.12-slim AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /tmp/build

# Отдельный слой под зависимости: при правке кода torch не переустанавливается.
COPY requirements.txt .
# --extra-index-url .../whl/cpu ОБЯЗАТЕЛЕН: без него pip берёт сборку с CUDA.
RUN pip install -r requirements.txt \
      --extra-index-url https://download.pytorch.org/whl/cpu


FROM python:3.12-slim

LABEL org.opencontainers.image.title="BPI-W3 voice lab" \
      org.opencontainers.image.description="Voice and intonation tuning workbench for Silero TTS (Russian)" \
      org.opencontainers.image.source="https://github.com/IlyaYukhimets/BPI-W3-Voice-Assistant" \
      org.opencontainers.image.licenses="MIT"

# libsndfile нужен модулю soundfile. В колёсах manylinux он не встроен.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

WORKDIR /app

COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY scripts/ /app/scripts/
COPY tools/ /app/tools/
COPY docs/ /app/docs/
COPY README.md requirements.txt ./

# Модель по умолчанию — в томе /models, а не в домашнем каталоге.
ENV TTS_MODEL=/models/silero_v5_5_ru_ok.pt \
    VOICE_LAB_NAME=bpi-w3-voice-lab \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Непривилегированный пользователь: инструмент не требует root, а запускать
# сторонний образ от root — лишний риск.
RUN useradd --create-home --uid 1000 lab \
 && mkdir -p /models /app/lab \
 && chown -R lab:lab /app /models \
 && chmod +x /app/docker/entrypoint.sh
USER lab

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["list"]
