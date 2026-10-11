#!/usr/bin/env python3
"""panel.py — веб-панель ассистента: схема, службы, фразы, журналы, доки.

ЗАЧЕМ. Проект вырос: три резидентные службы, слой KWS, модель ASR, папка
сегментов, журналы. Панель показывает состояние в одном месте: схему тракта
(кто с кем связан), службы с памятью, последние фразы с уровнями и текстом,
историю уровней, журналы в три столбца и документацию во всплывающем окне.

ПОЧЕМУ БЕЗ ЗАВИСИМОСТЕЙ. Только стандартная библиотека (http.server) и
системные команды, которые на плате уже есть (systemctl/journalctl). Ни БД, ни
шаблонизатора, ни сборки.

ЧТО МОЖЕТ МЕНЯТЬ. По умолчанию — НИЧЕГО (только чтение). Действия (перезапуск
службы, тестовая озвучка, прогон ASR по последнему сегменту) включаются файлом
с ключом:

    head -c 24 /dev/urandom | base64 > ~/.assistant/panel.token

и вводятся в панели в поле «ключ». Без файла ручки отвечают 403 и в интерфейсе
неактивны.

Запуск (user-юнит panel.service):
    ~/models/tts-venv/bin/python scripts/panel.py --port 8080
Открывается: http://<ip платы>:8080
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assistant   # noqa: E402  — варианты сигнала и значение по умолчанию

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNITS = ("ttsd", "asrd", "voiced")
SPOOL = os.path.expanduser("~/.assistant/segments")
TOKEN_FILE = os.path.expanduser("~/.assistant/panel.token")
PYTHON = os.path.expanduser("~/models/tts-venv/bin/python")
ASR_PORT, TTS_PORT = 6006, 6007
HISTORY = deque(maxlen=400)      # уровни фраз: для графика по времени


def run(cmd: list[str], timeout: float = 5.0) -> str:
    """Выполнить команду и вернуть stdout (ошибки — пустая строка)."""
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# Сбор данных: чистые функции, их и проверяют тесты.
# ---------------------------------------------------------------------------
def parse_show(text: str) -> dict:
    """Вывод `systemctl show -p K=V` -> словарь."""
    props = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def rss_mb(pid: str) -> float | None:
    """Память процесса из /proc: systemd для user-юнитов её не отдаёт."""
    try:
        with open(f"/proc/{int(pid)}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError):
        pass
    return None


def exec_from_cat(text: str) -> str:
    """ExecStart из `systemctl cat`, склеенный по переносам строк.

    В юнитах команда почти всегда разбита через «\\» (у нас — пять строк).
    Если читать только первую строку, все настройки теряются: схема показывала
    умолчания вместо реального устройства и форм имени, а кнопка «сказать имя»
    не знала, как зовут ассистента.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("ExecStart="):
            continue
        parts = [line.split("=", 1)[1].strip()]
        while parts[-1].endswith("\\") and index + 1 < len(lines):
            parts[-1] = parts[-1][:-1].rstrip()
            index += 1
            parts.append(lines[index].strip())
        return " ".join(part for part in parts if part)
    return ""


def units_state(names=UNITS) -> list[dict]:
    result = []
    for name in names:
        props = parse_show(run(["systemctl", "--user", "show",
                                "-p", "ActiveState,SubState,MainPID,"
                                      "ExecMainStartTimestamp,NRestarts", name]))
        pid = props.get("MainPID", "0")
        exec_line = exec_from_cat(run(["systemctl", "--user", "cat", name]))
        result.append({
            "name": name,
            "active": props.get("ActiveState", "unknown"),
            "sub": props.get("SubState", ""),
            "pid": pid if pid not in ("0", "") else None,
            "restarts": int(props.get("NRestarts", "0") or 0),
            "since": props.get("ExecMainStartTimestamp", ""),
            "rss_mb": rss_mb(pid) if pid not in ("0", "") else None,
            "exec": exec_line,
        })
    return result


NOISE = re.compile(r"onnxruntime|device_discovery|LOG \(Vosk|WARNING|"
                   r"systemd\[|Started |Stopped |Stopping |Consumed |Main PID")


def clean_lines(text: str) -> list[str]:
    """Убрать служебный шум onnxruntime/Vosk/systemd — он забивает журнал."""
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or NOISE.search(line):
            continue
        if "]: " in line:
            line = line.split("]: ", 1)[1]     # «ubuntu python[1]: текст» -> текст
        out.append(line)
    return out


def journal(unit: str, lines: int = 60) -> list[str]:
    return clean_lines(run(["journalctl", "--user", "-u", unit, "-n", str(lines),
                            "--no-pager", "-o", "cat"]))[-lines:]


SEG_RE = re.compile(r"^\[seg\] (\d+): ([\d.]+)с, речь ([\d.]+)с, "
                    r"пик (-?\d+) дБ \((\w+)\)$")
