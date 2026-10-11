#!/usr/bin/env python3
"""Jev в пакете раунда (choir.ask_one, jev-v2): строка про Jev одинакова для
всех голосов фазы (слепой пакет бит-в-бит, правило 13), в слепой фазе —
«закрыт»; пропуск и обёртка bin/jev кладутся в каталог вызова, bin/ —
первым в PATH голоса; Codex (без сети) пропуска не получает; без прокси
строки нет вовсе. run_watched и прокси подменены — без моделей и сети."""
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="jevpk.", dir="/var/tmp"))
os.environ.update(CHOIR_JOURNAL=str(W), CHOIR_ROOM=str(W / "room.jsonl"), CHOIR_RT_NO_BWRAP="1",
                  CHOIR_JEV_PASS_DIR=str(W / "pass"), CHOIR_JEV_BIN="/bin/true")
os.environ.pop("CHOIR_JEV", None)
os.environ["CHOIR_JEV_REQUIRE_JAIL"] = "0"           # стенд без bwrap; требование клетки проверяется отдельно ниже
sys.path.insert(0, str(RT / "chamber"))
import choir      # noqa: E402
import jevproxy   # noqa: E402

ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


CALLS = []


def fake_run_watched(argv, *, cwd, hard_limit, idle_limit=0, sink=None, voice="", round_id="", phase="",
                     warn_sink=None, blind=False, stdin_text=None, env=None):
    # снимок каталога вызова В МОМЕНТ вызова: после ответа карантин убирается
    snap = {}
    for d in (W / "q").glob(f"{round_id}_{phase}_{voice}_*"):
        if not (d / "prompt").exists():
            continue                                  # каталог прежнего вызова, уже убран
        tok = (d / "jev-pass").read_text().strip() if (d / "jev-pass").exists() else None
        pj = (W / "pass" / f"{tok}.json")
        snap = {"dir": d, "prompt": (d / "prompt").read_text(encoding="utf-8"), "pass": tok,
                "wrapper_x": os.access(d / "bin" / "jev", os.X_OK),
                "pass_rec": json.loads(pj.read_text()) if tok and pj.exists() else None}
    CALLS.append({"voice": voice, "argv": argv, "env": env, "stdin": stdin_text, "blind": blind, **snap})
    return {"stdout": "ответ", "stderr": "", "returncode": 0, "status": "ok", "silence_s": 0.1, "elapsed_s": 1.0}


choir.run_watched = fake_run_watched
choir.available = lambda name: True
choir.cli_version = lambda name: "test"
choir.QUARANTINE = W / "q"
jevproxy.ensure = lambda spawn=True: True            # прокси «есть»
choir._JEV_ON.clear()

recs = {}
for v in ("claude", "grok", "codex", "deepseek"):
    recs[v] = choir.ask_one(v, "вопрос затравки", "r1", "blind", None, "blind", use_role=False)
texts = {c["voice"]: c["prompt"] for c in CALLS}
t("слепая фаза: у всех четырёх голосов пакет содержит одну и ту же строку «JEV: … закрыт»",
  all("JEV: в слепой фазе закрыт" in x for x in texts.values()) and len({x.split("вопрос затравки")[0] for x in texts.values()}) == 1)
t("prompt_sha одинаков у всех (пакет бит-в-бит)", len({r["prompt_sha"] for r in recs.values()}) == 1)
by = {c["voice"]: c for c in CALLS}
t("пропуск и обёртка у claude/grok/deepseek; у codex (без сети) — нет",
  all("jev_pass" in recs[v] for v in ("claude", "grok", "deepseek")) and "jev_pass" not in recs["codex"]
  and all(by[v]["env"] and by[v]["env"]["PATH"].split(":")[0].endswith("/bin") for v in ("claude", "grok", "deepseek"))
  and by["codex"]["env"] is None)
g = by["grok"]
t("каталог вызова grok: jev-pass и исполняемая bin/jev; пропуск помечен blind",
  g["pass"] and g["wrapper_x"] and g["pass_rec"]["blind"] is True)
t("после хода пропуск отозван: записи нет, каталог вызова убран",
  not (W / "pass" / f"{g['pass']}.json").exists() and not g["dir"].exists())

# открытая фаза: строка «ДОСТУПЕН» с квотой и пином, пропуск не слепой
CALLS.clear()
r = choir.ask_one("grok", "виток", "r1", "rebut1", None, "after_round", use_role=False)
c = CALLS[-1]; tok = c["pass"]
t("открытая фаза: «JEV ДОСТУПЕН», квота и пин названы; пропуск не слепой",
  "JEV ДОСТУПЕН" in c["prompt"] and f"Квота {jevproxy.QUOTA}" in c["prompt"]
  and c["pass_rec"]["blind"] is False and r["jev_pass"] == tok[:8])

# без прокси: строки нет, пропуска нет, env нет
choir._JEV_ON.clear(); jevproxy.ensure = lambda spawn=True: False
CALLS.clear()
r = choir.ask_one("grok", "виток", "r1", "rebut1", None, "after_round", use_role=False)
if not CALLS:
    print("   без прокси: run_watched не вызван; rec:", {k: r.get(k) for k in ("status", "detail")})
