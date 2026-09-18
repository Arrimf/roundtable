#!/usr/bin/env bash
# Клетка bwrap (jail.py): кресло пишет только в worktree и ветку акта,
# рецензент — никуда; факт клетки — в событиях. Пробники с НАСТОЯЩИМ
# bwrap: нет его на машине — тест честно печатает SKIP, не PASS.
set -euo pipefail
RT="$(cd "$(dirname "$0")/.." && pwd)"
# W — НЕ под /tmp: /tmp в клетке — tmpfs, и подменный дом под ним прятался
# бы раньше, чем --tmpfs $HOME (субагент: «проверка вакуумна»). /var/tmp
# виден через ro-корень — пустоту дома обеспечивает только сама клетка.
W="$(mktemp -d /var/tmp/jailtest.XXXXXX)"
STUB="$(mktemp -d /tmp/jailstub.XXXXXX)"
trap 'rm -rf "$W" "$STUB" "$RT"/.rt-jail-leak-*' EXIT
unset DEEPSEEK_API_KEY OPENAI_API_KEY GITHUB_TOKEN
mkdir -p "$W/room" "$W/home/.claude/hooks" "$W/home/.claude/projects/p1" \
  "$W/home/.dsh/profiles/headless" "$W/home/.dsh/profiles/node_modules" "$W/home/.grok"
: > "$W/home/.grok/hooks-paths"; cp "$RT/chamber/"*.py "$W/room/"; : > "$W/room/live.jsonl"
git init -q -b main "$W/proj"
( cd "$W/proj" && printf 'x = 1\n' > a.py && git add -A \
  && git -c user.name=arr -c user.email=a@a commit -qm base )
export CHOIR_LEASE_DIR="$W/leases" CHOIR_WT_DIR="$W/wts" ROUNDTABLE_CHOIR="$W/room"
# ДОМ — подменный: каталоги состояния CLI берутся из ~, и клетка не
# должна открывать на запись настоящие ~/.claude и ~/.kimi-code теста.
export HOME="$W/home" CHOIR_CANARY_HOME="$W/home"
unset CHOIR_RT_NO_BWRAP

RT="$RT" STUB="$STUB" python3 - "$W" <<'PY'
import json, os, subprocess, sys, time
from pathlib import Path
w = Path(sys.argv[1]); rt = os.environ["RT"]
sys.path.insert(0, rt); sys.path.insert(0, f"{rt}/chamber")
import edits, jail, merge_gate as mg

ok = bad = 0
def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond); bad += not cond

proj = w / "proj"

# ── 1. Форма без bwrap: выключатель, своя песочница, порядок bind'ов ──
os.environ["CHOIR_RT_NO_BWRAP"] = "1"
c, f = jail.wrap(["x"], "claude", rw=["/a"])
t("CHOIR_RT_NO_BWRAP=1 → команда как есть, факт none с причиной",
  c == ["x"] and f["jail"] == "none" and "NO_BWRAP" in f["jail_why"])
del os.environ["CHOIR_RT_NO_BWRAP"]
c, f = jail.wrap(["codex", "exec"], "codex", rw=["/a"])
t("codex — своя песочница: не вкладываем, факт own",
  c == ["codex", "exec"] and f == {"jail": "own"})
pref = jail.prefix("claude", rw=[str(w / "wt")], ro=[str(w / "proj/.git")],
                   cwd=str(w / "wt"))
t("prefix: ro-корень, dev, proc, tmpfs /tmp, die-with-parent",
  pref[:3] == ["bwrap", "--ro-bind", "/"] and "--tmpfs" in pref
  and pref[pref.index("--tmpfs") + 1] == "/tmp" and "--die-with-parent" in pref
  and "--unshare-net" not in pref)
i_ro = pref.index(str(w / "proj/.git")); i_rw = pref.index(str(w / "wt"))
t("ro идёт ПЕРЕД rw (иначе ro-родитель закрыл бы rw-корни)", i_ro < i_rw
  and pref[i_ro - 1] == "--ro-bind" and pref[i_rw - 1] == "--bind")
t("состояние CLI из ПОДМЕННОГО дома, только существующее",
  jail.state_dirs("claude") == [str(w / "home/.claude")]
  and str(w / "home/.claude") in pref)