TEXT_RE = re.compile(r"^(\d+): (.+?)\s+\[ASR ([\d.]+) с\]$")
FAIL_RE = re.compile(r"^\[asr\] (\d+): не расслышал \(([\d.]+) с, пик (-?\d+) дБ\)")
KWS_RE = re.compile(r"^\[kws\] услышал имя \((\d+) кадров прослушано, "
                    r"срабатываний (\d+)(?:, громкость имени (-?\d+) дБ)?\)$")
SILENT_RE = re.compile(r"^\[kws\] команды не последовало за ([\d.]+) с "
                       r"\(шум (\d+), громче всего (\d+), порог (\d+)\)$")


def parse_events(lines: list[str]) -> dict:
    """Разобрать журнал voiced: фразы, срабатывания KWS, отказы.

    Фразы собираются по НОМЕРУ сегмента: [seg] даёт уровни, «N: текст» —
    распознанное, «[asr] N: не расслышал» — неудачу. Так панель показывает то же,
    что в журнале, не гоняя распознавание заново.
    """
    segs: dict[int, dict] = {}
    order: list[int] = []
    kws = {"hits": 0, "frames": 0, "name_db": None, "waited": 0, "last_margin": None}
    replies: list[str] = []

    for line in lines:
        if (m := KWS_RE.match(line)):
            kws["frames"] = int(m.group(1))
            kws["hits"] = int(m.group(2))
            if m.group(3):
                kws["name_db"] = int(m.group(3))
            continue
        if (m := SILENT_RE.match(line)):
            kws["waited"] += 1
            kws["last_margin"] = {"noise": int(m.group(2)),
                                  "loudest": int(m.group(3)),
                                  "threshold": int(m.group(4))}
            continue
        if (m := SEG_RE.match(line)):
            index = int(m.group(1))
            if index not in segs:
                order.append(index)
            segs.setdefault(index, {})["levels"] = {
                "dur": float(m.group(2)), "speech": float(m.group(3)),
                "peak": int(m.group(4)), "status": m.group(5)}
            continue
        if (m := TEXT_RE.match(line)):
            index = int(m.group(1))
            if index not in segs:
                order.append(index)
            segs.setdefault(index, {}).update({"text": m.group(2),
                                               "asr_s": float(m.group(3))})
            continue
        if (m := FAIL_RE.match(line)):
            index = int(m.group(1))
            if index not in segs:
                order.append(index)
            segs.setdefault(index, {}).update({"text": "", "asr_s": float(m.group(2)),
                                               "failed": True})
            continue
        if line.startswith("[reply]"):
            replies.append(line[len("[reply] "):])
            continue
    segments = [dict(index=i, **segs[i]) for i in order]
    return {"segments": segments[-12:], "kws": kws, "replies": replies[-3:]}


def add_history(segments: list[dict]) -> None:
    """Дописать уровни новых фраз в историю (для графика по времени)."""
    known = {h["index"] for h in HISTORY}
    for seg in segments:
        levels = seg.get("levels") or {}
        if seg["index"] in known or "peak" not in levels:
            continue
        HISTORY.append({"index": seg["index"], "peak": levels["peak"],
                        "speech": levels.get("speech", 0.0),
                        "text": seg.get("text", ""),
                        "time": time.strftime("%H:%M")})
        known.add(seg["index"])


def spool_files(limit: int = 8) -> list[dict]:
    """Что лежит на диске: имена с временем, размер, время изменения."""
    try:
        names = sorted(os.listdir(SPOOL),
                       key=lambda n: os.path.getmtime(os.path.join(SPOOL, n)))
    except OSError:
        return []
    out = []
    for name in names[-limit:]:
        path = os.path.join(SPOOL, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": name, "kb": round(st.st_size / 1024.0, 1),
                    "time": time.strftime("%H:%M:%S", time.localtime(st.st_mtime))})
    return out


def last_segment() -> str | None:
    """Самый свежий сегмент в spool — по нему прогоняем ASR из панели."""
    files = spool_files(limit=1)
    return os.path.join(SPOOL, files[-1]["name"]) if files else None


def models() -> list[dict]:
    """Модели и окружение: есть ли файлы и сколько занимают."""
    checks = [
        ("Vosk (имя)", os.path.expanduser("~/models/vosk-model-small-ru-0.22")),
        ("GigaAM (команда)", os.path.expanduser("~/models/gigaam-v3-punct")),
        ("Silero TTS (голос)", os.path.expanduser("~/models/tts/silero_v5_5_ru_ok.pt")),
    ]
    out = []
    for label, path in checks:
        if os.path.isdir(path):
            size = sum(os.path.getsize(os.path.join(dp, f))
                       for dp, _, files in os.walk(path) for f in files)
        elif os.path.exists(path):
            size = os.path.getsize(path)
        else:
            size = 0
        out.append({"label": label, "path": path, "ok": size > 0,
                    "mb": round(size / 1048576.0, 1)})
    return out


