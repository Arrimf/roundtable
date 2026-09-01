#!/usr/bin/env bash
# Полный цикл правки (этап 1): open_edit → executor_run → close → вердикт.
# Сценарии: штатный ход · вылет с карантином · осиротевший маркер (репост).
# Всё на изолированной комнате и временном репозитории.
set -euo pipefail
RT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d /tmp/edtest.XXXXXX)"
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/room"; cp "$RT/../Choir/"*.py "$W/room/"; : > "$W/room/live.jsonl"
git init -q "$W/proj"
git -C "$W/proj" -c user.name=t -c user.email=t@t commit -q --allow-empty -m base
export CHOIR_LEASE_DIR="$W/leases" CHOIR_WT_DIR="$W/wts" ROUNDTABLE_CHOIR="$W/room"

RT="$RT" python3 - "$W" <<'PY'
import json, os, signal, subprocess, sys, time
from pathlib import Path
w = Path(sys.argv[1]); rt = os.environ["RT"]
sys.path.insert(0, rt)
import edits, leases

ok = bad = 0
def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok, bad = ok + bool(cond), bad + (not cond)

def feed():
    return [json.loads(l) for l in (w/"room"/"live.jsonl").open() if l.strip()]

def run_executor(ed, cmd_json, wait=True):
    p = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                          "--act", ed["act"], "--epoch", str(ed["epoch"]),
                          "--worktree", str(ed["worktree"]),
                          "--voice", ed["voice"], "--cmd-json", cmd_json],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait:
        p.wait()
    return p

# Подменяем исполнителя лёгкой командой: тест проверяет МЕХАНИКУ, а не
# CLI голосов (те проверены живыми прогонами и стоят денег).
edits.EDIT_VOICES["codex"] = lambda task, bg: [
    "bash", "-c",
    "echo done > result.txt && git add -A && "
    "git -c user.name=x -c user.email=x@x commit -qm edit"]

# ── 1. Штатный ход: открыть → исполнить → close → вердикт «штатно» ──
ed = edits.open_edit(w/"proj", "тестовая правка", "codex")
evs = feed()
t("интент edit_open в ленте до запуска",
  any(e["kind"] == "edit_open" and e["act"] == ed["act"] for e in evs))
t("worktree построен на ветке акта",
  (ed["worktree"]/".git").exists())
fired = []
leases.watch(ed["act"],
             lambda why, e=ed: fired.append(
                 edits.verdict_on_drop(e["act"], e["epoch"],
                                       e["worktree"], e["voice"], why)),
             poll_grace=0.1)
run_executor(ed, json.dumps(edits.EDIT_VOICES["codex"]("", "")))
for _ in range(100):
    if fired: break
    time.sleep(0.05)
t("вердикт вынесен", bool(fired))
t("вердикт: штатное закрытие, не вылет",
  bool(fired) and fired[0].get("kind") == "edit_close")
head = subprocess.run(["git", "-C", str(ed["worktree"]), "log",
                       "--oneline"], capture_output=True, text=True).stdout
t("коммит исполнителя в worktree", "edit" in head)

# ── 2. ВЫЛЕТ: kill -9 обёртки → edit_crash, worktree цел ────────────
ed2 = edits.open_edit(w/"proj", "правка-которая-умрёт", "codex")
fired2 = []
leases.watch(ed2["act"],
             lambda why, e=ed2: fired2.append(
                 edits.verdict_on_drop(e["act"], e["epoch"],
                                       e["worktree"], e["voice"], why)),
             poll_grace=0.1)
p2 = run_executor(ed2, json.dumps(["sleep", "60"]), wait=False)
time.sleep(2)
os.kill(p2.pid, signal.SIGKILL); p2.wait()
for _ in range(100):
    if fired2: break
    time.sleep(0.05)
t("вылет замечен", bool(fired2))
t("вердикт: edit_crash со статусом crash",
  bool(fired2) and fired2[0].get("kind") == "edit_crash"
  and fired2[0].get("status") == "crash")
t("worktree карантина цел на диске", ed2["worktree"].is_dir())

# ── 3. Осиротевший маркер: окно спало, sweep репостит close ─────────
ed3 = edits.open_edit(w/"proj", "правка-при-упавшей-ленте", "codex")
feedf = w/"room"/"live.jsonl"
os.chmod(feedf, 0o444)                       # лента падает
run_executor(ed3, json.dumps(["true"]))
os.chmod(feedf, 0o644)                       # лента вернулась
mark = edits.marker_path(ed3["act"], ed3["epoch"])
t("маркер лёг, пока лента лежала", mark.exists())
t("close в ленте пока нет",
  edits.close_in_feed(ed3["act"], ed3["epoch"]) is None)
edits.sweep(set())                           # проход уборки окна
ev3 = edits.close_in_feed(ed3["act"], ed3["epoch"])
t("sweep репостнул close из маркера",
  ev3 is not None and ev3.get("reposted_from_marker") is True)
t("маркер снят после репоста", not mark.exists())

