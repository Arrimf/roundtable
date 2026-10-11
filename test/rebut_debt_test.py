#!/usr/bin/env python3
"""Долг витка и его добор (решение Автора 2026-10-06: «добери codex/grok
через catchup»). До этого долг заводился только на слепую фазу, и упавший в
витке голос пропадал из «долгов нет» свода. Теперь: отказ в витке → запись
debt с фазой rebutN и failed_id; `catchup` собирает пакет ТОГО витка заново
по журналу (rebut_build(rebuild=…)): раскрытие, состав и метки — из записи
unveil витка, тот же фокус, тот же оплаченный пересказ, ответы только до
читавшейся фазы — и сверяет prompt_sha с записью отказа (packet_same).
Без моделей: ask_one и beacon подменены."""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="rdebt.", dir="/var/tmp"))
os.environ["CHOIR_ROOM"] = str(W / "room.jsonl")
os.environ["CHOIR_DEBTS"] = str(W / "debts.jsonl")
os.environ["CHOIR_JOURNAL"] = str(W)
os.environ["CHOIR_RT_NO_BWRAP"] = "1"
os.environ["CHOIR_PACKET_CAP_GEMINI"] = "9000"
os.environ["CHOIR_DIGEST_MIN"] = "500"
os.environ.pop("CHOIR_REBUT_QUOTE_LIMIT", None)
for k in list(os.environ):
    if k.startswith("CHOIR_ROUND_"):
        os.environ.pop(k)
sys.path.insert(0, str(RT / "chamber"))
import choir                                                   # noqa: E402

ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


def rec(**k):
    d = {"ts": k.pop("ts", "2026-10-06T10:00:00+00:00"), "round": "r1", "schema": 2}
    d.update(k)
    return json.dumps(d, ensure_ascii=False)


NUM = {"claude": 1, "codex": 2, "grok": 3, "gemini": 4}
LONG = {v: f"Ответ номер {NUM[v]}: " + (f"довод {NUM[v]} " * 400) for v in NUM}
room = [
    rec(id="l1", phase="pick", voice="choir", role="lot", conductor="codex", text="codex",
        candidates=list(NUM)),
    rec(id="s1", phase="blind", voice="arr", role="seed", text="затравка", called=list(NUM)),
    rec(id="a1", phase="blind", voice="claude", role="answer", status="ok", text=LONG["claude"]),
    rec(id="a2", phase="blind", voice="codex", role="answer", status="ok", text=LONG["codex"]),
    rec(id="a3", phase="blind", voice="grok", role="answer", status="ok", text=LONG["grok"]),
    rec(id="a4", phase="blind", voice="gemini", role="answer", status="ok", text=LONG["gemini"]),
]


def reset(extra=()):
    (W / "room.jsonl").write_text("\n".join(list(room) + list(extra)) + "\n", encoding="utf-8")
    (W / "debts.jsonl").write_text("", encoding="utf-8")
    calls.clear()


def recs_now():
    return [json.loads(l) for l in (W / "room.jsonl").read_text(encoding="utf-8").splitlines()]