def host_info() -> dict:
    info = {"host": run(["hostname"]).strip() or "?"}
    ips = run(["hostname", "-I"]).split()
    info["ip"] = next((ip for ip in ips if ip.startswith("192.168.")),
                      ips[0] if ips else "?")
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            up = float(fh.read().split()[0])
        info["uptime"] = (f"{int(up // 86400)} д " if up >= 86400 else "") + \
                         f"{int(up % 86400 // 3600)} ч {int(up % 3600 // 60)} мин"
    except (OSError, ValueError):
        info["uptime"] = "?"
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in fh if ":" in l}
        info["mem_total_mb"] = round(mem["MemTotal"] / 1024)
        info["mem_avail_mb"] = round(mem["MemAvailable"] / 1024)
    except (OSError, KeyError, ValueError):
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as fh:
            info["temp_c"] = round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    return info


def exec_option(exec_line: str, name: str, default: str = "") -> str:
    """Значение ключа из ExecStart: `--key значение` или `--key "значение"`.

    Сначала ищем форму в кавычках (в ней бывают пробелы — список имён), и только
    потом одно слово: без этого `--device plughw:5,0 --kws-phrases "астра"`
    отдавало бы всё до кавычки.
    """
    text = exec_line or ""
    quoted = re.search(rf'{re.escape(name)}\s+"([^"]*)"', text)
    if quoted:
        return quoted.group(1).strip()
    bare = re.search(rf"{re.escape(name)}\s+(\S+)", text)
    return bare.group(1).strip() if bare else default


def wake_phrases(exec_line: str) -> list[str]:
    """Формы имени из ExecStart юнита: нужны схеме и кнопке «сказать имя»."""
    return [p.strip() for p in exec_option(exec_line, "--kws-phrases", "астра")
            .split(",") if p.strip()]


def pipeline(units: list[dict], kws_stats: dict) -> list[dict]:
    """Схема тракта с живым состоянием на каждой ступени."""
    by_name = {u["name"]: u for u in units}
    voiced = by_name.get("voiced", {})
    options = voiced.get("exec", "")
    mic = exec_option(options, "--device", "plughw:5,0")
    phrases = wake_phrases(options)
    return [
        {"stage": "Микрофон", "detail": mic,
         "state": "ok" if voiced.get("active") == "active" else "bad"},
        {"stage": "Vosk-KWS (имя)",
         "detail": ", ".join(phrases) + f"; срабатываний {kws_stats.get('hits', 0)}",
         "state": "ok" if kws_stats.get("hits") else "wait"},
        {"stage": "Сигнал", "detail": "тон 880 Гц, 0.12 с", "state": "ok"},
        {"stage": "Запись команды", "detail": "энергетический детектор",
         "state": "ok"},
        {"stage": "GigaAM (команда)", "detail": f"порт {ASR_PORT}",
         "state": "ok" if by_name.get("asrd", {}).get("active") == "active"
                  else "bad"},
        {"stage": "Голос (Silero TTS)", "detail": f"порт {TTS_PORT}",
         "state": "ok" if by_name.get("ttsd", {}).get("active") == "active"
                  else "bad"},
        {"stage": "Колонка", "detail": "plughw:3,0 (ES8316)", "state": "ok"},
    ]


# ---------------------------------------------------------------------------
# Действия (только при наличии ключа) и документы
# ---------------------------------------------------------------------------
DOC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.md$")


def doc_path(name: str) -> str | None:
    """Путь к документу по имени или None. Имя проверяется: выход из каталога
    (../, абсолютные пути, чужие расширения) отсекается по шаблону."""
    if not name or not DOC_RE.match(name):
        return None
    path = os.path.join(REPO, "docs", name)
    return path if os.path.isfile(path) else None


