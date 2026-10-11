#!/usr/bin/env python3
"""«Продолжить отсюда»: одно сообщение дословно как контекст следующего
вопроса — в комнате, быстром вопросе и раунде.

Решения раунда prodolzhit-lyuboe-v1 (2026-09-22, пять голосов; свод —
journal/rounds/AiSandbox/SUMMARY-prodolzhit-lyuboe-v1.md) и Автора
(2026-09-23):

- Уходит ОДНО сообщение дословно плюс новый вопрос Автора. Не ветка и не
  свод (свод — за «Развить тему»).
- Текст вставляет сервер по адресу и sha из журнала; клиент шлёт только
  адрес (правило 8.5: клиент мог бы подсунуть что угодно).
- Связь — новым полем `anchor` (скаляр: 3:2 за скаляр, довод grok —
  пустой список неотличим от отсутствия поля). `parent` и `continues`
  не трогать: у первого пять смыслов, у второго тип разошёлся.
- Якорем не бывает ПАС, отказ канала (error) и ответ незакрытой слепой
  фазы (его нет на диске — так что и адреса нет).
- В пакет раунда имя автора якоря НЕ идёт (решение Автора; правило 9:
  спорят с доводом, не с именем); в журнале имя есть — поле anchor.
- Потолок 6000 символов (решение Автора); больше — ОТКАЗ, не обрезка
  (четверо из пяти против любой обрезки). «Кратко» остаётся галочкой.
- В якорь идёт `text` — итог после снятия мыслей; мысли по правилу 17
  на экране есть, в якоре нет — полоска обязана это назвать (grok).
- Первый ход продолжения — в СВЕЖЕЙ сессии голоса (4 из 5): иначе голос
  помнит события после якоря, и «отсюда» врёт (codex по коду).
- Автор якоря — вне жребия ведущего и свода (4 из 5).
- Мягкий сигнал о монологе (цепочка ≥3 звеньев, где автор якоря равен
  единственному адресату), не предел: предел обойдут копипастой, и
  связь пропадёт из журнала.

Адрес якоря: `live:<id>` — событие ленты: с 2026-10-08 (решение Автора,
п. 3 кресла секретаря) ЛЮБОЕ событие с текстом, кроме ПАС, отказа канала и
событий со статусом-отказом (BAD_STATUS); `room:<id>` — запись комнаты
раундов room.jsonl (answer ok в закрытой фазе, summary ok, пересказ
digest, затравка seed/seed_expanded). Третий адресат — секретарь
(secretary_turn.py --anchor), блок с именем автора. Ответ витка несёт метки «Голос В», живущие внутри
своего раунда — это названо в блоке, расшифровка не даётся (claude).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

ANCHOR_CAP = 6000              # символов; решение Автора 2026-09-23
CHAIN_SOFT = 3                 # звеньев монолога подряд — мягкий сигнал
ADDR_RE = re.compile(r"^(live|room):([0-9a-zA-Z]{1,24})$")


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


class AnchorError(ValueError):
    pass


def _find_live(live_path: Path, ev_id: int) -> dict | None:
    """Событие ленты по id. Быстрый проход — по подстроке (`id` у
    live.post первый ключ, разделители json.dumps по умолчанию); не
    нашлось — полный разбор строк: формат мог бы измениться, и отказ
    «события нет» врал бы (deepseek)."""
    needle = f'"id": {ev_id},'
    for full in (False, True):
        try:
            with live_path.open(encoding="utf-8") as f:
                for line in f:
                    if not full and needle not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("id") == ev_id:
                        return e
        except OSError:
            return None
    return None


def _find_room(room_path: Path, rec_id: str) -> dict | None:
    needle = json.dumps(rec_id)
    try:
        with room_path.open(encoding="utf-8") as f:
            for line in f:
                if needle in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("id") == rec_id:
                        return r
    except OSError:
        return None
    return None


# Служебные авторы ленты: окно, комната, дирижёр. Их событие в блоке
# называется служебным, а не «голосом» (ревизия 2026-10-08).
SYS_AUTHORS = ("chamber", "choir", "roundtable")
# Статусы-отказы: событие с таким статусом якорем не бывает, даже с
# текстом. Список один для сервера и окна (окно получает его в /state),
# иначе кнопка есть, а POST даёт 400 (ревизия: субагент, grok). Статусы
# «ok / done / accepted / clean / …» — исходы, не отказы.
BAD_STATUS = ("error", "timeout", "stalled", "quota", "dropped", "empty", "missing",
              "busy", "auth", "crash", "aborted", "interrupted", "unknown",
              "upstream_error", "outcome_unknown")


def bad_status(st: str | None) -> bool:
    st = str(st or "")
    return st in BAD_STATUS or st.startswith("refused_")


def _jsonish(v) -> str:
    # как JSON.stringify в окне: компактно, без ASCII-экранирования — длина
    # у окна и сервера одна (потолок считают оба)
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(v)


def live_text(e: dict) -> str:
    """Текст события ленты для якоря. Одно правило у сервера и окна
    (anchorText в roundtable.py): у ревизии правки (edit_review) text —
    превью, полный текст в full_text; у отчёта ревизии (review_note) text —
    итог, full_text — запись в файле, итог и есть якорь (ревизия:
    субагент); у вызова Jev text — одна строка счётчика, в якорь идут и
    вопросы, состояние, ответы (ревизия: grok, субагент); у прочих — text."""
    kind = e.get("kind")
    if kind == "edit_review":
        return str(e.get("full_text") or e.get("text") or "").strip()
    text = str(e.get("text") or "").strip()
    if kind == "jev":
        parts = [text]
        for label, key in (("вопросы", "questions"), ("состояние", "state"), ("ответы", "answers")):
            if e.get(key) not in (None, "", {}, []):
                parts.append(f"{label}: {_jsonish(e[key])}")
        return "\n".join(p for p in parts if p).strip()
    return text


def _other_project(ev_project, project: str | None) -> bool:
    """Событие ДРУГОГО проекта? Сравнение по разрешённому пути: окно
    хранит проект как задан (`ROUNDTABLE_PROJECT`, cwd), ход секретаря —
    через resolve(); косая черта или ссылка на одной стороне делали бы
    свои события чужими (ревизия: субагент)."""
    if not project or not ev_project:
        return False
    a, b = str(ev_project), str(project)
    if a == b:
        return False
    try:
        return Path(a).resolve() != Path(b).resolve()
    except OSError:
        return True


def resolve(addr: str, *, live_path: Path, room_path: Path,
            project: str | None = None) -> dict:
    """Адрес → {addr, author, text, sha, kind, round?, phase?, at}.
    Отказы — AnchorError со словами для человека. project — проект окна:
    событие/запись ДРУГОГО проекта якорем не бывает (кнопка такого адреса
    не даст, ручной — дал бы; субагент); записи без поля проекта — общие,
    как у read_events."""
    m = ADDR_RE.match((addr or "").strip())
    if not m:
        raise AnchorError("адрес якоря: live:<id ленты> или room:<id записи раунда>")
    space, key = m.group(1), m.group(2)
    if space == "live":
        if not key.isdigit():
            raise AnchorError("live:<id> — числовой id события ленты")
        e = _find_live(live_path, int(key))
        if e is None:
            raise AnchorError(f"события {addr} в ленте нет")
        if _other_project(e.get("project"), project):
            raise AnchorError(f"событие {addr} — другого проекта ({e['project']})")
        kind = e.get("kind")
        if kind == "pass":
            raise AnchorError("ПАС — не якорь: продолжать нечего")
        if kind == "error":
            raise AnchorError("отказ канала — не якорь: ответа не было")
        # ЛЮБОЕ событие с текстом (решение Автора 2026-10-08, п. 3 кресла
        # секретаря): решение, отчёт, реплика секретаря, ревизия, заметка,
        # вызов Jev — всё, под чем Автор захочет продолжить. Но не отказ:
        # статус из BAD_STATUS (отказ канала, таймаут, снятый акт, упавшая
        # ревизия, упавший ход секретаря с обрывком ответа) — не якорь,
        # какой бы текст рядом ни лежал (ревизия: субагент, grok).
        st = str(e.get("status") or "")
        if bad_status(st):
            raise AnchorError(f"событие «{kind}» со статусом {st} — не якорь: это отказ, не ответ")
        text = live_text(e)
        if not text:
            raise AnchorError(f"у события «{kind}» нет текста — продолжать нечего")
        author = e.get("author") or "?"
        # у ревизии и тени ранних отказов автор события — окно/комната, а
        # ГОЛОС — в поле voice: продолжают от рецензента, не от окна, и
        # из жребия раунда выбывает он (ревизия: субагент)
        if author in SYS_AUTHORS and e.get("voice"):
            author = str(e["voice"])
        out = {"addr": addr, "author": author, "text": text, "kind": kind,
               "at": e.get("ts"), "sha": _sha(text)}
        if e.get("thread"):
            out["thread"] = e["thread"]
        return _cap(out)
    r = _find_room(room_path, key)
    if r is None:
        raise AnchorError(f"записи {addr} в журнале раундов нет")
    if _other_project(r.get("project"), project):
        raise AnchorError(f"запись {addr} — другого проекта ({r['project']})")
    role = r.get("role")
    if role not in ("answer", "summary", "digest", "seed_expanded", "seed"):
        raise AnchorError(f"запись «{role}» — не текст раунда, якорем не бывает")
    if role != "seed" and r.get("status") not in ("ok",):
        raise AnchorError(f"запись со статусом «{r.get('status')}» — не якорь "
                          f"(ПАС и отказы не продолжаются)")
    if role == "digest" and r.get("digest_used") is False:
        raise AnchorError("пересказ отвергнут получателем — не якорь")
    text = (r.get("text") or "").strip()
    if not text:
        raise AnchorError("у записи нет текста")
    out = {"addr": addr, "author": r.get("voice") or "?", "text": text,
           "kind": role, "round": r.get("round"), "phase": r.get("phase"),
           "at": r.get("ts"), "sha": _sha(text)}
    if str(r.get("phase", "")).startswith("rebut"):
        out["labels_note"] = True     # метки «Голос В» живут внутри раунда
    return _cap(out)


def _cap(a: dict) -> dict:
    n = len(a["text"])
    if n > ANCHOR_CAP:
        raise AnchorError(f"якорь {n} симв. — больше потолка {ANCHOR_CAP}: "
                          f"обрезки нет (решение стола); выделите фрагмент "
                          f"и вставьте его в вопрос сами")
    return a


def block(a: dict, *, with_author: bool) -> str:
    """Дословный блок для промпта. with_author=False — раунд (правило 9:
    ИМЯ ГОЛОСА не идёт в пакет; решение Автора). Род источника при этом
    остаётся: Автор, секретарь и служебное событие стола названы и в
    раунде — правило 9 прячет имя спорящего, а не то, что это была не
    позиция голоса (ревизия 2026-10-08: субагент, grok)."""
    au = a["author"]
    if au == "arr":
        who = "Автор (человек)"
    elif au == "secretary":
        who = "Секретарь (агент-посредник Автора)"
    elif au in SYS_AUTHORS:
        who = f"стол, служебное событие «{a['kind']}»"
    elif with_author:
        who = f"Голос {au}"
    else:
        who = "один из голосов стола"
    src = f"{a['kind']}"
    if a.get("round"):
        src += f" раунда «{a['round']}»" + (f", фаза {a['phase']}" if a.get("phase") else "")
    notes = []
    if au != "arr" and au not in SYS_AUTHORS:
        notes.append("это итог без мыслей модели (мысли на экране есть, здесь нет)")
    if a["kind"] == "digest":
        notes.append("это ПЕРЕСКАЗ чужих ответов для одного голоса, не позиция автора")
    if a["kind"] == "edit_review":
        notes.append("это ревизия правки рецензентом")
    if a.get("labels_note"):
        notes.append("метки «Голос А/Б/В» внутри — анонимные метки того раунда, "
                     "расшифровки нет")
    tail = ("; " + "; ".join(notes)) if notes else ""
    return (f"КОНТЕКСТ — сообщение, от которого продолжает Автор (дословно; "
            f"{src}; sha {a['sha']}{tail}):\n"
            f"<<< СООБЩЕНИЕ ({who})\n{a['text']}\n>>> КОНЕЦ СООБЩЕНИЯ\n\n")


def field(a: dict) -> dict:
    """Поле anchor для журнала: адрес, автор, sha, род."""
    out = {"addr": a["addr"], "author": a["author"], "sha": a["sha"], "kind": a["kind"]}
    if a.get("round"):
        out["round"] = a["round"]
    if a.get("phase"):
        out["phase"] = a["phase"]
    return out


def chain_mono(events: list[dict], anchor_author: str, targets: list[str]) -> int:
    """Длина цепочки монолога, считая ЭТОТ ход: непрерывный ряд
    последних якорных ходов Автора, где автор якоря равен единственному
    адресату (метрика claude из свода; пороги — догадка, названы
    мягким сигналом)."""
    if len(targets) != 1 or targets[0] != anchor_author:
        return 0
    n = 1
    for e in reversed(events):
        if e.get("author") != "arr" or e.get("kind") not in ("say", "topic"):
            continue
        an = e.get("anchor")
        if not isinstance(an, dict):
            break
        tv = e.get("anchor_targets") or []
        if an.get("author") == anchor_author and tv == [anchor_author]:
            n += 1
        else:
            break
    return n


def mono_note(n: int, author: str) -> str | None:
    if n >= CHAIN_SOFT:
        return (f"⚠ монолог: {n} продолжений подряд от ответа {author} к нему же — "
                f"стол вырождается в нить одного голоса (правило 7); это сигнал, "
                f"не запрет")
    return None