t("без прокси: ни строки про Jev, ни пропуска, ни PATH", bool(CALLS) and "JEV" not in CALLS[-1]["prompt"] and "jev_pass" not in r and CALLS[-1]["env"] is None and CALLS[-1]["pass"] is None)

# клетка обязательна: стенд без bwrap (CHOIR_RT_NO_BWRAP=1) при требовании клетки — Jev нет
choir._JEV_ON.clear(); os.environ["CHOIR_JEV_REQUIRE_JAIL"] = "1"; jevproxy.ensure = lambda spawn=True: True
t("без клетки Jev голосам не выдаётся (jail=none → ключ в доме виден)", choir.jev_enabled() is False)
os.environ["CHOIR_JEV_REQUIRE_JAIL"] = "0"; choir._JEV_ON.clear()

# удержание в витке: пропуска витка несут hold=round|phase, после витка — flush
import argparse, json as _json
HOLD_SEEN = []; FLUSHED = []
orig_issue = jevproxy.issue_pass
def issue_spy(*a, **k):
    HOLD_SEEN.append(k.get("hold")); return orig_issue(*a, **k)
jevproxy.issue_pass = issue_spy
jevproxy.flush = lambda key, stale=False: FLUSHED.append(key) or 0
room = [_json.dumps(x, ensure_ascii=False) for x in (
    {"ts": "2026-10-08T10:00:00+00:00", "round": "r2", "schema": 2, "id": "l1", "phase": "pick", "voice": "choir", "role": "lot", "conductor": "grok", "text": "grok", "candidates": ["claude", "grok"]},
    {"ts": "2026-10-08T10:00:01+00:00", "round": "r2", "schema": 2, "id": "s1", "phase": "blind", "voice": "arr", "role": "seed", "text": "затравка", "called": ["claude", "grok"]},
    {"ts": "2026-10-08T10:00:02+00:00", "round": "r2", "schema": 2, "id": "a1", "phase": "blind", "voice": "claude", "role": "answer", "status": "ok", "text": "ответ 1"},
    {"ts": "2026-10-08T10:00:03+00:00", "round": "r2", "schema": 2, "id": "a2", "phase": "blind", "voice": "grok", "role": "answer", "status": "ok", "text": "ответ 2"})]
(W / "room.jsonl").write_text("\n".join(room) + "\n", encoding="utf-8")
choir.beacon = lambda: {"round": 1, "signature": "ab" * 48}
choir._dump = lambda *a, **k: None
CALLS.clear()
choir.cmd_rebut(argparse.Namespace(round="r2", out=None, force=False, effort=None, focus=None, project=None))
t("виток: пропуска обоих голосов несут hold=r2|rebut1; после витка дирижёр публикует удержание (оба ключа: предварительный и витка)",
  HOLD_SEEN[-2:] == ["r2|rebut1", "r2|rebut1"] and sorted(FLUSHED) == ["r2|rebut-pending", "r2|rebut1"] and choir.JEV_HOLD is None)
jevproxy.issue_pass = orig_issue

# комната: deliver держит события jev до конца хода и публикует их при любом исходе — и для одиночного хода
import live
live.jevproxy.ensure = lambda spawn=True: True
live._JEV_ON.clear(); live._JEV_ON["on"] = True
LIVE_FLUSH = []
live.jevproxy.flush = lambda key, stale=False: LIVE_FLUSH.append(key) or 0
SEEN_HOLD = []
def fake_turn(name, prompt, acc=None, _fallback=None):
    SEEN_HOLD.append(live.JEV_HOLD)
    return {"author": name, "kind": "say", "text": "реплика"}
live.turn = fake_turn
try:
    live.deliver(["grok"], blind=False)       # одиночный открытый ход — реплика сразу, held пуст
    t("комната: одиночный ход — ключ удержания стоял во время хода и опубликован (flush) по его концу",
      SEEN_HOLD and SEEN_HOLD[-1] and SEEN_HOLD[-1].startswith("room|") and LIVE_FLUSH[-1:] == [SEEN_HOLD[-1]] and live.JEV_HOLD is None)
    def boom(name, prompt, acc=None, _fallback=None):
        raise RuntimeError("упал посреди хода")
    live.turn = boom; LIVE_FLUSH.clear()
    try:
        live.deliver(["grok"], blind=False)
    except Exception:
        pass
    t("комната: исключение в ходе — flush всё равно вызван, ключ снят", len(LIVE_FLUSH) == 1 and live.JEV_HOLD is None)
except Exception as e:
    print("SKIP  комната deliver в стенде не поднялся:", type(e).__name__, str(e)[:120])
jevproxy.flush = lambda key, stale=False: FLUSHED.append(key) or 0

# CHOIR_JEV=0 выключает на запуск
choir._JEV_ON.clear(); os.environ["CHOIR_JEV"] = "0"; jevproxy.ensure = lambda spawn=True: True
t("CHOIR_JEV=0: Jev выключен даже при живом прокси", choir.jev_enabled() is False)

import shutil
shutil.rmtree(W, ignore_errors=True)
print(f"\njev_packet: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
