#!/usr/bin/env python3
"""Ранние отказы (early.py): строгий признак квоты, тень по stderr,
снятие голоса по флагу в дирижёре и комнате. Живых голосов не зовёт."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="early."))
os.environ.update(CHOIR_DROP_DIR=str(T / "drop"),
                  ROUNDTABLE_JOURNAL=str(T / "journal"),
                  CHOIR_RT_NO_BWRAP="1")
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "chamber"))
import early                                    # noqa: E402
import choir                                    # noqa: E402
import live                                     # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond)
    FAIL += not cond


# ── 1. классификатор ─────────────────────────────────────────────────
uuid_line = 'task_progress id=3f2a4290-1c34-4290-8429-429cbe0f1a2d step 429/900'
check("uuid и «429/900» в строке — НЕ квота (три ложные quota сентября)",
      early.classify(uuid_line) is None)
check("HTTP 429 / rate limit / quota exceeded / «исчерпан по квоте» — квота; concurrency — busy",
      early.classify("Error: HTTP 429 Too Many Requests") == "quota"
      and early.classify("rate_limit_exceeded") == "quota"
      and early.classify("You exceeded your current quota") == "quota"
      and early.classify("ключ #2 исчерпан по квоте") == "quota"
      and early.classify("429 max organization concurrency: 1") == "busy")
check("503 у шлюза / high demand / ECONNRESET — шлюз; «item 503 error in tool output» — нет",
      early.classify("gemini-http: ключ #4: 503 у шлюза, повтор через 4 с") == "gateway"
      and early.classify("high demand") == "gateway" and early.classify("ECONNRESET") == "gateway"
      and early.classify("item 503 error in tool output") is None
      and early.classify("ключ #4: ответ пуст") is None)
check("«повтор через 4 с» — повтор; «нет первого байта» — шлюз",
      early.classify("повтор через 4 с") == "retry"
      and early.classify("попытка 2/3: нет первого байта за 10 с") == "gateway")
check("not logged in / invalid api key — авторизация",
      early.classify("Error: not logged in") == "auth"
      and early.classify("invalid api key") == "auth")
check("обычная строка — ничего", early.classify("• Read the file first.") is None
      and early.classify("Осматриваю комнату.") is None)
check("QUOTA_RE не ловит uuid в blob дирижёра",
      not early.QUOTA_RE.search(uuid_line) and early.QUOTA_RE.search("HTTP 429: rate limit"))

# ── 2. тень: только stderr, вне мыслей, отчёт по удвоению ───────────
sh = early.Shadow("gem")
check("stdout — не источник признаков", sh.feed("HTTP 429 rate limit", "out") is None)
sh.feed("💭 мысли модели", "err")
check("внутри блока мыслей — не признак", sh.feed("… 503 service unavailable …", "err") is None)
sh.feed("Цитирую маркер: 💭 конец мыслей", "err")
check("маркер посреди строки мыслей не закрывает блок (codex)",
      sh.feed("service unavailable — так и скажу", "err") is None)
sh.feed("💭 конец мыслей", "err")
w1 = sh.feed("ключ #1: 503 у шлюза, повтор через 4 с", "err")
w2 = sh.feed("ключ #2: 503 у шлюза, повтор через 4 с", "err")
w3 = sh.feed("ключ #3: 503 у шлюза, повтор через 4 с", "err")
w4 = sh.feed("ключ #4: 503 у шлюза, повтор через 4 с", "err")
check("первый признак — отчёт; второй — отчёт (удвоение); третий — нет; четвёртый — да",
      w1 and w1["hits"] == 1 and w2 and w2["hits"] == 2 and w3 is None and w4 and w4["hits"] == 4)
w5 = sh.feed("HTTP 429 quota exceeded", "err")
check("смена класса — отчёт сразу", w5 and w5["cause"] == "quota" and w5["hits"] == 5)
check("summary — все показанные предупреждения с first_seen_s",
      len(sh.summary()) == 4 and all("first_seen_s" in w for w in sh.summary()))
check("mark(): жёлтая строка называет голос, класс, номер и строку",
      "⚠ gem" in early.mark(w5, "gem") and "квота" in early.mark(w5, "gem") and "№5" in early.mark(w5, "gem"))

# ── 3. флаг снятия ───────────────────────────────────────────────────
p = early.request_drop(4242, "arr", "act1")
check("флаг лежит в каталоге drop, содержимое — кто|акт|голос", p.exists() and p.read_text() == "arr|act1|")
check("drop_requested: чужой акт — не команда (и флаг снят); свой — кто просил",
      early.drop_requested(4242, "act2") is None and not p.exists()
      and (early.request_drop(4242, "arr", "act1") and early.drop_requested(4242, "act1") == "arr")
      and early.drop_requested(4242, "act1") is None)
early.request_drop(4545, "arr"); early.clear_drop(4545)
check("clear_drop гасит флаг", not early.drop_file(4545).exists())
old = early.request_drop(4343, "arr"); os.utime(old, (time.time() - 7200, time.time() - 7200))
check("флаг старше часа — не команда (чужая жизнь pgid)", early.drop_requested(4343) is None)
early.request_drop(4444, "x"); os.utime(early.drop_file(4444), (time.time() - 7200,) * 2)
check("sweep_stale убирает старые флаги", early.sweep_stale() >= 1 and not early.drop_file(4444).exists())

# ── 4. дирижёр: run_watched — тень и снятие ──────────────────────────
(T / "journal").mkdir(exist_ok=True)
cmd = ["bash", "-c", "echo 'ключ #1: 503 у шлюза, повтор через 4 с' >&2; sleep 30; echo late"]
res_box = {}


def _run():
    res_box["r"] = choir.run_watched(cmd, cwd=str(T), hard_limit=120, idle_limit=120,
                                     voice="gemini", round_id="r1", phase="blind", blind=True)


th = threading.Thread(target=_run); th.start()
time.sleep(1.5)
# pgid — pid bash-обёртки; узнаём его из CHILD_PGIDS дирижёра
with choir._CHILD_LOCK:
    pg = sorted(choir._CHILD_PGIDS)
check("группа хода известна дирижёру", len(pg) == 1)
early.request_drop(pg[0], "arr")
th.join(timeout=15)
r = res_box.get("r") or {}
check("run_watched: снят по флагу — status dropped, ended_by arr, за секунды, не 30 с",
      r.get("status") == "dropped" and r.get("ended_by") == "arr" and "late" not in r.get("stdout", ""))
check("run_watched: тень записала признак шлюза",
      r.get("early_warns") and r["early_warns"][0]["cause"] == "gateway")
check("флаг после хода погашен", not early.drop_file(pg[0]).exists())
evs = [json.loads(l) for l in (T / "journal" / "live.jsonl").open(encoding="utf-8")]
ew = [e for e in evs if e.get("kind") == "early_warn"]
check("early_warn в ленте (слепая фаза): voice, cause, pgid, shadow=true, УЛИКА — только sha, не строка",
      ew and ew[0]["voice"] == "gemini" and ew[0]["cause"] == "gateway" and ew[0]["pgid"] == pg[0]
      and ew[0].get("shadow") is True and ew[0]["evidence"].startswith("sha256:")
      and "503" not in ew[0]["evidence"] and "503" not in ew[0]["text"] and ew[0].get("round") == "r1")
# флаг на БУДУЩИЙ pgid: pid выдаются по порядку — ставим флаги на следующие 64
_last = int(open("/proc/sys/kernel/ns_last_pid").read())
for _i in range(1, 65):
    early.request_drop(_last + _i, "arr", "", "codex")
pre = choir.run_watched(["bash", "-c", "sleep 0.5; echo fine"], cwd=str(T), hard_limit=30, idle_limit=30, voice="codex")
for _i in range(1, 65):
    early.clear_drop(_last + _i)
check("стартующий ход гасит чужой старый флаг своего pgid (clear_drop на старте)", pre["status"] == "ok" and "fine" in pre["stdout"])
p2 = early.request_drop(4646, "arr", "actA", "kimi")
check("флаг чужого ГОЛОСА того же акта — не команда (переиспользованный pgid)",
      early.drop_requested(4646, "actA", "grok") is None and not p2.exists())
check("group_alive: своя группа жива, 999999 — нет", early.group_alive(os.getpgrp()) and not early.group_alive(999999))
check("quota_in: uuid и concurrency — не квота; HTTP 429 rate limit в stdout — квота",
      early.quota_in(uuid_line + "\n429 max organization concurrency: 1") is None
      and early.quota_in("x\nHTTP 429 rate limit\n") == "HTTP 429 rate limit")
op = choir.run_watched(["bash", "-c", "echo 'HTTP 503 service unavailable' >&2; echo fine"],
                       cwd=str(T), hard_limit=30, idle_limit=30, voice="gemini", phase="rebut1")
ew2 = [e for e in (json.loads(l) for l in (T / "journal" / "live.jsonl").open(encoding="utf-8")) if e.get("kind") == "early_warn"]
check("открытая фаза: улика в ленте — строка целиком", ew2[-1]["evidence"].startswith("HTTP 503"))
ok_run = choir.run_watched(["bash", "-c", "echo 'ключ #1: 503 у шлюза' >&2; echo fine"],
                           cwd=str(T), hard_limit=30, idle_limit=30, voice="gemini")
check("ok-ход с признаком: status ok, но early_warns записаны (тень пишет всегда)",
      ok_run["status"] == "ok" and ok_run.get("early_warns") and "ended_by" not in ok_run)
quiet = choir.run_watched(["bash", "-c", f"echo '{uuid_line}' >&2; echo fine"],
                          cwd=str(T), hard_limit=30, idle_limit=30, voice="codex")
check("uuid с «429» в stderr — тень молчит", "early_warns" not in quiet)

# ── 5. комната: _run_capture — тень и снятие ────────────────────────
sink_lines = []
box2 = {}


def _run2():
    try:
        box2["r"] = live._run_capture(["bash", "-c", "echo 'HTTP 429 rate limit' >&2; sleep 30"],
                                      str(T), 60, sink_lines.append, voice="kimi")
    except Exception as e:                        # noqa: BLE001
        box2["e"] = e


th2 = threading.Thread(target=_run2); th2.start()
time.sleep(1.5)
with live._CHILD_LOCK:
    pg2 = sorted(live._CHILD_PGIDS)
early.request_drop(pg2[0], "arr")
th2.join(timeout=15)
r2 = box2.get("r")
check("_run_capture: снят по флагу — dropped_by, без TimeoutExpired, тень в early_warns",
      r2 is not None and getattr(r2, "dropped_by", None) == "arr"
      and getattr(r2, "early_warns", None) and r2.early_warns[0]["cause"] == "quota")
check("стенограмма комнаты получила жёлтую строку",
      any("⚠ kimi".encode() in x for x in sink_lines))
# слепой ход комнаты: HOLD_TR не None → в ленте только sha
live.BLIND_TURN = True
live._run_capture(["bash", "-c", "echo 'HTTP 503 service unavailable' >&2; echo x"], str(T), 30, sink_lines.append, voice="grok")
live.BLIND_TURN = False
ew3 = [e for e in (json.loads(l) for l in (T / "journal" / "live.jsonl").open(encoding="utf-8")) if e.get("kind") == "early_warn" and e.get("voice") == "grok"]
check("комната, слепой ход: улика в ленте — sha, не строка (codex)",
      ew3 and ew3[-1]["evidence"].startswith("sha256:") and "503" not in ew3[-1]["text"])
# with_retry не повторяет снятый ход
import serial_gate
calls = []
def _r():
    calls.append(1)
    return {"status": "dropped", "stdout": "", "stderr": "429 max organization concurrency: 1"}
res = serial_gate.with_retry(_r, blob_of=lambda r: r["stderr"], pauses=[0.01, 0.01], stop_if=lambda r: r.get("status") == "dropped")
check("with_retry: снятый голос не перезапускается (codex)", res["status"] == "dropped" and len(calls) == 1)
# таймаут комнаты не теряет тень
try:
    live._run_capture(["bash", "-c", "echo 'HTTP 503 service unavailable' >&2; sleep 5"], str(T), 1.5, sink_lines.append, voice="kimi")
    te = None
except subprocess.TimeoutExpired as e:
    te = e
check("TimeoutExpired несёт early_warns (grok)", te is not None and getattr(te, "early_warns", None) and te.early_warns[0]["cause"] == "gateway")

print(f"\nearly: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