# ── 3б. Рестарт окна: акты без наблюдателя получают вердикт ─────────
# (главная дыра первой редакции — нашли все пять ревьюеров)
ed4 = edits.open_edit(w/"proj", "убит при спящем окне", "codex")
p4 = run_executor(ed4, json.dumps(["sleep", "60"]), wait=False)
time.sleep(2)
os.kill(p4.pid, signal.SIGKILL); p4.wait()      # окна «не было» — watch никто не взводил
ed5 = edits.open_edit(w/"proj", "жив при рестарте", "codex")
p5 = run_executor(ed5, json.dumps(["sleep", "4"]), wait=False)
time.sleep(1.5)
armed = []
n_rec = edits.recover_edits(lambda a, e, wt_, v: armed.append(a), grace_s=0)
t("recover вынес вердикт мёртвому и взвёл живому",
  n_rec == 2 and armed == [ed5["act"]])
ev4 = [e for e in feed() if e.get("kind") == "edit_crash"
       and e.get("act") == ed4["act"]]
t("мёртвый при спящем окне получил edit_crash", bool(ev4))
t("повторный recover не дублирует edit_crash",
  (edits.recover_edits(lambda *a: None, grace_s=0),
   len([e for e in feed() if e.get("kind") == "edit_crash"
        and e.get("act") == ed4["act"]]))[1] == 1)
p5.wait()
t("после recover повторный проход не плодит вердиктов",
  edits.recover_edits(lambda *a: None, grace_s=0) <= 1)  # ed5 закрыт close'ом

# ── 3б2. МОЛОДОЙ акт с невзятым замком recover не хоронит ───────────
# (executor ещё в git-пробах; вердикт сейчас = вылет живому — codex)
edY = edits.open_edit(w/"proj", "молодой, замок ещё не взят", "codex")
armedY = []
edits.recover_edits(lambda a, *r: armedY.append(a))     # grace по умолчанию
t("молодой акт получил наблюдателя, а не вердикт",
  edY["act"] in armedY and not [e for e in feed()
      if e.get("kind") == "edit_crash" and e.get("act") == edY["act"]])
run_executor(edY, json.dumps(["true"]))                 # добить штатно
leases._WATCHED.discard(edY["act"])

# ── 3в. Вылет при ЛЕЖАЩЕЙ ленте: crash-маркер, потом репост ────────
ed6 = edits.open_edit(w/"proj", "вылет без ленты", "codex")
p6 = run_executor(ed6, json.dumps(["sleep", "60"]), wait=False)
time.sleep(2)
os.kill(p6.pid, signal.SIGKILL); p6.wait()
os.chmod(feedf, 0o444)
edits.verdict_on_drop(ed6["act"], ed6["epoch"], ed6["worktree"],
                      ed6["voice"], None)
crash_m = edits.marker_path(ed6["act"], ed6["epoch"], "crash")
t("вылет при лежащей ленте оставил crash-маркер", crash_m.exists())
os.chmod(feedf, 0o644)
edits.sweep(set())
ev6 = [e for e in feed() if e.get("kind") == "edit_crash"
       and e.get("act") == ed6["act"]]
t("sweep донёс вылет из crash-маркера в ленту",
  bool(ev6) and ev6[0].get("reposted_from_marker") is True
  and not crash_m.exists())

# ── 3г. Мусорный маркер не роняет sweep и не бьётся об него вечно ───
# crash-маркер с чужим содержимым — мусор, его не репостят
badm = leases.LEASE_DIR / "yyy.5.crash.json"   # НЕ «bad»: так зовётся
badm.write_text(json.dumps({"act": "ДРУГОЙ", "epoch": 5}))  # счётчик t()
edits.sweep(set())
t("crash-маркер с чужим act внутри не репостится и не удаляется",
  badm.exists() and not [e for e in feed()
      if e.get("kind") == "edit_crash" and e.get("act") == "ДРУГОЙ"])
badm.unlink()

junk = leases.LEASE_DIR / "zzz.7.close.json"
junk.write_text(json.dumps({"act": "zzz", "epoch": 7, "kind": "хаос",
                            "text": "мусор", "status": "done", "rc": 0}))
try:
    edits.sweep(set())
    t("маркер с полем kind внутри не роняет sweep", True)
except TypeError:
    t("маркер с полем kind внутри не роняет sweep", False)
t("мусорный маркер репостнут и снят", not junk.exists())

# ── 3д. Задание с ведущим дефисом уходит ЗА `--`, а не флагом ───────
# (claude-лямбда осталась настоящей — подменяли только codex)
argv_d = edits.EDIT_VOICES["claude"]("--dangerously-skip-permissions",
                                     Path("/g"))
t("дефисное задание стоит после `--`",
  "--" in argv_d and argv_d[argv_d.index("--") + 1]
  == "--dangerously-skip-permissions")

# ── 3е. Ворота серийного канала: второй акт ждёт первого ────────────
# (этап 2: кими-исполнитель; тестовые ворота, чтобы не трогать живые)
# ворота живут в ~/.cache/choir/gates (пути env нет); имя testgate-edit
# уникально тесту и живым голосам не мешает
edG1 = edits.open_edit(w/"proj", "первый в воротах", "codex")
edG2 = edits.open_edit(w/"proj", "второй в воротах", "codex")
mark1, mark2 = w/"g1.ts", w/"g2.ts"
cmd_slow = ["bash", "-c", f"date +%s.%N > {mark1} && sleep 3 && "
            "git commit -q --allow-empty -m g1"]
