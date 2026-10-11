#!/usr/bin/env python3
"""Ход секретаря из окна (chamber/secretary_turn.py): реплика Автора и ответ
агента — события sec в ленте; нить одна на проект (session-id на первом
ходе, resume дальше); преамбула роли только в первом ходе; поток работы
идёт в stdout (стенограмма акта); отказ CLI — событие с ошибкой. CLI
подменён скриптом, печатающим stream-json."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="sectn.", dir="/var/tmp"))
(W / "proj").mkdir()
STUB = W / "claude-stub"
STUB.write_text("""#!/usr/bin/env python3
import sys, json, os, time
args = sys.argv[1:]
prompt = sys.stdin.read()                      # промпт — через stdin
open(os.environ["STUB_LOG"], "a").write(json.dumps(args + [prompt], ensure_ascii=False) + "\\n")
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
if os.environ.get("STUB_LOST"):
    print("No conversation found with session ID: " + (args[args.index("--resume")+1] if "--resume" in args else "?")); sys.exit(1)
if os.environ.get("STUB_BADUTF"):
    sys.stdout.buffer.write(b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"\\xff\\xfe bad"}]}}\\n'); sys.stdout.flush()
if os.environ.get("STUB_FAIL"):
    print(json.dumps({"type": "result", "is_error": True, "result": "usage limit"})); sys.exit(1)
