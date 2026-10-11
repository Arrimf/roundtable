#!/usr/bin/env python3
"""Карта покрытия раунда — по журналу, одним модулем для дирижёра и окна.

Наказ Автора 2026-09-22 («положи карту покрытия в промпт сводчика»)
после двух сводов подряд, соврав одинаково: «все пять голосов ответили»
при шести вызванных (свод kimi) и «все четыре» при двух
долгах (свод deepseek). Сводчику давали только
удавшиеся ответы, и «все» он считал по ним; обрубок упавшего Джемини
(188 символов при status=error) содержал позицию, которую свод записал
как консенсус без автора (нашли grok и kimi, 20.09).

Модуль чистый: на вход — записи раунда (read_round) и открытые долги,
на выход — словарь и строки. Ни дирижёр (choir.py), ни окно
(roundtable.py) не считают состав по-своему: две арифметики разошлись бы
молча (первая редакция карты в choir.py считала иначе, чем round_view,
— ревизия 22.09: codex, grok, kimi, deepseek).

ПРАВИЛА СЧЁТА (ревизия 22.09):
- аннулированные записи (role=annul, ids) не существуют ни для карты, ни
  для тела свода — как в cmd_rebut и round_view;
- у голоса в фазе ОДНА действующая запись: приоритет ok › pass › отказ,
  при равном статусе — поздняя по времени (время разбирается, не
  сравнивается строкой: пояса в журнале бывали разные); та же запись
  идёт и в тело свода, и в карту — иначе сводчик видел позицию, которую
  карта отрицала;
- схема 1 без поля phase — слепая фаза (как у round_view);
- витки — любые rebutN, без потолка: `rebut --force` допускает и
  десятый;
- звали: слепая — `called` затравки плюс все, у кого есть запись (отказ
  канала — тоже запись); виток — метки записи unveil этой фазы плюс все,
  у кого есть запись; позванный без записи — missing;
- обрубок — непустой текст при статусе отказа: не позиция; в тело свода
  он идёт отдельным разделом с пометкой, чтобы правило «цитировать
  только как обрубок» было выполнимо (kimi);
- карта считается в момент сборки промпта и описывает то, что сводчик
  видел; ответ, добранный за время свода, в неё не входит по замыслу.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_REBUT = re.compile(r"^rebut(\d+)$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Ключи и идентификаторы организаций в причинах отказов (429 Кими нёс
# org-… и префикс ak-… — субагент, ревизия 22.09): в промпт сводчика
# другого провайдера и в журнал такое не едет.
_SECRET = re.compile(r"\b(org|ak|sk|key|token)[-_][A-Za-z0-9_-]{4,}")
_RANK = {"ok": 0, "pass": 1}
_RU = {"blind": "слепая фаза"}


def phase_of(rec: dict) -> str:
    return rec.get("phase") or "blind"


def phase_key(ph: str):
    if ph == "blind":
        return (0, 0)
    m = _REBUT.match(ph)
    return (1, int(m.group(1))) if m else (2, 0)


def phase_name(ph: str) -> str:
    m = _REBUT.match(ph)
    return _RU.get(ph) or (f"виток {m.group(1)}" if m else ph)


def _when(rec: dict) -> float:
    s = str(rec.get("ts") or "")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def clean_detail(detail, limit: int = 160) -> str:
    """Причина отказа в одну строку: без ANSI, без ключей, ПОСЛЕДНЯЯ
    непустая строка (в ней причина; первые — повторы «503, повтор через
    4 с»), не длиннее limit."""
    txt = _ANSI.sub("", str(detail or ""))
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    last = _SECRET.sub(lambda m: m.group(1) + "-…", " ".join(last.split()))
    return last if len(last) <= limit else last[:limit - 1] + "…"


def annulled_ids(rnd: list[dict]) -> set:
    return {i for x in rnd if x.get("role") == "annul" for i in (x.get("ids") or [])}


def _better(r: dict, cur: dict) -> bool:
    rk, ck = _RANK.get(r.get("status"), 2), _RANK.get(cur.get("status"), 2)
    if rk != ck:
        return rk < ck
    return _when(r) >= _when(cur)


def effective(rnd: list[dict]) -> dict:
    """{фаза: {голос: действующая запись}} — без аннулированных."""
    ann = annulled_ids(rnd)
    out: dict = {}
    for r in rnd:
        if r.get("role") != "answer" or (r.get("id") and r.get("id") in ann):
            continue
        ph, v = phase_of(r), r.get("voice") or "?"
        cur = out.setdefault(ph, {}).get(v)
        if cur is None or _better(r, cur):
            out[ph][v] = r
    return out


def called_in(rnd: list[dict], ph: str) -> list[str]:
    """Кого звали в фазе по служебным записям: затравка (blind, `called`)
    или unveil витка (`labels` — метки голосов)."""
    if ph == "blind":
        # ВСЕ затравки слепой фазы: повторный ask подмножества (добор
        # после аварии) не должен вычёркивать позванных первой затравкой
        # (субагент, ревизия 22.09).
        out: list[str] = []
        for x in rnd:
            if x.get("role") == "seed" and phase_of(x) == "blind":
                out += [str(v) for v in (x.get("called") or []) if str(v) not in out]
        return out
    unv = [x for x in rnd if x.get("role") == "unveil" and x.get("phase") == ph]
    return [str(v) for v in (unv[-1].get("labels") or {})] if unv else []


def body_records(rnd: list[dict]) -> list[dict]:
    """Что уходит сводчику как позиции: действующие ok/pass по фазам."""
    eff = effective(rnd)
    out = []
    for ph in sorted(eff, key=phase_key):
        for v in sorted(eff[ph]):
            r = eff[ph][v]
            if r.get("status") in ("ok", "pass"):
                out.append(r)
    return out


def stub_records(rnd: list[dict]) -> list[dict]:
    """Обрубки: действующие записи с отказом канала и непустым текстом."""
    eff = effective(rnd)
    out = []
    for ph in sorted(eff, key=phase_key):
        for v in sorted(eff[ph]):
            r = eff[ph][v]
            if r.get("status") not in ("ok", "pass") and (r.get("text") or "").strip():
                out.append(r)
    return out


def compute(rnd: list[dict], debts=(), round_id: str | None = None) -> dict:
    eff = effective(rnd)
    phases = set(eff)
    if called_in(rnd, "blind"):
        phases.add("blind")
    out_ph = []
    for ph in sorted(phases, key=phase_key):
        by_voice = eff.get(ph, {})
        called = sorted(set(called_in(rnd, ph)) | set(by_voice))
        ok = sorted(v for v, r in by_voice.items() if r.get("status") == "ok")
        passed = sorted(v for v, r in by_voice.items() if r.get("status") == "pass")
        fell = {v: {"status": r.get("status") or "?",
                    "detail": clean_detail(r.get("detail"))}
                for v, r in by_voice.items() if r.get("status") not in ("ok", "pass")}
        for v in called:
            if v not in by_voice:
                fell[v] = {"status": "missing", "detail": "записи ответа нет"}
        stubs = {v: len(r.get("text") or "") for v, r in by_voice.items()
                 if r.get("status") not in ("ok", "pass") and (r.get("text") or "").strip()}
        flags = {v: [f for f in ("nonblind", "late", "recovered") if r.get(f)]
                 + (["digest"] if r.get("eyes") == "digest" else [])   # читал пересказ, не оригиналы
                 + (["packet_changed"] if r.get("packet_same") is False else [])   # добор другим пакетом
                 for v, r in by_voice.items()}
        out_ph.append({"phase": ph, "called": called, "ok": ok, "pass": passed,
                       "fell": dict(sorted(fell.items())), "stubs": stubs,
                       "flags": {v: f for v, f in flags.items() if f}})
    dl = sorted(f"{d.get('voice')} ({d.get('phase')}, {d.get('status')})"
                for d in debts if round_id is None or d.get("round") == round_id)
    return {"phases": out_ph, "debts": dl}


def text(cov: dict) -> str:
    """Карта словами — в промпт сводчика."""
    lines = ["КАРТА ПОКРЫТИЯ (по журналу, не по памяти):"]
    for ph in cov.get("phases") or []:
        parts = [f"звали {len(ph['called'])} — {', '.join(ph['called']) or 'никого'}",
                 f"ответили {len(ph['ok'])} — {', '.join(ph['ok']) or 'никто'}"]
        if ph["pass"]:
            parts.append(f"ПАС {len(ph['pass'])} — {', '.join(ph['pass'])}")
        if ph["fell"]:
            parts.append("упали: " + "; ".join(
                f"{v} ({d['status']}" + (f": {d['detail']}" if d["detail"] else "") + ")"
                for v, d in ph["fell"].items()))
        if ph["stubs"]:
            parts.append("обрубки (текст при отказе канала — НЕ позиция, приводить "
                         "только как обрубок с пометкой): " + ", ".join(
                             f"{v} — {n} симв." for v, n in ph["stubs"].items()))
        if ph["flags"]:
            parts.append("пометки: " + ", ".join(
                f"{v} [{'/'.join(f)}]" for v, f in ph["flags"].items())
                + ("; digest = читал пересказ чужих ответов, не оригиналы (правило 14.5)"
                   if any("digest" in f for f in ph["flags"].values()) else ""))
        lines.append(f"- {phase_name(ph['phase'])}: " + "; ".join(parts))
    lines.append("- долги ответов по раунду: " + (", ".join(cov.get("debts") or []) or "нет"))
    lines.append("Выводы верны СРЕДИ ОТВЕТИВШИХ. Голос без ответа в «единогласно» "
                 "не входит; слово «все» без числа в своде запрещено.")
    return "\n".join(lines)


def head(cov: dict, by: str | None) -> str:
    """Одна строка-цитата для шапки карточки, карточки раунда в окне и
    вопроса «Развить тему» — из журнала, не от модели."""
    bits = []
    for ph in cov.get("phases") or []:
        b = f"{phase_name(ph['phase'])} — звали {len(ph['called'])}, ответили {len(ph['ok'])}"
        if ph["pass"]:
            b += f", ПАС {len(ph['pass'])}"
        if ph["fell"]:
            b += ", упали: " + ", ".join(f"{v} ({d['status']})" for v, d in ph["fell"].items())
        # Неслепые/поздние/восстановленные — в шапку: механическая «правда
        # о составе» не должна объявлять слепым добранный ответ (субагент).
        marked = [f"{v} [{'/'.join(f)}]" for v, f in ph["flags"].items()]
        if marked:
            b += ", с пометками: " + ", ".join(marked)
        bits.append(b)
    debts = cov.get("debts") or []
    return ("> Карта покрытия (по журналу): " + "; ".join(bits)
            + ("; долги: " + ", ".join(debts) if debts else "; долгов нет")
            + (f". Свод писал {by}." if by else "."))