def debts_now():
    return [json.loads(l) for l in (W / "debts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


calls = []
FAIL = {}          # voice -> status отказа в витке


def digest_for(prompt):
    labs = re.findall(r"^### Голос (\S+)$", prompt, re.M)
    return "\n\n".join(f"### Голос {l}\nсжато […]" for l in labs)


def fake_ask_one(name, prompt, round_id, phase, parent, visibility, use_role=True, _fallback=None):
    calls.append((name, prompt, phase))
    base = {"id": f"x{len(calls)}", "ts": "2026-10-06T10:05:00+00:00", "round": round_id, "phase": phase,
            "voice": name, "role": "answer", "parent": parent, "visibility": visibility,
            "prompt_sha": choir._sha(prompt), "elapsed_s": 1.0, "effort": "medium"}
    cap = choir.PACKET_CAP.get(name.split("-")[0], 0)
    if cap and len(prompt) + len(choir.project_notice() + choir.goal_notice()) > cap:
        return base | {"status": "oversize", "text": "", "packet_chars": len(prompt), "packet_cap": cap}
    if "ПЕРЕСКАЗАТЬ" in prompt:
        return base | {"status": "ok", "text": digest_for(prompt)}
    if phase != "probe" and name in FAIL:
        return base | {"status": FAIL[name], "text": "", "detail": "упал"}
    return base | {"status": "ok", "text": f"уточнение {name}"}


choir.ask_one = fake_ask_one
choir.beacon = lambda: {"round": 1, "signature": "ab" * 48}
choir._dump = lambda *a, **k: None
NS = lambda **k: argparse.Namespace(**({"round": "r1", "out": None, "force": False, "effort": None,
                                          "focus": None, "project": None} | k))
CU = lambda **k: argparse.Namespace(**({"round": "r1", "voice": "", "phase": "", "hard_timeout": None} | k))

# ── виток: grok и gemini падают; долги — только им ──
reset(); FAIL.clear(); FAIL.update(grok="stalled", gemini="error")
rc = choir.cmd_rebut(NS(focus="фокус витка"))
d = [x for x in debts_now() if x.get("role") == "debt"]
t("виток прошёл; долги витка — grok и gemini, с failed_id записи отказа",
  rc == 0 and sorted(x["voice"] for x in d) == ["gemini", "grok"] and all(x["phase"] == "rebut1" and x.get("failed_id") for x in d))
dig = [r for r in recs_now() if r.get("role") == "digest"]
t("пересказ для gemini в журнале один, использован", len(dig) == 1 and dig[0]["digest_used"] is True)
grok_then = [c for c in calls if c[0] == "grok" and c[2] == "rebut1"][0][1]
gem_then = [c for c in calls if c[0] == "gemini" and c[2] == "rebut1"][0][1]
t("в пакетах витка был фокус; у gemini — пересказ", "фокус витка" in grok_then and "ПЕРЕСКАЗ, не оригиналы" in gem_then)

# ── добор grok: тот же пакет бит-в-бит; журнал тем временем пополнился ответами витка ──
FAIL.clear(); calls.clear()
rc2 = choir.cmd_catchup(CU(voice="grok", phase="rebut1"))
gc = [c for c in calls if c[0] == "grok" and c[2] == "rebut1"]
t("catchup grok: один вызов на виток плюс пробник; пакет бит-в-бит тот же",
  rc2 == 0 and len(gc) == 1 and any(c[2] == "probe" for c in calls) and gc[0][1] == grok_then)
late = [r for r in recs_now() if r.get("role") == "answer" and r.get("voice") == "grok" and r.get("phase") == "rebut1" and r.get("late")]
t("запись добора: late, catchup_of, packet_same=true, labels_seen без лишних, nonblind нет",
  len(late) == 1 and late[0]["packet_same"] is True and set(late[0]["labels_seen"]) == {"claude", "codex", "gemini"}
  and "nonblind" not in late[0] and late[0]["catchup_of"] == [x for x in d if x["voice"] == "grok"][0]["id"])
t("добор не плодит второго раскрытия/фокуса/пересказа",
  len([r for r in recs_now() if r.get("role") == "unveil"]) == 1
  and len([r for r in recs_now() if r.get("role") == "focus"]) == 1
  and len([r for r in recs_now() if r.get("role") == "digest"]) == 1)
t("долг grok погашен, долг gemini открыт", [x["voice"] for x in choir._debts_open()] == ["gemini"])

# ── добор gemini: пересказ берётся готовый, нового жребия и вызова пересказчика нет ──
calls.clear()
choir.cmd_catchup(CU(voice="gemini", phase="rebut1"))
gcal = [c for c in calls if c[0] == "gemini" and c[2] == "rebut1"]
t("catchup gemini: пакет с тем же пересказом, без нового вызова пересказчика",
  len(gcal) == 1 and gcal[0][1] == gem_then and not any("ПЕРЕСКАЗАТЬ" in c[1] for c in calls))
gl = [r for r in recs_now() if r.get("role") == "answer" and r.get("voice") == "gemini" and r.get("phase") == "rebut1" and r.get("late")]
t("запись добора gemini: eyes=digest, digest_by, packet_same=true, долгов нет",
  len(gl) == 1 and gl[0].get("eyes") == "digest" and gl[0].get("digest_by") and gl[0]["packet_same"] is True
  and not choir._debts_open())

# ── поздний слепой ответ, добранный ПОСЛЕ витка, в пакет добора не входит ──
reset(); FAIL.clear(); FAIL.update(grok="stalled")
choir.cmd_rebut(NS())
grok_then2 = [c for c in calls if c[0] == "grok" and c[2] == "rebut1"][0][1]
with open(W / "room.jsonl", "a", encoding="utf-8") as f:
    f.write(rec(id="a9", phase="blind", voice="kimi", role="answer", status="ok", text="поздний слепой ответ", late=True) + "\n")
FAIL.clear(); calls.clear()
choir.cmd_catchup(CU(voice="grok", phase="rebut1"))
gc2 = [c for c in calls if c[0] == "grok" and c[2] == "rebut1"][0][1]
l2 = [r for r in recs_now() if r.get("role") == "answer" and r.get("voice") == "grok" and r.get("phase") == "rebut1" and r.get("late")][0]
t("состав пакета — из раскрытия витка: поздний kimi не в пакете, букв сверх раскрытия нет, packet_same=true",
  gc2 == grok_then2 and "поздний слепой ответ" not in gc2 and "kimi" not in l2["labels_seen"] and l2["packet_same"] is True)

# ── должник-опоздавший: в слепой фазе упал, в витке упал — добор неслепым входом ──
reset([rec(id="a3", phase="blind", voice="grok", role="answer", status="quota", text="")][:0])
(W / "room.jsonl").write_text("\n".join([r for r in room if '"a3"' not in r]
                                        + [rec(id="a3", phase="blind", voice="grok", role="answer", status="quota", text="")]) + "\n", encoding="utf-8")
FAIL.clear(); FAIL.update(grok="timeout"); calls.clear()
choir.cmd_rebut(NS())
FAIL.clear(); calls.clear()
choir.cmd_catchup(CU(voice="grok", phase="rebut1"))
gl3 = [r for r in recs_now() if r.get("role") == "answer" and r.get("voice") == "grok" and r.get("phase") == "rebut1" and r.get("late")]
gp3 = [c for c in calls if c[0] == "grok" and c[2] == "rebut1"][0][1]
t("опоздавший должник: пакет неслепого входа, запись nonblind + late, packet_same=true",
  len(gl3) == 1 and gl3[0].get("nonblind") is True and "первичного слепого" in gp3 and gl3[0]["packet_same"] is True)

# ── oversize и missing долгом не становятся; повторный отказ второй долг не заводит ──
choir._owe("r1", "rebut1", "gemini", "", None, "oversize")
choir._owe("r1", "rebut1", "gemini", "", None, "missing")
t("oversize/missing — не долг", not [x for x in choir._debts_open() if x["voice"] == "gemini"])
choir._owe("r1", "rebut2", "codex", "", None, "error", failed_id="z1")
choir._owe("r1", "rebut2", "codex", "", None, "error", failed_id="z2")
t("повторный отказ той же фазы — один долг", len([x for x in choir._debts_open() if x["voice"] == "codex"]) == 1)

# ── долг без failed_id (внесён руками) → packet_same=null ──
(W / "debts.jsonl").write_text("", encoding="utf-8")
choir._owe("r1", "rebut1", "codex", "", None, "quota")
calls.clear()
choir.cmd_catchup(CU(voice="codex", phase="rebut1"))
cl = [r for r in recs_now() if r.get("role") == "answer" and r.get("voice") == "codex" and r.get("phase") == "rebut1" and r.get("late")]
t("долг без записи отказа: добор принят, packet_same=null (не выдумываем причину)",
  len(cl) == 1 and cl[0]["packet_same"] is None and not choir._debts_open())

# ── витка нет в журнале → долг остаётся ──
(W / "debts.jsonl").write_text("", encoding="utf-8")
choir._owe("r1", "rebut3", "codex", "", None, "error")
calls.clear()
choir.cmd_catchup(CU(voice="codex", phase="rebut3"))
t("нераскрытый виток: вызова нет, долг остаётся", not [c for c in calls if c[2] == "rebut3"] and choir._debts_open())

# ── модель, переданная явно, видна в записи ──
os.environ["CHOIR_ROUND_GROK_MODEL"] = "grok-4.7"
t("model_passed: grok → grok-4.7 из переменной; kimi → None; deepseek — умолчание argv; gemini — первая из списка",
  choir.model_passed("grok") == "grok-4.7" and choir.model_passed("kimi") is None
  and choir.model_passed("deepseek") == "deepseek-v4-pro" and choir.model_passed("gemini") == choir._gemini_round_models().split(",")[0])
os.environ.pop("CHOIR_ROUND_GROK_MODEL")
t("model_passed: grok без переменной — None", choir.model_passed("grok") is None)

shutil.rmtree(W, ignore_errors=True)
print(f"\nrebut_debt: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
