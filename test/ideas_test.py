#!/usr/bin/env python3
"""Дополнения голосов («своя идея», chamber/ideas.py): извлечение всех форм
маркера, корпус без аннулированных, оценка Jev (подменён) — ценность и
«дошло ли до свода», накопитель (проект по правилу ленты, годная запись не
затирается неудачной), дозаполнение пачками с записью по ходу. Без сети."""
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(prefix="ideas."))
os.environ.update(CHOIR_JOURNAL=str(T), ROUNDTABLE_JOURNAL=str(T), CHOIR_ROOM=str(T / "room.jsonl"), CHOIR_RT_NO_BWRAP="1")
sys.path.insert(0, str(RT / "chamber"))
import ideas   # noqa: E402

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond); FAIL += not cond


forms = {
    "x\n\n**СВОЯ ИДЕЯ:** проверить хвост\nраспределения\n\nпост": "проверить хвост распределения",
    "x\n5. **СВОЯ ИДЕЯ:** номер перед маркером": "номер перед маркером",
    "x\n**5. СВОЯ ИДЕЯ:** номер внутри жирного": "номер внутри жирного",
    "x\n\n**СВОЯ ИДЕЯ.** точка вместо двоеточия": "точка вместо двоеточия",
    "x\n## 5. СВОЯ ИДЕЯ\n**Заголовок.** тело ниже заголовка\n\nдальше": "Заголовок. тело ниже заголовка",
    "x\n\n### Своя идея\n\nтело через пустую строку": "тело через пустую строку",
    "СВОЯ ИДЕЯ — тире после маркера": "тире после маркера",
    "x\n  - **Своя идея:** в пункте списка": "в пункте списка",
    "x\n\n`СВОЯ ИДЕЯ:` **в обратных кавычках**": "в обратных кавычках",
    "x\n\n**П.3 / СВОЯ ИДЕЯ.** с префиксом пункта": "с префиксом пункта",
    "решающий тест [СВОЯ ИДЕЯ]: в квадратных скобках": "в квадратных скобках",
    "Рвущая проверка (СВОЯ ИДЕЯ): в круглых скобках": "в круглых скобках",
}
check(all(ideas.extract(t) == want for t, want in forms.items()),
      "извлечение: двоеточие, точка, тире, номер, заголовок с телом ниже — все формы журнала: "
      + str([ideas.extract(t) for t, w in forms.items() if ideas.extract(t) != w]))
check(ideas.extract("п. 11 — редактура (лимит слов, «СВОЯ ИДЕЯ:»), не способ") == ""
      and ideas.extract("упоминание «СВОЯ ИДЕЯ:» посреди\n\n**СВОЯ ИДЕЯ:** настоящая идея тут") == "настоящая идея тут",
      "извлечение: упоминание посреди фразы — не идея; берётся маркер в начале строки")
recs = [
    {"round": "r1", "role": "lot", "project": "/p/A"},
    {"round": "r1", "role": "seed", "text": "Вопрос раунда про прокси"},
    {"round": "r1", "role": "answer", "phase": "blind", "voice": "grok", "status": "ok", "id": "a1", "text": "x\n\nСВОЯ ИДЕЯ: канарейка пересказа на подменах"},
    {"round": "r1", "role": "answer", "phase": "rebut1", "voice": "grok", "status": "ok", "id": "a2", "text": "y\n\nСВОЯ ИДЕЯ: канарейка пересказа на подменах"},
    {"round": "r1", "role": "answer", "phase": "blind", "voice": "kimi", "status": "ok", "id": "a3", "text": "z\n\nСВОЯ ИДЕЯ: ретривер досье по заголовкам карточек"},
    {"round": "r1", "role": "answer", "phase": "blind", "voice": "codex", "status": "timeout", "id": "a4", "text": "СВОЯ ИДЕЯ: из упавшего ответа"},
    {"round": "r1", "role": "answer", "phase": "rebut1", "voice": "gemini", "status": "ok", "id": "a5", "text": "СВОЯ ИДЕЯ: из аннулированного витка"},
    {"round": "r1", "role": "annul", "ids": ["a5"]},
    {"round": "r1", "role": "summary", "status": "ok", "id": "s1", "text": "Свод: прокси нужен; канарейка пересказа на подменах — принята."},
]
its = ideas.collect(recs, "r1")
check([i["voice"] for i in its] == ["grok", "kimi"],
      "сбор: только действующие ответы ok (аннулированный и упавший — нет), повтор идеи в витке — одна")
