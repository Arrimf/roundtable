#!/usr/bin/env python3
"""Jev у дирижёра: проверки, которые только ПОМЕЧАЮТ (раунд jev-v2, пункты
1–4, приняты Автором 2026-10-07; одним заходом — решение Автора 2026-10-08).

Ни одна проверка не меняет текст, порядок, состав, жребий или усилие:
она пишет запись в room.jsonl (роль *_check / effort_shadow) и, если есть
что сказать, заметку в ленту. Решает человек (правило 10: дирижёр —
журналирующий координатор, а не арбитр). Слепой фазы проверки не
касаются: всё зовётся после её закрытия (решение Автора: «в слепой фазе
Jev не звать»).

1. Канарейка пересказа (1420, идея grok в jev-v2): пересказ для голоса с
   потолком канала (gemini) сверяется с первыми строками оригиналов —
   вывод каждого голоса сохранён? P < DIGEST_WARN → `digest_check.warn`.
2. Второй глаз на свод (1421): всё, что свод говорит о голосе X,
   подтверждается его ответами? P < SUMMARY_WARN → `summary_check.warn`.
3. Советник усилия (1422): тень. Jev оценивает затравку по шкале усилия,
   запись кладётся рядом с тем, каким усилием реально шёл каждый голос;
   ничего не применяется (Автор: «только понижать» — потом, по данным).
4. Опыт с вердиктами (1419) — разовый локальный скрипт, не здесь.

Каждый вызов идёт через прокси стола (chamber/jevproxy.py) по пропуску
`choir` — событие `jev` в ленте пишет прокси, как у голосов. Нет прокси
или CHOIR_JEV=0 / CHOIR_JEV_CHECKS=0 — проверок нет, раунд идёт как шёл.
У каждой проверки есть самопроверка подменой (`selftest`): по построению
известно, где ответ должен быть «нет», — так меряется, ловит ли она.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import jevproxy   # noqa: E402

DIGEST_WARN = float(os.environ.get("CHOIR_JEV_DIGEST_WARN") or 0.6)
SUMMARY_WARN = float(os.environ.get("CHOIR_JEV_SUMMARY_WARN") or 0.5)
# Бюджет состояния на вызов — в СИМВОЛАХ: у Jev 32k токенов на состояние
# и самый длинный вопрос, а русский текст идёт ~1,5 символа на токен
# (замер 2026-10-08: 37 188 символов = 24 555 токенов; 77 263 символа свода
# с ответами шести голосов — max_tokens_exceeded). 30 тыс. — с запасом под
# JSON и плотный текст (числа, код); переполнение всё равно ловится повтором.
STATE_CAP = int(os.environ.get("CHOIR_JEV_STATE_CHARS") or 30_000)
SUMMARY_CAP = 12_000         # символов свода в состоянии
VOICE_CAP = 14_000           # символов ответов одного голоса для сверки свода
EFFORT_LEVELS = ("low", "medium", "high", "max")
EFFORT_ORDER = {"low": 0, "minimal": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}


def enabled() -> bool:
    if os.environ.get("CHOIR_JEV", "1") == "0" or os.environ.get("CHOIR_JEV_CHECKS", "1") == "0":
        return False
    try:
        return bool(jevproxy.ensure())
    except Exception:                                   # noqa: BLE001
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    q = {"type": "noul", "instructions": instructions}
    crit = {k: v for k, v in (("true", true), ("false", false)) if v}
    if crit:
        q["criteria"] = crit
    return q


def choice(instructions: str, options: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, levels: list) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def ask(questions: dict, state, *, round_id: str, phase: str, project: str | None = None,
        timeout: float = 30.0, hold: str | None = None) -> tuple[dict | None, str]:
    """Один вызов Jev через прокси стола по одноразовому пропуску `choir`.
    → (answers | None, статус словами). Событие в ленту пишет прокси."""
    tok = jevproxy.issue_pass("choir", round_id, phase, blind=False, project=project,
                              call_dir=None, quota=1, hold=hold)
    try:
        body = json.dumps({"state": state, "questions": questions}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(jevproxy.BASE + "/v1/systemone", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {tok}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        ans = resp.get("answers")
        if not isinstance(ans, dict) or not ans:
            return None, "Jev ответил без ответов на вопросы"   # пустое — не «всё чисто» (ревизия)
        return ans, "ok"
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:                               # noqa: BLE001
            msg = ""
        return None, f"прокси ответил HTTP {e.code}: {str(msg)[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"прокси не ответил: {str(e)[:200]}"
    finally:
        try:
            jevproxy.revoke_pass(tok)
        except Exception:                               # noqa: BLE001
            pass


def _p(answers: dict | None, qid: str) -> float | None:
    a = (answers or {}).get(qid) or {}
    v = a.get("noul")
    return float(v) if isinstance(v, (int, float)) else None


def first_line(text: str) -> str:
    """Вывод ответа — первая содержательная строка: заголовки («# …»,
    «Ответ:», «1. Вывод») пропускаются, иначе канарейка проверяла бы
    заголовок (ревизия: субагент). Нет такой — первая непустая."""
    first = ""
    for ln in (text or "").splitlines():
        raw = ln.strip()
        s = raw.strip("*#>` ").strip()
        if not s:
            continue
        first = first or s
        if raw.startswith("#") or s.endswith(":") or len(s) < 20:
            continue
        return s[:600]
    return first[:600]


def _rec(role: str, round_id: str, phase: str, **kw) -> dict:
    return {"id": secrets.token_hex(6), "ts": _now(), "round": round_id, "phase": phase,
            "voice": "choir", "role": role, **kw}


# ── 1. канарейка пересказа ──────────────────────────────────────────

def digest_questions(first_lines: dict) -> dict:
    return {f"keep_{lab}": noul(
        f"Is the conclusion stated in `first_lines.{lab}` (the opening line of voice {lab}'s answer) "
        f"preserved in the section «### Голос {lab}» of `digest` — the same claim, not reversed, "
        f"not replaced by another voice's claim, not dropped?",
        true="the digest keeps that voice's conclusion",
        false="the conclusion is missing, reversed or swapped")
        for lab in sorted(first_lines)}


def digest_check(digest_text: str, sources: dict, *, round_id: str, phase: str, digest_id: str | None,
                 digest_for: str, project: str | None = None, asker=ask) -> dict:
    """sources: {метка: полный текст ответа}. → запись digest_check."""
    fl = {lab: first_line(t) for lab, t in sources.items() if first_line(t)}
    rec = _rec("digest_check", round_id, phase, digest_id=digest_id, digest_for=digest_for,
               threshold=DIGEST_WARN, labels=sorted(fl))
    if not fl:
        rec.update(status="empty", detail="у оригиналов нет первых строк")
        return rec
    state = {"digest": digest_text[:STATE_CAP], "first_lines": fl}
    answers, st = asker(digest_questions(fl), state, round_id=round_id, phase=f"{phase}:digest_check",
                        project=project)
    if answers is None:
        rec.update(status="error", detail=st)
        return rec
    p = {lab: _p(answers, f"keep_{lab}") for lab in fl}
    miss = sorted(lab for lab, v in p.items() if v is None)
    rec.update(status="partial" if miss else "ok", p=p,
               warn=sorted(lab for lab, v in p.items() if v is not None and v < DIGEST_WARN))
    if miss:
        rec["detail"] = f"Jev не ответил по меткам: {', '.join(miss)}"
    return rec


# ── 2. второй глаз на свод ──────────────────────────────────────────

ALIASES = {"claude": "Клод", "codex": "Кодекс", "grok": "Грок", "kimi": "Кими",
           "gemini": "Джемини", "deepseek": "ДипСик"}


def answers_of(body: list[dict]) -> dict:
    """{голос: его записи, ушедшие сводчику (cover.body_records: действующие
    ok/ПАС по фазам, без аннулированных)} — сверка по тому же корпусу, что
    видел сводчик (ревизия: субагент, grok)."""
    out: dict = {}
    for r in body:
        t = (r.get("text") or "").strip()
        if t:
            out.setdefault(r["voice"], []).append(f"[{r.get('phase')}]\n{t}")
    return {v: "\n\n".join(x) for v, x in out.items()}


# ЧТО ПОТЕРЯЛ СВОД (решение Автора 2026-10-08: «шкала важных потерь, чтобы
# замечать интересное»). Jev не пишет текст — он оценивает: насколько
# важное из ответов голоса не дошло до свода. Ступень 2+ — пометка
# «потеря», человек открывает ответ голоса и смотрит сам.
LOSS_LEVELS = [
    "nothing important from these answers is missing in the summary",
    "a minor point is missing",
    "an important point is missing (a distinct argument, number, objection or proposal)",
    "the central point of these answers is missing",
]
# Пометка — только «главное потеряно» (3): свод в 400 слов на шесть голосов почти
# всегда теряет отдельный довод (ступень 2), и пометка на 2 была бы шумом
# (ревизия: субагент); ступень 2 видна в карточке без ⚠
LOSS_WARN = int(os.environ.get("CHOIR_JEV_LOSS_WARN") or 3)


def _name(v: str) -> str:
    al = ALIASES.get(v.split("-")[0])
    return f"«{v}»" + (f" (also written «{al}»)" if al else "")


def summary_questions(voices: list) -> dict:
    name = _name
    loss = {f"loss_{v}": score(
        f"How much of what matters in `answers.{v}` (the answers of {name(v)}) is missing from `summary`? "
        f"Judge importance for the round's question (`question`, if given), not completeness: wording, "
        f"repetition and politeness do not count; a point the summary carries without naming {v} (as a "
        f"shared view or under another name) is not missing.", LOSS_LEVELS) for v in sorted(voices)}
    return {**loss, **{f"faithful_{v}": noul(
        f"Is everything `summary` says about the participant {name(v)} supported by `answers.{v}` "
        f"(that participant's own answers in this round)? Answer no only if the summary attributes to "
        f"them a position, number or proposal that is not in `answers.{v}`, or reverses one. If the "
        f"summary says nothing about them, answer yes.",
        true=f"what the summary says about {v} matches their answers, or it says nothing about them",
        false=f"the summary misattributes or reverses a position of {v}")
        for v in sorted(voices)}}


def _pack(voices: list, sizes: dict, room: int) -> list[list]:
    """Голоса — в вызовы так, чтобы сумма их долей влезала в `room`."""
    calls, cur, used = [], [], 0
    for v in voices:
        n = sizes[v]
        if cur and used + n > room:
            calls.append(cur); cur, used = [], 0
        cur.append(v); used += n
    if cur:
        calls.append(cur)
    return calls


def _clip(t: str, per: int) -> str:
    """Голова и хвост: в витках голос часто меняет позицию к концу, и
    обрезка «с конца» отрезала бы именно её (ревизия: субагент)."""
    if len(t) <= per:
        return t
    h = int(per * 0.4)
    return t[:h] + f"\n… [вырезано для сверки: {len(t) - per} из {len(t)} симв.] …\n" + t[-(per - h):]


def summary_check(summary_text: str, answers_by_voice: dict, *, round_id: str, by: str,
                  summary_id: str | None = None, project: str | None = None, question: str = "",
                  asker=ask) -> dict:
    """answers_by_voice: {голос: текст его ответов — те же записи, что видел
    сводчик}. Свод называет голоса по имени — сверка по именам (с русскими
    написаниями). Голоса пакуются в вызовы в пределах бюджета состояния
    (каждый вызов — событие jev в ленте). Переполнение у Jev — повтор ТОЙ
    ЖЕ группы с вдвое меньшим бюджетом вызова и долей голоса; обрезка
    помечена в состоянии и в записи (clipped)."""
    voices = sorted(v for v, t in answers_by_voice.items() if (t or "").strip())
    rec = _rec("summary_check", round_id, "summary", by=by, summary_id=summary_id,
               threshold=SUMMARY_WARN, voices=voices)
    if not voices or not (summary_text or "").strip():
        rec.update(status="empty", detail="нет свода или ответов")
        return rec
    summ = summary_text if len(summary_text) <= SUMMARY_CAP else _clip(summary_text, SUMMARY_CAP)
    p: dict = {}
    loss: dict = {}
    clipped: dict = {}
    calls = 0
    q_state = (question or "")[:1500]
    room = max(4000, STATE_CAP - len(summ) - len(q_state))
    queue = [v for v in voices]
    while queue:
        per = min(VOICE_CAP, room)
        sizes = {v: min(len(answers_by_voice[v]), per) for v in queue}
        group = _pack(queue, sizes, room)[0]
        for attempt in range(4):
            state_ans = {v: _clip(answers_by_voice[v], per) for v in group}
            answers, st = asker(summary_questions(group), {**({"question": q_state} if q_state else {}),
                                                           "summary": summ, "answers": state_ans},
                                round_id=round_id, phase="summary:summary_check", project=project)
            calls += 1
            if answers is not None:
                break
            if "max_tokens" not in st or (len(group) == 1 and per <= 2000):
                rec.update(status="error", detail=st, p=p or None, calls=calls, clipped=clipped or None)
                return rec
            # переполнение: группа пополам (если в ней больше одного голоса), иначе доля пополам
            if len(group) > 1:
                group = group[: len(group) // 2]
            else:
                per = max(2000, per // 2)
        else:
            rec.update(status="error", detail="бюджет Jev не вместил и после повторов", p=p or None,
                       calls=calls, clipped=clipped or None)
            return rec
        for v in group:
            p[v] = _p(answers, f"faithful_{v}")
            la = (answers or {}).get(f"loss_{v}") or {}
            try:
                loss[v] = int(float(la.get("score")) + 0.5) if la.get("score") is not None else None   # половина вверх
            except (TypeError, ValueError):
                loss[v] = None
            if len(answers_by_voice[v]) > per:
                clipped[v] = {"from": len(answers_by_voice[v]), "to": per}
            queue.remove(v)
    miss = sorted(v for v in voices if p.get(v) is None or loss.get(v) is None)
    rec.update(status="partial" if miss else "ok", p=p,
               warn=sorted(v for v, x in p.items() if x is not None and x < SUMMARY_WARN),
               loss=loss, loss_warn=sorted(v for v, x in loss.items() if x is not None and x >= LOSS_WARN),
               loss_levels=["ничего важного", "мелочь", "важное", "главное"],
               clipped=clipped or None, calls=calls)
    if miss:
        rec["detail"] = f"Jev не ответил по голосам: {', '.join(miss)}"
    return rec


# ── 3. советник усилия (тень) ───────────────────────────────────────

EFFORT_RUBRIC = [
    "low: a lookup, a yes/no or a short factual answer; little reasoning",
    "medium: a few reasoning steps over the given material",
    "high: multi-step analysis across several sources or trade-offs",
    "max: a hard open problem needing long, careful reasoning and verification",
]


def effort_shadow(seed_text: str, used: dict, *, round_id: str, project: str | None = None,
                  asker=ask) -> dict:
    """used: {голос: усилие, с которым он реально шёл (поле effort записи)}.
    Ничего не применяется: запись — данные для решения «понижать ли»."""
    rec = _rec("effort_shadow", round_id, "blind", used=dict(used), applied=False,
               rule="только запись; применять — после накопления данных и только понижать (Автор, 2026-10-07)")
    q = {"effort": score("How much reasoning effort does answering the question in `seed` well require "
                         "from a strong model?", EFFORT_RUBRIC)}
    answers, st = asker(q, {"seed": (seed_text or "")[:STATE_CAP]}, round_id=round_id,
                        phase="blind:effort_shadow", project=project)
    if answers is None:
        rec.update(status="error", detail=st)
        return rec
    a = (answers or {}).get("effort") or {}
    sc = a.get("score")
    try:
        lvl = EFFORT_LEVELS[int(round(float(sc)))] if sc is not None else None
    except (TypeError, ValueError, IndexError):
        lvl = None
    lower = {v: (lvl is not None and str(e) in EFFORT_ORDER and EFFORT_ORDER[str(e)] > EFFORT_ORDER[lvl])
             for v, e in used.items()}
    rec.update(status="ok" if lvl else "error", jev_level=lvl, score=sc,
               probabilities=a.get("probabilities"), would_lower=sorted(v for v, x in lower.items() if x))
    if not lvl:
        rec["detail"] = f"ответ Jev не разобран: {json.dumps(a, ensure_ascii=False)[:200]}"
    return rec


# ── заметки в ленту ─────────────────────────────────────────────────

def note_for(rec: dict) -> str | None:
    """Строка для ленты: только когда есть что сказать человеку."""
    if rec.get("status") == "error" and not rec.get("warn"):
        return f"Jev-проверка «{rec['role']}» раунда «{rec['round']}» не состоялась: {rec.get('detail', '')[:200]}"
    if rec.get("role") == "digest_check" and rec.get("warn"):
        ps = ", ".join(f"Голос {lab} P={rec['p'][lab]:.2f}" for lab in rec["warn"])
        return (f"⚠ канарейка пересказа (Jev): в пересказе для {rec.get('digest_for')} раунда «{rec['round']}» "
                f"вывод мог потеряться или исказиться — {ps} (порог {rec['threshold']}). Пересказ не изменён; "
                f"решает человек — сверьте с оригиналами в карточке раунда.")
    if rec.get("role") == "idea_scores":
        # одна заметка на раунд: только потерянные ценные (не дошли до свода), не
        # больше трёх — остальное в накопителе (ревизия: заметка на каждую
        # «стоящую» была бы шумом)
        # в ленту — только СТОЯЩИЕ потерянные (ступень ≥ worthy); «полезные»
        # потерянные — в накопителе и карточке (ревизия: шум на ступени 2)
        wv = rec.get("worthy_value")
        lost = sorted((it for it in rec.get("ideas") or []
                       if it.get("lost") and ((it.get("value") or 0) >= wv if wv is not None
                                              else (it.get("score") or 0) >= rec.get("worthy", 3))),
                      key=lambda it: -(it.get("value") or it.get("score") or 0))
        if not lost:
            return None
        lv = rec.get("levels") or []
        shown = lost[:3]
        return (f"💡 раунд «{rec['round']}»: дополнения, которые Jev считает ценными, а в свод они не попали — "
                + "; ".join(f"{it['voice']} [{it.get('value', it['score'])} — {lv[it['score']] if it['score'] is not None and it['score'] < len(lv) else ''}] "
                            f"«{it['text'][:160]}{'…' if len(it['text']) > 160 else ''}»" for it in shown)
                + (f" (и ещё {len(lost) - len(shown)})" if len(lost) > len(shown) else "")
                + ". Все — в накопителе вкладки «секретарь»; в работу — только кнопкой Автора.")
    if rec.get("role") == "summary_check" and (rec.get("warn") or rec.get("loss_warn")):
        cl = rec.get("clipped") or {}
        def cut(v):
            return f" (ответы обрезаны для сверки: {cl[v]['from']} → {cl[v]['to']} симв.)" if v in cl else ""
        parts = []
        if rec.get("warn"):
            ps = ", ".join(f"{v} P={rec['p'][v]:.2f}{cut(v)}" for v in rec["warn"])
            parts.append(f"сказанное о голосах {ps} может не совпадать с их ответами (порог {rec['threshold']})")
        if rec.get("loss_warn"):
            names = rec.get("loss_levels") or []
            ps = ", ".join(f"{v} — потеряно {names[rec['loss'][v]] if rec['loss'][v] < len(names) else rec['loss'][v]}{cut(v)}"
                           for v in rec["loss_warn"])
            parts.append(f"из ответов не дошло до свода: {ps} — стоит открыть их ответы")
        tail = f" Не все голоса сверены: {rec.get('detail')}." if rec.get("status") != "ok" else ""
        return (f"⚠ второй глаз на свод (Jev), раунд «{rec['round']}» (писал {rec.get('by')}): "
                + "; ".join(parts) + f". Свод не изменён; решает человек.{tail}")
    return None


def post_note(rec: dict, project: str | None = None) -> None:
    text = note_for(rec)
    if not text:
        return
    try:
        import live    # noqa: PLC0415  ленивый: choir не тянет live на импорте
        live.post("choir", "note", text, round=rec.get("round"), check=rec.get("role"),
                  **({"project": project} if project else {}))
    except Exception as e:                              # noqa: BLE001
        print(f"заметка Jev-проверки не записана: {e}", file=sys.stderr)


# ── самопроверка подменой ───────────────────────────────────────────

def selftest(room: Path, asker=ask) -> dict:
    """Проверить сами проверки на журнале: берётся последний принятый
    пересказ и последний свод с ответами; каждый проверяется дважды —
    как есть и с подменой по построению (первые строки двух голосов
    переставлены; ответы двух голосов переставлены). Ловит — если на
    подмене P падает ниже порога, а на правде — нет."""
    recs = [json.loads(l) for l in room.read_text(encoding="utf-8").splitlines() if l.strip()]
    out: dict = {}
    dig = [r for r in recs if r.get("role") == "digest" and r.get("digest_used") and r.get("status") == "ok"]
    if dig:
        d = dig[-1]
        rnd = [r for r in recs if r.get("round") == d["round"]]
        prev_phase = None
        # пересказ витка N сжимает ответы предыдущей фазы
        order = ["blind"] + [f"rebut{i}" for i in range(1, 10)]
        if d.get("phase") in order and order.index(d["phase"]) > 0:
            prev_phase = order[order.index(d["phase"]) - 1]
        # метки витка — из записи раскрытия (unveil): так их раздал дирижёр
        lab_of: dict = {}
        for r in rnd:
            if r.get("role") == "unveil" and r.get("phase") == d.get("phase"):
                for vv, ll in (r.get("labels") or {}).items():
                    lab_of.setdefault(vv, ll)
        src_by_label = {lab_of[r["voice"]]: r["text"] for r in rnd
                        if r.get("role") == "answer" and r.get("phase") == prev_phase
                        and r.get("status") == "ok" and r["voice"] in lab_of}
        if len(src_by_label) >= 2:
            true_rec = digest_check(d["text"], src_by_label, round_id=d["round"], phase=d["phase"],
                                    digest_id=d.get("id"), digest_for=d.get("digest_for", "?"), asker=asker)
            a_, b_ = sorted(src_by_label)[:2]
            swapped = dict(src_by_label); swapped[a_], swapped[b_] = src_by_label[b_], src_by_label[a_]
            fake_rec = digest_check(d["text"], swapped, round_id=d["round"], phase=d["phase"],
                                    digest_id=d.get("id"), digest_for=d.get("digest_for", "?"), asker=asker)
            out["digest"] = {"round": d["round"], "true_p": true_rec.get("p"), "swapped": [a_, b_],
                             "fake_p": fake_rec.get("p"),
                             "caught": bool(fake_rec.get("p")) and all(fake_rec["p"].get(x) is not None and fake_rec["p"][x] < DIGEST_WARN for x in (a_, b_)),
                             "false_alarm": bool(true_rec.get("warn")),
                             "status": [true_rec.get("status"), fake_rec.get("status")],
                             "detail": [true_rec.get("detail"), fake_rec.get("detail")]}
        else:
            out["digest"] = {"round": d["round"], "skipped": f"меток раскрытия {len(lab_of)}, оригиналов {len(src_by_label)}"}
    sums = [r for r in recs if r.get("role") == "summary" and r.get("status") == "ok"]
    if sums:
        s = sums[-1]
        import coverage as _cov   # noqa: PLC0415
        ans = answers_of(_cov.body_records([r for r in recs if r.get("round") == s["round"]]))
        if len(ans) >= 2:
            true_rec = summary_check(s["text"], ans, round_id=s["round"], by=s.get("voice", "?"), asker=asker)
            a_, b_ = sorted(ans)[:2]
            sw = dict(ans); sw[a_], sw[b_] = ans[b_], ans[a_]
            fake_rec = summary_check(s["text"], sw, round_id=s["round"], by=s.get("voice", "?"), asker=asker)
            out["summary"] = {"round": s["round"], "true_p": true_rec.get("p"), "swapped": [a_, b_],
                              "fake_p": fake_rec.get("p"),
                              "caught": bool(fake_rec.get("p")) and all(fake_rec["p"].get(x) is not None and fake_rec["p"][x] < SUMMARY_WARN for x in (a_, b_)),
                              "false_alarm": true_rec.get("warn"),
                              "status": [true_rec.get("status"), fake_rec.get("status")],
                              "detail": [true_rec.get("detail"), fake_rec.get("detail")],
                              "calls": [true_rec.get("calls"), fake_rec.get("calls")]}
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Jev-проверки дирижёра: самопроверка подменой")
    ap.add_argument("cmd", choices=["selftest"])
    ap.add_argument("--room", default=os.environ.get("CHOIR_ROOM") or str(HERE.parent / "journal" / "room.jsonl"))
    a = ap.parse_args(argv)
    if not enabled():
        print("Jev недоступен (нет прокси или ключа, либо CHOIR_JEV=0)", file=sys.stderr)
        return 2
    print(json.dumps(selftest(Path(a.room)), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