i_st = pref.index(str(w / "home/.claude")); i_hk = pref.index(str(w / "home/.claude/hooks"))
t("хуки CLI — ro ПОВЕРХ rw-состояния (последними)", i_st < i_hk
  and pref[i_hk - 1] == "--ro-bind" and i_rw < i_hk)
t("отсутствующий каталог из STATE_RO_DIRS → ro-bind /var/empty на его место",
  (jail.EMPTY, str(w / "home/.claude/agents")) in jail.state_ro("claude")
  and (jail.EMPTY, str(w / "home/.claude/projects/p1/memory")) in jail.state_ro("claude")
  and "--ro-bind" in pref and pref[pref.index(str(w / "home/.claude/agents")) - 1] == jail.EMPTY)
# пробник под замком: медленная проба, два потока — оба ждут её итог,
# никто не получает «клетки нет» из недописанного кэша (codex)
import threading, time as _t
jail._AVAIL = None
_orig = jail._probe
jail._probe = lambda: (_t.sleep(0.3), True)[1]
got = []
th = [threading.Thread(target=lambda: got.append(jail.available())) for _ in range(2)]
[x.start() for x in th]; [x.join() for x in th]
jail._probe = _orig
t("available(): два потока сразу — оба видят итог пробы, не промежуточное False",
  got == [True, True])
jail._AVAIL = None
jail._probe = lambda: False
t("отрицательный итог кэшируется на время, не навсегда",
  jail.available() is False and jail._AVAIL_AT > 0 and jail._RETRY_S < 120)
jail._probe = _orig; jail._AVAIL = None
t("--chdir последним", pref[-2:] == ["--chdir", str(w / "wt")])
t("sha стабильна и зависит от открытых путей",
  jail.sha(pref) == jail.sha(list(pref)) and len(jail.sha(pref)) == 16
  and jail.sha(pref) != jail.sha(jail.prefix("claude", rw=[str(w / "other")])))
dup = jail.prefix("claude", rw=[str(w / "wt"), str(w / "wt")])
ro_dsh = jail.state_ro("dsh")
t("grok: hooks-paths — файл, и он в ro (в списке каталогов выпадал)",
  (str(w / "home/.grok/hooks-paths"),) * 2 in jail.state_ro("grok"))
junk = w / "xdg" / "choir" / "jail-empty"; junk.mkdir(parents=True); (junk / "evil").write_text("x")
os.environ["XDG_CACHE_HOME"] = str(w / "xdg")
t("свой «пустой» каталог с содержимым — не источник для ro-подмен", jail._cache_empty() == "")
del os.environ["XDG_CACHE_HOME"]
t("dsh: ro — общий profiles/node_modules, профили без подмен (glob целил мимо)",
  (str(w / "home/.dsh/profiles/node_modules"),) * 2 in ro_dsh
  and not any("headless" in d for _, d in ro_dsh))
t("повтор пути не дублирует bind", dup.count(str(w / "wt")) == 2)

if not jail.available():
    print("SKIP  bwrap недоступен — пробники с настоящей клеткой не прогнаны")
    print(f"\njail: PASS {ok} · FAIL {bad} · SKIP 1")
    sys.exit(1 if bad else 0)

# ── 2. Кресло в клетке: коммит проходит, запись наружу — нет ─────────
def run_exec(voice, cmd, env=None):
    ed = edits.open_edit(proj, "задание", voice)
    e = dict(os.environ, **(env or {}))
    p = subprocess.run([sys.executable, f"{rt}/executor_run.py",
                        "--act", ed["act"], "--epoch", str(ed["epoch"]),
                        "--worktree", str(ed["worktree"]), "--voice", voice,
                        "--cmd-json", json.dumps(cmd)],
                       capture_output=True, text=True, env=e, timeout=120)
    close = [json.loads(l) for l in (w / "room/live.jsonl").open(encoding="utf-8")
             if json.loads(l).get("kind") == "edit_close"
             and json.loads(l).get("act") == ed["act"]]
    return ed, p, (close[-1] if close else {})