print(json.dumps({"type": "system", "subtype": "init", "model": "claude-stub"}, ensure_ascii=False))
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "думаю над репликой"}]}}, ensure_ascii=False))
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ответ секретаря: " + prompt[-40:]}]}}, ensure_ascii=False))
print(json.dumps({"type": "result", "result": "ответ секретаря: " + prompt[-40:], "duration_ms": 10, "total_cost_usd": 0.001}, ensure_ascii=False))
""", encoding="utf-8")
os.chmod(STUB, 0o755)
env = dict(os.environ, CHOIR_JOURNAL=str(W / "journal"), ROUNDTABLE_JOURNAL=str(W / "journal"),
           CHOIR_ROOM=str(W / "journal" / "room.jsonl"), CHOIR_SEC_BIN=str(STUB), CHOIR_SEC_MODEL="claude-stub",
           CHOIR_SEC_EFFORT="low", STUB_LOG=str(W / "stub.log"), RT_ACT_ID="act12345", CHOIR_RT_URL="http://127.0.0.1:1",
           CHOIR_GATE_DIR=str(W / "gates"), CHOIR_DSH_PATCH_DIR=str(W / "dsh-patches"))
for k in ("CHOIR_EVENT_PROJECT", "STUB_FAIL", "STUB_SLEEP", "STUB_LOST", "STUB_BADUTF", "CHOIR_SEC_TIMEOUT", "CHOIR_SEC_AGENT",
          "CHOIR_CODEX_MODEL", "CHOIR_CODEX_EFFORT", "CHOIR_GROK_MODEL", "CHOIR_KIMI_MODEL", "DEEPSEEK_MODEL"):
    env.pop(k, None)
(W / "journal").mkdir()
ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


def feed():
    p = W / "journal" / "live.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def turn(text, extra_env=None):
    tf = W / "msg.txt"; tf.write_text(text, encoding="utf-8")
    e = dict(env, **(extra_env or {}))
    return subprocess.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"),
                           "--text-file", str(tf)], capture_output=True, text=True, env=e, timeout=60)


p = turn("давай сделаем окно чата")
f = feed()
args1 = json.loads((W / "stub.log").read_text().splitlines()[0])
t("первый ход: rc=0, реплика Автора и ответ секретаря — события sec проекта с act",
  p.returncode == 0 and [e["author"] for e in f] == ["arr", "secretary"] and all(e["kind"] == "sec" and e["project"] == str(W / "proj") and e["act"] == "act12345" for e in f))
t("ответ секретаря: текст из result, agent=claude (agent_bin — стаб), model/effort/session/elapsed/rights=full/turn",
  f[1]["text"].startswith("ответ секретаря:") and f[1]["agent"] == "claude" and f[1]["agent_bin"] == str(STUB) and f[1]["model"] == "claude-stub" and f[1]["effort"] == "low"
  and len(f[1]["session"]) == 8 and f[1]["rights"] == "full" and f[1]["turn"] == 1 and "elapsed_s" in f[1])
t("первый ход: CLI получил --session-id, права полные, stream-json; промпт через stdin — преамбула роли и реплика",
  "--session-id" in args1 and "--dangerously-skip-permissions" in args1 and "stream-json" in args1
  and "СЕКРЕТАРЬ стола" in args1[-1] and "secretary.py tail" in args1[-1] and args1[-1].rstrip().endswith("давай сделаем окно чата")
  and not any(a.startswith("Ты — СЕКРЕТАРЬ") for a in args1[:-1]))
t("поток работы ушёл в stdout (стенограмму акта): шапка хода, команда, мысли", "→ secretary" in p.stdout and "$ " in p.stdout and "думаю над репликой" in p.stdout)
t("файл реплики убран после чтения", not (W / "msg.txt").exists())
sid1 = [a for a in args1 if len(a) == 36 and a.count("-") == 4][0]
st = json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())
t("состояние нити: session_id, turns=1, agent=claude", st["session_id"] == sid1 and st["turns"] == 1 and st["agent"] == "claude")

p = turn("а теперь второй вопрос")
args2 = json.loads((W / "stub.log").read_text().splitlines()[1])
f = feed()
t("второй ход: --resume той же нити, без преамбулы, только реплика (stdin)",
  "--resume" in args2 and sid1 in args2 and "СЕКРЕТАРЬ стола" not in args2[-1] and args2[-1] == "а теперь второй вопрос")
t("второй ход: turns=2, события arr/secretary снова, turn=2", len(f) == 4 and f[2]["turn"] == 2 and f[3]["turn"] == 2)

p = turn("упади", {"STUB_FAIL": "1"})
f = feed()
t("отказ CLI (is_error): rc≠0, событие sec от секретаря со status=error и причиной, нить сохранена",
  p.returncode != 0 and f[-1]["author"] == "secretary" and f[-1]["status"] == "error" and "usage limit" in f[-1]["detail"]
  and json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())["session_id"] == sid1)

tf = W / "empty.txt"; tf.write_text("   ")
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"), "--text-file", str(tf)],
                   capture_output=True, text=True, env=env, timeout=30)
t("пустая реплика — отказ без события", p.returncode == 2 and len(feed()) == 6)

# «продолжить отсюда» → секретарю: блок якоря в промпте (с именем автора), поле anchor у обоих событий
import subprocess as _sp
anchor_src = feed()[1]           # ответ секретаря первого хода
tf = W / "msg.txt"; tf.write_text("разверни это", encoding="utf-8")
p = _sp.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"),
             "--text-file", str(tf), "--anchor", f"live:{anchor_src['id']}"], capture_output=True, text=True, env=env, timeout=60)
args_a = json.loads((W / "stub.log").read_text().splitlines()[-1])
f = feed()
t("--anchor: промпт начинается с блока КОНТЕКСТ с автором и текстом якоря, затем реплика",
  p.returncode == 0 and "КОНТЕКСТ — сообщение" in args_a[-1] and "Секретарь (агент-посредник Автора)" in args_a[-1]
  and anchor_src["text"] in args_a[-1] and args_a[-1].rstrip().endswith("разверни это"))
t("--anchor: поле anchor (addr, author, sha, kind) у реплики Автора и у ответа",
  f[-2]["anchor"]["addr"] == f"live:{anchor_src['id']}" and f[-2]["anchor"]["author"] == "secretary" and f[-1]["anchor"]["kind"] == "sec")
tf.write_text("x", encoding="utf-8")
turns_before = json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())["turns"]
p = _sp.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"),
             "--text-file", str(tf), "--anchor", "live:999999"], capture_output=True, text=True, env=env, timeout=60)
t("--anchor на несуществующее событие: отказ словами, без событий, счётчик ходов не вырос",
  p.returncode == 2 and "в ленте нет" in p.stderr and len(feed()) == len(f)
  and json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())["turns"] == turns_before)
# room:-якорь — по CHOIR_ROOM (тот же путь, что у окна); событие другого проекта — отказ без событий
(W / "journal" / "room.jsonl").write_text(json.dumps({"id": "rrrrrrrrrrr1", "round": "r9", "phase": "summary", "role": "summary",
                                                       "voice": "codex", "status": "ok", "text": "свод раунда r9: решено так-то"}, ensure_ascii=False) + "\n", encoding="utf-8")
tf.write_text("продолжи от свода", encoding="utf-8")
p = _sp.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"),
             "--text-file", str(tf), "--anchor", "room:rrrrrrrrrrr1"], capture_output=True, text=True, env=env, timeout=60)
args_r = json.loads((W / "stub.log").read_text().splitlines()[-1])
f = feed()
t("--anchor room:<id>: запись раунда из CHOIR_ROOM в блоке, поле anchor с round/phase",
  p.returncode == 0 and "свод раунда r9" in args_r[-1] and "раунда «r9»" in args_r[-1]
  and f[-1]["anchor"] == {"addr": "room:rrrrrrrrrrr1", "author": "codex", "sha": f[-1]["anchor"]["sha"], "kind": "summary", "round": "r9", "phase": "summary"})
_other = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import live, json; e = live.post('arr', 'say', 'реплика чужого проекта', project=sys.argv[2]); print(e['id'])",
                         str(RT / "chamber"), str(W / "other")], capture_output=True, text=True, env=env, timeout=30)
oid = _other.stdout.strip()
n_before = len(feed())
tf.write_text("продолжи чужое", encoding="utf-8")
p = _sp.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"),
             "--text-file", str(tf), "--anchor", f"live:{oid}"], capture_output=True, text=True, env=env, timeout=60)
t("--anchor на событие другого проекта: отказ словами (rc 2), событий нет", p.returncode == 2 and "другого проекта" in p.stderr and len(feed()) == n_before)

# нить потеряна у CLI («No conversation found») → сброс, следующий ход — новый session-id с преамбулой
p = turn("ты тут?", {"STUB_LOST": "1"})
f = feed()
stf = next((W / "journal" / "secretary").glob("state.*.json"))
t("потерянная нить: ошибка с пометкой, session_id сброшен", p.returncode != 0 and "нить у CLI потеряна" in f[-1]["detail"] and "session_id" not in json.loads(stf.read_text()))
p = turn("снова")
args_new = json.loads((W / "stub.log").read_text().splitlines()[-1])
t("после сброса: новый --session-id и преамбула снова", "--session-id" in args_new and sid1 not in args_new and "СЕКРЕТАРЬ стола" in args_new[-1])

# упавший ПЕРВЫЙ ход новой нити не сохраняет session_id (иначе вечный --resume в никуда)
import shutil as _sh
_sh.rmtree(W / "journal" / "secretary", ignore_errors=True)
p = turn("первый и сразу упал", {"STUB_FAIL": "1"})
t("упавший первый ход: session_id не сохранён", "session_id" not in json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text()))

# таймаут: сторож снимает группу процессов, событие с причиной
p = turn("думай долго", {"STUB_SLEEP": "20", "CHOIR_SEC_TIMEOUT": "2"})
f = feed()
t("таймаут хода: процесс снят за секунды, событие error с причиной «таймаут»", p.returncode != 0 and "таймаут" in f[-1]["detail"] and f[-1]["elapsed_s"] < 15)

# невалидный UTF-8 в выводе не роняет ход
p = turn("а теперь мусор", {"STUB_BADUTF": "1"})
f = feed()
t("невалидный UTF-8 в потоке: ход завершён, ответ записан", p.returncode == 0 and f[-1]["author"] == "secretary" and f[-1]["text"].startswith("ответ секретаря"))


# ── ВЕНДОРЫ КРЕСЛА (решение Автора 2026-10-08): codex / grok / kimi / dsh стабами; gemini — отказ ──
# стаб-агент общий: пишет argv+cwd+stdin в лог, отвечает по вендору
AG = W / "agent-stub"
AG.write_text("""#!/usr/bin/env python3
import sys, json, os
args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
pf = args[args.index("--prompt-file")+1] if "--prompt-file" in args else ""
prompt = stdin or (open(pf, encoding="utf-8").read() if pf else "")
open(os.environ["STUB_LOG"], "a").write(json.dumps({"argv": args, "cwd": os.getcwd(), "stdin": stdin, "prompt": prompt}, ensure_ascii=False) + "\\n")
kind = os.environ.get("STUB_KIND", "")
if kind == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "thread-abc-123"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex думает"}}))
    out = args[args.index("-o")+1]
    open(out, "w", encoding="utf-8").write("ответ codex: " + (prompt or args[-1])[-30:])
    sys.exit(0)
