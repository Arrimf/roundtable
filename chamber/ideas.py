#!/usr/bin/env python3
"""Дополнения голосов, которые теряются, — накопитель с оценкой Jev.

Наказ Автора 2026-10-08: «"своя идея" — что, по-твоему, остальные
пропустили… я постоянно вижу в ответах дополнения, но они все теряются».
Правило 15: каждый ответ раунда кончается «СВОЯ ИДЕЯ: …» — мысль, которой,
по мнению голоса, ни у кого больше не будет. К 08.10 таких 399 в ответах
56 раундов, и жили они только внутри ответов: свод в 400 слов на шесть
голосов их почти не несёт.

Что делается:
- `extract` — дополнение из ответа (маркер в начале строки: «СВОЯ ИДЕЯ:»,
  «**СВОЯ ИДЕЯ.**», «5. СВОЯ ИДЕЯ», заголовок «### Своя идея» с телом ниже);
- `score_round` — Jev по каждому дополнению отвечает на два вопроса:
  ЦЕННОСТЬ для вопроса раунда (шкала 0–4) и ДОШЛО ЛИ ДО СВОДА (да/нет).
  Ценное и не дошедшее — «потерянное»: его накопитель показывает первым;
- `ledger` — накопитель проекта во вкладке «секретарь» с кнопкой «в очередь».

КАК НЕ РАЗВЕТВИТЬСЯ (вопрос Автора): дополнение само никуда не идёт.
Накопитель — список, не очередь; в работу его переносит только Автор
кнопкой (пункт очереди с полем idea_id). Вкладка показывает не всё:
потерянные ценные сверху, свежие следом, остальное — `ideas.py list`.
Оценка Jev — подсветка, не фильтр.

Оценка зовётся дирижёром после УДАЧНОГО свода (там же, где второй глаз),
один раз на свод; прошлые раунды — `ideas.py backfill`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jevtable   # noqa: E402
import coverage as cover   # noqa: E402

# Маркер — В НАЧАЛЕ СТРОКИ (упоминание посреди фразы — не идея): необязательные
# «#», «**», номер «5.», снова «**», затем «СВОЯ ИДЕЯ». Формы из журнала 08.10:
# «**СВОЯ ИДЕЯ:**», «5. **СВОЯ ИДЕЯ:**», «**5. СВОЯ ИДЕЯ:**», «**СВОЯ ИДЕЯ.**»,
# «## 5. СВОЯ ИДЕЯ» и «### Своя идея» с телом ниже (первая редакция требовала
# двоеточие и теряла 14 % — у Джемини 29 %; ревизия: субагент)
# + пункт списка «- **Своя идея:**», обратные кавычки «`СВОЯ ИДЕЯ:`», префикс
# «**П.3 / СВОЯ ИДЕЯ.**»; посреди строки — только в скобках «[СВОЯ ИДЕЯ]:»,
# «(СВОЯ ИДЕЯ):» (упоминание в «ёлочках» — не идея). Замер 08.10: 394 из 399.
IDEA_RE = re.compile(
    r"(?m)(?:^[ \t]*(?:[-*+][ \t]+)?(?:#+[ \t]*)?(?:\*\*|`)?[ \t]*(?:[^\n*`«»]{0,12}?/[ \t]*)?"
    r"(?:\d+[.)][ \t]*)?(?:\*\*|`)?[ \t]*СВОЯ\s+ИДЕЯ\b|[\[(]СВОЯ\s+ИДЕЯ[\])])", re.I)
IDEA_CAP = 1200               # символов дополнения в состоянии Jev и в записи
QUESTION_CAP = 1500           # символов вопроса раунда (один раз на раунд в состоянии)
SUMMARY_CAP = 12000           # символов свода — головой и хвостом (jevtable._clip): раздел
                              # «свои идеи» стоит в КОНЦЕ свода, обрезка с головы давала
                              # ложные «не дошло» (ревизия: 14 из 26 сводов длиннее 4000)
# Jev отдаёт НЕПРЕРЫВНУЮ оценку (ожидаемая ступень, напр. 2,66): округление до
# ступени схлопывало 317 из 397 дополнений в «3» (замер дозаполнения 11.10).
# Пороги — по непрерывной: медиана 2,77, 85-й процентиль 3,00, 90-й — 3,07; «стоящее»
# от 3,05 — около верхних 10 %.
WORTHY = int(os.environ.get("CHOIR_IDEA_WORTHY") or 3)            # ступень (для подписи)
WORTHY_VALUE = float(os.environ.get("CHOIR_IDEA_WORTHY_VALUE") or 3.05)   # «стоящее» — ≈ верхние 10 %
LOST_VALUE = float(os.environ.get("CHOIR_IDEA_LOST_VALUE") or 2.5)        # «ценное» для «потерянного»
LOST_P = 0.5                  # P(дошло до свода) ниже — «не дошло»
LEVELS = [
    "restates the obvious, repeats the question or is off-topic",
    "a minor angle: correct but adds little",
    "useful: a distinct check, angle or step worth noting",
    "strong: new and actionable — could change what the round decides",
    "exceptional: reframes the problem or exposes a flaw no one else saw",
]
LEVEL_RU = ["очевидное", "мелкий угол", "полезное", "стоящее", "исключительное"]


def extract(text: str) -> str:
    """Дополнение из ответа: после ПОСЛЕДНЕГО маркера в начале строки —
    остаток строки, а если он пуст (заголовок) — следующий абзац; до пустой
    строки. Последнее — первое бывает в пересказе затравки."""
    t = text or ""
    ms = list(IDEA_RE.finditer(t))
    if not ms:
        return ""
    rest = t[ms[-1].end():]
    rest = re.sub(r"^[ \t]*[\])]?[ \t]*[*`]*[ \t]*[:.—–\-]?[ \t]*[*`]*[ \t]*", "", rest)
    line, _, tail = rest.partition("\n")
    if line.strip():
        body = line + "\n" + tail
    else:
        body = tail.lstrip("\n")
    para = re.split(r"\n[ \t]*\n", body.strip(), maxsplit=1)[0]
    return " ".join(para.replace("**", "").split())[:IDEA_CAP]


def idea_id(round_id: str, voice: str, text: str) -> str:
    return hashlib.sha256(f"{round_id}\n{voice}\n{text}".encode("utf-8")).hexdigest()[:12]


def collect(recs: list[dict], round_id: str) -> list[dict]:
    """Дополнения раунда из ДЕЙСТВУЮЩИХ ответов ok (без аннулированных —
    coverage.effective, тот же корпус, что у сводчика; ревизия), одно на
    (голос, текст): повтор той же идеи в витке не плодит вторую."""
    rnd = [r for r in recs if r.get("round") == round_id]
    eff = cover.effective(rnd)
    out, seen = [], set()
    for ph in sorted(eff, key=cover.phase_key):
        for v in sorted(eff[ph]):
            r = eff[ph][v]
            if r.get("role") != "answer" or r.get("status") != "ok":
                continue
            txt = extract(r.get("text") or "")
            if len(txt) < 15 or (v, txt) in seen:
                continue
            seen.add((v, txt))
            out.append({"id": idea_id(round_id, v, txt), "voice": v, "phase": r.get("phase"),
                        "answer_id": r.get("id"), "text": txt})
    return out


def round_question(recs: list[dict], round_id: str) -> str:
    """Вопрос раунда — запись `seed` (файл затравки, который голоса читали; её же
    видит сводчик и сверка свода), не `seed_expanded`: тот бывает черновиком
    модели («Пишу затравку…») или пустым отказом (ревизия: grok). Длинный —
    головой и хвостом: подвопросы и требования стоят в конце."""
    seeds = [r for r in recs if r.get("round") == round_id and r.get("role") == "seed"
             and (r.get("text") or "").strip()]
    t = (seeds[-1].get("text") if seeds else "") or ""
    return t if len(t) <= QUESTION_CAP else jevtable._clip(t, QUESTION_CAP)


def round_summary(recs: list[dict], round_id: str) -> dict | None:
    s = [r for r in recs if r.get("round") == round_id and r.get("role") == "summary" and r.get("status") == "ok"]
    return s[-1] if s else None


def round_project(recs: list[dict], round_id: str, explicit: str | None = None) -> str | None:
    """Проект раунда: явный (дирижёр знает свой PROJECT) → запись затравки →
    запись жребия. У раундов без записи жребия проект лежит в
    затравке — иначе их дополнения уходили бы в чужие окна (ревизия)."""
    if explicit:
        return explicit
    for role in ("seed_expanded", "seed", "lot"):
        rs = [r for r in recs if r.get("round") == round_id and r.get("role") == role and r.get("project")]
        if rs:
            return str(rs[-1]["project"])
    return None


def _lvl(v) -> int | None:
    """Ступень из непрерывной оценки Jev: округление «половина вверх» (round()
    банковский — 2.5 → 2, 3.5 → 4; пороги срабатывали бы несимметрично)."""
    try:
        return int(float(v) + 0.5) if v is not None else None
    except (TypeError, ValueError):
        return None


def _questions(rid: str, items: list[dict], has_summary: bool = True) -> dict:
    qs = {}
    for it in items:
        k = f"i_{it['id']}"
        qs[f"v_{it['id']}"] = jevtable.score(
            f"`ideas.{k}.idea` is a point a participant added, believing the others missed it. How valuable "
            f"is it for the question in `rounds.{rid}.question`? Value means new (not a restatement of the "
            f"question or common advice) and actionable.", LEVELS)
        if not has_summary:
            continue
        qs[f"s_{it['id']}"] = jevtable.noul(
            f"Is the point of `ideas.{k}.idea` present in `rounds.{rid}.summary` (the round's summary) — "
            f"stated or clearly covered, under any name?",
            true="the summary carries this point", false="the summary does not carry it")
    return qs


def _chunks(groups: list[tuple[str, dict, list[dict]]], budget: int) -> list[list[tuple[str, dict, list[dict]]]]:
    """Пачки раундов в пределах бюджета: вопрос и свод раунда — ОДИН раз в
    состоянии, идеи ссылаются на раунд (ревизия: вопрос у каждой идеи
    удваивал бюджет)."""
    out, cur, used = [], [], 0
    for rid, head, items in groups:
        n = len(head.get("question", "")) + len(head.get("summary", "")) + sum(len(i["text"]) + 150 for i in items)
        if cur and used + n > budget:
            out.append(cur); cur, used = [], 0
        cur.append((rid, head, items)); used += n
    if cur:
        out.append(cur)
    return out


def _ask_chunk(chunk, *, phase: str, project: str | None, asker, round_label: str) -> tuple[dict, list[str]]:
    """Один вызов на пачку; переполнение у Jev — пачка делится пополам (по
    раундам, затем по идеям), как у сверки свода."""
    state = {"rounds": {rid: head for rid, head, _ in chunk},
             "ideas": {f"i_{it['id']}": {"round": rid, "idea": it["text"]} for rid, _, items in chunk for it in items}}
    qs: dict = {}
    for rid, head, items in chunk:
        qs.update(_questions(rid, items, has_summary=bool(head.get("summary"))))
    answers, st = asker(qs, state, round_id=round_label, phase=phase, project=project)
    if answers is not None:
        return answers, []
    if "max_tokens" in st:
        if len(chunk) > 1:
            h = len(chunk) // 2
            a1, e1 = _ask_chunk(chunk[:h], phase=phase, project=project, asker=asker, round_label=round_label)
            a2, e2 = _ask_chunk(chunk[h:], phase=phase, project=project, asker=asker, round_label=round_label)
            return {**a1, **a2}, e1 + e2
        rid, head, items = chunk[0]
        if len(items) > 1:
            h = len(items) // 2
            a1, e1 = _ask_chunk([(rid, head, items[:h])], phase=phase, project=project, asker=asker, round_label=round_label)
            a2, e2 = _ask_chunk([(rid, head, items[h:])], phase=phase, project=project, asker=asker, round_label=round_label)
            return {**a1, **a2}, e1 + e2
    return {}, [st]


def _value(v) -> float | None:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def _apply(items: list[dict], answers: dict, *, has_summary: bool = True) -> None:
    for it in items:
        raw = (answers.get(f"v_{it['id']}") or {}).get("score")
        it["value"] = _value(raw)
        it["score"] = _lvl(raw)
        s = (answers.get(f"s_{it['id']}") or {}).get("noul") if has_summary else None
        # свода нет — не с чем сверять: «потерянным» дополнение не объявляется
        # (ревизия: 37 % накопителя были бы ложными «потерянными»)
        it["in_summary"] = float(s) if isinstance(s, (int, float)) else None
        it["lost"] = bool(it["value"] is not None and it["value"] >= LOST_VALUE
                          and it["in_summary"] is not None and it["in_summary"] < LOST_P)


def _record(rid: str, items: list[dict], *, project, summary_id, errs: list[str], backfill: bool = False,
            has_summary: bool = True, round_ts: str | None = None) -> dict:
    n_ok = sum(1 for it in items if it.get("score") is not None)
    full = all(it.get("in_summary") is not None for it in items) if has_summary else True
    status = "ok" if n_ok == len(items) and full else ("error" if n_ok == 0 else "partial")
    rec = jevtable._rec("idea_scores", rid, "ideas", project=project, summary_id=summary_id, ideas=items,
                        worthy=WORTHY, worthy_value=WORTHY_VALUE, lost_value=LOST_VALUE,
                        lost_p=LOST_P, levels=LEVEL_RU, status=status,
                        has_summary=has_summary, round_ts=round_ts,
                        **({"backfill": True} if backfill else {}))
    if status != "ok":
        rec["detail"] = ("; ".join(errs) if errs else "Jev не ответил по части вопросов")[:400]
    return rec


def _round_ts(recs: list[dict], round_id: str) -> str | None:
    ts = [str(r.get("ts")) for r in recs if r.get("round") == round_id and r.get("role") == "answer" and r.get("ts")]
    return max(ts) if ts else None


def _head(recs: list[dict], round_id: str) -> tuple[dict, dict | None]:
    summ = round_summary(recs, round_id)
    st = (summ or {}).get("text") or ""
    return {"question": round_question(recs, round_id),
            "summary": st if len(st) <= SUMMARY_CAP else jevtable._clip(st, SUMMARY_CAP)}, summ


def score_round(recs: list[dict], round_id: str, *, summary_id: str | None = None, asker=None,
                project: str | None = None) -> dict | None:
    """Запись idea_scores раунда (None — дополнений нет). summary_id —
    свод, против которого сверено «дошло ли»."""
    asker = asker or jevtable.ask
    items = collect(recs, round_id)
    if not items:
        return None
    head, summ = _head(recs, round_id)
    proj = round_project(recs, round_id, project)
    answers, errs = {}, []
    for ch in _chunks([(round_id, head, items)], jevtable.STATE_CAP):
        a, e = _ask_chunk(ch, phase="ideas", project=proj, asker=asker, round_label=round_id)
        answers.update(a); errs += e
    _apply(items, answers, has_summary=bool(head["summary"]))
    return _record(round_id, items, project=proj, summary_id=summary_id or (summ or {}).get("id"), errs=errs,
                   has_summary=bool(head["summary"]), round_ts=_round_ts(recs, round_id))


# ── накопитель ──────────────────────────────────────────────────────

_RANK = {"ok": 2, "partial": 1}


def _usable(r: dict) -> bool:
    return r.get("status") in _RANK


def latest(recs: list[dict]) -> dict:
    """{раунд: лучшая последняя запись idea_scores}: полная сильнее неполной,
    неполная сильнее неудачной; при равенстве — свежая. Неполная повторная не
    затирает полную (ревизия: grok)."""
    out: dict = {}
    for r in recs:
        if r.get("role") != "idea_scores":
            continue
        rd = r.get("round")
        if rd not in out or _RANK.get(r.get("status"), 0) >= _RANK.get(out[rd].get("status"), 0):
            out[rd] = r
    return out


def ledger(recs: list[dict], *, project: str | None = None, taken: set | None = None, belongs=None) -> list[dict]:
    """Все оценённые дополнения. project — окно этого проекта: принадлежность
    по тому же правилу, что лента (belongs: записи без проекта — только у
    окон стола и песочницы; ревизия: 64 % старых идей без проекта утекали во
    все окна). Порядок: потерянные ценные → по ступени → свежие."""
    out = []
    for rnd, r in latest(recs).items():
        if project is not None and belongs is not None and not belongs({"project": r.get("project")}, project):
            continue
        # одно дополнение на (раунд, голос) — ПОСЛЕДНЕЕ по фазе: то, что голос
        # оставил в силе (в витке он часто переформулирует или отзывает
        # прежнее; ревизия: grok — в очередь попадала отозванная); все — в карточке
        best: dict = {}
        for it in r.get("ideas") or []:
            v = it.get("voice")
            key = cover.phase_key(str(it.get("phase") or "blind"))
            if v not in best or key >= best[v][0]:
                best[v] = (key, it)
        wv = r.get("worthy_value", WORTHY_VALUE)
        for _, it in best.values():
            sc = it.get("score")
            val = it.get("value")
            out.append({**it, "round": rnd, "ts": r.get("round_ts") or r.get("ts"), "project": r.get("project"),
                        "summary_id": r.get("summary_id"), "partial": r.get("status") != "ok",
                        "worthy": (val >= wv) if val is not None else (sc is not None and sc >= r.get("worthy", WORTHY)),
                        "taken": bool(taken and it.get("id") in taken)})
    def val(x):
        return x["value"] if x.get("value") is not None else (x["score"] if x.get("score") is not None else -1)
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    out.sort(key=lambda x: -val(x))
    out.sort(key=lambda x: 0 if x.get("lost") else 1)
    return out


def rederive(room: Path, feed: Path) -> list[dict]:
    """Пересчитать записи idea_scores из УЖЕ полученных ответов Jev (события
    jev ленты хранят answers целиком) — без новых вызовов: непрерывная
    ценность и «потерянное» по новым порогам. → новые записи (rederived)."""
    vals: dict = {}
    for line in feed.read_text(encoding="utf-8").splitlines():
        if '"kind": "jev"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("status") == "ok" and isinstance(e.get("answers"), dict):
            for k, a in e["answers"].items():
                if isinstance(a, dict):
                    vals[k] = a
    recs = [json.loads(l) for l in room.read_text(encoding="utf-8").splitlines() if '"idea_scores"' in l]
    out = []
    for rd, r in latest(recs).items():
        items = [dict(it) for it in r.get("ideas") or []]
        if all(it.get("value") is not None for it in items):
            continue
        ans = {}
        for it in items:
            for pre in ("v_", "s_"):
                if f"{pre}{it['id']}" in vals:
                    ans[f"{pre}{it['id']}"] = vals[f"{pre}{it['id']}"]
        # пересчёт — только если ответы есть на ВСЕ дополнения раунда: иначе
        # прежняя оценка части идей затёрлась бы пустой (ревизия)
        if not all(f"v_{it['id']}" in ans for it in items):
            continue
        _apply(items, ans, has_summary=bool(r.get("has_summary", True)))
        rec = _record(rd, items, project=r.get("project"), summary_id=r.get("summary_id"), errs=[],
                      backfill=bool(r.get("backfill")), has_summary=bool(r.get("has_summary", True)),
                      round_ts=r.get("round_ts"))
        rec["rederived"] = True
        out.append(rec)
    return out


def backfill(room: Path, *, asker=None, rounds: list[str] | None = None, on_record=None) -> list[dict]:
    """Оценить раунды без ГОДНОЙ записи idea_scores (неудачные дооцениваются).
    Пачки идут через раунды; запись раунда отдаётся `on_record` сразу по
    его пачке — прерывание не теряет оплаченное (ревизия)."""
    asker = asker or jevtable.ask
    recs = [json.loads(l) for l in room.read_text(encoding="utf-8").splitlines() if l.strip()]
    good = {rd for rd, r in latest(recs).items() if r.get("status") == "ok"}   # неполные — дооцениваются
    order: list[str] = []
    for r in recs:
        rd = r.get("round")
        if rd and rd not in good and rd not in order and r.get("role") == "answer":
            order.append(rd)
    if rounds:
        order = [x for x in order if x in rounds]
    groups = []
    for rd in order:
        its = collect(recs, rd)
        if its:
            head, summ = _head(recs, rd)       # те же потолки, что у живой оценки — оценки сравнимы
            groups.append((rd, head, its, (summ or {}).get("id")))
    out = []
    for ch in _chunks([(rd, h, its) for rd, h, its, _ in groups], jevtable.STATE_CAP):
        a, e = _ask_chunk(ch, phase="ideas", project=None, asker=asker, round_label="ideas-backfill")
        for rd, h, its in ch:
            _apply(its, a, has_summary=bool(h["summary"]))
            sid = next(s for r2, _, _, s in groups if r2 == rd)
            rec = _record(rd, its, project=round_project(recs, rd), summary_id=sid, errs=e, backfill=True,
                          has_summary=bool(h["summary"]), round_ts=_round_ts(recs, rd))
            out.append(rec)
            if on_record and rec["status"] != "error":     # провал целиком (прокси лёг) — в журнал не пишем
                on_record(rec)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Дополнения голосов («своя идея»): оценка Jev и накопитель")
    ap.add_argument("cmd", choices=["backfill", "list", "rederive"])
    ap.add_argument("--room", default=os.environ.get("CHOIR_ROOM") or str(HERE.parent / "journal" / "room.jsonl"))
    ap.add_argument("--round", action="append", default=None, help="только эти раунды (backfill)")
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args(argv)
    room = Path(a.room)
    if a.cmd == "list":
        recs = [json.loads(l) for l in room.read_text(encoding="utf-8").splitlines() if l.strip()]
        for it in ledger(recs)[: a.top]:
            sc = it.get("score")
            print(f"[{it.get('value') if it.get('value') is not None else (sc if sc is not None else '?')}]"
                  f"{' ПОТЕРЯНО' if it.get('lost') else ''} {it['round']} · "
                  f"{it['voice']}: {it['text'][:160]}")
        return 0
    if a.cmd == "rederive":
        import choir   # noqa: PLC0415
        import live    # noqa: PLC0415
        new = rederive(room, live.LIVE)
        for r in new:
            choir._append(r, room)
        print(f"пересчитано раундов: {len(new)} (без вызовов Jev)")
        return 0
    if not jevtable.enabled():
        print("Jev недоступен (нет прокси или ключа, либо CHOIR_JEV=0)", file=sys.stderr)
        return 2
    import choir   # noqa: PLC0415  запись в журнал — тем же замком, что у дирижёра
    new = backfill(room, rounds=a.round, on_record=lambda r: choir._append(r, room))
    n = sum(len(r["ideas"]) for r in new)
    w = sum(1 for r in new for it in r["ideas"] if (it.get("value") or 0) >= WORTHY_VALUE)
    lost = sum(1 for r in new for it in r["ideas"] if it.get("lost"))
    bad = [r["round"] for r in new if r["status"] != "ok"]
    print(f"оценено дополнений: {n} в {len(new)} раундах; стоящих (ценность ≥ {WORTHY_VALUE}): {w}; "
          f"ценных и не дошедших до свода: {lost}" + (f"; не всё оценено: {', '.join(bad)}" if bad else ""))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