outside = w / "outside.txt"; home_leak = w / "home/leak.txt"
state_mark = w / "home/.claude/mark.txt"
# Путь ВНЕ /tmp: всё под /tmp в клетке — tmpfs, и запись в каталоги-
# «леса» точек монтирования проходит (никуда не ложится). Ro-корень
# проверяется только путём на настоящем диске; ловушка снимается в trap.
real_leak = Path(rt) / f".rt-jail-leak-{os.getpid()}"
cmd = ["bash", "-c",
       f"printf 'x = 2\\n' > a.py && git add -A && "
       f"git -c user.name=v -c user.email=v@v commit -qm edit; echo commit_rc=$?; "
       f"touch {real_leak}; echo real_rc=$?; "
       f"touch {outside}; echo outside_rc=$?; "
       f"touch {home_leak}; echo home_rc=$?; "
       f"touch {proj}/leak.py; echo proj_rc=$?; "
       f"touch {proj}/.git/refs/heads/main2; echo mainref_rc=$?; "
       f"echo m > {state_mark}; echo state_rc=$?; "
       f"touch {w}/home/.claude/hooks/evil.sh; echo hooks_rc=$?; "
       f"mkdir -p {w}/home/.claude/agents && touch {w}/home/.claude/agents/evil.md; echo agents_rc=$?; "
       f"mkdir -p {w}/home/.claude/projects/p1/memory && echo x > {w}/home/.claude/projects/p1/memory/MEMORY.md; echo memory_rc=$?; "
       f"echo 'gitdir: /nowhere' > .git; echo gitfile_rc=$?; "
       f"echo t > /tmp/scratch-{os.getpid()}.txt; echo tmp_rc=$?; "
       f"cat {rt}/chamber/jail.py > /dev/null; echo readcode_rc=$?"]
ed, p, close = run_exec("claude", cmd)
log = ""
for lp in (w / "leases").glob("*.log"):
    log += lp.read_text(encoding="utf-8", errors="replace")
def rc(k):
    import re
    m = re.search(rf"{k}_rc=(\d+)", log); return int(m.group(1)) if m else None
t("кресло claude: close несёт jail=bwrap и jail_sha",
  close.get("jail") == "bwrap" and len(close.get("jail_sha") or "") == 16)
t("коммит в ветку акта прошёл (index, objects, refs/heads/act открыты)",
  rc("commit") == 0 and close.get("head") and
  subprocess.run(["git", "-C", str(proj), "rev-parse", f"refs/heads/act/{ed['act']}"],
                 capture_output=True, text=True).stdout.strip() == close.get("head"))
t("запись на настоящий диск вне worktree — Read-only file system",
  rc("real") != 0 and not real_leak.exists())
t("запись рядом с worktree (под /tmp — поглощена tmpfs, не отбита) на диск не легла",
  not outside.exists())
t("запись в подменный HOME (под /tmp — поглощена tmpfs) на диск не легла",
  not home_leak.exists())
t("главный checkout проекта нетронут", not (proj / "leak.py").exists())
t("refs/heads вне act/ — ro", rc("mainref") != 0
  and not (proj / ".git/refs/heads/main2").exists())
t("каталог состояния CLI — rw", rc("state") == 0 and state_mark.exists())
t("хуки CLI внутри состояния — Read-only file system",
  rc("hooks") != 0 and not (w / "home/.claude/hooks/evil.sh").exists())
t("память проекта без памяти нельзя подложить (projects/*/memory → пустой ro)",
  rc("memory") != 0 and not (w / "home/.claude/projects/p1/memory/MEMORY.md").exists())
t("gitfile .git worktree — ro поверх rw (подмена репозитория обёртке невозможна)",
  rc("gitfile") != 0 and (ed["worktree"] and
  Path(ed["worktree"], ".git").read_text().startswith("gitdir: ")))
t("отсутствующий каталог agents нельзя СОЗДАТЬ (на его месте пустой /var/empty ro)",
  rc("agents") != 0 and not (w / "home/.claude/agents/evil.md").exists()
  and (not (w / "home/.claude/agents").exists()
       or not any((w / "home/.claude/agents").iterdir())))
t("/tmp в клетке — свой tmpfs: запись прошла, снаружи файла нет",
  rc("tmp") == 0 and not Path(f"/tmp/scratch-{os.getpid()}.txt").exists())
t("код стола читается (ro-корень — видно всё)", rc("readcode") == 0)
if "packed-refs.lock" in log:
    print("      (git написал packed-refs.lock: Read-only file system — известная цена, коммит прошёл)")

# прерванный ход: обёртке SIGINT посреди CLI — close несёт факт клетки
edi = edits.open_edit(proj, "задание", "claude")
pi = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                       "--act", edi["act"], "--epoch", str(edi["epoch"]),
                       "--worktree", str(edi["worktree"]), "--voice", "claude",
                       "--cmd-json", json.dumps(["sleep", "30"])],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import signal, time as _tm
