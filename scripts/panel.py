#!/usr/bin/env python3
"""panel.py — лёгкая веб-панель ассистента: схема, службы, журнал, фразы.

ЗАЧЕМ. Проект вырос: три резидентные службы, KWS, модель ASR, папка сегментов,
журналы. Чтобы не ходить в ssh за каждой мелочью, панель показывает текущее
состояние в одном месте: схему тракта (кто с кем связан), состояние служб,
последние фразы с уровнями и распознанным текстом, хвосты журнала, модели и
настройки запуска.

ПОЧЕМУ БЕЗ ЗАВИСИМОСТЕЙ. Только стандартная библиотека (http.server) и
системные команды, которые на плате уже есть (systemctl/journalctl/arecord).
Ни БД, ни авторизации, ни сборки: панель НИЧЕГО не меняет, поэтому и защищать
нечего — она читает журнал пользователя и листинг своего же spool.

Запуск (user-юнит assistant-panel.service):
    ~/models/tts-venv/bin/python scripts/panel.py --port 8080
Открывается: http://<ip платы>:8080
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNITS = ("ttsd", "asrd", "voiced")
SPOOL = os.path.expanduser("~/.assistant/segments")


def run(cmd: list[str], timeout: float = 5.0) -> str:
    """Выполнить команду и вернуть stdout (ошибки — пустая строка)."""
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# Сбор данных (всё — чистые функции: их и проверяют тесты)
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


def units_state(names=UNITS) -> list[dict]:
    result = []
    for name in names:
        props = parse_show(run(["systemctl", "--user", "show",
                                "-p", "ActiveState,SubState,MainPID,"
                                      "ExecMainStartTimestamp,NRestarts", name]))
        pid = props.get("MainPID", "0")
        exec_line = ""
        for line in run(["systemctl", "--user", "cat", name]).splitlines():
            if line.startswith("ExecStart="):
                exec_line = line.split("=", 1)[1].strip()
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
        # «ubuntu python[123]: текст» -> «текст»
        if "]: " in line:
            line = line.split("]: ", 1)[1]
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
    """Разобрать журнал voiced в события: фразы, срабатывания, отказы.

    Фразы собираются по НОМЕРУ сегмента: строка [seg] даёт уровни, строка
    «N: текст» — распознанное, «[asr] N: не расслышал» — неудачу. Так панель
    показывает то же, что видно в журнале, без повторного распознавания.
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
            segs.setdefault(index, {}).update(
                {"text": m.group(2), "asr_s": float(m.group(3))})
            continue
        if (m := FAIL_RE.match(line)):
            index = int(m.group(1))
            if index not in segs:
                order.append(index)
            segs.setdefault(index, {}).update(
                {"text": "", "asr_s": float(m.group(2)), "failed": True})
            continue
        if line.startswith("[reply]"):
            replies.append(line[len("[reply] "):])
            continue
    segments = [dict(index=i, **segs[i]) for i in order]
    return {"segments": segments[-12:], "kws": kws, "replies": replies[-3:]}


def spool_files() -> list[dict]:
    """Что лежит на диске: имена с временем, размер, время изменения."""
    try:
        names = sorted(os.listdir(SPOOL),
                       key=lambda n: os.path.getmtime(os.path.join(SPOOL, n)))
    except OSError:
        return []
    out = []
    for name in names[-8:]:
        path = os.path.join(SPOOL, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": name, "kb": round(st.st_size / 1024.0, 1),
                    "time": time.strftime("%H:%M:%S", time.localtime(st.st_mtime))})
    return out


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
    info["capture"] = " | ".join(
        l.split("[", 1)[1].rstrip("]") if "[" in l else l
        for l in run(["arecord", "-l"]).splitlines() if l.startswith("card"))
    return info


