#!/usr/bin/env python3
"""Jev-проверки дирижёра (chamber/jevtable.py) без сети: Jev подменён.
Канарейка пересказа, второй глаз на свод, тень усилия — пороги, записи,
заметки; встраивание в choir (_jev_check): запись в комнату, раунд не
падает при сбое, выключатель CHOIR_JEV_CHECKS=0."""
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(prefix="jevtable."))
os.environ.update(CHOIR_JOURNAL=str(T), ROUNDTABLE_JOURNAL=str(T), CHOIR_ROOM=str(T / "room.jsonl"),
                  CHOIR_RT_NO_BWRAP="1", CHOIR_JEV_PASS_DIR=str(T / "pass"))
os.environ.pop("CHOIR_JEV_CHECKS", None)
os.environ.pop("CHOIR_JEV", None)
sys.path.insert(0, str(RT / "chamber"))
import jevtable   # noqa: E402

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond); FAIL += not cond


CALLS = []


def fake(answers_by_q):
    def _ask(questions, state, **kw):
        CALLS.append({"questions": questions, "state": state, **kw})
        return {q: answers_by_q(q) for q in questions}, "ok"
    return _ask


# ── канарейка пересказа ──
src = {"А": "Вывод: прокси нужен.\nдалее довод", "Б": "\n\nВывод: прокси не нужен.\n…", "В": ""}
r = jevtable.digest_check("### Голос А\nпрокси нужен\n### Голос Б\nпрокси нужен", src, round_id="r1", phase="rebut1",
                          digest_id="d1", digest_for="gemini",
                          asker=fake(lambda q: {"type": "noul", "noul": 0.9 if q == "keep_А" else 0.2}))
q = CALLS[-1]
check(set(q["questions"]) == {"keep_А", "keep_Б"} and q["state"]["first_lines"] == {"А": "Вывод: прокси нужен.", "Б": "Вывод: прокси не нужен."},
      "канарейка: вопрос на метку с первой строкой (пустые строки пропущены, пустой ответ — без вопроса)")
check(r["status"] == "ok" and r["warn"] == ["Б"] and r["p"]["А"] == 0.9 and r["role"] == "digest_check" and r["digest_for"] == "gemini",
      "канарейка: P ниже порога — пометка на метке, запись digest_check")
check(q["phase"] == "rebut1:digest_check" and q["round_id"] == "r1", "канарейка: вызов помечен раундом и фазой (событие jev в ленте)")
n = jevtable.note_for(r)
check(n and "канарейка пересказа" in n and "Голос Б P=0.20" in n and "не изменён" in n, "канарейка: заметка в ленту называет метку, P и что текст не тронут")
r2 = jevtable.digest_check("x", src, round_id="r1", phase="rebut1", digest_id="d1", digest_for="gemini",
                           asker=lambda *a, **k: (None, "прокси не ответил: timeout"))
check(r2["status"] == "error" and "timeout" in r2["detail"] and "не состоялась" in jevtable.note_for(r2), "канарейка: сбой Jev — запись error с причиной, не молчание")

# ── второй глаз на свод ──
ans = {"claude": "мой довод: А", "codex": "x" * 50000, "grok": ""}
r = jevtable.summary_check("claude считает А; codex считает Б", ans, round_id="r1", by="kimi",
                           asker=fake(lambda q: {"type": "noul", "noul": 0.95 if q.endswith("claude") else 0.1}))
q = CALLS[-1]
check(set(q["questions"]) == {"faithful_claude", "faithful_codex", "loss_claude", "loss_codex"} and "grok" not in q["state"]["answers"]
      and q["questions"]["loss_codex"]["type"] == "score" and len(q["questions"]["loss_codex"]["criteria"]) == 4,
      "свод: на каждого ответившего — верность (да/нет) и шкала потерь (4 ступени); пустые не спрашиваются")
r_l = jevtable.summary_check("свод", {"a": "ответ a", "b": "ответ b"}, round_id="r1", by="k",
                             asker=fake(lambda q: {"type": "score", "score": 3} if q == "loss_b" else
                                        ({"type": "score", "score": 0} if q.startswith("loss_") else {"type": "noul", "noul": 0.9})))
check(r_l["loss"] == {"a": 0, "b": 3} and r_l["loss_warn"] == ["b"] and r_l["warn"] == [] and r_l["status"] == "ok"
      and "не дошло до свода: b — потеряно главное" in (jevtable.note_for(r_l) or ""),
      "свод: шкала потерь — «главное» у b даёт пометку и заметку даже при верном своде")