for _ in range(100):
    if any(e.get("kind") == "edit_close" for e in []): break
    _tm.sleep(0.1)
    if pi.poll() is not None: break
    if any(x.name.startswith(edi["act"]) for x in (w / "leases").glob("*.log")): 
        _tm.sleep(1.0); break
pi.send_signal(signal.SIGINT); pi.wait(timeout=30)
closes = [json.loads(l) for l in (w / "room/live.jsonl").open(encoding="utf-8")
          if json.loads(l).get("kind") == "edit_close" and json.loads(l).get("act") == edi["act"]]
t("прерванный ход (SIGINT обёртке): close error несёт jail=bwrap и jail_sha",
  closes and closes[-1].get("status") == "error" and closes[-1].get("jail") == "bwrap"
  and len(closes[-1].get("jail_sha") or "") == 16)

ed2, p2, close2 = run_exec("codex", ["bash", "-c", "true"])
t("кресло codex: jail=own (своя песочница)", close2.get("jail") == "own")

ed3, p3, close3 = run_exec("claude", ["bash", "-c", f"touch {outside}"],
                           env={"CHOIR_RT_NO_BWRAP": "1"})
c3 = mg.checks(ed3["act"])
t("без клетки: close jail=none с причиной, запись наружу прошла",
  close3.get("jail") == "none" and "NO_BWRAP" in (close3.get("jail_why") or "")
  and outside.exists())
t("гейт: jail=none — мягкая причина, не блок",
  c3.get("jail") == "none" and any("БЕЗ клетки" in r for r in c3.get("reasons_soft", []))
  and not any("клетк" in r for r in c3.get("reasons", [])))
c1 = mg.checks(ed["act"])
t("гейт: jail=bwrap в checks, мягких причин о клетке нет",
  c1.get("jail") == "bwrap" and not any("клетк" in r for r in c1.get("reasons_soft", [])))

# ── 3. Рецензент в клетке: пакет виден, проект и дом — ro ───────────
leak = proj / "review-leak.txt"
rv_leak = Path(rt) / f".rt-jail-leak-rv-{os.getpid()}"   # настоящий диск
def stub(pf):
    return ["bash", "-c",
            f"cat {pf} > /dev/null || exit 9; touch {leak} 2>/dev/null; "
            f"touch {rv_leak} 2>/dev/null; touch {home_leak} 2>/dev/null; "
            f"git -C {proj} show HEAD:a.py > /dev/null || exit 8; "
            f"printf 'ВЕРДИКТ: ОДОБРЯЮ\\nчитал пакет\\n'"]
# ревизия акта codex (ed2): исполнитель своё не судит, остальные — в клетке
subprocess.run(["git", "-C", str(proj), "update-ref", f"refs/heads/act/{ed2['act']}",
                close["head"]], check=True)          # диф для ревизии
# grok здесь — стаб без клетки (своя песочница у настоящего CLI, не у
# bash): ему писать нельзя, иначе ловушка сработает от него
grok_stub = lambda pf: ["bash", "-c", "printf 'ВЕРДИКТ: ОДОБРЯЮ\\n'"]
evs = mg.review(ed2["act"], reviewers={"claude": stub, "grok": grok_stub,
                                       "kimi": stub}, timeout=60)
by = {e["voice"]: e for e in evs}
t("claude-рецензент: в клетке (jail=bwrap), пакет из /tmp прочитан, вердикт есть",
  by["claude"].get("jail") == "bwrap" and by["claude"]["verdict"] == "approve")
t("kimi-рецензент: в клетке", by["kimi"].get("jail") == "bwrap")
t("grok-рецензент: своя песочница, jail=own", by["grok"].get("jail") == "own")
t("запись рецензента на настоящий диск — отбита; git проекта из клетки читается",
  not rv_leak.exists() and by["claude"]["status"] == "ok")
t("запись рецензента в проект и дом (под /tmp) на диск не легла",
  not leak.exists() and not home_leak.exists())
t("отказ канала не теряет факт клетки",
  mg.review(ed2["act"], reviewers={"claude": lambda pf: ["bash", "-c", "exit 4"]},
            timeout=60)[0].get("jail") == "bwrap")
