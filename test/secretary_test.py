#!/usr/bin/env python3
"""Секретарь стола (chamber/secretary.py, роль введена Автором 2026-10-06):
решения, очередь и отчёты — события ленты через live.post; очередь — свёртка
событий; tail — приём роли. На подменной ленте, без окна и без моделей."""
import atexit
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="sectest.", dir="/var/tmp"))
os.environ["CHOIR_JOURNAL"] = str(W)
os.environ["ROUNDTABLE_JOURNAL"] = str(W)
os.environ["CHOIR_ROOM"] = str(W / "room.jsonl")
os.environ.pop("CHOIR_EVENT_PROJECT", None)
os.environ.pop("SECRETARY_AGENT", None)
sys.path.insert(0, str(RT / "chamber"))
atexit.register(shutil.rmtree, W, True)      # и при падении теста каталог не остаётся
os.chdir(RT)                                  # cwd = стол → проект по умолчанию стол
import live        # noqa: E402
import secretary   # noqa: E402

ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


def feed():
    return [json.loads(l) for l in live.LIVE.read_text(encoding="utf-8").splitlines() if l.strip()]


t("лента теста — в подменном каталоге", str(live.LIVE).startswith(str(W)))

# ── решение: дословно, author = кто решил, recorded_by = кто записал, проект = стол ──
e = secretary.decision("  в слепой фазе Jev не звать  ", by="arr", recorded_by="claude", about="Jev, jev-v2", source="диалог 2026-10-06")
f = feed()
t("decision: событие в ленте, текст без краёв, author=arr, recorded_by, about, source",
  len(f) == 1 and f[0]["kind"] == "decision" and f[0]["text"] == "в слепой фазе Jev не звать"
  and f[0]["author"] == "arr" and f[0]["recorded_by"] == "claude" and f[0]["about"] == "Jev, jev-v2"
  and f[0]["source"] == "диалог 2026-10-06")
t("decision: проект по умолчанию из cwd — каталог стола", f[0]["project"] == str(RT))
os.chdir(RT / "test")
t("_project: из подкаталога стола — всё равно стол", secretary._project(None) == str(RT))
other = next((d for d in sorted(RT.parent.iterdir()) if d.is_dir() and d.name != RT.name and not d.name.startswith(".")), None)
if other is not None:
    os.chdir(other)
    t("_project: из папки другого проекта песочницы — этот проект", secretary._project(None) == str(other.resolve()))
os.chdir(RT)
try:
    secretary.decision("   ")
    t("decision: пустое решение — отказ", False)
except SystemExit:
    t("decision: пустое решение — отказ", True)

# ── очередь: add → start → done; drop; свёртка; закрытое не трогать ──
a1 = secretary.queue_add("secretary.py и вкладка", by="claude")
a2 = secretary.queue_add("прокси Jev для голосов", by="claude")
a3 = secretary.queue_add("опыт: вердикты против словаря", by="claude")
q = {i["item"]: i for i in secretary.fold_queue()}
t("queue add: три открытых пункта, item = id события", [q[i]["status"] for i in (a1["id"], a2["id"], a3["id"])] == ["open"] * 3)
secretary.queue_op("start", a1["id"], by="claude")
secretary.queue_op("done", a1["id"], by="claude", note="коммит abc")
secretary.queue_op("drop", a3["id"], by="arr", note="отложено Автором")
q = {i["item"]: i for i in secretary.fold_queue()}
t("queue: start→done с пометкой; drop с пометкой; второй пункт открыт",
  q[a1["id"]]["status"] == "done" and q[a1["id"]]["note"] == "коммит abc" and q[a1["id"]]["last_by"] == "claude"
  and q[a3["id"]]["status"] == "dropped" and q[a3["id"]]["note"] == "отложено Автором"
  and q[a2["id"]]["status"] == "open")
for op, item, name in (("done", a1["id"], "закрытый пункт нельзя закрыть снова"),
                       ("start", 999999, "несуществующий пункт — отказ")):
    try:
        secretary.queue_op(op, item)
        t("queue: " + name, False)
    except SystemExit:
        t("queue: " + name, True)
t("queue: события op ссылаются на пункт полем item, текст пункта повторён",
  [x for x in feed() if x.get("kind") == "queue" and x.get("op") == "done"][0]["item"] == a1["id"])
# гонка: op после закрытия, записанный мимо проверки, свёртку не переоткрывает
live.post("x", "queue", "поздний start", op="start", item=a1["id"], project=str(RT))
t("fold_queue: закрытое терминально — поздний start не переоткрывает", {i["item"]: i for i in secretary.fold_queue()}[a1["id"]]["status"] == "done")
# чужой --project на операции — отказ, событие в чужой проект не уходит
try:
    secretary.queue_op("start", a2["id"], project=str(W))
    t("queue_op: --project не проекта пункта — отказ", False)
except SystemExit:
    t("queue_op: --project не проекта пункта — отказ", True)
secretary.queue_op("start", a2["id"], project=str(RT))
t("queue_op: --project совпадает — операция записана в проект пункта",
  feed()[-1]["project"] == str(RT) and feed()[-1]["op"] == "start")

# ── отчёт ──
r = secretary.report("# Итог\n\nсделано то-то\n", by="claude", about="долги витка")
t("report: текст целиком, author, about", r["kind"] == "report" and r["text"] == "# Итог\n\nсделано то-то" and r["about"] == "долги витка")

# ── приём роли: tail ──
st = secretary.state()
txt = secretary.render(st)
t("state: последние решения, только открытая очередь, последние отчёты",
  [d["text"] for d in st["decisions"]] == ["в слепой фазе Jev не звать"]
  and [i["item"] for i in st["queue"]] == [a2["id"]] and len(st["reports"]) == 1)