r_m = jevtable.summary_check("свод", {"a": "ответ a"}, round_id="r1", by="k", question="вопрос раунда",
                             asker=fake(lambda q: {"type": "noul", "noul": 0.9} if q.startswith("faithful") else {}))
check(r_m["status"] == "partial" and CALLS[-1]["state"]["question"] == "вопрос раунда",
      "свод: шкала потерь без ответа — partial, не «чисто»; вопрос раунда в состоянии")
r_2 = jevtable.summary_check("свод", {"a": "x"}, round_id="r1", by="k",
                             asker=fake(lambda q: {"type": "score", "score": 2} if q.startswith("loss") else {"type": "noul", "noul": 0.9}))
check(r_2["loss_warn"] == [] and jevtable.note_for(r_2) is None, "свод: ступень 2 («важное») — без пометки, видна в карточке")
cx = q["state"]["answers"]["codex"]
check(len(cx) < 50000 and "вырезано для сверки" in cx and cx.startswith("x") and cx.rstrip().endswith("x"),
      "свод: длинные ответы обрезаны головой и хвостом с пометкой (бюджет состояния)")
check(r["warn"] == ["codex"] and r["by"] == "kimi" and r["clipped"]["codex"]["from"] == 50000 and "второй глаз" in jevtable.note_for(r)
      and "обрезаны для сверки" in jevtable.note_for(r), "свод: пометка, обрезка названа в записи и заметке — решает человек")
check("also written «Грок»" in jevtable.summary_questions(["grok"])["faithful_grok"]["instructions"]
      and "says nothing about them, answer yes" in jevtable.summary_questions(["grok"])["faithful_grok"]["instructions"],
      "свод: вопрос знает русское написание имени; молчание свода о голосе — не тревога")
# переполнение: группа делится, вызов уменьшается
seen = []
def over(questions, state, **kw):
    seen.append(sorted(state["answers"]))
    if len(state["answers"]) > 1:
        return None, "прокси ответил HTTP 400: {'error_type': 'upstream_error', 'message': 'max_tokens_exceeded'}"
    return {q: ({"type": "score", "score": 0} if q.startswith("loss_") else {"type": "noul", "noul": 0.9}) for q in questions}, "ok"
big = {v: "y" * 9000 for v in ("a", "b", "c")}
r = jevtable.summary_check("свод", big, round_id="r1", by="k", summary_id="s1", asker=over)
check(r["status"] == "ok" and set(r["p"]) == {"a", "b", "c"} and seen == [["a", "b", "c"], ["a"], ["b", "c"], ["b"], ["c"]]
      and r["calls"] == 5 and r["summary_id"] == "s1",
      f"свод: max_tokens — группа делится до одного голоса, все сверены ({len(seen)} вызовов)")
# частичный ответ Jev — не «чисто»
r = jevtable.summary_check("свод", {"a": "x", "b": "y"}, round_id="r1", by="k",
                           asker=fake(lambda q: {"type": "noul", "noul": 0.2} if q.endswith("a") else {}))
check(r["status"] == "partial" and r["warn"] == ["a"] and "не ответил" in r["detail"] and "Не все голоса сверены" in jevtable.note_for(r),
      "свод: Jev ответил не по всем — статус partial, пометка сохраняется, заметка говорит о пробеле")

# ── тень усилия ──
r = jevtable.effort_shadow("простой вопрос", {"claude": "max", "grok": "medium", "kimi": None}, round_id="r1",
                           asker=fake(lambda q: {"type": "score", "score": 1, "probabilities": {"0": 0.2, "1": 0.7}}))
check(r["status"] == "ok" and r["jev_level"] == "medium" and r["would_lower"] == ["claude"] and r["applied"] is False,
      "усилие: уровень Jev, кому можно было бы понизить; ничего не применено")
check(jevtable.note_for(r) is None, "усилие: заметки в ленту нет (только запись)")
r = jevtable.effort_shadow("q", {"x": "turbo", "y": "high"}, round_id="r1",
                           asker=fake(lambda q: {"type": "score", "score": 0}))
check(r["would_lower"] == ["y"], "усилие: неизвестная ступень не попадает в «понизить можно было бы»")
check(jevtable.first_line("# Ответ\nВывод:\nПрокси нужен, потому что ключ вне клетки.\n") == "Прокси нужен, потому что ключ вне клетки."
      and jevtable.first_line("коротко") == "коротко", "первая строка-вывод: заголовки пропускаются, короткий ответ — как есть")