os.environ["CHOIR_RT_NO_BWRAP"] = "1"
evn = mg.review(ed2["act"], reviewers={"kimi": stub}, timeout=60)
del os.environ["CHOIR_RT_NO_BWRAP"]
cn = mg.checks(ed2["act"])
t("рецензент без клетки: jail=none в событии, мягкая причина в гейте, не блок",
  evn[0].get("jail") == "none" and cn.get("jail_reviews_none") == ["kimi"]
  and any("ревизия БЕЗ клетки" in r for r in cn.get("reasons_soft", []))
  and not any("клетк" in r for r in cn.get("reasons", [])))
rv_leak.unlink(missing_ok=True)

# ── 4. Свой HOME на голос: комната и раунд, голос видит только своё ──
# Дом: чужие каталоги состояния, чужой файл, чужие каталоги в журнале,
# чужие ключи в окружении. Голос — codex (своя песочница ВНУТРИ клетки;
# ответ — через файл/fifo, как у настоящего).
# merge_gate выше вставил в sys.path копию chamber ($W/room, старая
# раскладка) — комната и дирижёр нужны НАСТОЯЩИЕ: у копии SANDBOX = /var/tmp,
# и ro-bind песочницы открыл бы весь каталог теста (так и упал первый прогон)
sys.path.insert(0, f"{rt}/chamber")
import live, choir
VD = live.VOICEDIR                     # журнал комнаты здесь — $W/room (старая раскладка)
choir.JOURNAL = live.JOURNAL; choir.ROOM = live.JOURNAL / "room.jsonl"
for d in ("home/.kimi-code/sessions", "home/.codex/sessions", "home/.gemini",
          "home/.cache/choir-voices/claude"):
    (w / d).mkdir(parents=True, exist_ok=True)
for d in (VD / "claude", VD / "kimi-alt", VD / "codex"):
    d.mkdir(parents=True, exist_ok=True)
(w / "home/.kimi-code/sessions/s1.jsonl").write_text("чужой ответ кими")
(w / "home/.codex/sessions/mine.jsonl").write_text("своё")
(w / "home/.gemini/keys.txt").write_text("KEY")
(w / "home/.claude.json").write_text("{}")
(w / "home/.claude.json.bak-1").write_text("{oauth}")
os.environ["GITHUB_TOKEN"] = "gh-x"; os.environ["XDG_RUNTIME_DIR"] = str(w / "run"); (w / "run").mkdir()
(w / "run/bus").write_text("sock")
(VD / "claude/state.json").write_text("{}")
(VD / "kimi-alt/state.json").write_text("{}")
(w / "home/.cache/choir/neighbour_blind_claude_ab12").mkdir(parents=True)
(w / "home/.cache/choir/neighbour_blind_claude_ab12/prompt").write_text("чужой промпт")
hid = jail.hidden("codex", extra=[str(VD / "claude")])
t("hidden(): extra — tmpfs; чужое В ДОМЕ не перечисляется (дом — tmpfs, его там нет); своё не скрыто",
  ("--tmpfs", "", str(VD / "claude")) in hid
  and not any(".kimi-code" in dst or ".claude.json" in dst for _, _, dst in hid)
  and not any(dst == str(w / "home/.codex") for _, _, dst in hid))
pfx = jail.prefix("codex")
t("prefix: дом — tmpfs, поверх ro только ~/.local/bin|lib и своё состояние",
  pfx[pfx.index(str(w / "home")) - 1] == "--tmpfs"
  and str(w / "home/.codex") in pfx and str(w / "home/.kimi-code") not in pfx
  and str(w / "home/.claude.json.bak-1") not in pfx)
t("STATE claude: только ~/.claude.json, резервные копии не bind'ятся (гонка tmp+rename)",
  str(w / "home/.claude.json") in jail.state_dirs("claude")
  and str(w / "home/.claude.json.bak-1") not in jail.state_dirs("claude"))
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/x"; os.environ["ANTHROPIC_AUTH_TOKEN"] = "t"
t("unset_env(): свои по префиксу не снимаются (GOOGLE_APPLICATION_CREDENTIALS у gemini, ANTHROPIC_AUTH_TOKEN у claude)",
  "GOOGLE_APPLICATION_CREDENTIALS" not in jail.unset_env("gemini") and "GOOGLE_APPLICATION_CREDENTIALS" in jail.unset_env("codex")
  and "ANTHROPIC_AUTH_TOKEN" not in jail.unset_env("claude") and "ANTHROPIC_AUTH_TOKEN" in jail.unset_env("kimi"))