def load_token(path: str = TOKEN_FILE) -> str:
    """Ключ доступа из файла; нет файла — действия выключены."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def check_token(given: str | None, expected: str) -> str:
    """ok — можно выполнять, disabled — ключа нет на плате, denied — не тот.

    Сравниваем БАЙТЫ: secrets.compare_digest не принимает строки с не-ASCII
    символами (падает с TypeError), а ключ в файле может быть любым.
    """
    if not expected:
        return "disabled"
    if given and secrets.compare_digest(given.strip().encode("utf-8"),
                                        expected.encode("utf-8")):
        return "ok"
    return "denied"


def name_from_unit() -> str:
    """Имя ассистента из настроек юнита — то, что реально настроено."""
    return wake_phrases(exec_from_cat(run(["systemctl", "--user", "cat",
                                           "voiced"])))[0]


def do_action(payload: dict, token: str) -> dict:
    """Выполнить действие из панели. Возвращает {ok, message}."""
    action = payload.get("action")
    if action == "restart":
        unit = payload.get("unit", "")
        if unit not in UNITS + ("panel",):
            return {"ok": False, "message": f"неизвестная служба: {unit}"}
        out = run(["systemctl", "--user", "restart", unit], timeout=20)
        return {"ok": True, "message": f"{unit}: перезапуск отправлен"}
    if action == "tone":
        kind = str(payload.get("kind") or assistant.DEFAULT_WAKE_TONE)
        if kind not in assistant.WAKE_TONES:
            return {"ok": False, "message": f"неизвестный сигнал: {kind}"}
        rc = assistant.beep(kind=kind)
        return {"ok": rc == 0,
                "message": (f"сигнал «{kind}» проигран" if rc == 0
                            else f"сигнал «{kind}» не прозвучал (aplay rc={rc})")}

    if action in ("say", "say_name"):
        text = str(payload.get("text", ""))[:200].strip()
        if action == "say_name" and not text:
            text = name_from_unit()          # кнопка «сказать имя»
        if not text:
            return {"ok": False, "message": "пустой текст"}
        # Список аргументов, без shell: текст не может стать командой.
        proc = subprocess.run([PYTHON, os.path.join(REPO, "scripts", "tts_say.py"),
                               text], capture_output=True, timeout=60)
        ok = proc.returncode == 0
        return {"ok": ok, "message": (f"сказано: «{text}»" if ok else
                                      f"озвучка вернула код {proc.returncode}")}
    if action == "asr":
        path = last_segment()
        if not path:
            return {"ok": False, "message": "в spool нет сегментов"}
        import voiced                       # тот же путь, что в рабочем цикле
        with open(path, "rb") as fh:
            import wave
            with wave.open(path, "rb") as w:
                pcm = w.readframes(w.getnframes())
        text, took = voiced.recognize_segment(path, pcm, "127.0.0.1", ASR_PORT)
        return {"ok": bool(text.strip()),
                "message": (f"«{text}» за {took:.2f} с" if text.strip()
                            else f"не расслышал ({took:.2f} с)")}
    return {"ok": False, "message": f"неизвестное действие: {action}"}


def state(token: str) -> dict:
    units = units_state()
    events = parse_events(journal("voiced", 120))
    add_history(events["segments"])
    voiced_exec = next((u["exec"] for u in units if u["name"] == "voiced"), "")
    return {
        "host": host_info(),
        "units": units,
        "pipeline": pipeline(units, events["kws"]),
        "segments": events["segments"],
        "kws": events["kws"],
        "replies": events["replies"],
        "history": list(HISTORY)[-80:],
        "spool": spool_files(),
        "models": models(),
        "logs": {u: journal(u, 40) for u in UNITS},
        "docs": sorted(f for f in os.listdir(os.path.join(REPO, "docs"))
                       if f.endswith(".md")),
        "actions": bool(token),
        "wake_phrases": wake_phrases(voiced_exec),
        "tones": list(assistant.WAKE_TONES),
        "tone": exec_option(voiced_exec, "--wake-tone",
                             assistant.DEFAULT_WAKE_TONE),
        "ts": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Голосовой ассистент — панель</title>
<style>
:root{--bg:#12151a;--card:#1b2027;--fg:#e6e9ee;--dim:#8b95a5;--ok:#3ecf8e;
--bad:#ef5f5f;--wait:#e0a94a;--line:#2a313b;--code:#161a20}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;
align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:600}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
margin:0 0 10px;display:flex;gap:8px;align-items:center}
/* Компоновка нарочно не авто-адаптивная: при repeat(auto-fit) блоки
   разной высоты оставляли дыры. Здесь фиксированная двухколоночная схема по
   смыслу: слева «что происходит», справа «чем управлять», снизу на всю ширину
   журналы и документы. */
main{padding:16px 20px;display:flex;flex-direction:column;gap:14px}
.cols{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:14px;
align-items:start}
.col{display:flex;flex-direction:column;gap:14px;min-width:0}
@media(max-width:1000px){.cols{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;min-width:0}
textarea{width:100%;background:#232a33;border:1px solid var(--line);color:var(--fg);
border-radius:6px;padding:8px 10px;font:13px/1.45 system-ui;resize:vertical;
min-height:56px}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.hint{color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.45}
select{background:#232a33;border:1px solid var(--line);color:var(--fg);
border-radius:6px;padding:6px 8px;font:13px system-ui;min-width:110px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid var(--line);border-radius:12px;padding:2px 9px;font-size:12px;
cursor:pointer;color:#9db6d8}
.chip:hover{border-color:#3d4753;color:#cfe0f5}
.retry{padding:1px 7px;font-size:12px;line-height:1.35}
.logs{display:grid;gap:12px;grid-template-columns:repeat(3,minmax(0,1fr))}
@media(max-width:900px){.logs{grid-template-columns:1fr}}
.pipe{display:flex;flex-wrap:wrap;gap:6px;align-items:stretch}
.step{flex:1 1 120px;border:1px solid var(--line);border-radius:8px;padding:7px 9px}
.step b{display:block;font-size:13px}
.step span{color:var(--dim);font-size:12px}
.step.ok{border-left:3px solid var(--ok)}
.step.bad{border-left:3px solid var(--bad)}
.step.wait{border-left:3px solid var(--wait)}
.arrow{align-self:center;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;padding:3px 6px;
border-bottom:1px solid var(--line)}
td{padding:3px 6px;border-bottom:1px solid #20262e;vertical-align:top}
.mono{font-family:ui-monospace,monospace}
.bar{height:7px;border-radius:4px;background:#2a313b;position:relative;min-width:50px}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:4px;background:var(--ok)}
.dim{color:var(--dim)}.miss{color:var(--wait)}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;
border:1px solid var(--line)}
.tag.ok{color:var(--ok);border-color:#2b5f45}
.tag.bad{color:var(--bad);border-color:#5f2b2b}
pre{margin:0;padding:8px;background:var(--code);border-radius:6px;max-height:230px;
overflow:auto;font-size:11.5px;font-family:ui-monospace,monospace;white-space:pre-wrap;
word-break:break-word}
pre .t{color:#5c6675}
.lhit{color:var(--ok)}.lmiss{color:var(--wait)}.lerr{color:var(--bad)}
button{background:#232a33;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer}
button:hover{border-color:#3d4753}
button:disabled{opacity:.45;cursor:not-allowed}
.doclink{cursor:pointer;color:#7fb3ff;font-size:12.5px;display:block;padding:1px 0}
#modal{position:fixed;inset:0;background:rgba(6,8,11,.72);display:none;
align-items:center;justify-content:center;padding:24px;z-index:20}
#modal.on{display:flex}
#doc{background:var(--card);border:1px solid var(--line);border-radius:12px;
max-width:900px;width:100%;max-height:84vh;overflow:auto;padding:20px 24px}
#doc h3{margin:14px 0 6px;font-size:15px}
#doc h4{margin:12px 0 4px;font-size:14px;color:#cfd6df}
#doc code{background:var(--code);padding:1px 5px;border-radius:4px;
font-family:ui-monospace,monospace;font-size:12.5px}
#doc pre{background:var(--code);padding:10px;overflow:auto;font-size:12px;
border-radius:6px}
#doc ul{margin:6px 0;padding-left:20px}
#doc a{color:#7fb3ff}
#doc table{margin:8px 0}
#doc .close{float:right;cursor:pointer;color:var(--dim)}
#key{width:110px;background:#232a33;border:1px solid var(--line);color:var(--fg);
border-radius:6px;padding:3px 7px;font-size:12px}
#note{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);
background:#232a33;border:1px solid var(--line);border-radius:8px;padding:8px 14px;
font-size:13px;display:none;z-index:30}
#note.on{display:block}
</style></head><body>
<header>
  <h1>🎙 Голосовой ассистент «Астра»</h1>
  <span id="host" class="dim"></span><span id="ts" class="dim"></span>
  <span style="margin-left:auto" class="dim">ключ: <input id="key" type="password"
  placeholder="для действий"></span>
</header>
<main>
<section><h2>Схема тракта</h2><div class="pipe" id="pipe"></div></section>
<div class="cols">
  <div class="col">
    <section><h2>Уровни фраз</h2>
      <svg id="chart" width="100%" height="150" viewBox="0 0 400 150"
        preserveAspectRatio="none"></svg>
      <div class="hint" id="chart-note"></div></section>
    <section><h2>Последние фразы</h2><table id="segments"></table></section>
    <section><h2>KWS и пропуски</h2><pre id="kws"></pre></section>
  </div>
  <div class="col">
    <section><h2>Службы</h2><table id="units"></table>
      <div class="hint">⟳ — перезапуск службы (нужен ключ)</div></section>
    <section><h2>Действия <span id="act-state" class="dim"
      style="text-transform:none;letter-spacing:0"></span></h2>
      <textarea id="phrase" placeholder="фраза для теста озвучки — Enter, чтобы
        сказать (Shift+Enter — перенос строки)"></textarea>
      <div class="btns">
        <button class="act" data-act="say">сказать</button>
        <button class="act" data-act="say_name">сказать имя</button>
        <button class="act" data-act="asr">ASR по последнему</button>
      </div>
      <div class="btns">
        <select id="tone"></select>
        <button class="act" data-act="tone">прослушать сигнал</button>
        <span class="dim mono" id="tone-cur" style="font-size:11.5px;
          align-self:center"></span>
      </div>
      <div class="hint"><b>Сигнал после имени:</b> chime — две ноты вверх,
        blip — один скользящий, soft — мягкая нота пониже, tone — прежний тон
        880 Гц. Выбери в списке и нажми «прослушать» — услышишь его в колонке;
        каким он играется в цикле, написано рядом. «Сказать имя» выводит на
        колонку имя ассистента: он услышит себя через микрофон, сработает KWS и
        даст сигнал — это сквозная проверка тракта.</div></section>
    <section><h2>Модели</h2><table id="models"></table></section>
    <section><h2>Файлы сегментов</h2><table id="spool"></table></section>
  </div>
</div>
<section><h2>Журналы</h2>
  <div class="logs">
    <div><div class="dim mono" style="font-size:11.5px;margin-bottom:4px">voiced</div>
      <pre id="log-voiced"></pre></div>
    <div><div class="dim mono" style="font-size:11.5px;margin-bottom:4px">asrd</div>
      <pre id="log-asrd"></pre></div>
    <div><div class="dim mono" style="font-size:11.5px;margin-bottom:4px">ttsd</div>
      <pre id="log-ttsd"></pre></div>
  </div></section>
<section><h2>Документы</h2><div class="chips" id="docs"></div></section>
</main>
<div id="modal"><div id="doc"><span class="close">✕ закрыть</span><div id="doc-body"></div></div></div>
<div id="note"></div>
<script>
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const $ = id => document.getElementById(id);
const keyInput = $('key');
keyInput.value = localStorage.getItem('panelKey') || '';
keyInput.addEventListener('input', () => localStorage.setItem('panelKey', keyInput.value));

function note(msg, bad){
  const el = $('note');
  el.textContent = msg;
  el.style.borderColor = bad ? '#5f2b2b' : '#2b5f45';
  el.classList.add('on');
  clearTimeout(note.t);
  note.t = setTimeout(() => el.classList.remove('on'), 6000);
}

/* Своя мини-разметка: заголовки, списки, код, жирный, ссылки — без библиотек. */
function md(text){
  const lines = String(text).split('\n');
  let html = '', inCode = false, inList = false, inTable = false;
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  const closeTable = () => { if (inTable) { html += '</table>'; inTable = false; } };
  for (const raw of lines){
    const line = raw.replace(/\s+$/, '');
    if (line.startsWith('```')){
      closeList(); closeTable();
      html += inCode ? '</pre>' : '<pre>';
      inCode = !inCode; continue;
    }
    if (inCode){ html += esc(raw) + '\n'; continue; }
    if (/^\s*$/.test(line)){ closeList(); closeTable(); continue; }
    if (/^\|/.test(line)){
      closeList();
      const cells = line.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      if (!inTable){ inTable = true; html += '<table>'; }
      if (/^[-: ]+$/.test(cells.join(''))) continue;
      const tag = html.endsWith('<table>') ? 'th' : 'td';
      html += '<tr>' + cells.map(c => `<${tag}>${inline(c)}</${tag}>`).join('') + '</tr>';
      continue;
    }
    closeTable();
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h){ closeList(); html += `<h4>${inline(h[2])}</h4>`; continue; }
    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (li){ if (!inList){ html += '<ul>'; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    closeList();
    html += `<p>${inline(line)}</p>`;
  }
  if (inCode) html += '</pre>';
  closeList(); closeTable();
  return html;
}

async function openDoc(name){
  try {
    const r = await fetch('/api/doc?name=' + encodeURIComponent(name));
    const d = await r.json();
    $('doc-body').innerHTML = `<h3>${esc(d.name)}</h3>` + (d.text ? md(d.text) : 'нет файла');
  } catch(e){ $('doc-body').innerHTML = 'не удалось загрузить документ'; }
  $('modal').classList.add('on');
}
$('modal').addEventListener('click', e => { if (e.target.id === 'modal') $('modal').classList.remove('on'); });
document.querySelector('#doc .close').addEventListener('click', () => $('modal').classList.remove('on'));
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('modal').classList.remove('on'); });

let wakeName = 'астра';        // из ExecStart: кнопка «сказать имя»

async function act(btn){
  const action = btn.dataset.act;
  const body = {action};
  if (action === 'restart') body.unit = btn.dataset.unit;
  if (action === 'say') body.text = $('phrase').value;
  if (action === 'say_name'){ body.action = 'say'; body.text = wakeName; }
  if (action === 'tone') body.kind = $('tone').value;
  btn.disabled = true;
  try {
    const r = await fetch('/api/action', {method:'POST',
      headers:{'Content-Type':'application/json','X-Panel-Token':keyInput.value},
      body: JSON.stringify(body)});
    const d = await r.json();
    note(d.message + (d.ok ? '' : ' — ошибка'), !d.ok);
  } catch(e){ note('панель не ответила', true); }
  btn.disabled = false;
  tick();
}
document.addEventListener('click', e => { if (e.target.classList.contains('act')) act(e.target); });
$('phrase').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); act({dataset:{act:'say'}}); }
});

function drawChart(hist){
  const svg = $('chart');
  const pts = hist || [];
  if (pts.length < 2){ svg.innerHTML = '<text x="8" y="60" fill="#8b95a5" '
    + 'font-size="11">мало данных: нужно хотя бы две фразы</text>'; return; }
  const w = 400, h = 120, pad = 6;
  const y = db => pad + (1 - Math.max(0, Math.min(1, (db + 60) / 60))) * (h - 2 * pad);
  const step = (w - 2 * pad) / (pts.length - 1);
  let line = '', marks = '';
  pts.forEach((p, i) => { line += `${i ? 'L' : 'M'}${(pad + i * step).toFixed(1)},${y(p.peak).toFixed(1)} `; });
  pts.forEach((p, i) => {
    const x = pad + i * step, yy = y(p.peak);
    marks += `<circle cx="${x.toFixed(1)}" cy="${yy.toFixed(1)}" r="2.2"
      fill="${p.text ? '#3ecf8e' : '#e0a94a'}"><title>${esc(p.time)} · ${p.peak} дБ ·
      ${esc(p.text || 'не расслышал')}</title></circle>`;
  });
  let grid = '';
  for (const db of [0, -20, -40, -60]) {
    grid += `<line x1="0" y1="${y(db)}" x2="${w}" y2="${y(db)}" stroke="#232a33"/>`
      + `<text x="3" y="${y(db) - 2}" fill="#5c6675" font-size="9">${db} дБ</text>`;
  }
  svg.innerHTML = grid
    + `<path d="${line}" fill="none" stroke="#7fb3ff" stroke-width="1.4"/>${marks}`;
}

async function tick(){
  let d;
  try { d = await (await fetch('/api/state')).json(); }
  catch(e){ $('ts').textContent = 'нет связи с панелью'; return; }
  const h = d.host || {};
  $('host').textContent = `${h.host} · ${h.ip} · up ${h.uptime} · RAM `
    + `${h.mem_avail_mb}/${h.mem_total_mb} МБ` + (h.temp_c ? ` · ${h.temp_c} °C` : '');
  $('ts').textContent = 'обновлено ' + d.ts;
  $('pipe').innerHTML = (d.pipeline||[]).map((p,i) =>
    (i ? '<div class="arrow">→</div>' : '') +
    `<div class="step ${p.state}"><b>${esc(p.stage)}</b><span>${esc(p.detail)}</span></div>`
  ).join('');
  $('units').innerHTML = '<tr><th>служба</th><th>состояние</th><th>PID</th>'
    + '<th>RSS</th><th>⟳</th></tr>' + (d.units||[]).map(u =>
    `<tr><td class="mono">${esc(u.name)}</td><td><span class="tag
    ${u.active==='active'?'ok':'bad'}">${esc(u.active)}</span></td>
    <td class="mono">${u.pid||'-'}</td><td class="mono">${u.rss_mb?u.rss_mb+' МБ':'-'}</td>
    <td><button class="act retry" data-act="restart" data-unit="${esc(u.name)}"
    title="перезапустить">⟳</button></td></tr>`).join('');
  drawChart(d.history);
  $('chart-note').textContent = `точки: ${(d.history||[]).length} · зелёная — фраза
    распознана, жёлтая — нет · по вертикали пик, дБ (-60…0)`;
  $('segments').innerHTML = '<tr><th>№</th><th>речь</th><th>пик</th><th>распознано</th></tr>'
    + (d.segments||[]).slice().reverse().map(s => {
      const l = s.levels || {}, peak = l.peak ?? -99;
      const w = Math.max(2, Math.min(100, 100 + peak * 3));
      return `<tr><td class="mono">${s.index}</td><td class="mono">${l.speech ?? '-'} с</td>
      <td><div class="bar"><i style="width:${w}%"></i></div>
      <span class="dim mono">${peak} дБ</span></td>
      <td>${s.text ? esc(s.text) : '<span class="miss">не расслышал</span>'}</td></tr>`;
    }).join('');
  $('models').innerHTML = '<tr><th>модель</th><th>состояние</th><th>размер</th></tr>'
    + (d.models||[]).map(m => `<tr><td>${esc(m.label)}<div class="dim mono"
    style="font-size:11px">${esc(m.path)}</div></td><td><span class="tag
    ${m.ok?'ok':'bad'}">${m.ok?'на месте':'нет'}</span></td>
    <td class="mono">${m.mb} МБ</td></tr>`).join('');
  $('spool').innerHTML = '<tr><th>файл</th><th>время</th><th>размер</th></tr>'
    + (d.spool||[]).map(f => `<tr><td class="mono">${esc(f.name)}</td>
    <td class="mono">${esc(f.time)}</td><td class="mono">${f.kb} КБ</td></tr>`).join('');
  const k = d.kws || {};
  let kw = `срабатываний имени: ${k.hits||0} (кадров прослушано ${k.frames||0})`;
  if (k.name_db !== null && k.name_db !== undefined) kw += `, громкость имени ${k.name_db} дБ`;
  if (k.last_margin) kw += `\nпоследний пропуск команды: шум ${k.last_margin.noise}, `
    + `громче всего ${k.last_margin.loudest} против порога ${k.last_margin.threshold}`;
  if ((d.replies||[]).length) kw += `\nпоследний ответ: ${d.replies.slice(-1)[0]}`;
  $('kws').textContent = kw;
  for (const u of ['voiced','asrd','ttsd']){
    $('log-'+u).innerHTML = ((d.logs||{})[u]||[]).map(line => {
      let cls = '';
      if (line.startsWith('[kws]')) cls = 'lhit';
      else if (line.startsWith('[asr]')) cls = 'lmiss';
      else if (line.startsWith('[reply]')) cls = 'lhit';
      else if (/Traceback|Error|error/.test(line)) cls = 'lerr';
      const m = line.match(/^\[([a-z]+)\]|^(\d+):/);
      const head = m ? `<span class="t">${esc(m[0])}</span>` : '';
      const body = m ? line.slice(m[0].length) : line;
      return `<div class="${cls}">${head}${esc(body)}</div>`;
    }).join('');
  }
  $('docs').innerHTML = (d.docs||[]).map(f =>
    `<span class="chip" data-doc="${esc(f)}">${esc(f.replace(/\.md$/, ''))}</span>`
  ).join('');
  const tones = d.tones || [];
  if (tones.length && $('tone').options.length !== tones.length){
    $('tone').innerHTML = tones.map(t =>
      `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  }
  if (d.tone){
    $('tone').value = d.tone;
    $('tone-cur').textContent = 'в цикле: ' + d.tone;
  }
  if ((d.wake_phrases||[]).length){
    wakeName = d.wake_phrases[0];
    const pretty = wakeName.charAt(0).toUpperCase() + wakeName.slice(1);
    document.querySelector('[data-act="say_name"]').textContent = `сказать «${pretty}»`;
  }
  const enabled = !!d.actions;
  $('act-state').textContent = enabled ? '(ключ есть)' : '(только чтение)';
  document.querySelectorAll('.act').forEach(b => { b.disabled = !enabled; });
}
document.addEventListener('click', e => {
  const t = e.target.closest('.chip');
  if (t) openDoc(t.dataset.doc);
});
tick(); setInterval(tick, 3000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    cache: tuple[float, dict] | None = None
    lock = threading.Lock()
    token = ""

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", code)

    def do_GET(self):                                  # noqa: N802
        if self.path.startswith("/api/state"):
            with Handler.lock:
                now = time.time()
                if Handler.cache is None or now - Handler.cache[0] > 2.0:
                    Handler.cache = (now, state(Handler.token))
                payload = Handler.cache[1]
            self._json(payload)
            return
        if self.path.startswith("/api/doc"):
            name = ""
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("name="):
                        from urllib.parse import unquote
                        name = unquote(part[5:])
            path = doc_path(name)
            if path is None:
                self._json({"name": name, "text": ""}, 404)
                return
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            self._json({"name": name, "text": text})
            return
        if self.path in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
            return
        self._send(b"not found", "text/plain; charset=utf-8", 404)

    def do_POST(self):                                # noqa: N802
        if not self.path.startswith("/api/action"):
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        status = check_token(self.headers.get("X-Panel-Token"), Handler.token)
        if status != "ok":
            message = ("действия выключены: нет файла с ключом "
                       "(~/.assistant/panel.token)" if status == "disabled"
                       else "неверный ключ")
            self._json({"ok": False, "message": message}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            payload = {}
        result = do_action(payload, Handler.token)
        with Handler.lock:                 # состояние менялось — сбросим кэш
            Handler.cache = None
        self._json(result)

    def log_message(self, *args):
        pass                    # не засоряем журнал панели своими запросами


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="адрес прослушивания (0.0.0.0 — вся локальная сеть)")
    ap.add_argument("--token-file", default=TOKEN_FILE,
                    help="файл с ключом доступа; нет файла — панель только читает")
    args = ap.parse_args()
    Handler.token = load_token(args.token_file)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[panel] http://{args.bind}:{args.port} — панель ассистента"
          + ("" if Handler.token else
             "; действия выключены (нет файла с ключом)"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