CALLS = []
def asker(qs, state, **kw):
    CALLS.append((qs, state, kw))
    out = {}
    for q in qs:
        iid = q.split("_", 1)[1]
        idea = state["ideas"][f"i_{iid}"]["idea"]
        out[q] = ({"type": "score", "score": 3.4} if q.startswith("v_") else
                  {"type": "noul", "noul": 0.9 if idea in state["rounds"][state["ideas"][f"i_{iid}"]["round"]]["summary"] else 0.1})
    return out, "ok"
rec = ideas.score_round(recs, "r1", asker=asker)
d = {i["voice"]: i for i in rec["ideas"]}
check(rec["status"] == "ok" and rec["project"] == "/p/A" and rec["summary_id"] == "s1" and len(CALLS) == 1
      and d["grok"]["lost"] is False and d["kimi"]["lost"] is True and d["kimi"]["in_summary"] == 0.1,
      "оценка: ценность и «дошло ли до свода»; ценное, не попавшее в свод, — потерянное")
st = CALLS[0][1]
check(st["rounds"]["r1"]["question"].startswith("Вопрос раунда") and all("question" not in v for v in st["ideas"].values()),
      "состояние: вопрос и свод раунда — один раз, идеи ссылаются на раунд")
import jevtable  # noqa: E402
n = jevtable.note_for(rec)
check(n and "в свод они не попали" in n and "kimi" in n and "grok" not in n, "заметка: только потерянные ценные")
check(jevtable.note_for({**rec, "ideas": [d["grok"]]}) is None, "заметка: нет потерянных — нет заметки")
# переполнение — пачка делится
calls2 = []
def over(qs, state, **kw):
    calls2.append(len(state["ideas"]))
    if len(state["ideas"]) > 1:
        return None, "прокси ответил HTTP 400: max_tokens_exceeded"
    return asker(qs, state)
rec2 = ideas.score_round(recs, "r1", asker=over)
check(rec2["status"] == "ok" and calls2 == [2, 1, 1], "переполнение: пачка делится пополам до одной идеи")
# все ответы пустые — error, не partial
rec3 = ideas.score_round(recs, "r1", asker=lambda qs, state, **kw: ({"x": {}}, "ok"))
check(rec3["status"] == "error", "Jev ответил без нужных ключей — error, не partial")
# накопитель: годная запись не затирается неудачной; проект по belongs
import live  # noqa: E402
bad = {**rec3, "ts": "2099"}
led = ideas.ledger([rec, bad], project="/p/A", taken={d["grok"]["id"]}, belongs=lambda e, pj: live.belongs(e, Path(pj)))
check([x["voice"] for x in led] == ["kimi", "grok"] and led[0]["lost"] and led[1]["taken"],
      "накопитель: годная оценка не затёрта неудачной; потерянные сверху; взятые помечены")
old = {**rec, "round": "r0", "project": None}
check(ideas.ledger([old], project="/p/A", belongs=lambda e, pj: live.belongs(e, Path(pj))) == []
      and len(ideas.ledger([old], project=None)) == 2,
      "накопитель: запись без проекта не утекает в окно чужого проекта; окно без проекта видит всё")
# дозаполнение: пачки через раунды, запись по ходу, неудачные — дооцениваются
room = T / "room.jsonl"
room.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs + [
    {"round": "r3", "role": "seed", "text": "второй вопрос"},
    {"round": "r3", "role": "answer", "phase": "blind", "voice": "claude", "status": "ok", "text": "СВОЯ ИДЕЯ: сверка счёта effort по ленте"},
    {**rec3, "round": "r3"}]) + "\n", encoding="utf-8")
CALLS.clear(); got = []
new = ideas.backfill(room, asker=asker, on_record=got.append)
check(sorted(r["round"] for r in new) == ["r1", "r3"] and len(CALLS) == 1 and len(got) == 2 and all(r.get("backfill") for r in new),
      "дозаполнение: раунд с неудачной оценкой дооценивается; два раунда одной пачкой; записи отдаются по ходу")
# раунд без свода — «не с чем сверять»: потерянных нет, вопроса «дошло ли» нет
nos = [r for r in recs if r.get("role") != "summary"]
CALLS.clear()
r4 = ideas.score_round(nos, "r1", asker=asker)
check(r4["has_summary"] is False and not any(it["lost"] for it in r4["ideas"]) and r4["status"] == "ok"
      and not any(q.startswith("s_") for q in CALLS[-1][0]), "раунд без свода: «дошло ли» не спрашивается, потерянных нет")