del os.environ["GOOGLE_APPLICATION_CREDENTIALS"], os.environ["ANTHROPIC_AUTH_TOKEN"]
t("unset_env(): ничей ключ (GITHUB_TOKEN) и адрес D-Bus снимаются всем",
  "GITHUB_TOKEN" in jail.unset_env("codex") and "GITHUB_TOKEN" in jail.unset_env("claude"))
t("hidden(): dsh и deepseek делят дом — своё не закрывается совпадением с «чужим»",
  not any(".dsh" in dst for _, _, dst in jail.hidden("deepseek")))
os.environ["DEEPSEEK_API_KEY"] = "ds-secret"; os.environ["OPENAI_API_KEY"] = "own-key"
t("unset_env(): чужие ключи снимаются, свой остаётся",
  "DEEPSEEK_API_KEY" in jail.unset_env("codex") and "OPENAI_API_KEY" not in jail.unset_env("codex")
  and "DEEPSEEK_API_KEY" not in jail.unset_env("deepseek"))
t("base_voice(): линия → голос", jail.base_voice("kimi-alt") == "kimi" and jail.base_voice("codex") == "codex")
stub = Path(os.environ["STUB"]); os.environ["PATH"] = f"{stub}:" + os.environ["PATH"]
t("PATH под /tmp — ro в клетке (стабы тестов)", str(stub) in jail.path_tmp_dirs()
  and str(stub) in jail.prefix("codex"))
for d in ("journal/voices/codex", "home/.cache/choir-voices/claude", "proj/journal/voices"):
    (w / d).mkdir(parents=True, exist_ok=True)
pv = jail.prefix("codex", ro=[str(w / "journal")], hide=[str(w / "journal/voices")],
                 rw=[str(w / "journal/voices/codex")], cwd=str(w / "journal/voices/codex"))
try:
    jail.prefix("codex", ro=[str(w / "home")]); refused = False
except RuntimeError:
    refused = True
try:
    jail.prefix("codex", cwd=str(w)); refused2 = False
except RuntimeError:
    refused2 = True
t("prefix: дом или его предок в ro/cwd — отказ, не тихое открытие чужого", refused and refused2)
try:
    jail.prefix("codex", ro=[str(w / "home/.codex/..")]); refused3 = False
except RuntimeError:
    refused3 = True
(w / "lnk").symlink_to(w / "home")
try:
    jail.prefix("codex", ro=[str(w / "lnk")]); refused4 = False
except RuntimeError:
    refused4 = True
try:
    jail.prefix("codex", ro=[str(w / "journal/voices")], hide=[str(w / "journal/voices")]); refused5 = False
except RuntimeError:
    refused5 = True
t("prefix: «/дом/x/..», симлинк на дом и ro ровно на скрытом корне — отказ",
  refused3 and refused4 and refused5)
(w / "home/.cache/choir/dsh-patches").mkdir(parents=True, exist_ok=True)
_patch = w / "home/.cache/choir/dsh-patches/model-x.yaml"; _patch.write_text("m")
t("argv_paths(): существующий путь из аргументов (--patch dsh, --flag=/путь) открывается ro",
  str(_patch) in jail.argv_paths(["dsh", "--patch", str(_patch), f"--x={stub}", "текст /nonexistent"], "dsh")
  and str(stub) in jail.argv_paths([f"--x={stub}"], "dsh")
  and str(_patch) in jail.wrap(["dsh", "--patch", str(_patch)], "dsh")[0])
(w / "home/.kimi-code").mkdir(exist_ok=True)
(w / "home/.cache/choir/other_blind_kimi_ab12").mkdir(parents=True, exist_ok=True)
(w / "home/.cache/choir/mine_blind_codex_ab12").mkdir(parents=True, exist_ok=True)
_wr = jail.wrap(["codex", "-p", str(w / "home/.cache/choir/other_blind_kimi_ab12")], "codex",
                hide=[str(w / "home/.cache/choir")], ro_after=[str(w / "home/.cache/choir/mine_blind_codex_ab12")], nest_own=True)[0]