def pipeline(units: list[dict], kws_stats: dict) -> list[dict]:
    """Схема тракта со живым состоянием на каждой ступени."""
    by_name = {u["name"]: u for u in units}
    voiced = by_name.get("voiced", {})
    options = voiced.get("exec", "")
    mic = re.search(r"--device (\S+)", options)
    phrases = re.search(r"--kws-phrases \"?([^\"\\]+)\"?", options)
    return [
        {"stage": "Микрофон", "detail": mic.group(1) if mic else "plughw:5,0",
         "state": "ok" if voiced.get("active") == "active" else "bad"},
        {"stage": "Vosk-KWS (имя)",
         "detail": (phrases.group(1) if phrases else "астра") +
                   f"; срабатываний {kws_stats.get('hits', 0)}",
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


ASR_PORT = 6006
TTS_PORT = 6007


def state() -> dict:
    units = units_state()
    events = parse_events(journal("voiced", 120))
    return {
        "host": host_info(),
        "units": units,
        "pipeline": pipeline(units, events["kws"]),
        "segments": events["segments"],
        "kws": events["kws"],
        "replies": events["replies"],
        "spool": spool_files(),
        "models": models(),
        "logs": {u: journal(u, 25) for u in UNITS},
        "docs": sorted(f for f in os.listdir(os.path.join(REPO, "docs"))
                       if f.endswith(".md")),
        "ts": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Голосовой ассистент — панель</title>
<style>
:root{--bg:#12151a;--card:#1b2027;--fg:#e6e9ee;--dim:#8b95a5;--ok:#3ecf8e;
--bad:#ef5f5f;--wait:#e0a94a;--line:#2a313b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,
sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
gap:18px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);
margin:0 0 10px}
main{padding:18px 20px;display:grid;gap:18px;grid-template-columns:repeat(auto-fit,
minmax(340px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px}
.card-wide{grid-column:1/-1}
.pipe{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch}
.step{flex:1 1 130px;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.step b{display:block;font-size:13px}
.step span{color:var(--dim);font-size:12px}
.step.ok{border-left:3px solid var(--ok)}
.step.bad{border-left:3px solid var(--bad)}
.step.wait{border-left:3px solid var(--wait)}
.arrow{align-self:center;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;padding:4px 6px;
border-bottom:1px solid var(--line)}
td{padding:4px 6px;border-bottom:1px solid #20262e;vertical-align:top}
.mono{font-family:ui-monospace,monospace}
.bar{height:8px;border-radius:4px;background:#2a313b;position:relative;
min-width:60px}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:4px;background:var(--ok)}
.bad{color:var(--bad)}.dim{color:var(--dim)}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;
border:1px solid var(--line)}
.tag.ok{color:var(--ok);border-color:#2b5f45}
.tag.bad{color:var(--bad);border-color:#5f2b2b}
pre{margin:0;max-height:260px;overflow:auto;font-size:12px;
font-family:ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}
.hit{color:var(--ok)}.miss{color:var(--wait)}
a{color:#7fb3ff}
</style></head><body>
<header><h1>🎙 Голосовой ассистент «Астра»</h1>
<span id="host" class="dim"></span><span id="ts" class="dim"></span></header>
<main>
<section class="card-wide"><h2>Схема тракта</h2><div class="pipe" id="pipe"></div></section>
<section><h2>Службы</h2><table id="units"></table></section>
<section><h2>Последние фразы</h2><table id="segments"></table></section>
<section><h2>Модели</h2><table id="models"></table></section>
<section><h2>Файлы сегментов</h2><table id="spool"></table></section>
<section><h2>Журнал: KWS и отказы</h2><pre id="kws"></pre></section>
<section class="card-wide"><h2>Журнал voiced</h2><pre id="log-voiced"></pre>
<h2 style="margin-top:12px">Журнал asrd</h2><pre id="log-asrd"></pre>
<h2 style="margin-top:12px">Журнал ttsd</h2><pre id="log-ttsd"></pre></section>
<section><h2>Документы</h2><div id="docs"></div></section>
</main>
<script>
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;',
'>':'&gt;'}[c]));
async function tick(){
  let d;
  try { d = await (await fetch('/api/state')).json(); }
  catch(e){ document.getElementById('ts').textContent = 'нет связи с панелью';
    return; }
  const h = d.host || {};
  document.getElementById('host').textContent =
    `${h.host} · ${h.ip} · up ${h.uptime} · RAM ${h.mem_avail_mb}/${h.mem_total_mb} МБ`
    + (h.temp_c ? ` · ${h.temp_c} °C` : '') + (h.capture ? ` · ${h.capture}` : '');
  document.getElementById('ts').textContent = 'обновлено ' + d.ts;
  document.getElementById('pipe').innerHTML = (d.pipeline||[]).map((p,i) =>
    (i? '<div class="arrow">→</div>':'') +
    `<div class="step ${p.state}"><b>${esc(p.stage)}</b><span>${esc(p.detail)}</span></div>`
  ).join('');
  document.getElementById('units').innerHTML =
    '<tr><th>служба</th><th>состояние</th><th>PID</th><th>RSS</th><th>перезапусков</th></tr>' +
    (d.units||[]).map(u => `<tr><td class="mono">${esc(u.name)}</td>
    <td><span class="tag ${u.active==='active'?'ok':'bad'}">${esc(u.active)}</span></td>
    <td class="mono">${u.pid||'-'}</td><td class="mono">${u.rss_mb?u.rss_mb+' МБ':'-'}</td>
    <td class="mono">${u.restarts}</td></tr>`).join('');
  document.getElementById('segments').innerHTML =
    '<tr><th>№</th><th>речь</th><th>пик</th><th>распознано</th></tr>' +
    (d.segments||[]).slice().reverse().map(s => {
      const l = s.levels || {};
      const peak = l.peak ?? -99;
      const w = Math.max(2, Math.min(100, 100 + peak * 3));
      return `<tr><td class="mono">${s.index}</td>
      <td class="mono">${l.speech ?? '-'} с</td>
      <td><div class="bar"><i style="width:${w}%"></i></div>
      <span class="dim mono">${peak} дБ</span></td>
      <td>${s.text ? esc(s.text) : '<span class="miss">не расслышал</span>'}</td></tr>`;
    }).join('');
  document.getElementById('models').innerHTML =
    '<tr><th>модель</th><th>состояние</th><th>размер</th></tr>' +
    (d.models||[]).map(m => `<tr><td>${esc(m.label)}<div class="dim mono"
    style="font-size:11px">${esc(m.path)}</div></td>
    <td><span class="tag ${m.ok?'ok':'bad'}">${m.ok?'на месте':'нет'}</span></td>
    <td class="mono">${m.mb} МБ</td></tr>`).join('');
  document.getElementById('spool').innerHTML =
    '<tr><th>файл</th><th>время</th><th>размер</th></tr>' +
    (d.spool||[]).map(f => `<tr><td class="mono">${esc(f.name)}</td>
    <td class="mono">${esc(f.time)}</td><td class="mono">${f.kb} КБ</td></tr>`).join('');
  const k = d.kws || {};
  let kw = `срабатываний имени: ${k.hits||0} (кадров прослушано ${k.frames||0})`;
  if (k.name_db !== null && k.name_db !== undefined) kw += `, громкость имени ${k.name_db} дБ`;
  if (k.last_margin) kw += `\\nпоследний пропуск команды: шум ${k.last_margin.noise}, `
    + `громче всего ${k.last_margin.loudest} против порога ${k.last_margin.threshold}`;
  if ((d.replies||[]).length) kw += `\\nпоследний ответ: ${d.replies.slice(-1)[0]}`;
  document.getElementById('kws').textContent = kw;
  for (const u of ['voiced','asrd','ttsd']) {
    const el = document.getElementById('log-'+u);
    el.innerHTML = ((d.logs||{})[u]||[]).map(line => {
      let cls = '';
      if (line.startsWith('[kws]')) cls = 'hit';
      else if (line.startsWith('[asr]')) cls = 'miss';
      return `<div class="${cls}">${esc(line)}</div>`;
    }).join('');
  }
  document.getElementById('docs').innerHTML = (d.docs||[]).map(f =>
    `<div><a href="https://github.com/IlyaYukhimets/BPI-W3-Voice-Assistant/blob/main/docs/${f}"
    target="_blank">${esc(f)}</a></div>`).join('');
}
tick(); setInterval(tick, 3000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    state_cache: tuple[float, dict] | None = None
    lock = threading.Lock()

    def do_GET(self):                                  # noqa: N802
        if self.path.startswith("/api/state"):
            with Handler.lock:
                now = time.time()
                if Handler.state_cache is None or now - Handler.state_cache[0] > 2.0:
                    Handler.state_cache = (now, state())
                payload = Handler.state_cache[1]
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_response(404)
            body = b"not found"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass                       # не засоряем журнал панели запросами


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="адрес прослушивания (0.0.0.0 — вся локальная сеть)")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[panel] http://{args.bind}:{args.port} — панель ассистента", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
