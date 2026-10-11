#!/usr/bin/env python3
"""Пересказ по жребию в витке (cmd_rebut, решение Автора 2026-10-06):
голос, чей канал не принимает полный пакет витка (PACKET_CAP — gemini),
получает чужие ответы пересказом участника, выбранного жребием drand;
пересказ — запись role=digest, у адресата eyes=digest/digest_by/digest_id,
в карте покрытия — пометка [digest]. Без моделей и без сети: ask_one и
beacon подменены, журнал — подменный."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="digtest.", dir="/var/tmp"))
os.environ["CHOIR_ROOM"] = str(W / "room.jsonl")
os.environ["CHOIR_DEBTS"] = str(W / "debts.jsonl")
os.environ["CHOIR_JOURNAL"] = str(W)
os.environ["CHOIR_RT_NO_BWRAP"] = "1"
os.environ["CHOIR_PACKET_CAP_GEMINI"] = "9000"      # маленький потолок, чтобы пакет витка его превысил
os.environ["CHOIR_DIGEST_MIN"] = "500"
os.environ.pop("CHOIR_REBUT_QUOTE_LIMIT", None)
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


import re
NUM = {"claude": 1, "codex": 2, "gemini": 3, "grok": 4}
LONG = {v: f"Ответ номер {NUM[v]}: " + (f"довод {NUM[v]} " * 400) for v in NUM}   # без имён голосов: имя в тексте — не утечка механики
room = [
    rec(id="l1", phase="pick", voice="choir", role="lot", conductor="codex", text="codex",
        candidates=["claude", "codex", "gemini", "grok"]),
    rec(id="s1", phase="blind", voice="arr", role="seed", text="затравка", called=["claude", "codex", "gemini", "grok"]),
    rec(id="a1", phase="blind", voice="claude", role="answer", status="ok", text=LONG["claude"]),
    rec(id="a2", phase="blind", voice="codex", role="answer", status="ok", text=LONG["codex"]),
    rec(id="a3", phase="blind", voice="gemini", role="answer", status="ok", text=LONG["gemini"]),
    rec(id="a4", phase="blind", voice="grok", role="answer", status="ok", text=LONG["grok"]),
]
(W / "room.jsonl").write_text("\n".join(room) + "\n", encoding="utf-8")

calls = []          # (voice, prompt, phase, visibility)
def digest_for(prompt):
    """Подменный пересказ: по одной сжатой строке на каждую метку из промпта."""
    labs = re.findall(r"^### Голос (\S+)$", prompt, re.M)
    return "\n\n".join(f"### Голос {l}\nсжато […]" for l in labs)


def fake_ask_one(name, prompt, round_id, phase, parent, visibility, use_role=True, _fallback=None):
    calls.append((name, prompt, phase, visibility))
    text = digest_for(prompt) if "ПЕРЕСКАЗАТЬ" in prompt else f"уточнение {name}"
    return {"id": f"x{len(calls)}", "ts": "2026-10-06T10:05:00+00:00", "round": round_id, "phase": phase,
            "voice": name, "role": "answer", "parent": parent, "visibility": visibility,
            "status": "ok", "text": text, "elapsed_s": 1.0, "prompt_sha": choir._sha(prompt)}


choir.ask_one = fake_ask_one
choir.beacon = lambda: {"round": 777, "signature": "ab" * 48}
choir._dump = lambda *a, **k: None

rc = choir.cmd_rebut(argparse.Namespace(round="r1", out=None, force=False, effort=None, focus=None, project=None))
t("cmd_rebut отработал (rc=0)", rc == 0)
by = {}
for c in calls:
    by.setdefault(c[0], []).append(c)
t("каждый из четырёх голосов вызван на виток", all(v in by for v in ("claude", "codex", "gemini", "grok")))
digest_calls = [c for c in calls if "ПЕРЕСКАЗАТЬ" in c[1]]
t("пересказ заказан ровно один — для gemini", len(digest_calls) == 1)
t("пересказчику не называют ни адресата, ни потолок канала",
  "9000" not in digest_calls[0][1] and "gemini" not in digest_calls[0][1] and "не гадайте" in digest_calls[0][1])
t("пересказчику уходят ВСЕ четыре ответа под метками (включая адресата — метка не выдаёт его)",
  len(re.findall(r"^### Голос \S+$", digest_calls[0][1], re.M)) == 4 and LONG["gemini"] in digest_calls[0][1])
writer = digest_calls[0][0]
DIG = digest_for(digest_calls[0][1])        # что реально вернул подменный пересказчик
t("пересказчик — не сам gemini и из ответивших", writer in ("claude", "codex", "grok"))
# жребий воспроизводим: sha256(signature:sha(round+phase+voice)) mod N по отсортированным кандидатам
import hashlib
mix = hashlib.sha256(f"{'ab' * 48}:{choir._sha('r1' + 'rebut1' + 'gemini')}".encode()).hexdigest()
expect = sorted(["claude", "codex", "grok"])[int(mix, 16) % 3]
t("пересказчик выбран по формуле жребия (проверяемо)", writer == expect)
t("пересказчику ушли чужие ответы под метками и бюджет", "### Голос" in digest_calls[0][1] and "не более" in digest_calls[0][1])
gem = [c for c in by["gemini"] if c[2] == "rebut1"][0]
t("пакет gemini — с пересказом и пометкой, оригиналов в нём нет",
  "ПЕРЕСКАЗ, не оригиналы" in gem[1] and DIG in gem[1] and LONG["claude"] not in gem[1])
t("пакет gemini уложился в потолок", len(gem[1]) + len(choir.project_notice() + choir.goal_notice()) <= 9000)
t("свой ответ gemini в его пакете дословно", LONG["gemini"] in gem[1])
oth = [c for c in by["claude"] if c[2] == "rebut1"][0]
t("остальные читают оригиналы целиком (обрезки нет)", LONG["codex"] in oth[1] and LONG["grok"] in oth[1] and "обрезано" not in oth[1])

recs = [json.loads(l) for l in (W / "room.jsonl").read_text(encoding="utf-8").splitlines()]
dig = [r for r in recs if r.get("role") == "digest"]
t("запись пересказа в журнале: role=digest, для кого, кем, жребий (mix_sha, index), бюджет, кандидаты, used",
  len(dig) == 1 and dig[0]["digest_for"] == "gemini" and dig[0]["voice"] == writer
  and dig[0]["drand_round"] == 777 and dig[0]["digest_budget"] > 0 and dig[0]["digest_candidates"] == ["claude", "codex", "grok"]
  and dig[0]["mix_sha"] == mix and dig[0]["index"] == int(mix, 16) % 3 and dig[0]["digest_used"] is True)
g = [r for r in recs if r.get("role") == "answer" and r.get("voice") == "gemini" and r.get("phase") == "rebut1"][0]
t("запись gemini: eyes=digest, digest_by, digest_id указывают на пересказ",
  g.get("eyes") == "digest" and g.get("digest_by") == writer and g.get("digest_id") == dig[0]["id"])
c = [r for r in recs if r.get("role") == "answer" and r.get("voice") == "claude" and r.get("phase") == "rebut1"][0]
t("у читавшего оригиналы метки digest нет", "eyes" not in c)
cov = choir.coverage("r1")
ph = {p["phase"]: p for p in cov["phases"]}
t("карта покрытия: gemini с пометкой [digest], пересказ не считается ответом",
  ph["rebut1"]["flags"].get("gemini") == ["digest"] and ph["rebut1"]["ok"] == ["claude", "codex", "gemini", "grok"])
t("шапка карты называет пометку", "gemini [digest]" in choir.coverage_card_head(cov, "x"))

# ── свод видит пересказ отдельным разделом, не позицией ──
seen = {}
def fake_sum(name, prompt, round_id, phase, parent, visibility, use_role=False, _fallback=None):
    seen["prompt"] = prompt
    return {"id": "sum", "ts": "2026-10-06T10:07:00+00:00", "round": round_id, "phase": phase,
            "voice": name, "status": "ok", "text": "свод", "elapsed_s": 1.0}
choir.ask_one = fake_sum
choir.cmd_summarize(argparse.Namespace(round="r1", by="codex", out=str(W / "SUM.md")))
t("промпт свода: раздел «Пересказы (НЕ позиции)» с текстом пересказа, автором и адресатом; легенда [digest] в карте",
  "## Пересказы (НЕ позиции)" in seen["prompt"] and DIG in seen["prompt"]
  and f"пересказ для gemini (rebut1), писал {writer}" in seen["prompt"]
  and "digest = читал пересказ" in seen["prompt"])
t("текст пересказа в промпте свода один раз и после раздела позиций",
  seen["prompt"].count(DIG) == 1 and seen["prompt"].index(DIG) > seen["prompt"].index("## Пересказы"))

# ── пересказ длиннее бюджета → отвергнут, gemini oversize БЕЗ eyes=digest ──
def run_rebut():
    (W / "room.jsonl").write_text("\n".join(room) + "\n", encoding="utf-8")
    calls.clear()
    return choir.cmd_rebut(argparse.Namespace(round="r1", out=None, force=False, effort=None, focus=None, project=None))
def recs_now():
    return [json.loads(l) for l in (W / "room.jsonl").read_text(encoding="utf-8").splitlines()]
def ask_with_cap(digest_text=None, digest_status="ok"):
    def f(name, prompt, *a, **k):
        cap = choir.PACKET_CAP.get(name.split("-")[0], 0)
        if cap and len(prompt) + len(choir.project_notice() + choir.goal_notice()) > cap:
            return {"id": f"ov{len(calls)}", "ts": "2026-10-06T10:06:00+00:00", "round": "r1", "phase": a[1], "voice": name,
                    "role": "answer", "status": "oversize", "text": "", "packet_chars": len(prompt), "packet_cap": cap}
        if "ПЕРЕСКАЗАТЬ" in prompt and digest_text is not None:
            calls.append((name, prompt, a[1], a[2]))
            txt = digest_text(prompt) if callable(digest_text) else digest_text
            return {"id": f"d{len(calls)}", "ts": "2026-10-06T10:06:00+00:00", "round": "r1", "phase": a[1], "voice": name,
                    "role": "answer", "status": digest_status, "text": txt, "elapsed_s": 1.0}
        return fake_ask_one(name, prompt, *a, **k)
    return f
choir.beacon = lambda: {"round": 778, "signature": "cd" * 48}
choir.ask_one = ask_with_cap(lambda pr: digest_for(pr).replace("сжато […]", "x" * 2500))
run_rebut()
r3 = recs_now()
d3 = [r for r in r3 if r.get("role") == "digest"]
g3 = [r for r in r3 if r.get("role") == "answer" and r.get("voice") == "gemini" and r.get("phase") == "rebut1"][0]
t("пересказ длиннее бюджета: запись digest с used=false и причиной; gemini — oversize без eyes=digest",
  len(d3) == 1 and d3[0]["digest_used"] is False and "пакет с пересказом" in d3[0]["digest_reject"]
  and g3["status"] == "oversize" and "eyes" not in g3 and "digest_by" not in g3)
choir.ask_one = ask_with_cap("ПАС")
run_rebut()
d4 = [r for r in recs_now() if r.get("role") == "digest"]
t("пересказ «ПАС» (меток нет) отвергнут с причиной про метки", d4 and d4[0]["digest_used"] is False and "меток" in d4[0]["digest_reject"])
choir.ask_one = ask_with_cap("", digest_status="error")
run_rebut()
d5 = [r for r in recs_now() if r.get("role") == "digest"]
t("пересказчик упал: запись digest со статусом error, used=false", d5 and d5[0]["status"] == "error" and d5[0]["digest_used"] is False)

# ── опоздавший с потолком: gemini выпал в слепой фазе, входит неслепым — пересказ той же веткой ──
room_late = [r for r in room if '"a3"' not in r] + [
    rec(id="a3", phase="blind", voice="gemini", role="answer", status="quota", text="")]
(W / "room.jsonl").write_text("\n".join(room_late) + "\n", encoding="utf-8")
calls.clear()
choir.ask_one = fake_ask_one
choir.cmd_rebut(argparse.Namespace(round="r1", out=None, force=False, effort=None, focus=None, project=None))
gl = [c for c in calls if c[0] == "gemini" and c[2] == "rebut1"]
rl = [r for r in recs_now() if r.get("role") == "answer" and r.get("voice") == "gemini" and r.get("phase") == "rebut1"][0]
t("опоздавший с потолком: пакет — неслепой вход с пересказом; запись nonblind + eyes=digest",
  gl and "первичного слепого" in gl[0][1] and "ПЕРЕСКАЗ, не оригиналы" in gl[0][1]
  and rl.get("nonblind") is True and rl.get("eyes") == "digest")

# ── маяк недоступен → пересказа нет, gemini уходит oversize (fail-closed) ──
(W / "room.jsonl").write_text("\n".join(room) + "\n", encoding="utf-8")
calls.clear()
def no_beacon():
    raise RuntimeError("drand down")
choir.beacon = no_beacon
choir.ask_one = ask_with_cap()
rc2 = choir.cmd_rebut(argparse.Namespace(round="r1", out=None, force=False, effort=None, focus=None, project=None))
recs2 = [json.loads(l) for l in (W / "room.jsonl").read_text(encoding="utf-8").splitlines()]
g2 = [r for r in recs2 if r.get("role") == "answer" and r.get("voice") == "gemini" and r.get("phase") == "rebut1"]
t("маяк недоступен: пересказа нет, gemini записан oversize, остальные ответили",
  not any("ПЕРЕСКАЗАТЬ" in c[1] for c in calls) and g2 and g2[0]["status"] == "oversize"
  and not [r for r in recs2 if r.get("role") == "digest"] and rc2 == 0)

import shutil
shutil.rmtree(W, ignore_errors=True)
print(f"\ndigest: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