_binds = _wr[:_wr.index("--")]          # только аргументы клетки, не сама команда
t("wrap(): путь из argv под скрытым карантином — только свой каталог вызова, чужой не переоткрывается",
  str(w / "home/.cache/choir/other_blind_kimi_ab12") not in _binds
  and str(w / "home/.cache/choir/mine_blind_codex_ab12") in _binds)
_h0 = (w / "home/.codex").exists(); jail._AVAIL = None; jail.available()
t("_probe(): пробник не создаёт каталоги состояния (голос «probe» вне STATE)",
  (w / "home/.codex").exists() == _h0 and not (w / "home/.probe").exists())
t("argv_paths(): путь под домом вне кэша стола и своего состояния (чужое) НЕ открывается",
  str(w / "home/.kimi-code") not in jail.argv_paths(["-p", str(w / "home/.kimi-code")], "codex")
  and str(w / "home/.codex") in jail.argv_paths(["-p", str(w / "home/.codex")], "codex"))
(w / "home/.gemini").rename(w / "home/.gemini.off")     # «свежая машина» для gemini
jail.hidden("codex"); jail.prefix("codex")
_still = (w / "home/.gemini").exists()
jail.prefix("gemini")
_made = (w / "home/.gemini").is_dir()
(w / "home/.gemini").rmdir(); (w / "home/.gemini.off").rename(w / "home/.gemini")
t("prefix(): главный каталог СВОЕГО голоса создаётся, если его нет (первый запуск не в tmpfs); prefix/hidden чужих не плодят",
  not _still and _made)
t("cwd под своим rw — не дублируется bind'ом (bind src+dst и --chdir)",
  pv.count(str(w / "journal/voices/codex")) == 3)
pc = jail.prefix("claude", hide=[str(w / "home/.cache/choir-voices")], cwd=str(w / "home/.cache/choir-voices/claude"))
_ic = pc.index(str(w / "home/.cache/choir-voices/claude"))
t("cwd под скрытым родителем — ro ПОСЛЕ сокрытия", _ic > pc.index(str(w / "home/.cache/choir-voices"))
  and pc[_ic - 1] == "--ro-bind" and pc[_ic + 1] == str(w / "home/.cache/choir-voices/claude"))
pr = jail.prefix("codex", hide=[str(w / "proj/journal/voices")], cwd=str(w / "proj/journal"))
t("cwd-предок скрытого, не открытый ro — в ro ДО сокрытий (не открывает соседей заново)",
  pr.index(str(w / "proj/journal")) < pr.index(str(w / "proj/journal/voices")))
i_j = pv.index(str(w / "journal")); i_h = pv.index(str(w / "journal/voices")); i_o = pv.index(str(w / "journal/voices/codex"))
t("порядок: ro журнала → tmpfs voices/ → rw свой каталог поверх; --unshare-pid есть",
  i_j < i_h < i_o and pv[i_h - 1] == "--tmpfs" and "--unshare-pid" in pv)
t("--unshare-ipc и tmpfs поверх XDG_RUNTIME_DIR", "--unshare-ipc" in pv
  and pv[pv.index(str(w / "run")) - 1] == "--tmpfs")
os.environ["CHOIR_RT_JAIL_REQUIRED"] = "1"; jail._AVAIL = False; jail._AVAIL_AT = time.monotonic()
try:
    jail.wrap(["x"], "claude"); strict = False
except RuntimeError:
    strict = True
del os.environ["CHOIR_RT_JAIL_REQUIRED"]; jail._AVAIL = None
t("CHOIR_RT_JAIL_REQUIRED=1: без bwrap ход не выдаётся (RuntimeError)", strict)
probe = (f"{{ printf 'kimi=%s ' $(ls -A {w}/home/.kimi-code | wc -l); "
         f"printf 'codex=%s ' $(ls -A {w}/home/.codex/sessions | wc -l); "
         f"printf 'cj=%s ' $(cat {w}/home/.claude.json 2>/dev/null | wc -c); "
         f"printf 'vc=%s ' $(ls -A {VD}/claude 2>/dev/null | wc -l); "
         f"printf 'vk=%s ' $(ls -A {VD}/kimi-alt 2>/dev/null | wc -l); "
         f"printf 'ds=%s oa=%s ' ${{DEEPSEEK_API_KEY:-unset}} ${{OPENAI_API_KEY:-unset}}; "
         f"printf 'procs=%s ' $(ls /proc | grep -c '^[0-9]'); "
         f"sleep 1.2; printf 'late=%s ' $(ls -A {VD} 2>/dev/null | grep -vc '^codex$'); "
         f"printf 'quar=%s ' $(ls -A {w}/home/.cache/choir 2>/dev/null | wc -l); "
         f"printf 'run=%s gh=%s ' $(ls -A {w}/run | wc -l) ${{GITHUB_TOKEN:-unset}}; "
         f"touch {w}/home/.codex/sessions/new && printf 'own_rw=ok ' || printf 'own_rw=no '; "
         f"touch {rt}/.rt-jail-leak-room-{os.getpid()} 2>/dev/null && printf 'leak=yes' || printf 'leak=no'; }}")