cmd_fast = ["bash", "-c", f"date +%s.%N > {mark2} && "
            "git commit -q --allow-empty -m g2"]
pg1 = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                        "--act", edG1["act"], "--epoch", str(edG1["epoch"]),
                        "--worktree", str(edG1["worktree"]), "--voice", "x",
                        "--serial-gate", "testgate-edit",
                        "--cmd-json", json.dumps(cmd_slow)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)                        # первый уже внутри ворот
pg2 = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                        "--act", edG2["act"], "--epoch", str(edG2["epoch"]),
                        "--worktree", str(edG2["worktree"]), "--voice", "x",
                        "--serial-gate", "testgate-edit",
                        "--cmd-json", json.dumps(cmd_fast)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pg1.wait(); pg2.wait()
t1 = float(mark1.read_text()); t2 = float(mark2.read_text())
# Контрольная пара БЕЗ ворот: тот же sleep, те же два процесса — если
# и тут разрыв большой, значит его давал планировщик/аренда, и замер с
# воротами ничего не доказывал (нашли codex и deepseek: тест был бы
# зелёным даже при выключенном --serial-gate).
edC1 = edits.open_edit(w/"proj", "контроль-1", "codex")
edC2 = edits.open_edit(w/"proj", "контроль-2", "codex")
mc1, mc2 = w/"c1.ts", w/"c2.ts"
pc1 = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                        "--act", edC1["act"], "--epoch", str(edC1["epoch"]),
                        "--worktree", str(edC1["worktree"]), "--voice", "x",
                        "--cmd-json", json.dumps(
                            ["bash", "-c", f"date +%s.%N > {mc1} && sleep 3 && "
                             "git commit -q --allow-empty -m c1"])],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
pc2 = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                        "--act", edC2["act"], "--epoch", str(edC2["epoch"]),
                        "--worktree", str(edC2["worktree"]), "--voice", "x",
                        "--cmd-json", json.dumps(
                            ["bash", "-c", f"date +%s.%N > {mc2} && "
                             "git commit -q --allow-empty -m c2"])],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pc1.wait(); pc2.wait()
dt_free = float(mc2.read_text()) - float(mc1.read_text())
t("ворота держатся весь акт (с ними ждём, без них нет)",
  t2 - t1 >= 2.5 and dt_free < 2.0)

# щит-префикс Кими: дефисное задание не станет флагом его CLI
argv_k = edits.EDIT_VOICES["kimi"]("-опасный-текст", Path("/g")) \
    if "kimi" in edits.EDIT_VOICES and callable(edits.EDIT_VOICES.get("kimi")) \
    else None
t("щит-префикс Кими прикрывает дефисное задание",
  argv_k is not None and argv_k[-1].startswith("ЗАДАНИЕ СТОЛА. -опасный"))

# грок: командная строка script — shell; кавычки задания обязаны
# вернуться из shlex ЦЕЛЫМИ, иначе это инъекция под uid Автора
import shlex as _sh
gargv = edits.EDIT_VOICES["grok"]('он сказал "так"; rm -rf $HOME',
                                  Path("/g"))
inner = _sh.split(gargv[2])
t("кавычки и ; в задании грока не рвут shell-строку script",
  inner[0] == "grok" and inner[2].startswith("ЗАДАНИЕ СТОЛА. он сказал")
  and 'rm -rf $HOME' in inner[2] and inner[-1] == "--no-plan")
dargv = edits.EDIT_VOICES["deepseek"]("-дефис", Path("/g"))
t("щит deepseek прикрывает дефисное задание",
  dargv[-1].startswith("ЗАДАНИЕ СТОЛА. -дефис"))
# ЖИВОЙ script: перенос строки, бэктики и $() обязаны дойти литерально
# (gemini предположил разрыв по \n — рассуждение проверяется прогоном)
import shlex as _sh2
tricky = "перенос\nвторая -строка `id` $(id)"
q = _sh2.quote("ЗАДАНИЕ. " + tricky)
got = subprocess.run(["script", "-qec", f"printf '%s' {q}", "/dev/null"],
                     capture_output=True, text=True).stdout
t("script+quote доносят многострочное задание литерально",
  "`id`" in got and "$(id)" in got and "перенос" in got
  and "uid=" not in got)
ev_last = [e for e in feed() if e.get("kind") == "edit_open"][-1]
t("edit_open несёт seat (чем исполняется голос)",
  bool(ev_last.get("seat")))

# ── 4. Отказы open_edit ─────────────────────────────────────────────
try:
    edits.open_edit(w/"proj"/"нет", "x", "codex")
    t("кривой project → отказ", False)
except edits.EditRefused:
    t("кривой project → отказ", True)
try:
    edits.open_edit(w/"proj", "x", "gemini")
    t("голос без рук → отказ", False)
except edits.EditRefused:
    t("голос без рук → отказ", True)

print(f"\nedits: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
PY
