#!/usr/bin/env python3
"""
Резидентный TTS-воркер: модель живёт в памяти, реплики приходят по unix-сокету.

Зачем: каждый запуск say.sh платит 3.4–3.9 с за загрузку модели Silero, а сам
синтез занимает ~0.16 с. Процесс, в котором модель уже загружена, отвечает за
доли секунды — это единственный кратно ускоряющий сдвиг, потоки дают лишь
10–26% (см. замеры в docs/09-tts.md).

Протокол — unix-сокет, путь через TTSD_SOCKET (по умолчанию ~/.assistant/ttsd.sock).
Клиент шлёт ОДНУ строку ASCII-JSON, заканчивающуюся "\n":

    {"voice": "baya", "ssml": "<speak>…</speak>"}   синтез реплики
    {"cmd": "ping"}                                  жив ли воркер
    {"cmd": "shutdown"}                              выйти чисто

Ответ — одна строка JSON; после синтезирующих запросов следом идут ровно
`samples` байт float32 моно-звука частотой core.SR:

    {"ok": true,  "samples": N, "gen_s": 0.15, "threads": 4} + N*4 байт
    {"ok": false, "error": "…"}                       в случае неудачи

Запуск:  ~/models/tts-venv/bin/python scripts/ttsd.py
Служба:  systemd/*.service из репо → systemctl --user enable --now ttsd asrd
Проверка живого воркера (без синтеза):
    echo '{"cmd": "ping"}' | timeout 2 socat - ~/.assistant/ttsd.sock
Нет socat — нет и ручного пинга: клиент скажет, что воркера нет, и пойдёт
холодным путём. Отказ воркера не ломает работу, только возвращает старую
задержку в 3–4 с.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tts_core as core  # noqa: E402

MAX_LINE = 1024 * 1024
"""Ограничение на строку запроса. SSML реплики измеряется килобайтами; 1 МБ —
граница, за которой не протекает ни запрос, ни атака на память."""


def reply(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def read_line(conn: socket.socket) -> bytes:
    """Читает строку до "\n", не выше MAX_LINE. Превышение -> ValueError."""
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            raise ValueError("соединение закрыто до конца запроса")
        buf.extend(chunk)
        if len(buf) > MAX_LINE:
            raise ValueError("строка запроса длиннее MAX_LINE")
    return bytes(buf)


class State:
    """Модель загружена один раз; запросы сериализуются."""

    def __init__(self, model_path: str):
        t0 = time.time()
        self.model = core.build_model(model_path)
        self.lock = threading.Lock()
        gen = time.time() - t0
        print(f"[ttsd] модель готова за {gen:.1f}с, потоков {core.torch_threads()}", flush=True)

    def synth(self, voice: str, ssml: str) -> tuple[bytes, float]:
        with self.lock:
            t0 = time.time()
            mono = core.synth(self.model, ssml, voice=voice)
            gen = time.time() - t0
        print(f"[ttsd] {voice} | {len(mono) / core.SR:.2f}с | {gen:.2f}с", file=sys.stderr,
              flush=True)
        return mono.tobytes(), gen


def handle(conn: socket.socket, state: State) -> bool:
    """Обрабатывает ОДНО соединение. Возвращает False — серверу пора завершаться."""
    try:
        line = read_line(conn)
    except ValueError as exc:
        reply(conn, {"ok": False, "error": str(exc)})
        return True
    try:
        req = json.loads(line.decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("запрос — не объект")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        reply(conn, {"ok": False, "error": f"мусорный запрос: {exc}"})
        return True

    cmd = req.get("cmd")
    if cmd == "ping":
        reply(conn, {"ok": True, "threads": core.torch_threads()})
        return True
    if cmd == "shutdown":
        reply(conn, {"ok": True})
        return False

    voice = req.get("voice") or core.DEFAULT_VOICE
    ssml = req.get("ssml")
    if not isinstance(ssml, str) or not ssml:
        reply(conn, {"ok": False, "error": "не передан ssml"})
        return True
    try:
        data, gen_s = state.synth(voice, ssml)
    except Exception as exc:  # синтез должен падать ответом, а не рушить воркер
        reply(conn, {"ok": False, "error": str(exc)})
        return True
    # gen_s обязательно в ответе: клиент не знает, где кончается синтез и
    # начинается ожидание, и без него RTF в отчёте обнулится (проверено).
    reply(conn, {"ok": True, "samples": len(data) // 4,
                 "gen_s": round(gen_s, 3),
                 "threads": core.torch_threads()})
    conn.sendall(data)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Резидентный TTS-воркер (Silero)")
    ap.add_argument("--socket", default=None,
                    help="путь unix-сокета (или переменная TTSD_SOCKET)")
    ap.add_argument("--threads", type=int, default=None,
                    help="потоков на синтез (см. tts_core.resolve_threads)")
    args = ap.parse_args()

    path = args.socket or core.worker_socket_path()

    if args.threads is not None:
        os.environ[core.THREADS_ENV] = str(args.threads)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    state = State(core.DEFAULT_MODEL)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Прежний воркер мог быть убит — его сокет остался файлом. Файл без слушателя
    # (ECONNREFUSED) убираем, живой процесс узнаём и выходим с ошибкой.
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        probe.connect(path)
        probe.close()
        print(f"[ttsd] сокет {path} уже отвечает живому воркеру, выхожу", file=sys.stderr)
        return 1
    except OSError:
        pass
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(4)
    print(f"[ttsd] слушаю {path}", file=sys.stderr, flush=True)

    stop = threading.Event()

    def bye(_sig=None, _frame=None) -> None:
        # Ровно два параметра: signum и frame. Обработчик signal.signal всегда
        # зовётся с двумя позиционными; одна заглушка упала с TypeError (проверено).
        stop.set()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    srv.settimeout(0.5)
    # PID-файл — единственно надёжный способ управлять воркером: pkill -f и
    # pgrep -f по слову "ttsd.py" совпадают и с командной строкой самого
    # скрипта-обёртки, после чего обёртка убивает сама себя (проверено).
    pidfile = os.path.join(os.path.dirname(path) or ".", "ttsd.pid")
    with open(pidfile, "w") as fh:
        fh.write(f"{os.getpid()}\n")

    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            with conn:
                conn.settimeout(120)   # реплика может быть длинной
                if not handle(conn, state):
                    stop.set()
        except OSError as exc:
            print(f"[ttsd] соединение сорвалось: {exc}", file=sys.stderr)

    srv.close()
    for f in (path, pidfile):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass
    print("[ttsd] остановлен", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())