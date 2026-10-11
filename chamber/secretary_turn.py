#!/usr/bin/env python3
"""Ход секретаря из окна: реплика Автора → агент-посредник с постоянной нитью → ответ в ленту.

Решение Автора 2026-10-08: «не хватает окна чата». Пока Автор говорил с
посредником в терминале, сам разговор нигде не лежал — секретарь
переписывал решения в ленту событиями. Через окно разговор ложится в
ленту целиком: реплика Автора — событие `sec` от `arr`, ответ — событие
`sec` от `secretary` (agent, model, effort, session, act), поток работы
(мысли, инструменты — правило 17) виден во вкладке по стенограмме акта.

КРЕСЛО СЕКРЕТАРЯ — ВНЕ КЛЕТКИ, С ПРАВАМИ АВТОРА. Голоса стола читают
только; секретарь ведёт стол: запускает раунды и кресла, правит код,
гоняет тесты, коммитит, записывает решения (`secretary.py`). Поэтому CLI
идёт без клетки и без подтверждений (`--dangerously-skip-permissions`),
в каталоге проекта окна. Это перенос доверия из терминала в окно, не
новое доверие; названо в SECRETARY.md и в каждом событии полем
`rights: full`. Слепоту это не трогает: ответы слепой фазы живут в
памяти процесса дирижёра, секретарь — другой процесс.

Нить — одна на проект: `journal/secretary/state.<sha(проект)>.json`
(session_id, agent, turns). Первый ход несёт преамбулу роли и
`secretary.py tail`; дальше — только реплика, память у нити своя.
Голоса этот разговор не читают (в пакеты хода идут только реплики
комнаты): чтобы стол обсудил что-то отсюда — «в комнату» руками Автора.

Запуск (окно): secretary_turn.py --project P --text-file F
Среда: CHOIR_SEC_AGENT (claude | codex | grok | kimi | deepseek; окно
даёт из выпадающего списка), модель и усилие — константы live.py, то есть
настройка «комнаты» окна (CHOIR_<ГОЛОС>_MODEL/_EFFORT); CHOIR_SEC_BIN /
CHOIR_SEC_MODEL / CHOIR_SEC_EFFORT — явные переопределения (стенды),
CHOIR_RT_URL (адрес окна для секретаря), RT_ACT_ID.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live        # noqa: E402
import secretary   # noqa: E402
import grokstream  # noqa: E402  ответ Грока из потока событий
import channels    # noqa: E402  линии Кими (замок той линии, чьим алиасом идёт ход)
import anchor as anchor_mod   # noqa: E402  «продолжить отсюда» — секретарю
from serial_gate import first_free_gate   # noqa: E402

# ВЕНДОР СЕКРЕТАРЯ (решение Автора 2026-10-08: «выпадающий список
# вендоров, чтобы можно было менять»). Роль передаваемая — значит кресло
# обязано уметь сажать любого агента с диском: claude, codex, grok, kimi,
# deepseek (dsh — агентный харнесс той же лаборатории, как в кресле
# исполнителя). gemini кресла не имеет: HTTP-канал без файлов и команд.
# МОДЕЛЬ И УСИЛИЕ — ИЗ ВКЛАДКИ «КОМНАТА» (решение Автора): окно отдаёт
# детям CHOIR_<ГОЛОС>_MODEL/_EFFORT из своей настройки комнаты, live.py
# читает их на импорте — здесь берутся те же константы, что у голоса в
# комнате. CHOIR_SEC_MODEL/EFFORT — явное переопределение (стенды).
AGENTS = ("claude", "codex", "grok", "kimi", "deepseek")
NO_CHAIR = {"gemini": "кресла секретаря нет: HTTP-канал, файлов не видит, команд не выполняет"}
SEC_AGENT = (os.environ.get("CHOIR_SEC_AGENT") or "claude").strip()
SEC_BIN = os.environ.get("CHOIR_SEC_BIN") or ""          # стенды: стаб вместо CLI агента
SEC_MODEL = os.environ.get("CHOIR_SEC_MODEL") or ""
SEC_EFFORT = os.environ.get("CHOIR_SEC_EFFORT") or ""
STATE_DIR = live.JOURNAL / "secretary"
TURN_TIMEOUT = int(os.environ.get("CHOIR_SEC_TIMEOUT") or 3600)
GATE_WAIT = int(os.environ.get("CHOIR_SEC_GATE_WAIT") or 3600)
HISTORY_TURNS = 12          # передача роли / агент без сессии: сколько реплик диалога подать
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")   # CSI и OSC (заголовок терминала)
ARGV_CAP = 110_000          # байт промпта для CLI, берущих его аргументом (MAX_ARG_STRLEN 131072)
SHIELD = "РЕПЛИКА АВТОРА. "   # щит-префикс для CLI, берущих промпт аргументом: дефис не станет флагом


def agent_plan(agent: str) -> dict:
    """Чем пойдёт ход этого агента: bin, model, effort, note. Модель и
    усилие — константы live.py (= настройка комнаты из окна), если не
    переопределены CHOIR_SEC_MODEL/EFFORT."""
    if agent not in AGENTS:
        raise ValueError(NO_CHAIR.get(agent) or f"агент «{agent}» столу неизвестен")
    model, effort, note = SEC_MODEL, SEC_EFFORT, ""
    if agent == "claude":
        model = model or live.CLAUDE_MODEL; effort = effort or live.CLAUDE_EFFORT
        bin_ = SEC_BIN or "claude"
    elif agent == "codex":
        model = model or live.CODEX_MODEL; effort = effort or live.CODEX_EFFORT
        bin_ = SEC_BIN or "codex"
        if not model:
            note = "модель — из ~/.codex/config.toml (окно флага не задало)"
    elif agent == "grok":
        model = model or live.GROK_MODEL; effort = effort or live.GROK_EFFORT
        bin_ = SEC_BIN or "grok"
        if not model:
            note = "модель — серверный дефолт xAI (окно флага не задало)"
    elif agent == "kimi":
        chans = live.channels_of("kimi")
        line = channels.plan_line(chans) or (chans[0] if chans else None)
        if line is None:
            raise ValueError("линии Кими не прочитались (~/.kimi-code/config.toml) — кресло не выдаётся")
        model = model or live._kimi_line_model(line)      # «провайдер/модель», провайдер = ключ линии
        effort = ""                                        # у kimi-code флага усилия нет (правило 8.5)
        bin_ = SEC_BIN or str(live.KIMI_BIN)
        note = "усилия у kimi CLI нет; линия " + str(line.get("gate") or "")
    else:   # deepseek → dsh, как в кресле исполнителя (edits.py)
        model = model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-pro"
        effort = ""
        bin_ = SEC_BIN or "dsh"
        note = "dsh --profile headless; усилия у dsh нет; сессий нет — история диалога подаётся из ленты"
    return {"agent": agent, "bin": bin_, "model": model, "effort": effort, "note": note,
            "no_session": agent == "deepseek"}


def state_path(project: str) -> Path:
    return STATE_DIR / f"state.{hashlib.sha1(project.encode()).hexdigest()[:12]}.json"


def load_state(project: str) -> dict:
    p = state_path(project)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"project": project, "turns": 0}


def save_state(project: str, st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = state_path(project).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, state_path(project))


def preamble(project: str) -> str:
    """Первый ход нити: кто ты, что здесь лежит, что от тебя ждут."""
    rt = HERE.parent
    try:
        tail = secretary.render(secretary.state(10, project=project))
    except Exception as e:                      # noqa: BLE001
        tail = f"(secretary.py tail не прочитан: {e})"
    url = os.environ.get("CHOIR_RT_URL") or ""
    return f"""Ты — СЕКРЕТАРЬ стола (роль: {rt}/SECRETARY.md; правила стола: {rt}/CLAUDE.md и
{rt.parent}/CLAUDE.md). Автор говорит с тобой через окно стола; этот разговор целиком ложится
в ленту журнала событиями `sec` — твои ответы читает Автор, а потом любой агент, который примет
роль. Проект окна: {project}. Каталог стола: {rt}.{(' Окно стола: ' + url + ' (GET /state, POST /round, /act, /edit — как в roundtable.py).') if url else ''}