t("render: три раздела и пункт очереди с номером",
  "РЕШЕНИЯ АВТОРА" in txt and "ОЧЕРЕДЬ" in txt and f"[{a2['id']}] started" in txt and "ОТЧЁТЫ" in txt and "# Итог" in txt)

# ── CLI: те же команды из оболочки, с SECRETARY_AGENT ──
env = dict(os.environ, SECRETARY_AGENT="grok")
run = lambda *args: subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), *args],
                                   capture_output=True, text=True, env=env)
p = run("decision", "ворота проверки снять", "--about", "Jev")
t("CLI decision: записано, recorded_by из SECRETARY_AGENT", p.returncode == 0 and "решение записано" in p.stdout
  and feed()[-1]["recorded_by"] == "grok" and feed()[-1]["author"] == "arr")
p = run("queue", "add", "перезапустить окна")
item = int(re.search(r"пункт (\d+)", p.stdout).group(1))
p2 = run("queue", "start", str(item))
p3 = run("queue", "list")
t("CLI queue add/start/list", p.returncode == 0 and p2.returncode == 0 and f"[{item}] started" in p3.stdout)
p = run("queue", "done", "abc")
t("CLI queue done с не-номером — отказ", p.returncode != 0)
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), "decision", "-", "--about", "stdin"],
                   input="решение с $долларом и `кавычками` -ведущим\n", capture_output=True, text=True, env=env)
t("CLI decision - (stdin): текст дословно, мимо оболочки", p.returncode == 0 and feed()[-1]["text"] == "решение с $долларом и `кавычками` -ведущим")
p = run("report", "--file", str(W / "нет-такого.md"))
t("CLI report --file несуществующий — отказ без трассы", p.returncode != 0 and "Traceback" not in p.stderr and "не читается" in p.stderr)
rep = W / "rep.md"; rep.write_text("отчёт из файла\nстрока 2\n", encoding="utf-8")
p = run("report", "--file", str(rep), "--by", "grok")
t("CLI report --file: записан целиком, author=grok", p.returncode == 0 and feed()[-1]["text"] == "отчёт из файла\nстрока 2" and feed()[-1]["author"] == "grok")
p = run("tail", "--n", "5")
t("CLI tail: показывает решение, открытый пункт и отчёт", p.returncode == 0 and "ворота проверки снять" in p.stdout
  and f"[{item}] started" in p.stdout and "отчёт из файла" in p.stdout)
p = run("--project", str(W), "decision", "решение чужого проекта")
t("CLI --project: событие помечено проектом", p.returncode == 0 and feed()[-1]["project"] == str(W))
# диалог из терминала: два события sec (arr, secretary) с channel=terminal, агент, late
(W / "q.txt").write_text("вопрос Автора из терминала\nвторая строка", encoding="utf-8")
(W / "a.txt").write_text("ответ секретаря из терминала", encoding="utf-8")
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), "--project", str(W), "dialogue", "--author-file", str(W / "q.txt"),
                    "--answer-file", str(W / "a.txt"), "--agent", "claude", "--late"], capture_output=True, text=True, env=dict(os.environ))
f2 = feed()[-2:]
t("CLI dialogue: реплика Автора (recorded_by=агент, без rights) и ответ (rights=full) — события sec с channel=terminal, agent, late",
  p.returncode == 0 and "диалог записан" in p.stdout and [e["author"] for e in f2] == ["arr", "secretary"]
  and all(e["kind"] == "sec" and e["channel"] == "terminal" and e["agent"] == "claude" and e["late"] is True for e in f2)
  and f2[0]["text"] == "вопрос Автора из терминала\nвторая строка" and f2[0]["recorded_by"] == "claude" and "rights" not in f2[0] and f2[1]["rights"] == "full")
n0 = len(feed())
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), "--project", str(W), "dialogue", "--answer-file", str(W / "a.txt")], capture_output=True, text=True,
                   env=dict(os.environ, SECRETARY_AGENT="grok"))
t("CLI dialogue: только ответ — ровно одно событие, агент из SECRETARY_AGENT, поля late нет без --late",
  p.returncode == 0 and len(feed()) == n0 + 1 and feed()[-1]["author"] == "secretary" and feed()[-1]["agent"] == "grok" and "late" not in feed()[-1])
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), "--project", str(W), "dialogue", "--answer-file", str(W / "a.txt")], capture_output=True, text=True,
                   env={k: v for k, v in os.environ.items() if k != "SECRETARY_AGENT"})
t("CLI dialogue без агента — отказ словами, событий нет", p.returncode != 0 and "кто отвечал" in (p.stderr + p.stdout) and len(feed()) == n0 + 1)
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), "--project", str(W), "dialogue", "--agent", "claude", "--author-file", str(W / "nope.txt")], capture_output=True, text=True)
t("CLI dialogue: нет файла — отказ словами, не трасса", p.returncode != 0 and "Traceback" not in p.stderr and len(feed()) == n0 + 1)
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary.py"), "--project", str(W), "dialogue", "--agent", "claude"], capture_output=True, text=True)
t("CLI dialogue: ни реплики, ни ответа — отказ", p.returncode != 0 and "ни реплики" in (p.stderr + p.stdout))
t("state без --all не видит чужой проект, с --all видит",
  "решение чужого проекта" not in secretary.render(secretary.state())
  and "решение чужого проекта" in secretary.render(secretary.state(all_projects=True)))

shutil.rmtree(W, ignore_errors=True)
print(f"\nsecretary: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
