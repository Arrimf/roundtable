#!/usr/bin/env python3
"""Карта покрытия сводчика (choir.py: coverage, coverage_text,
summary_prompt, cmd_summarize): по подменному журналу, без вызовов
моделей — ask_one подменяется. Повод — два свода подряд «все ответили»
при упавших голосах (раунд «1», «ранние-отказы»)."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="covtest.", dir="/var/tmp"))
os.environ["CHOIR_ROOM"] = str(W / "room.jsonl")
os.environ["CHOIR_DEBTS"] = str(W / "debts.jsonl")
os.environ["CHOIR_JOURNAL"] = str(W)
os.environ["CHOIR_RT_NO_BWRAP"] = "1"
sys.path.insert(0, str(RT / "chamber"))
import choir                                                   # noqa: E402

ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


def rec(**k):
    d = {"ts": k.pop("ts", "2026-09-22T10:00:00+00:00"), "round": "r1", "schema": 2}
    d.update(k)
    return json.dumps(d, ensure_ascii=False)


room = [
    rec(id="l1", phase="pick", voice="choir", role="lot", conductor="kimi", text="kimi",
        candidates=["claude", "codex", "deepseek", "gemini", "grok", "kimi"]),
    rec(id="s1", phase="blind", voice="arr", role="seed", text="ВОПРОС",
        called=["claude", "codex", "deepseek", "gemini", "grok", "kimi"]),
    rec(id="a1", phase="blind", voice="claude", role="answer", status="ok", text="ответ клода"),
    rec(id="a2", phase="blind", voice="codex", role="answer", status="ok", text="ответ кодекса"),
    rec(id="a3", phase="blind", voice="deepseek", role="answer", status="ok", text="ответ дипсика"),
    rec(id="a4", phase="blind", voice="gemini", role="answer", status="error",
        text="DAG с типизированными связями — обрубок", detail="gemini-http: повтор через 4 с\ngemini-http: 503 у шлюза, поток закрылся"),
    rec(id="a5", phase="blind", voice="grok", role="answer", status="ok", text="ответ грока", ts="2026-09-22T10:01:00+00:00"),
    # kimi: без записи вовсе (missing)
    rec(id="u1", phase="rebut1", voice="choir", role="unveil", text="раскрытие", from_phase="blind"),
    rec(id="b1", phase="rebut1", voice="claude", role="answer", status="ok", text="виток клода"),
    rec(id="b2", phase="rebut1", voice="codex", role="answer", status="pass", text="ПАС"),
    rec(id="b3", phase="rebut1", voice="grok", role="answer", status="timeout", text="", detail="не уложился"),
    rec(id="b4", phase="rebut1", voice="deepseek", role="answer", status="ok", text="виток дипсика", nonblind=True),
]
(W / "room.jsonl").write_text("\n".join(room) + "\n", encoding="utf-8")
(W / "debts.jsonl").write_text(
    rec(id="d1", phase="blind", voice="gemini", role="debt", status="error") + "\n"
    + rec(id="d2", phase="blind", voice="kimi", role="debt", status="quota") + "\n"
    + rec(id="d3", phase="blind", voice="kimi", role="paid") + "\n"      # kimi добрал — долг закрыт
    + rec(id="d4", phase="rebut1", voice="grok", role="debt", status="timeout") + "\n"
    + json.dumps({"ts": "x", "round": "other", "phase": "blind", "voice": "grok", "role": "debt", "status": "quota"}) + "\n",
    encoding="utf-8")

cov = choir.coverage("r1")
ph = {p["phase"]: p for p in cov["phases"]}
t("две фазы в порядке: слепая, виток 1", [p["phase"] for p in cov["phases"]] == ["blind", "rebut1"])
b = ph["blind"]
t("слепая: звали шестерых по затравке, ответили четверо",
  b["called"] == ["claude", "codex", "deepseek", "gemini", "grok", "kimi"]
  and b["ok"] == ["claude", "codex", "deepseek", "grok"])
t("слепая: gemini упал со статусом и причиной (последняя строка detail), kimi — missing (записи нет)",
  b["fell"]["gemini"]["status"] == "error" and b["fell"]["gemini"]["detail"] == "gemini-http: 503 у шлюза, поток закрылся"
  and "\n" not in b["fell"]["gemini"]["detail"] and b["fell"]["kimi"]["status"] == "missing")
t("слепая: обрубок gemini учтён с длиной", b["stubs"] == {"gemini": len("DAG с типизированными связями — обрубок")})
r = ph["rebut1"]
t("виток: звали тех, у кого есть запись (4); ответили 2; ПАС codex; grok упал timeout",
  r["called"] == ["claude", "codex", "deepseek", "grok"] and r["ok"] == ["claude", "deepseek"]
  and r["pass"] == ["codex"] and r["fell"] == {"grok": {"status": "timeout", "detail": "не уложился"}})
t("виток: пометка nonblind у deepseek", r["flags"] == {"deepseek": ["nonblind"]})
t("долги: только этого раунда и только незакрытые (kimi добрал — нет; other — нет)",
  cov["debts"] == ["gemini (blind, error)", "grok (rebut1, timeout)"])

txt = choir.coverage_text(cov)
t("текст карты: числа и имена слепой фазы, обрубок помечен как не-позиция",
  "слепая фаза: звали 6 — claude, codex, deepseek, gemini, grok, kimi; ответили 4 — claude, codex, deepseek, grok" in txt
  and "обрубки (текст при отказе канала — НЕ позиция" in txt and "gemini — " in txt)
t("текст карты: виток 1 с ПАС и упавшим, долги, правило «среди ответивших»",
  "виток 1: звали 4" in txt and "ПАС 1 — codex" in txt and "grok (timeout: не уложился)" in txt
  and "долги ответов по раунду: gemini (blind, error), grok (rebut1, timeout)" in txt
  and "СРЕДИ ОТВЕТИВШИХ" in txt)
head = choir.coverage_card_head(cov, "kimi")
t("шапка карточки: цитата с числами, упавшими, долгами и автором свода",
  head.startswith("> Карта покрытия (по журналу): слепая фаза — звали 6, ответили 4, упали: gemini (error), kimi (missing); виток 1 — звали 4, ответили 2, ПАС 1, упали: grok (timeout), с пометками: deepseek [nonblind]; долги: gemini (blind, error), grok (rebut1, timeout). Свод писал kimi.\n\n"))
pr = choir.summary_prompt("r1", "ВОПРОС", "### claude (blind)\nответ", cov)
t("промпт: карта стоит до ответов и правило 4 запрещает «все» без числа",
  pr.index("## Кто был позван и кто ответил") < pr.index("## Все ответы")
  and "КАРТА ПОКРЫТИЯ (по журналу" in pr and "Слово «все» без числа запрещено" in pr
  and "(обрубок, канал упал)" in pr)

# ── Ревизия 22.09: аннулирование, повторы, пояса, схема 1, витки 10+, unveil, обрубок в теле ──
room2 = [
    rec(id="s1", round="r2", phase="blind", voice="arr", role="seed", text="q", called=["claude", "codex", "grok"]),
    rec(id="a1", round="r2", phase="blind", voice="claude", role="answer", status="ok", text="GHOST"),
    rec(id="a2", round="r2", phase="blind", voice="codex", role="answer", status="error", text="", detail="упал"),
    rec(id="a3", round="r2", phase="blind", voice="codex", role="answer", status="ok", text="добрано", late=True, nonblind=True,
        ts="2026-09-22T10:00:00+00:00"),                           # добор ПОЗЖЕ по UTC…
    rec(id="a4", round="r2", phase="blind", voice="codex", role="answer", status="error", text="", detail="повтор упал",
        ts="2026-09-22T12:30:00+03:00"),                           # …а эта строка «больше» строкой, но РАНЬШЕ по времени
    json.dumps({"ts": "2026-09-22T10:00:05+00:00", "round": "r2", "voice": "grok", "role": "answer", "status": "ok",
                "text": "схема 1 без phase", "schema": 1, "id": "a5"}, ensure_ascii=False),
    rec(id="an1", round="r2", phase="blind", voice="choir", role="annul", ids=["a1"], text="аннулировано"),
    json.dumps({"ts": "2026-09-22T11:00:00+00:00", "round": "r2", "phase": "rebut10", "voice": "choir", "role": "unveil",
                "text": "", "labels": {"claude": "А", "codex": "Б", "grok": "В", "kimi": "Г"}}, ensure_ascii=False),
    rec(id="b1", round="r2", phase="rebut10", voice="codex", role="answer", status="ok", text="виток десятый"),
    rec(id="b2", round="r2", phase="rebut10", voice="grok", role="answer", status="pass", text="ПАС"),
    rec(id="b3", round="r2", phase="rebut10", voice="grok", role="answer", status="ok", text="передумал",
        ts="2026-09-22T11:05:00+00:00"),                           # ok после pass: ok сильнее
    rec(id="b4", round="r2", phase="rebut10", voice="claude", role="answer", status="error",
        text="обрубок клода в витке", detail="timeout"),
]
(W / "room.jsonl").write_text("\n".join(room2) + "\n", encoding="utf-8")
(W / "debts.jsonl").write_text("", encoding="utf-8")
cov2 = choir.coverage("r2")
p2 = {p["phase"]: p for p in cov2["phases"]}
t("аннулированный ответ claude — не ответ: слепая «ответили» без claude, claude — missing",
  "claude" not in p2["blind"]["ok"] and p2["blind"]["fell"]["claude"]["status"] == "missing")
t("codex: ok-добор побеждает оба отказа (приоритет статуса, а не строка ts с другим поясом); пометки late/nonblind",
  "codex" in p2["blind"]["ok"] and "codex" not in p2["blind"]["fell"]
  and p2["blind"]["flags"]["codex"] == ["nonblind", "late"])
t("схема 1 без phase — слепая фаза: grok ответил", "grok" in p2["blind"]["ok"])
t("виток rebut10 не выпал; звали по меткам unveil (4), kimi — missing",
  "rebut10" in p2 and p2["rebut10"]["called"] == ["claude", "codex", "grok", "kimi"]
  and p2["rebut10"]["fell"]["kimi"]["status"] == "missing")
t("grok в витке: ok сильнее позднего/раннего pass — ответил, не ПАС",
  p2["rebut10"]["ok"] == ["codex", "grok"] and p2["rebut10"]["pass"] == [])
t("обрубок клода в витке учтён", p2["rebut10"]["stubs"] == {"claude": len("обрубок клода в витке")})
import coverage as cover
rnd2 = choir.read_round("r2")
bodies = cover.body_records(rnd2)
t("тело свода = действующие ok/pass: GHOST не цитируется, у codex одна запись (добор), у grok одна",
  [ (cover.phase_of(r), r["voice"], r["text"]) for r in bodies ]
  == [("blind", "codex", "добрано"), ("blind", "grok", "схема 1 без phase"),
      ("rebut10", "codex", "виток десятый"), ("rebut10", "grok", "передумал")])
inv = all(set(v for v in p["ok"]) | set(p["pass"]) == {r["voice"] for r in bodies if cover.phase_of(r) == p["phase"]}
          for p in cov2["phases"])
t("инвариант (grok): по каждой фазе голоса в теле == ok ∪ pass карты", inv)
t("обрубки для тела: только действующие записи с отказом и текстом",
  [(r["voice"], cover.phase_of(r)) for r in cover.stub_records(rnd2)] == [("claude", "rebut10")])
t("текст карты: виток 10 назван, порядок фаз blind → rebut10",
  "виток 10:" in choir.coverage_text(cov2) and choir.coverage_text(cov2).index("слепая фаза") < choir.coverage_text(cov2).index("виток 10"))

# ── Субагент 22.09: detail с ANSI и ключом, несколько затравок, пометки в шапке, чужие имена фаз ──
room3 = [
    rec(id="s1", round="r3", phase="blind", voice="arr", role="seed", text="q", called=["claude", "kimi", "grok"]),
    rec(id="s2", round="r3", phase="blind", voice="arr", role="seed", text="q", called=["kimi"], ts="2026-09-22T10:10:00+00:00"),
    rec(id="a1", round="r3", phase="blind", voice="kimi", role="answer", status="quota", text="",
        detail="\x1b[38;2;232;84;84mошибка\x1b[0m 429 Your account org-614e6d1234 <ak-fbyzq4f4abcd> exceeded\nвторая строка: повтор\n\x1b[31mпоследняя: лимит организации исчерпан\x1b[0m"),
    rec(id="a2", round="r3", phase="blind", voice="claude", role="answer", status="ok", text="ответ", recovered=True),
    rec(id="a3", round="r3", phase="draw", voice="grok", role="answer", status="ok", text="рисунок"),
]
(W / "room.jsonl").write_text("\n".join(room3) + "\n", encoding="utf-8")
cov3 = choir.coverage("r3")
p3 = {p["phase"]: p for p in cov3["phases"]}
d = p3["blind"]["fell"]["kimi"]["detail"]
t("detail: последняя строка, без ANSI, ключ и org замаскированы",
  d == "последняя: лимит организации исчерпан" and "\x1b" not in d and "org-614e6d1234" not in choir.coverage_text(cov3)
  and "ak-fbyzq4f4abcd" not in choir.coverage_text(cov3))
t("несколько затравок слепой фазы: звали — объединение (grok без записи → missing)",
  p3["blind"]["called"] == ["claude", "grok", "kimi"] and p3["blind"]["fell"]["grok"]["status"] == "missing")
t("шапка называет пометку recovered у claude", "с пометками: claude [recovered]" in choir.coverage_card_head(cov3, "x"))
t("фаза с чужим именем (draw) не выпадает и идёт после слепой",
  [p["phase"] for p in cov3["phases"]] == ["blind", "draw"] and p3["draw"]["ok"] == ["grok"])
import coverage as _cv
t("clean_detail: пусто → пусто; длинная строка режется до 160 с многоточием",
  _cv.clean_detail(None) == "" and len(_cv.clean_detail("x" * 500)) == 160 and _cv.clean_detail("x" * 500).endswith("…"))

# ── cmd_summarize без модели: ask_one подменён ──
(W / "room.jsonl").write_text("\n".join(room) + "\n", encoding="utf-8")
(W / "debts.jsonl").write_text(
    rec(id="d1", phase="blind", voice="gemini", role="debt", status="error") + "\n"
    + rec(id="d4", phase="rebut1", voice="grok", role="debt", status="timeout") + "\n", encoding="utf-8")
seen = {}
def fake_ask_one(name, prompt, round_id, phase, parent, visibility, use_role=False):
    seen["prompt"] = prompt; seen["name"] = name
    return {"id": "sum1", "ts": "2026-09-22T10:05:00+00:00", "round": round_id, "phase": phase,
            "voice": name, "status": "ok", "text": "**Все ответили.** свод", "elapsed_s": 1.0}
choir.ask_one = fake_ask_one
out = W / "SUMMARY-r1.md"
rc = choir.cmd_summarize(argparse.Namespace(round="r1", by=None, out=str(out)))
t("cmd_summarize без --by берёт ведущего по жребию (kimi), rc=0", rc == 0 and seen["name"] == "kimi")
t("промпт сводчика содержит карту покрытия с шестью позванными",
  "звали 6 — claude, codex, deepseek, gemini, grok, kimi" in seen["prompt"])
last = json.loads((W / "room.jsonl").read_text(encoding="utf-8").splitlines()[-1])
t("запись свода несёт coverage полем; текст записи — чистый ответ сводчика",
  last["role"] == "summary" and last["coverage"]["phases"][0]["called"][3] == "gemini"
  and last["coverage"]["debts"] == cov["debts"] and last["text"] == "**Все ответили.** свод")
card = out.read_text(encoding="utf-8")
t("карточка начинается с шапки из журнала, потом текст сводчика",
  card.startswith("> Карта покрытия (по журналу): слепая фаза — звали 6, ответили 4")
  and card.endswith("**Все ответили.** свод"))
t("обрубок gemini — в промпте отдельным разделом с пометкой, не среди ответов",
  "## Обрубки упавших" in seen["prompt"] and "### gemini (blind) — ОБРУБОК, status=error" in seen["prompt"]
  and seen["prompt"].index("## Все ответы") < seen["prompt"].index("## Обрубки упавших") < seen["prompt"].index("## Как сводить"))
t("запись свода несёт метку времени карты (at)", bool(last["coverage"].get("at")))
def failing_ask_one(name, prompt, round_id, phase, parent, visibility, use_role=False):
    return {"id": "sum2", "ts": "2026-09-22T10:06:00+00:00", "round": round_id, "phase": phase,
            "voice": name, "status": "error", "text": "", "detail": "упал", "elapsed_s": 1.0}
choir.ask_one = failing_ask_one
out2 = W / "SUMMARY-r1-2.md"
rc2 = choir.cmd_summarize(argparse.Namespace(round="r1", by="claude", out=str(out2)))
last2 = json.loads((W / "room.jsonl").read_text(encoding="utf-8").splitlines()[-1])
t("свод упал: rc=1, карточка не пишется, запись summary с coverage всё же есть",
  rc2 == 1 and not out2.exists() and last2["role"] == "summary" and last2["status"] == "error" and "coverage" in last2)

# пустой раунд
(W / "room.jsonl").write_text(rec(id="s9", round="r9", phase="blind", voice="arr", role="seed", text="q", called=["claude"]) + "\n", encoding="utf-8")
c9 = choir.coverage("r9")
t("затравка без ответов: фаза есть, звали 1, missing 1", c9["phases"][0]["called"] == ["claude"] and c9["phases"][0]["fell"]["claude"]["status"] == "missing")
t("раунда нет — карта пуста, текст не падает", choir.coverage("nope")["phases"] == [] and "долги ответов по раунду: нет" in choir.coverage_text(choir.coverage("nope")))

print(f"\n{ok} PASS, {bad} FAIL")
import shutil; shutil.rmtree(W, ignore_errors=True)
sys.exit(1 if bad else 0)