Что ты делаешь: ведёшь стол по решениям Автора — запускаешь раунды ({rt}/chamber/choir.py или POST /round
окна) и кресла, правишь код по порядку «тесты → ревизия → коммит» (см. CLAUDE.md), записываешь
решения Автора в тот же ход (`python3 {rt}/chamber/secretary.py decision "…" --by arr --about "…"`),
ведёшь очередь (`{rt}/chamber/secretary.py queue add|start|done`), кладёшь отчёты (`… report`).
Твой рабочий каталог — проект окна; пути к столу — абсолютные, как здесь.
Права полные (кресло секретаря вне клетки) — это доверие Автора, не трать его молча: о рискованном
действии скажи до него. Отвечай по-русски, коротко, вывод первой строкой; мысли по ходу работы
не прячь — их видно во вкладке. Если эту нить продолжили В ТЕРМИНАЛЕ (не из окна), клади каждый
обмен в ленту сам: `python3 {rt}/chamber/secretary.py dialogue --author-file Q --answer-file A --agent <ты>`
— иначе Автор не увидит разговор в окне.

Что сейчас в ленте (secretary.py tail):
{tail}

Первая реплика Автора ниже.
"""


def run_turn(project: str, text: str, anchor_addr: str = "") -> int:
    import fcntl
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lockf = open(state_path(project).with_suffix(".lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # второе окно того же проекта: одна нить — один ход за раз и между
        # процессами, иначе два --resume одной сессии (ревизия: grok, субагент)
        live.post("secretary", "sec", "", project=project, status="error", rights="full",
                  detail="ход секретаря уже идёт из другого окна этого проекта — реплика не отправлена",
                  act=os.environ.get("RT_ACT_ID") or None)
        print("✗ secretary: ход уже идёт из другого окна", flush=True)
        return 1
    try:
        return _run_turn(project, text, anchor_addr)
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN); lockf.close()
        except OSError:
            pass


def resume_command(project: str) -> dict:
    """Строка для терминала — продолжить нить секретаря этого проекта
    (решение Автора 2026-10-11: «команда, а не запуск»). Окно её только
    ПОКАЗЫВАЕТ с кнопкой «скопировать»; терминал открывает Автор. Зеркало
    в ленту делает сам агент командой secretary.py dialogue (SECRETARY.md,
    преамбула роли)."""
    st = load_state(project)
    agent = str(st.get("agent") or "claude")
    if "/" in agent:
        agent = Path(agent).name
    sid = st.get("session_id") or ""
    q = shlex.quote
    key = state_path(project).stem.split(".", 1)[1]
    adir = STATE_DIR / f"{agent}.{key}"
    note = ""
    if agent == "claude":
        cmd = f"cd {q(project)} && claude" + (f" --resume {q(sid)}" if sid else "")
    elif agent == "codex":
        cmd = f"cd {q(project)} && codex" + (f" resume {q(sid)}" if sid else "")
    elif agent == "grok":
        cmd = f"cd {q(project)} && grok" + (f" --resume {q(sid)}" if sid else "")
    elif agent == "kimi":
        # нить Кими — по каталогу запуска (каталог нити секретаря), проект — --add-dir;
        # модель линии — та же, что у хода из окна (иначе kimi взял бы умолчание
        # конфига, возможно, резервную линию; ревизия)
        try:
            m = st.get("model") or agent_plan("kimi")["model"]
        except ValueError:
            m = ""
        mflag = f" -m {q(m)}" if m else ""
        if sid and adir.exists():
            cmd = f"cd {q(str(adir))} && kimi -c{mflag} --add-dir {q(project)}"
        else:
            cmd = f"cd {q(project)} && kimi{mflag}"
            sid = ""                                  # «cwd» без каталога — нити нет
    elif agent == "deepseek":
        cmd = f"cd {q(project)} && dsh"
        note = "у dsh нет нитей: новый разговор; роль — SECRETARY.md"
    else:
        return {"agent": agent, "cmd": "", "session": "", "note": f"для агента «{agent}» строки нет"}
    if not sid and agent in ("claude", "codex", "grok", "kimi"):
        note = "нити секретаря у этого проекта ещё нет — будет новый разговор; роль — SECRETARY.md"
    shown = "" if sid == "cwd" else sid[:8]
    return {"agent": agent, "cmd": cmd, "session": shown, "note": note}


def dialogue_tail(project: str, n: int = HISTORY_TURNS) -> str:
    """Последние реплики диалога Автора с секретарём из ленты — для
    передачи роли другому агенту и для агента без сессии (dsh)."""
    try:
        evs = [e for e in live.read_events(all_projects=True)
               if e.get("kind") == "sec" and e.get("status") != "error"
               and (e.get("text") or "").strip() and live.belongs(e, Path(project))][-n:]
    except Exception:                               # noqa: BLE001
        return ""
    if not evs:
        return ""
    rows = []
    for e in evs:
        ag = str(e.get("agent") or "?")
        if "/" in ag:
            ag = Path(ag).name              # старые записи: путь к бинарю
        who = "Автор" if e.get("author") == "arr" else f"секретарь ({ag})"
        t = (e.get("text") or "").strip()
        rows.append(f"— {who} [#{e.get('id')}]: " + (t if len(t) <= 1500 else t[:1500] + " …(обрезано)"))
    return "\n".join(rows)


def _argv(plan: dict, *, first: bool, sid: str, prompt: str, project: str,
          pfile: Path, afile: Path, agent_dir: Path) -> tuple[list[str], str | None, Path | None]:
    """argv хода + что подать в stdin (None — ничего) + cwd процесса.
    Права полные у всех: это кресло секретаря, не голос (SECRETARY.md)."""
    ag, m, e = plan["agent"], plan["model"], plan["effort"]
    if ag == "claude":
        return ([plan["bin"], "-p", "--output-format", "stream-json", "--verbose",
                 "--dangerously-skip-permissions", "--model", m, "--effort", e,
                 *(["--resume", sid] if not first else ["--session-id", sid])],
                prompt, Path(project))
    if ag == "codex":
        tune = (["-m", m] if m else []) + (["-c", f"model_reasoning_effort={e}"] if e else [])
        base = [plan["bin"], "exec", "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check", "--json", *tune]
        # «-» — промпт из stdin и у exec, и у exec resume (--help: «If '-' is
        # used, read from stdin»; ревизия): argv не ограничивает длину
        if first:
            return (base + ["-o", str(afile), "-"], prompt, Path(project))
        return (base + ["resume", sid, "-o", str(afile), "-"], prompt, Path(project))
    if ag == "grok":
        # pty через script (без терминала CLI не работает). Нить — СВОИМ
        # UUID: --session-id (новая) / --resume <uuid>. Не -c: сессии Грока
        # ключуются каталогом --cwd (проектом), и -c подхватил бы последнюю
        # сессию проекта — голоса комнаты или самого Автора, а следующий -c
        # комнаты взял бы нить секретаря с полными правами (ревизия:
        # субагент, grok — по ~/.grok/sessions живой пробы). В строку shell
        # идут только пути, модель, усилие и uuid через shlex.quote —
        # реплика живёт в файле.
        cmd = (f"{shlex.quote(plan['bin'])} {'--session-id' if first else '--resume'} {shlex.quote(sid)} "
               f"--cwd {shlex.quote(project)} "
               + (f"--model {shlex.quote(m)} " if m else "")
               + f"--prompt-file {shlex.quote(str(pfile))} --permission-mode bypassPermissions --no-plan "
               + "--output-format streaming-json "
               + (f"--effort {shlex.quote(e)}" if e else ""))
        return (["script", "-qec", cmd.strip(), "/dev/null"], None, agent_dir)
    if ag == "kimi":
        return ([plan["bin"], "-m", m, "--add-dir", project, *(["-c"] if not first else []),
                 "-p", SHIELD + prompt], None, agent_dir)
    # deepseek → dsh headless: без сессии, промпт аргументом, модель патч-слоем
    sys.path.insert(0, str(HERE.parent))
    import edits as _edits                           # noqa: PLC0415
    return ([plan["bin"], "--profile", "headless", "--patch", str(_edits.dsh_patch(m)),
             SHIELD + prompt], None, Path(project))


def _extract(plan: dict, out: str, afile: Path) -> str:
    ag = plan["agent"]
    if ag == "claude":
        return live._claude_result(out).strip()
    if ag == "codex":
        try:
            return afile.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    if ag == "grok":
        return grokstream.grok_stream_answer(out)
    text = ANSI_RE.sub("", out).replace("\r", "").strip()
    lines = text.splitlines()
    # служебные строки CLI вокруг ответа (замер живьём 2026-10-08): grok
    # завершает «Finishing session…», kimi открывает «kimi version N» и
    # закрывает «To resume this session: kimi -r …» — это не реплика
    while lines and lines[-1].strip() in ("Finishing session…", "Finishing session...", ""):
        lines.pop()
    while lines and (lines[-1].strip().startswith("To resume this session:") or not lines[-1].strip()):
        lines.pop()
    while lines and (re.match(r"^kimi version \S+$", lines[0].strip()) or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def _codex_thread(out: str) -> str | None:
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "thread.started" and d.get("thread_id"):
            return str(d["thread_id"])
    return None


def _run_turn(project: str, text: str, anchor_addr: str = "") -> int:
    st = load_state(project)
    act = os.environ.get("RT_ACT_ID") or None
    turn_no = st.get("turns", 0) + 1
    try:
        plan = agent_plan(SEC_AGENT)
    except ValueError as e:
        live.post("secretary", "sec", "", project=project, act=act, status="error", rights="full",
                  detail=f"агент {SEC_AGENT}: {e}", agent=SEC_AGENT, turn=turn_no)
        print(f"✗ secretary: {e}", flush=True)
        return 1
    # ПЕРЕДАЧА РОЛИ: нить другого агента не продолжается — новая нить с
    # преамбулой и хвостом диалога из ленты (память роли — лента, SECRETARY.md)
    # старые записи (до 2026-10-08) хранили в agent путь к бинарю — имя агента
    prev_agent = str(st.get("agent") or "")
    if "/" in prev_agent:
        prev_agent = Path(prev_agent).name
    handover = bool(prev_agent) and prev_agent != plan["agent"]
    # СОСТОЯНИЕ НЕ ТРОГАЕТСЯ ДО УСПЕХА: упавший первый ход нового агента не
    # должен стирать нить прежнего — иначе возврат к нему был бы ещё одной
    # «передачей» без нити (ревизия: субагент, grok). Здесь — копия.
    saved = dict(st)
    if handover:
        st = dict(st); st.pop("session_id", None)
    first = not st.get("session_id") or plan["no_session"]
    # claude — свой UUID; codex — thread_id из его вывода (после хода); grok и
    # kimi — нить по cwd процесса (каталог нити секретаря): метка «cwd»,
    # чтобы второй ход знал, что продолжать есть что (-c)
    sid = st.get("session_id") or (str(uuid.uuid4()) if plan["agent"] in ("claude", "grok")
                                   else "cwd" if plan["agent"] == "kimi" else "")
    # «ПРОДОЛЖИТЬ ОТСЮДА» → секретарю: текст якоря вставляет не окно, а этот
    # процесс по адресу из журнала (правило 8.5), с именем автора — секретарь
    # не голос в слепой фазе, ему имя нужно
    extra: dict = {}
    ablock = ""
    if anchor_addr:
        arec = anchor_mod.resolve(anchor_addr, live_path=live.LIVE,
                                  room_path=live._room_path(),      # тот же путь, что у окна (anchor_resolve)
                                  project=project)
        ablock = anchor_mod.block(arec, with_author=True)
        extra["anchor"] = anchor_mod.field(arec)
    body = ablock + text
    # история — ДО записи текущей реплики в ленту, иначе она попадала бы в
    # «последние реплики» и шла телом второй раз (ревизия: субагент, grok)
    hist = dialogue_tail(project) if first and (handover or plan["no_session"]) else ""
    if first:
        head = preamble(project)
        if handover:
            head += (f"\nПЕРЕДАЧА РОЛИ: до тебя секретарём был агент «{prev_agent}» "
                     f"({saved.get('turns', 0)} ходов). Разговор Автора с ним — ниже, "
                     f"продолжай его, а не начинай заново.\n")
        if hist:
            head += "\nПОСЛЕДНИЕ РЕПЛИКИ ДИАЛОГА (из ленты, дословно):\n" + hist + "\n"
        prompt = head + "\n" + body
    else:
        prompt = body
    bin_ = shutil.which(plan["bin"]) or plan["bin"]
    plan = dict(plan, bin=bin_)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = state_path(project).stem.split(".", 1)[1]
    agent_dir = STATE_DIR / f"{plan['agent']}.{key}"       # нить kimi — по cwd процесса; файлы хода
    agent_dir.mkdir(parents=True, exist_ok=True)
    pfile = agent_dir / "prompt.txt"
    afile = agent_dir / "answer.txt"
    try:
        afile.unlink()
    except OSError:
        pass
    pfile.write_text(prompt, encoding="utf-8")
    # Промпт — через stdin (claude, codex), файл (grok) или аргумент с щитом
    # (kimi, dsh): у последних потолок MAX_ARG_STRLEN 128 КБ — преамбула с
    # историей плюс реплика с якорем к нему подходят, поэтому сверх ARGV_CAP
    # байт — отказ словами, не E2BIG с потерянным ходом (ревизия). argv и
    # патч dsh собираются ДО записи реплики в ленту: отказ сборки — тоже
    # до неё.
    try:
        cmd, stdin_text, cwd = _argv(plan, first=first, sid=sid, prompt=prompt, project=project,
                                     pfile=pfile, afile=afile, agent_dir=agent_dir)
        if stdin_text is None and plan["agent"] != "grok" and len(prompt.encode("utf-8")) > ARGV_CAP:
            raise ValueError(f"промпт {len(prompt.encode('utf-8'))} байт — больше потолка аргумента "
                             f"{ARGV_CAP} у CLI {plan['agent']} (преамбула и история + реплика); "
                             f"укоротите реплику или возьмите агента со stdin (claude, codex)")
    except (ValueError, OSError) as e:
        live.post("secretary", "sec", "", project=project, act=act, status="error", rights="full",
                  detail=f"ход не собрался: {e}"[:500], agent=plan["agent"], turn=turn_no)
        print(f"✗ secretary: {e}", flush=True)
        return 1
    live.post("arr", "sec", text, project=project, act=act, turn=turn_no,
              session=sid[:8] if sid else "", agent=plan["agent"], **extra)
    shown = [("<промпт>" if (c == SHIELD + prompt or c == prompt) else c) for c in cmd]
    print(f"→ secretary · {plan['agent']} · {plan['bin']} · {plan['model'] or '(модель CLI)'}"
          f"{('/' + plan['effort']) if plan['effort'] else ''} · "
          + (f"нить {sid[:8]} " if sid else "")
          + f"({'первый ход' if first else 'ход ' + str(turn_no)}"
          + (f"; ПЕРЕДАЧА РОЛИ от {prev_agent}" if handover else "") + ") · права полные"
          + (f" · {plan['note']}" if plan.get("note") else ""), flush=True)
    print("$ " + " ".join(shown) + (f"  < <реплика, {len(prompt)} симв., stdin>" if stdin_text is not None else
                                     f"  (промпт {len(prompt)} симв.)"), flush=True)
    t0 = time.time()
    out_chunks: list[str] = []
    timed_out = {"v": False}
    queued = 0.0
    gates = []
    if plan["agent"] == "kimi":
        # замок линии Кими (concurrency=1 у организации): той линии, чьим
        # алиасом идёт ход — как у кресла (executor_run --serial-gate)
        gates = channels.gates_for_model(live.channels_of("kimi"), plan["model"]) or ["kimi"]
    import contextlib
    gate_cm = (first_free_gate(gates, wait_limit=GATE_WAIT,
                               on_wait=lambda sec, who: print(f"  ⏳ kimi: в очереди {sec} с (занято: {who})", flush=True)
                               if sec % 15 == 0 else None)
               if gates else contextlib.nullcontext())
    try:
        with gate_cm:
            # время хода — ПОСЛЕ ворот: очередь Кими не должна съедать
            # TURN_TIMEOUT и записываться в elapsed_s (как в live.py; ревизия)
            queued = round(time.time() - t0, 1)
            t0 = time.time()
            # БЕЗ своей сессии: кнопка «Прервать» и закрытие окна бьют killpg по
            # группе акта, и CLI с полными правами обязан умереть вместе с ней
            # (ревизия: grok). Внуков целиком снимает cgroup акта у окна;
            # сторож здесь бьёт сам CLI.
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1,
                                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL)

            def _feed():
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(stdin_text); proc.stdin.close()
                except OSError:
                    pass

            def _watch():
                # сторож по времени — отдельной нитью: чтение stdout на молчащем
                # CLI не возвращается, и проверка «после строки» не сработала бы
                while proc.poll() is None:
                    if time.time() - t0 > TURN_TIMEOUT:
                        timed_out["v"] = True
                        try:
                            proc.terminate()
                            for _ in range(30):
                                if proc.poll() is not None:
                                    break
                                time.sleep(0.1)
                            if proc.poll() is None:
                                proc.kill()
                        except OSError:
                            pass
                        try:
                            proc.stdout.close()      # читатель stdout иначе ждал бы внуков, державших пайп
                        except OSError:
                            pass
                        return
                    time.sleep(1)
            import threading
            if stdin_text is not None:
                threading.Thread(target=_feed, daemon=True).start()
            threading.Thread(target=_watch, daemon=True).start()
            assert proc.stdout is not None
            for line in proc.stdout:
                out_chunks.append(line)
                # поток работы — в стенограмму акта (правило 17); у grok строка
                # вывода инструмента — без байтового массива (grokstream.slim_line)
                sys.stdout.write(grokstream.slim_line(line) if plan["agent"] == "grok" else line)
                sys.stdout.flush()
            rc = proc.wait()
    except (OSError, TimeoutError) as e:
        # OSError — CLI не запустился; TimeoutError — ворота линии Кими не
        # освободились за GATE_WAIT (ревизия: иначе traceback без события)
        live.post("secretary", "sec", "", project=project, act=act, status="error",
                  detail=(f"ворота линии не освободились: {e}" if isinstance(e, TimeoutError)
                          else f"CLI секретаря не запустился: {e}")[:500],
                  agent=plan["agent"], agent_bin=plan["bin"], rights="full", turn=turn_no)
        print(f"✗ secretary: {e}", flush=True)
        return 1
    out = "".join(out_chunks)
    try:
        answer = _extract(plan, out, afile)
    except Exception as e:                      # noqa: BLE001
        answer = ""
        err = str(e)
    else:
        err = ""
    note = ""
    if plan["agent"] == "codex" and first:
        tid = _codex_thread(out)
        if tid:
            sid = tid
        else:
            note = "thread_id в выводе codex не найден — нить не сохранена, следующий ход начнёт новую"
            print("⚠ " + note, flush=True)
    elapsed = round(time.time() - t0, 1)
    failed = rc != 0 or not answer or timed_out["v"]
    # НИТЬ сохраняется только после удавшегося хода: упавший первый ход
    # оставил бы session_id без сессии, и дальше каждый ход шёл бы --resume
    # в никуда (ревизия). «No conversation found» (claude) / «no rollout
    # found» (codex) у любого хода — нить потеряна у CLI: сбросить,
    # следующий ход начнёт новую с преамбулой.
    plain = "\n".join(l for l in out.splitlines() if not l.lstrip().startswith("{"))
    if plan["agent"] == "grok":
        plain = grokstream.grok_stream_diag(out)     # сообщения CLI и события error — без мыслей и вывода инструментов
    lost = any(k in plain.lower() for k in ("no conversation found", "no rollout found"))
    if not failed:
        st.update(agent=plan["agent"], model=plan["model"], turns=turn_no, last=live._now())
        if handover:
            st["session_prev"] = {"agent": prev_agent, "session_id": saved.get("session_id"),
                                  "turns": saved.get("turns", 0)}
        if sid and not plan["no_session"] and (plan["agent"] != "codex" or not first or _codex_thread(out)):
            st["session_id"] = sid
        else:
            st.pop("session_id", None)
        save_state(project, st)
    elif handover:
        # передача не состоялась: нить прежнего агента цела, только счётчик
        st = dict(saved); st.update(turns=turn_no, last_error=live._now())
        save_state(project, st)
    elif lost or first:
        st.pop("session_id", None)
        st.update(turns=turn_no, last_error=live._now())
        save_state(project, st)
    else:
        st.update(turns=turn_no, last_error=live._now())
        save_state(project, st)
    fields = dict(agent=plan["agent"], agent_bin=plan["bin"], model=plan["model"], effort=plan["effort"],
                  session=sid[:8] if sid else "", elapsed_s=elapsed, rights="full", turn=turn_no)
    if queued:
        fields["queued_s"] = queued
    if note:
        fields["note"] = note
    if handover:
        fields["handover_from"] = prev_agent
    if failed:
        detail = ("таймаут хода " + str(TURN_TIMEOUT) + " с; процесс снят" if timed_out["v"] else
                  err or f"код {rc}" + (f"; хвост: {plain[-300:]}" if plain.strip() else ""))
        if lost:
            detail += "; нить у CLI потеряна — следующий ход начнёт новую"
        live.post("secretary", "sec", answer, project=project, act=act, status="error",
                  detail=detail[:500], **fields)
        print(f"✗ secretary {elapsed} с: {detail[:200]}", flush=True)
        return 1
    live.post("secretary", "sec", answer, project=project, act=act, **fields, **extra)
    print(f"● secretary {elapsed} с, {len(answer)} симв.", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ход секретаря из окна стола")
    ap.add_argument("--project", required=True)
    ap.add_argument("--text-file", required=True, help="файл с репликой Автора («-» — stdin)")
    ap.add_argument("--anchor", default="", help="адрес якоря (live:<id> | room:<id>) — «продолжить отсюда»")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.text_file == "-" else Path(a.text_file).read_text(encoding="utf-8")
    if a.text_file != "-":
        try:
            Path(a.text_file).unlink()
        except OSError:
            pass
    text = text.strip()
    if not text:
        print("пустая реплика", file=sys.stderr)
        return 2
    try:
        return run_turn(str(Path(a.project).resolve()), text, a.anchor.strip())
    except anchor_mod.AnchorError as e:
        print(f"якорь: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
