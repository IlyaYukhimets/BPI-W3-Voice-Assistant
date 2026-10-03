#!/usr/bin/env python3
"""
fetch_model.py — скачать модель Silero v5_5_ru с докачкой и проверкой целостности.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ `curl`. Модель 145 МБ, и её легко получить битой:
обрыв соединения оставляет обрезанный файл, причём РАЗМЕР при этом может
выглядеть правдоподобно. Проверять надо не размер, а контрольную сумму.
Скрипт делает докачку (HTTP Range), сверяет md5 и не оставляет мусора при
ошибке.

Зависимостей нет — только стандартная библиотека, поэтому работает даже там,
где torch ещё не установлен (например, в контейнере на этапе подготовки).

Использование:
    python3 tools/fetch_model.py                    # -> ./silero_v5_5_ru_ok.pt
    python3 tools/fetch_model.py -o /models/model.pt
    python3 tools/fetch_model.py --force            # перекачать заново
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.request

URL = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"
SIZE = 145420684
MD5 = "3f9553af786a7c6da468d436276eebb4"


def md5_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} МБ"


def download(path: str, force: bool = False) -> int:
    if os.path.exists(path) and not force:
        size = os.path.getsize(path)
        if size == SIZE and md5_of(path) == MD5:
            print(f"модель уже на месте и целая: {path}")
            return 0
        print(f"файл есть, но не подходит ({human(size)}) — докачиваю")

    pos = os.path.getsize(path) if os.path.exists(path) else 0
    if pos > SIZE:
        print("файл больше ожидаемого — начинаю заново")
        os.unlink(path)
        pos = 0

    mode = "ab" if pos else "wb"
    req = urllib.request.Request(URL)
    if pos:
        # Докачка: сервер отдаст остаток. Если Range не поддержан, ответ будет
        # 200 и всё скачается заново — обрабатываем это явно.
        req.add_header("Range", f"bytes={pos}-")

    print(f"скачивание с {human(pos)} из {human(SIZE)}...")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status = getattr(r, "status", 200)
            if pos and status != 206:
                print("  сервер не поддержал докачку — качаю с начала")
                pos, mode = 0, "wb"
            total = SIZE
            done = pos
            with open(path, mode) as f:
                while True:
                    b = r.read(1 << 18)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if done % (8 << 20) < (1 << 18):
                        pct = done / total * 100 if total else 0
                        print(f"  {human(done)} / {human(total)}  ({pct:.0f}%)",
                              flush=True)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"обрыв на {human(os.path.getsize(path) if os.path.exists(path) else 0)}: {e}")
        print("перезапусти скрипт — он продолжит с этого места")
        return 1

    size = os.path.getsize(path)
    if size != SIZE:
        print(f"РАЗМЕР НЕ СОВПАЛ: {size} вместо {SIZE}")
        print("перезапусти скрипт для докачки")
        return 1

    print("проверяю контрольную сумму...")
    got = md5_of(path)
    if got != MD5:
        print(f"КОНТРОЛЬНАЯ СУММА НЕ СОВПАЛА: {got} вместо {MD5}")
        print("файл повреждён — удали его и качай заново (--force)")
        return 1

    print(f"готово: {path}")
    print(f"  размер {size} байт, md5 {got} (совпал с эталоном)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Скачать модель Silero v5_5_ru")
    ap.add_argument("-o", "--output", default=None,
                    help="куда сохранить (по умолчанию TTS_MODEL или "
                         "./silero_v5_5_ru_ok.pt)")
    ap.add_argument("--force", action="store_true", help="перекачать заново")
    ap.add_argument("--url", default=URL, help="источник модели")
    args = ap.parse_args()

    path = args.output or os.environ.get("TTS_MODEL") or "./silero_v5_5_ru_ok.pt"
    globals()["URL"] = args.url
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    return download(path, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