live.VOICES["codex"]["start"] = lambda p, f, a, s, d: ["bash", "-c", probe + f" > {a}"]
live.available = lambda n: True
import threading
def _late():                            # сосед появляется при ЖИВОЙ клетке
    time.sleep(0.5); (VD / "grok").mkdir(exist_ok=True)
    (VD / "grok/answer.txt").write_text("поздний чужой")
import time
threading.Thread(target=_late, daemon=True).start()
evl = live.deliver(["codex"])          # настоящий путь: turn → post в ленту
ev = evl[0] if evl else {}
txt = ev.get("text", "")
print("    комната, пробник:", txt or ev)
t("комната: ход codex — jail=bwrap+own (своя песочница внутри клетки), jail_sha есть",
  ev.get("jail") == "bwrap+own" and len(ev.get("jail_sha") or "") == 16)
t("комната: чужое состояние CLI пусто, своё видно и rw",
  "kimi=0" in txt and "codex=1" in txt and "own_rw=ok" in txt)
t("комната: чужого файла (~/.claude.json) в доме нет", "cj=0 " in txt)
t("комната: чужие каталоги журнала (voices/claude, voices/kimi-alt) пусты",
  "vc=0" in txt and "vk=0" in txt)
t("комната: чужой ключ снят из окружения, свой остался", "ds=unset" in txt and "oa=own-key" in txt)
t("комната: свой pid-namespace — /proc не показывает соседей", any(f"procs={n} " in txt for n in range(1, 12)))
t("комната: сосед, появившийся при живой клетке, не виден (родитель voices/ скрыт целиком)", "late=0" in txt)
t("комната: карантин раундов скрыт", "quar=0 " in txt)
t("комната: XDG_RUNTIME_DIR пуст, ничей ключ снят", "run=0 " in txt and "gh=unset" in txt)
t("комната: настоящий диск вне своего — ro", "leak=no" in txt
  and not Path(rt, f".rt-jail-leak-room-{os.getpid()}").exists())
evs = [json.loads(l) for l in live.LIVE.open(encoding="utf-8")]
t("комната: событие в ленте несёт jail", any(e.get("jail") == "bwrap+own" for e in evs))
choir.VOICES["codex"]["cmd"] = lambda p, f, a: ["bash", "-c", probe + f" > {a}"]
rec = choir.ask_one("codex", "з", "r-jail", "blind", None, "blind")
rt_txt = rec.get("text", "")
print("    раунд, пробник:", rt_txt or {k: rec.get(k) for k in ("status", "detail", "jail")})
t("раунд: запись голоса несёт jail=bwrap+own и jail_sha; ответ через fifo на ro-bind дошёл",
  rec.get("jail") == "bwrap+own" and len(rec.get("jail_sha") or "") == 16 and rec.get("status") == "ok")
t("раунд: чужое пусто, своё rw, ключ соседа снят",
  "kimi=0" in rt_txt and "own_rw=ok" in rt_txt and "ds=unset" in rt_txt and "leak=no" in rt_txt)
t("раунд: карантин скрыт целиком — виден только каталог своего вызова (чужой промпт нет)",
  "quar=1 " in rt_txt)
t("раунд: каталог вызова убран после хода", not any(p.name.startswith("r-jail_") for p in (w / "home/.cache/choir").iterdir()))
t("mark(): строка стенограммы называет клетку и число скрытых путей",
  "bwrap+own" in jail.mark({"jail": "bwrap+own", "jail_sha": "x"}, ["a", "b"]) and "2 +" in jail.mark({"jail": "bwrap+own", "jail_sha": "x"}, ["a", "b"]))

print(f"\njail: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
PY