check(CALLS[-1]["phase"] == "blind:effort_shadow", "усилие: зовётся с меткой фазы blind — после её закрытия (choir)")

# ── встраивание в choir ──
import choir   # noqa: E402
recs = []
choir._append = lambda rec, target=None: recs.append(rec)
posted = []
jevtable.post_note = lambda rec, project=None: posted.append(rec["role"])
jevtable.enabled = lambda: True
choir._jev_check(lambda: jevtable.digest_check("### Голос А\nx", {"А": "Вывод"}, round_id="r2", phase="rebut1",
                                                digest_id="d", digest_for="gemini",
                                                asker=fake(lambda q: {"type": "noul", "noul": 0.1})))
check(recs and recs[-1]["role"] == "digest_check" and recs[-1]["warn"] == ["А"] and posted == ["digest_check"],
      "choir._jev_check: запись в комнату и заметка")
def boom():
    raise RuntimeError("сеть")
n0 = len(recs)
choir._jev_check(boom)
check(len(recs) == n0, "choir._jev_check: исключение проверки — раунд идёт дальше, записи нет")
jevtable.enabled = lambda: False
choir._jev_check(lambda: (_ for _ in ()).throw(AssertionError("не должна звать")))
check(len(recs) == n0, "выключено (нет прокси, CHOIR_JEV=0 или CHOIR_JEV_CHECKS=0) — проверка не зовётся")
import importlib
os.environ["CHOIR_JEV_CHECKS"] = "0"
importlib.reload(jevtable)
check(jevtable.enabled() is False, "CHOIR_JEV_CHECKS=0 выключает проверки при живом прокси")
os.environ.pop("CHOIR_JEV_CHECKS")

# канарейка — после витка: rebut_build кладёт проверку в очередь, cmd_rebut исполняет её в finally
importlib.reload(jevtable)
jevtable.enabled = lambda: True
jevtable.post_note = lambda rec, project=None: None
choir.jevtable = jevtable
ran = []
def body(a):
    choir._JEV_AFTER_PHASE.append(lambda: (ran.append("check"), {"role": "digest_check", "round": "r", "phase": "rebut1", "status": "ok", "p": {}, "warn": []})[1])
    ran.append("phase")
    return 0
orig_body = choir._cmd_rebut_body
choir._cmd_rebut_body = body
choir.jevproxy.flush = lambda key, stale=False: 0
import argparse
choir.cmd_rebut(argparse.Namespace(round="r"))
choir._cmd_rebut_body = orig_body
check(ran == ["phase", "check"] and choir._JEV_AFTER_PHASE == [], "канарейка: зовётся после витка (finally cmd_rebut), очередь пуста")

# самопроверка: P = 0.0 на подмене — поймано; метки — из записи раскрытия
room = T / "room-self.jsonl"
rows = [
    {"round": "rs", "phase": "blind", "role": "answer", "voice": "claude", "status": "ok", "text": "Вывод claude: прокси нужен всегда."},
    {"round": "rs", "phase": "blind", "role": "answer", "voice": "codex", "status": "ok", "text": "Вывод codex: прокси не нужен вовсе."},
    {"round": "rs", "phase": "rebut1", "role": "unveil", "voice": "choir", "labels": {"claude": "А", "codex": "Б"}},
    {"round": "rs", "phase": "rebut1", "role": "digest", "voice": "kimi", "status": "ok", "digest_used": True, "digest_for": "gemini", "id": "dg",
     "text": "### Голос А\nВывод claude: прокси нужен всегда.\n### Голос Б\nВывод codex: прокси не нужен вовсе."},
]
room.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n", encoding="utf-8")
def judge(questions, state, **kw):
    out = {}
    for q in questions:
        lab = q.split("_", 1)[1]
        fl = state["first_lines"][lab]
        sec = state["digest"].split(f"### Голос {lab}\n", 1)[1].split("###")[0]
        out[q] = {"type": "noul", "noul": 0.95 if fl in sec else 0.0}
    return out, "ok"
res = jevtable.selftest(room, asker=judge)
check(res["digest"]["caught"] is True and res["digest"]["false_alarm"] is False and res["digest"]["fake_p"]["А"] == 0.0,
      "самопроверка: P=0.0 на подмене засчитано как поймано; метки из unveil; ложных тревог нет")

import shutil
shutil.rmtree(T, ignore_errors=True)
print(f"\njevtable: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
