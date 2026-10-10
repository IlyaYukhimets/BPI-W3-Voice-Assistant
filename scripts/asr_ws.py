#!/usr/bin/env python3
"""
Клиент резидентного ASR: реплика -> sherpa-onnx-offline-websocket-server.

Зачем: каждый запуск sherpa-onnx-offline платит ~2 с за загрузку моделей
(encoder.int8/decoder/joiner), при этом сам счёт — десятые доли секунды.
Сервер держит модели в памяти; клиент посылает готовый WAV и получает текст.

Протокол (по образцу sherpa-onnx/python-api-examples/offline-websocket-client-…):
4 байта sample rate (LE uint32) + 4 байта размера данных (LE uint32) + float32
сэмплы — чанками по 10240 байт, затем одна строка с результатом, затем "Done".

Запись на USB-микрофоне делает listen-and-recognize.sh (16 кГц, моно, int16) —
частота файла не обязана быть 16 кГц: серверу передаётся она сама.

Адрес/порт: переменные ASR_ADDR (по умолчанию 127.0.0.1) и ASR_PORT (6006).
Если сервер не отвечает — скрипт завершится ошибкой, и say-скрипт уйдёт
холодным путём (как раньше). Печатает распознанный текст в stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

CHUNK = 10240
"""Размер фрейма с данными. Значение из примера sherpa-onnx; смысловой роли,
кроме разбиения на пакеты, не играет."""


def samples_message(sample_rate: int, samples: bytes) -> bytes:
    """Фрейм протокола offline-сервера: rate + длина данных + сами данные.

    Чистая функция — проверяется тестами, чтобы формат не разъехался с
    ожиданиями сервера (uint32 little-endian, размер в БАЙТАХ сэмплов).
    """
    return (sample_rate.to_bytes(4, "little")
            + len(samples).to_bytes(4, "little")
            + samples)


def parse_result(raw: str) -> str:
    """Ответ сервера -> текст реплики. Сервер отвечает JSON или просто строкой."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            for key in ("text", "result", "transcript"):
                val = obj.get(key) if isinstance(obj, dict) else None
                if isinstance(val, str):
                    return val
        except json.JSONDecodeError:
            pass
    return raw


def pcm16_to_float32(samples: bytes) -> bytes:
    """int16-PCM (arecord/сегменты триггер-цикла) -> float32 для протокола.

    Протокол offline-сервера несёт сэмплы float32 в [-1, 1]; сегменты VAD —
    int16. Один раз конвертируем здесь, чтобы клиенты не плодили конверсии.
    """
    import numpy as np
    return (np.frombuffer(samples, dtype=np.int16).astype(np.float32)
            / 32768.0).tobytes()


def gain_pcm16(samples: bytes, gain: float) -> bytes:
    """Программный гейн для int16-PCM с насыщением по диапазону.

    USB-микрофон платы отдаёт тихий сигнал: у silero на такой громкости
    вероятности речи едут 0.2-0.7 и порог 0.5 не срабатывает (проверено
    пробником на плате). Гейн до VAD и ASR ставит вероятности на рабочую
    высоту; при близком голосе настрой под тишину--шёпот--громкость своей
    комнаты (--gain 1..8), клиппинг при переборе хуже недоусиления.
    """
    import numpy as np
    x = np.frombuffer(samples, dtype=np.int16).astype(np.float32) * gain
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


async def recognize(payload: bytes, addr: str, port: int) -> str:
    """Отправить фреймы и получить одну строку результата."""
    import websockets

    async with websockets.connect(f"ws://{addr}:{port}", max_size=None) as ws:
        for i in range(0, len(payload), CHUNK):
            await ws.send(payload[i:i + CHUNK])
        result = await ws.recv()
        if isinstance(result, bytes):
            result = result.decode("utf-8", "replace")
        await ws.send("Done")     # сигнализируем: данных больше не будет
        return parse_result(str(result))


def main() -> int:
    ap = argparse.ArgumentParser(description="Клиент резидентного ASR (websocket)")
    ap.add_argument("wav", help="WAV-файл (моно int16)")
    ap.add_argument("--addr", default=os.environ.get("ASR_ADDR", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ASR_PORT", "6006")))
    args = ap.parse_args()

    try:
        import soundfile as sf
    except ImportError:
        print("нет soundfile (нужен venv ~/models/tts-venv)", file=sys.stderr)
        return 3

    samples, rate = sf.read(args.wav, dtype="float32", always_2d=False)
    if samples.ndim != 1:
        samples = samples.reshape(-1)

    try:
        text = asyncio.run(recognize(samples_message(rate, samples.tobytes()),
                                     args.addr, args.port))
    except OSError as exc:
        print(f"сервер не отвечает: {exc}", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())