if kind == "grok":
    # grok идёт --output-format streaming-json: мысли, текст дельтами, end
    print(json.dumps({"type": "thought", "data": "думаю"}, ensure_ascii=False))
    for part in ("ответ grok:", " " + (prompt or args[-1])[-30:]):
        print(json.dumps({"type": "text", "data": part}, ensure_ascii=False))
    print(json.dumps({"type": "end", "stopReason": "end_turn"}))
    sys.exit(0)
print("\\x1b[32mответ " + kind + ":\\x1b[0m " + (prompt or args[-1])[-30:])
""", encoding="utf-8")
os.chmod(AG, 0o755)


def agent_turn(agent, text, kind):
    tf = W / "msg.txt"; tf.write_text(text, encoding="utf-8")
    e = dict(env, CHOIR_SEC_AGENT=agent, CHOIR_SEC_BIN=str(AG), STUB_KIND=kind)
    e.pop("CHOIR_SEC_MODEL", None); e.pop("CHOIR_SEC_EFFORT", None)
    return subprocess.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"),
                           "--text-file", str(tf)], capture_output=True, text=True, env=e, timeout=90)


def last_call():
    return json.loads((W / "stub.log").read_text(encoding="utf-8").splitlines()[-1])


turns0 = json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())["turns"]
# codex: передача роли от claude — новая нить, преамбула + хвост диалога; exec с обходом песочницы, промпт stdin
p = agent_turn("codex", "теперь ты, Кодекс", "codex")
c = last_call(); f = feed()
t("codex: первый ход — exec --dangerously-bypass-approvals-and-sandbox --json -o … - (промпт stdin), cwd = проект",
  p.returncode == 0 and c["argv"][:4] == ["exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json"]
  and "-o" in c["argv"] and c["argv"][-1] == "-" and c["stdin"].rstrip().endswith("теперь ты, Кодекс") and c["cwd"] == str(W / "proj"))
t("codex: ПЕРЕДАЧА РОЛИ от claude — преамбула, строка передачи и последние реплики диалога из ленты",
  "СЕКРЕТАРЬ стола" in c["stdin"] and "ПЕРЕДАЧА РОЛИ" in c["stdin"] and "«claude»" in c["stdin"]
  and "ПОСЛЕДНИЕ РЕПЛИКИ ДИАЛОГА" in c["stdin"] and "а теперь мусор" in c["stdin"] and "ответ секретаря" in c["stdin"])
t("codex: ответ из файла -o, событие с agent=codex, handover_from=claude; нить = thread_id из thread.started",
  f[-1]["author"] == "secretary" and f[-1]["text"].startswith("ответ codex:") and f[-1]["agent"] == "codex" and f[-1]["handover_from"] == "claude"
  and json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text()).get("session_id") == "thread-abc-123")
p = agent_turn("codex", "второй вопрос Кодексу", "codex")
c = last_call()
t("codex: второй ход — exec … resume thread-abc-123 «-», промпт через stdin, без преамбулы",
  p.returncode == 0 and "resume" in c["argv"] and c["argv"][c["argv"].index("resume")+1] == "thread-abc-123"
  and c["argv"][-1] == "-" and c["stdin"] == "второй вопрос Кодексу")

# grok: pty через script, нить по cwd ПРОЦЕССА (каталог нити секретаря), --cwd проект, обход разрешений; второй ход -c
p = agent_turn("grok", "теперь Грок", "grok")
c = last_call(); f = feed()
GSID = c["argv"][c["argv"].index("--session-id")+1] if "--session-id" in c["argv"] else ""
t("grok: script -qec → стаб получил --session-id <uuid> (своя нить, не -c по проекту), --cwd <проект>, --prompt-file, --permission-mode bypassPermissions, --no-plan",
  p.returncode == 0 and len(GSID) == 36 and "streaming-json" in c["argv"] and "--cwd" in c["argv"] and c["argv"][c["argv"].index("--cwd")+1] == str(W / "proj")
  and "--permission-mode" in c["argv"] and "bypassPermissions" in c["argv"] and "--no-plan" in c["argv"] and "-c" not in c["argv"]
  and "ПЕРЕДАЧА РОЛИ" in c["prompt"] and c["prompt"].count("теперь Грок") == 1)
t("grok: ответ из stdout без ANSI, событие agent=grok", f[-1]["agent"] == "grok" and f[-1]["text"].startswith("ответ grok:") and "\x1b" not in f[-1]["text"])
p = agent_turn("grok", "ещё Грок", "grok")
c = last_call()
t("grok: второй ход — --resume <тот же uuid>, без преамбулы", p.returncode == 0 and "--resume" in c["argv"] and c["argv"][c["argv"].index("--resume")+1] == GSID and "СЕКРЕТАРЬ стола" not in c["prompt"])

# kimi: -m провайдер/модель, --add-dir проект, -p со щитом; второй ход -c; замок линии — в CHOIR_GATE_DIR
p = agent_turn("kimi", "теперь Кими", "kimi")
c = last_call(); f = feed()
t("kimi: -m <провайдер/модель>, --add-dir <проект>, -p со щитом, преамбула первым ходом; cwd — каталог нити",
  p.returncode == 0 and c["argv"][0] == "-m" and "/" in c["argv"][1] and "--add-dir" in c["argv"] and c["argv"][c["argv"].index("--add-dir")+1] == str(W / "proj")
  and c["argv"][-2] == "-p" and c["argv"][-1].startswith("РЕПЛИКА АВТОРА. Ты — СЕКРЕТАРЬ") and c["cwd"].startswith(str(W / "journal" / "secretary" / "kimi.")))
t("kimi: событие agent=kimi, усилие пустое (у CLI нет рычага), замок линии взят в каталоге стенда",
  f[-1]["agent"] == "kimi" and f[-1]["effort"] == "" and (W / "gates").exists())
p = agent_turn("kimi", "ещё Кими", "kimi")
c = last_call()
t("kimi: второй ход — -c, без преамбулы", p.returncode == 0 and "-c" in c["argv"] and "СЕКРЕТАРЬ" not in c["argv"][-1])

# deepseek → dsh: без сессии — каждый ход преамбула + история диалога; патч модели
p = agent_turn("deepseek", "теперь DeepSeek", "dsh")
c = last_call(); f = feed()
t("deepseek: dsh --profile headless --patch <модель> <промпт со щитом>; преамбула и история диалога каждый ход",
  p.returncode == 0 and c["argv"][:3] == ["--profile", "headless", "--patch"] and c["argv"][3].endswith(".yaml")
  and c["argv"][-1].startswith("РЕПЛИКА АВТОРА. Ты — СЕКРЕТАРЬ") and "ПОСЛЕДНИЕ РЕПЛИКИ ДИАЛОГА" in c["argv"][-1] and "ещё Кими" in c["argv"][-1])
p = agent_turn("deepseek", "второй DeepSeek", "dsh")
c = last_call()
t("deepseek: второй ход — снова преамбула и история (сессии нет), текущая реплика в истории НЕ дублируется, session пустая",
  p.returncode == 0 and "СЕКРЕТАРЬ стола" in c["argv"][-1] and "теперь DeepSeek" in c["argv"][-1]
  and c["argv"][-1].count("второй DeepSeek") == 1 and feed()[-1]["session"] == "")
# потолок аргумента у kimi/dsh — отказ словами, без события реплики
n_ev = len(feed())
p = agent_turn("deepseek", "x" * 120000, "dsh")
t("dsh: промпт больше ARGV_CAP — отказ словами (событие error, реплика Автора не записана), CLI не звался",
  p.returncode != 0 and feed()[-1]["status"] == "error" and "потолка аргумента" in feed()[-1]["detail"] and len(feed()) == n_ev + 1)

# gemini: кресла нет — отказ событием, без вызова
n0 = len((W / "stub.log").read_text().splitlines())
p = agent_turn("gemini", "а ты, Джемини?", "gemini")
f = feed()
t("gemini: кресла секретаря нет — событие error с причиной, CLI не звался",
  p.returncode != 0 and f[-1]["status"] == "error" and "кресла секретаря нет" in f[-1]["detail"] and f[-1]["agent"] == "gemini"
  and len((W / "stub.log").read_text().splitlines()) == n0)

# обратно к claude: передача роли от deepseek — новая нить с преамбулой
p = turn("вернись, Клод")
args_b = json.loads((W / "stub.log").read_text().splitlines()[-1])
t("claude после других: ПЕРЕДАЧА РОЛИ от deepseek — новый --session-id, преамбула и история",
  p.returncode == 0 and "--session-id" in args_b and "ПЕРЕДАЧА РОЛИ" in args_b[-1] and "«deepseek»" in args_b[-1] and feed()[-1]["handover_from"] == "deepseek")
# упавшая передача роли: нить прежнего агента (deepseek — без нити; проверим на claude ниже) не теряется
sid_c = json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())["session_id"]
tf = W / "msg.txt"; tf.write_text("Кодекс, упади", encoding="utf-8")
p = subprocess.run([sys.executable, str(RT / "chamber" / "secretary_turn.py"), "--project", str(W / "proj"), "--text-file", str(tf)],
                   capture_output=True, text=True, env=dict(env, CHOIR_SEC_AGENT="codex", CHOIR_SEC_BIN=str(STUB), STUB_FAIL="1"), timeout=90)
stf = json.loads(next((W / "journal" / "secretary").glob("state.*.json")).read_text())
t("упавший первый ход нового агента: передача не состоялась — в состоянии прежний агент и его нить целы",
  p.returncode != 0 and stf["agent"] == "claude" and stf.get("session_id") == sid_c)
p = turn("Клод, ты ещё тут?")
args_c = json.loads((W / "stub.log").read_text().splitlines()[-1])
t("после упавшей передачи прежний агент продолжает СВОЮ нить (--resume, без преамбулы)", p.returncode == 0 and "--resume" in args_c and sid_c in args_c and "ПЕРЕДАЧА РОЛИ" not in args_c[-1])
# старое состояние: agent = путь к бинарю → имя; не ложная передача
stf = next((W / "journal" / "secretary").glob("state.*.json")); d = json.loads(stf.read_text()); d["agent"] = "/usr/local/bin/claude"; stf.write_text(json.dumps(d))
p = turn("а если state старый?")
args_o = json.loads((W / "stub.log").read_text().splitlines()[-1])
t("старое состояние (agent = путь к claude): не передача роли — нить продолжается --resume", p.returncode == 0 and "--resume" in args_o and "ПЕРЕДАЧА РОЛИ" not in args_o[-1])

# строка для терминала: показ, не запуск — по агенту и нити состояния
import importlib
sys.path.insert(0, str(RT / "chamber"))
os.environ.update(CHOIR_JOURNAL=str(W / "journal"), ROUNDTABLE_JOURNAL=str(W / "journal"))
import secretary_turn as STm   # noqa: E402
importlib.reload(STm)
rc = STm.resume_command(str(W / "proj"))
check_ok = rc["agent"] == "claude" and rc["cmd"].startswith(f"cd {W / 'proj'} && claude --resume ") and rc["session"]
t("строка для терминала: cd в проект и продолжение нити текущего агента; окно её только показывает", check_ok)
t("строка для терминала: без флагов прав — обычный запуск (разрешения спросит у Автора)", "dangerously" not in rc["cmd"] and "bypass" not in rc["cmd"])
rc2 = STm.resume_command(str(W / "other-proj"))
t("строка для терминала: нити нет — новый разговор и пометка", rc2["cmd"].endswith("&& claude") and "ещё нет" in rc2["note"])
sp = str(W / "proj with space")
def put(agent, sid, model=""):
    STm.save_state(sp, {"agent": agent, "session_id": sid, "turns": 1, **({"model": model} if model else {})})
put("codex", "thread-1")
r = STm.resume_command(sp)
t("строка: codex resume <нить>, путь с пробелом в кавычках", r["cmd"] == f"cd '{sp}' && codex resume thread-1")
put("grok", "00000000-0000-4000-8000-000000000001")
t("строка: grok --resume <uuid>", STm.resume_command(sp)["cmd"].endswith("grok --resume 00000000-0000-4000-8000-000000000001"))
put("kimi", "cwd", "kimi-code/k3")
r = STm.resume_command(sp)
t("строка: kimi без каталога нити — новый разговор, модель линии, без «cwd» в поле нити",
  r["cmd"] == f"cd '{sp}' && kimi -m kimi-code/k3" and r["session"] == "" and "ещё нет" in r["note"])
key = STm.state_path(sp).stem.split(".", 1)[1]
(STm.STATE_DIR / f"kimi.{key}").mkdir(parents=True, exist_ok=True)
r = STm.resume_command(sp)
t("строка: kimi с каталогом нити — -c и та же модель линии", " && kimi -c -m kimi-code/k3 --add-dir " in r["cmd"] and r["session"] == "")
put("mystery", "x")
t("строка: неизвестный агент — пустая строка с пометкой, не dsh", STm.resume_command(sp)["cmd"] == "")

shutil.rmtree(W, ignore_errors=True)
print(f"\nsecretary_turn: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
