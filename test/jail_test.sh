#!/usr/bin/env bash
# Клетка bwrap (jail.py): кресло пишет только в worktree и ветку акта,
# рецензент — никуда; факт клетки — в событиях. Пробники с НАСТОЯЩИМ
# bwrap: нет его на машине — тест честно печатает SKIP, не PASS.
set -euo pipefail
RT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d /tmp/jailtest.XXXXXX)"
trap 'rm -rf "$W" "$RT"/.rt-jail-leak-*' EXIT
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

RT="$RT" python3 - "$W" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
w = Path(sys.argv[1]); rt = os.environ["RT"]
sys.path.insert(0, rt)
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
       f"cat {rt}/jail.py > /dev/null; echo readcode_rc=$?"]
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

print(f"\njail: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
PY