# длинный свод: хвост (где раздел идей) доходит до Jev
long = recs[:-1] + [{**recs[-1], "text": "голова " * 3000 + " канарейка пересказа на подменах"}]
CALLS.clear()
ideas.score_round(long, "r1", asker=asker)
check("канарейка пересказа на подменах" in CALLS[-1][1]["rounds"]["r1"]["summary"], "длинный свод: обрезка головой и хвостом — конец свода в состоянии")
# проект: явный → затравка → жребий
check(ideas.round_project([{"round": "z", "role": "seed", "project": "/p/S"}], "z") == "/p/S"
      and ideas.round_project(recs, "r1", "/p/X") == "/p/X", "проект раунда: явный, затем из затравки (раунды без записи жребия)")
check(ideas._lvl(2.5) == 3 and ideas._lvl(1.49) == 1 and ideas._lvl(None) is None, "ступень: округление «половина вверх»")
# накопитель: одно на (раунд, голос)
dup = {**rec, "ideas": rec["ideas"] + [{**d["kimi"], "id": "zzzzzzzzzzzz", "score": 1, "lost": False, "text": "переформулировка"}]}
check([x["voice"] for x in ideas.ledger([dup])].count("kimi") == 1, "накопитель: одно дополнение на голос в раунде")
late = {**rec, "ideas": [{**d["kimi"], "phase": "blind", "lost": True, "score": 4},
                         {**d["kimi"], "id": "yyyyyyyyyyyy", "phase": "rebut1", "lost": False, "score": 1, "text": "отзываю прежнее"}]}
check([x["id"] for x in ideas.ledger([late])] == ["yyyyyyyyyyyy"], "накопитель: берётся последнее по фазе (то, что голос оставил в силе)")
part = {**rec, "status": "partial", "ts": "2099"}
check(ideas.latest([rec, part])["r1"]["status"] == "ok", "неполная повторная оценка не затирает полную")
qr = [{"round": "q", "role": "seed_expanded", "text": "Пишу затравку…"}, {"round": "q", "role": "seed", "text": "Вопрос " + "x" * 3000 + " ХВОСТ"}]
qq = ideas.round_question(qr, "q")
check(qq.startswith("Вопрос") and qq.rstrip().endswith("ХВОСТ") and "Пишу" not in qq, "вопрос раунда — из файла затравки, длинный — головой и хвостом")

# непрерывная ценность: значение сохраняется, «стоящее» — по порогу значения
check(d["kimi"]["value"] == 3.4 and d["kimi"]["score"] == 3, "ценность Jev хранится непрерывной (3.4), ступень — для подписи")
# пересчёт из сохранённых ответов Jev без вызовов
feed = T / "feed.jsonl"
old_rec = {**rec, "ideas": [{k: v for k, v in it.items() if k not in ("value",)} for it in rec["ideas"]], "ts": "2026-10-11T00:00:00+00:00"}
feed.write_text(json.dumps({"kind": "jev", "status": "ok", "answers": {f"v_{d['kimi']['id']}": {"score": 3.2}, f"s_{d['kimi']['id']}": {"noul": 0.2},
                                                                      f"v_{d['grok']['id']}": {"score": 2.1}, f"s_{d['grok']['id']}": {"noul": 0.9}}}) + "\n", encoding="utf-8")
room2 = T / "room2.jsonl"; room2.write_text(json.dumps(old_rec, ensure_ascii=False) + "\n", encoding="utf-8")
rd = ideas.rederive(room2, feed)
dd = {it["voice"]: it for it in rd[0]["ideas"]} if rd else {}
check(rd and rd[0]["rederived"] and dd["kimi"]["value"] == 3.2 and dd["kimi"]["lost"] and not dd["grok"]["lost"],
      "пересчёт: непрерывная ценность и «потерянное» из сохранённых ответов, без вызовов")

feed.write_text(json.dumps({"kind": "jev", "status": "ok", "answers": {f"v_{d['kimi']['id']}": {"score": 3.9}}}) + "\n", encoding="utf-8")
check(ideas.rederive(room2, feed) == [], "пересчёт: ответы не на все дополнения раунда — запись не трогается (оценка не затирается)")

# очередь
import secretary  # noqa: E402
e = secretary.queue_add("Дополнение kimi", by="arr", project=str(T), idea_id=d["kimi"]["id"], idea_round="r1")
q = [x for x in secretary.fold_queue(all_projects=True) if x["item"] == e["id"]][0]
check(q.get("idea_id") == d["kimi"]["id"] and q.get("idea_round") == "r1", "очередь: пункт из дополнения несёт idea_id и раунд")

import shutil
shutil.rmtree(T, ignore_errors=True)
print(f"\nideas: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
