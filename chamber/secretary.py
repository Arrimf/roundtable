#!/usr/bin/env python3
"""Секретарь стола — запись решений Автора, очереди работ и отчётов в ленту.

Роль введена Автором 2026-10-06 («секретарь — чудно! заводим»), описана в
`RoundTable/SECRETARY.md`. Дыра, которую она закрывает: разговор Автора с
агентом-посредником (Claude Code сегодня, Grok или Codex завтра) не
журналируется — решения по раунду jev-v1 две недели жили только в
сессии агента и не исполнялись. Правило 8.5: «я запомню» — не механика.

Три рода событий в `journal/live.jsonl`, все через `live.post` (одна
истина, один формат, flock):
  decision — слова Автора дословно; author = кто решил (arr), recorded_by =
             кто записал; about — о чём одной строкой; source — откуда.
  queue    — очередь работ: op=add заводит пункт (item = id события),
             op=start/done/drop ссылается на него; состояние очереди —
             свёртка событий (`fold_queue`), а не поле, которое правят.
  report   — отчёт агента в комнату дословно (text целиком).

Роль передаваемая, поэтому память роли — лента, а не сессия: новый агент
начинает с `secretary.py tail`. Никакого чтения чужих сессий и файлов
CLI — только то, что секретарь записал сам явной командой: иначе роль
зависела бы от формата одного CLI и читала бы то, что ей не адресовано.

Запуск: python3 chamber/secretary.py decision "…" --by arr --about "…"
        python3 chamber/secretary.py queue add "…" | start ID | done ID | drop ID | list
        python3 chamber/secretary.py report --file отчёт.md | --text "…"
        python3 chamber/secretary.py tail [--n 10]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live  # noqa: E402

QUEUE_OPS = ("add", "start", "done", "drop")
SANDBOX = HERE.parent.parent          # песочница: проекты — её прямые подкаталоги


def _project(explicit: str | None) -> str:
    """Проект событий секретаря. Порядок: явный --project; проект окна
    (CHOIR_EVENT_PROJECT); ПРОЕКТ ТЕКУЩЕГО КАТАЛОГА — прямой подкаталог
    песочницы, в котором (или под которым) запущен агент; иначе стол.
    Правило «из какой папки Автор включил агента, тому проекту и решения»
    (роль передаваемая: Grok запускается из папки проекта). Ревизия
    (deepseek, grok, kimi, субагент): умолчание «всегда стол» клало бы
    решения по одному проекту в журнал стола, где окно того проекта их не покажет.
    Проект печатается в ответе каждой пишущей команды."""
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    if live.event_project():
        return str(live.event_project())
    cwd = Path.cwd().resolve()
    try:
        rel = cwd.relative_to(SANDBOX.resolve())
        if rel.parts:
            return str(SANDBOX.resolve() / rel.parts[0])
    except ValueError:
        pass
    return str(HERE.parent)


def _post(kind: str, text: str, project: str | None, **extra) -> dict:
    extra = {k: v for k, v in extra.items() if v not in (None, "")}
    author = extra.pop("author", "secretary")
    return live.post(author, kind, text, project=_project(project), **extra)


def decision(text: str, *, by: str = "arr", recorded_by: str = "",
             about: str = "", source: str = "", project: str | None = None) -> dict:
    """Решение Автора — дословно, в тот же ход. author — кто решил."""
    text = text.strip()
    if not text:
        raise SystemExit("решение пустое")
    return _post("decision", text, project, author=by, recorded_by=recorded_by or None,
                 about=about or None, source=source or None)


def queue_add(text: str, *, by: str = "secretary", project: str | None = None,
              idea_id: str | None = None, idea_round: str | None = None) -> dict:
    text = text.strip()
    if not text:
        raise SystemExit("пункт очереди пустой")
    # idea_id — пункт взят из накопителя «своих идей» (chamber/ideas.py):
    # накопитель помечает идею «взята», в очереди видно, откуда пункт
    return _post("queue", text, project, author=by, op="add", idea_id=idea_id, idea_round=idea_round)


def queue_op(op: str, item: int, *, by: str = "secretary", note: str = "",
             project: str | None = None) -> dict:
    """Операция над пунктом. Проект события — ПРОЕКТ ПУНКТА (из add):
    явный --project, не совпадающий с ним, — отказ, иначе операция легла
    бы в чужой проект и в свёртке своего пункт остался бы open навсегда
    (ревизия: все четверо). Закрытый пункт (done/dropped) не трогается —
    и здесь, и в свёртке (fold_queue): гонка двух агентов не переоткроет."""
    if op not in ("start", "done", "drop"):
        raise SystemExit(f"op: start | done | drop, не {op!r}")
    items = {q["item"]: q for q in fold_queue(all_projects=True)}
    if item not in items:
        raise SystemExit(f"пункта очереди {item} нет (см. `queue list --all`)")
    it = items[item]
    if it["status"] in ("done", "dropped"):
        raise SystemExit(f"пункт {item} уже закрыт ({it['status']})")
    own = it.get("project")
    if project and own and _project(project) != own:
        raise SystemExit(f"пункт {item} принадлежит проекту {own}, а не {_project(project)}")
    text = note.strip() or it["text"]
    return _post("queue", text, own, author=by, op=op, item=item)


def dialogue(author_text: str = "", answer_text: str = "", *, agent: str = "", project: str | None = None,
             late: bool = False) -> list[dict]:
    """Обмен Автора с секретарём В ТЕРМИНАЛЕ — в ленту теми же событиями
    `sec`, что и кресло в окне (наказ Автора 2026-10-08: «не вижу твоих
    ответов»). Реплика Автора — от arr, ответ — от secretary, оба с
    `channel: terminal`; `late` — записано задним числом (время события —
    момент записи, не разговора). Разговор в терминале иначе нигде не лежит."""
    agent = agent or os.environ.get("SECRETARY_AGENT") or ""
    if not agent:
        # поле, по которому передаётся роль, не угадывается: «кто отвечал» —
        # обязательно (ревизия; умолчание claude приписало бы ответ не тому)
        raise SystemExit("dialogue: укажите агента — --agent или SECRETARY_AGENT (кто отвечал)")
    out = []
    if author_text.strip():
        # реплику Автора кладёт агент — помечено, как у decision (правило 3)
        out.append(_post("sec", author_text.rstrip(), project, author="arr", channel="terminal",
                         agent=agent, recorded_by=agent, late=late or None))
    if answer_text.strip():
        out.append(_post("sec", answer_text.rstrip(), project, author="secretary", channel="terminal",
                         agent=agent, rights="full", late=late or None))
    if not out:
        raise SystemExit("ни реплики, ни ответа")
    return out


def report(text: str, *, by: str = "secretary", about: str = "", project: str | None = None) -> dict:
    text = text.rstrip()
    if not text.strip():
        raise SystemExit("отчёт пустой")
    return _post("report", text, project, author=by, about=about or None)


# ── чтение ──────────────────────────────────────────────────────────

def events(kind: str, *, project: str | None = None, all_projects: bool = False) -> list[dict]:
    """События рода kind — своего проекта (см. _project), либо всех.
    Фильтр здесь свой, а не live.event_project(): у CLI без окна проект
    не задан, и лента читалась бы целиком."""
    proj = None if all_projects else _project(project)
    return [e for e in live.read_events(all_projects=True)
            if e.get("kind") == kind and (all_projects or live.belongs(e, proj))]


def fold_queue(*, project: str | None = None, all_projects: bool = False) -> list[dict]:
    """Состояние очереди — свёртка событий queue в порядке ленты:
    add → open; start → started; done → done; drop → dropped. Пункт
    остаётся с текстом и проектом из add; последняя операция — полем."""
    items: dict[int, dict] = {}
    for e in events("queue", project=project, all_projects=all_projects):
        op = e.get("op")
        if op == "add":
            items[e["id"]] = {"item": e["id"], "text": e.get("text", ""), "status": "open",
                              "by": e.get("author"), "ts": e.get("ts"), "project": e.get("project"),
                              "last_op": "add", "last_ts": e.get("ts"),
                              **({"idea_id": e["idea_id"], "idea_round": e.get("idea_round")} if e.get("idea_id") else {})}
        elif op in ("start", "done", "drop") and e.get("item") in items:
            it = items[e["item"]]
            if it["status"] in ("done", "dropped"):
                continue          # закрытое терминально: поздний op гонки не переоткрывает
            it["status"] = {"start": "started", "done": "done", "drop": "dropped"}[op]
            it["last_op"], it["last_ts"], it["last_by"] = op, e.get("ts"), e.get("author")
            if e.get("text") and e.get("text") != it["text"]:
                it["note"] = e["text"]
    return list(items.values())


def state(n: int = 10, *, project: str | None = None, all_projects: bool = False) -> dict:
    """Что нужно новому агенту при приёме роли: последние решения,
    открытая очередь, последние отчёты."""
    n = max(1, n)
    dec = events("decision", project=project, all_projects=all_projects)[-n:]
    q = [i for i in fold_queue(project=project, all_projects=all_projects) if i["status"] in ("open", "started")]
    rep = events("report", project=project, all_projects=all_projects)[-3:]
    return {"decisions": dec, "queue": q, "reports": rep}


def render(st: dict) -> str:
    out = ["РЕШЕНИЯ АВТОРА (последние):"]
    for e in st["decisions"] or []:
        out.append(f"  #{e['id']} {e.get('ts', '')[:16]} {e.get('author')}: {e.get('text')}"
                   + (f"  — {e['about']}" if e.get("about") else ""))
    if not st["decisions"]:
        out.append("  (нет)")
    out.append("ОЧЕРЕДЬ (открытое):")
    for i in st["queue"] or []:
        out.append(f"  [{i['item']}] {i['status']:<8} {i['text']}")
    if not st["queue"]:
        out.append("  (пусто)")
    out.append("ОТЧЁТЫ (последние):")
    for e in st["reports"] or []:
        head = (e.get("text") or "").strip().splitlines()[0] if e.get("text") else ""
        out.append(f"  #{e['id']} {e.get('ts', '')[:16]} {e.get('author')}: {head[:120]}")
    if not st["reports"]:
        out.append("  (нет)")
    return "\n".join(out)


# ── CLI ──────────────────────────────────────────────────────────────

def _read_text(file: str, text: str) -> str:
    """Текст команды: из файла, из stdin («-»), иначе аргумент. Файл и
    stdin — путь для дословности (правило 3): через оболочку теряются
    «$», обратные кавычки и ведущий «-» (субагент-ревьюер)."""
    if file:
        try:
            return Path(file).read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"файл не читается: {e}")
    if text == "-":
        return sys.stdin.read()
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Секретарь стола: решения, очередь, отчёты — в ленту")
    ap.add_argument("--project", default="", help="проект событий (иначе окно/стол)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decision", help="решение Автора дословно")
    d.add_argument("text", nargs="?", default="", help="текст; «-» — из stdin (дословно, мимо оболочки)")
    d.add_argument("--file", default="", help="текст решения из файла")
    d.add_argument("--by", default="arr", help="кто решил (author события)")
    d.add_argument("--recorded-by", default=os.environ.get("SECRETARY_AGENT", ""), help="кто записал")
    d.add_argument("--about", default="", help="о чём, одной строкой")
    d.add_argument("--source", default="", help="откуда: раунд, событие, файл")
    q = sub.add_parser("queue", help="очередь работ")
    q.add_argument("op", choices=QUEUE_OPS + ("list",))
    q.add_argument("what", nargs="?", default="", help="текст пункта (add) или его номер (start/done/drop)")
    q.add_argument("--by", default=os.environ.get("SECRETARY_AGENT", "secretary"))
    q.add_argument("--note", default="", help="пометка к start/done/drop")
    q.add_argument("--all", action="store_true", help="list: очередь всех проектов")
    r = sub.add_parser("report", help="отчёт в комнату дословно")
    r.add_argument("--file", default="")
    r.add_argument("--text", default="")
    r.add_argument("--by", default=os.environ.get("SECRETARY_AGENT", "secretary"))
    r.add_argument("--about", default="")
    g = sub.add_parser("dialogue", help="обмен Автора с секретарём в терминале — в ленту (события sec)")
    g.add_argument("--author-file", default="", help="реплика Автора дословно (файл)")
    g.add_argument("--answer-file", default="", help="ответ секретаря дословно (файл)")
    g.add_argument("--agent", default=os.environ.get("SECRETARY_AGENT", ""), help="кто отвечал (claude, codex, …)")
    g.add_argument("--late", action="store_true", help="записано задним числом")
    t = sub.add_parser("tail", help="приём роли: решения, очередь, отчёты")
    t.add_argument("--n", type=int, default=10)
    t.add_argument("--all", action="store_true", help="все проекты")
    a = ap.parse_args(argv)
    pj = a.project or None
    if a.cmd == "decision":
        text = _read_text(a.file, a.text)
        e = decision(text, by=a.by, recorded_by=a.recorded_by, about=a.about, source=a.source, project=pj)
        print(f"решение записано: событие {e['id']} · проект {e.get('project')}")
    elif a.cmd == "queue":
        if a.op == "list":
            for i in fold_queue(project=pj, all_projects=a.all):
                print(f"[{i['item']}] {i['status']:<8} {i['text']}" + (f"  — {i['note']}" if i.get("note") else "")
                      + (f"  ({i.get('project')})" if a.all else ""))
            return 0
        if a.op == "add":
            e = queue_add(_read_text("", a.what), by=a.by, project=pj)
            print(f"в очередь: пункт {e['id']} · проект {e.get('project')}")
        else:
            try:
                item = int(a.what)
            except ValueError:
                raise SystemExit(f"{a.op}: нужен номер пункта, не {a.what!r}")
            e = queue_op(a.op, item, by=a.by, note=a.note, project=pj)
            print(f"пункт {item}: {a.op} (событие {e['id']})")
    elif a.cmd == "dialogue":
        at = _read_text(a.author_file, "") if a.author_file else ""
        an = _read_text(a.answer_file, "") if a.answer_file else ""
        evs = dialogue(at, an, agent=a.agent, project=pj, late=a.late)
        print("диалог записан: события " + ", ".join(f"{e['id']} ({e['author']})" for e in evs) + f" · проект {evs[0].get('project')}")
    elif a.cmd == "report":
        text = _read_text(a.file, a.text)
        e = report(text, by=a.by, about=a.about, project=pj)
        print(f"отчёт в ленте: событие {e['id']} ({len(text)} симв.) · проект {e.get('project')}")
    elif a.cmd == "tail":
        print(render(state(a.n, project=pj, all_projects=a.all)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
