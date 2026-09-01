#!/usr/bin/env bash
# Сценарии обёртки исполнителя (СПЕКА-исполнитель-v1 п.3 + ревизия стола
# 2026-08-31): штатный успех · штатная ошибка CLI · вылет kill -9 ·
# сирота CLI · упавшая лента · молчащий git.
# Всё на изолированной комнате — живые журналы не трогаются.
set -euo pipefail
RT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d /tmp/extest.XXXXXX)"
trap 'chmod u+w "$W/room/live.jsonl" 2>/dev/null || true; rm -rf "$W"' EXIT
mkdir -p "$W/room" && cp "$RT/../Choir/"*.py "$W/room/" && : > "$W/room/live.jsonl"
# Дерево акта — ПРИСТЁГНУТЫЙ worktree, как в спеке: обёртка отличает
# его от главного checkout'а и в главный писать отказывается.
git init -q "$W/base"
git -C "$W/base" -c user.name=t -c user.email=t@t commit -q --allow-empty -m base
git -C "$W/base" worktree add -q -b act/one "$W/wt" >/dev/null
git -C "$W/base" worktree add -q -b act/two "$W/wt2" >/dev/null
export CHOIR_LEASE_DIR="$W/leases" ROUNDTABLE_CHOIR="$W/room"

# 1–2: успех и честная ошибка CLI
python3 "$RT/executor_run.py" --act e1 --epoch 1 --worktree "$W/wt" --voice codex \
  --cmd-json '["bash","-c","echo x > f.txt && git add -A && git -c user.name=c -c user.email=c@x commit -qm p"]' >/dev/null
python3 "$RT/executor_run.py" --act e2 --epoch 2 --worktree "$W/wt" --voice grok \
  --cmd-json '["bash","-c","exit 7"]' >/dev/null || true

# 3–4: ВЫЛЕТ обёртки. Ни close в ленте, ни СИРОТЫ-CLI после неё
#      (сирота продолжал бы писать в worktree, уехавший в карантин).
python3 "$RT/executor_run.py" --act e3 --epoch 3 --worktree "$W/wt" --voice kimi \
  --cmd-json '["sleep","57.31"]' >/dev/null 2>&1 & WPID=$!
# `|| true` обязателен: под set -e уже умершая обёртка (то есть
# НАЙДЕННЫЙ баг) роняла бы скрипт до блока проверок, и вместо FAIL
# получался пустой выход (нашёл субагент-ревьюер).
sleep 2; kill -9 "$WPID" 2>/dev/null || true; wait "$WPID" 2>/dev/null || true
sleep 1

# 5: лента недоступна — close обязан лечь МАРКЕРОМ на диск.
# Под root chmod не помеха записи, и сценарий стал бы no-op'ом,
# молча показывая PASS (заметил субагент-ревьюер).
[ "$(id -u)" = 0 ] && { echo "ПРОПУСК: под root chmod ленту не защитит"; exit 2; }
chmod a-w "$W/room/live.jsonl"
python3 "$RT/executor_run.py" --act e4 --epoch 4 --worktree "$W/wt" --voice deepseek \
  --cmd-json '["true"]' >/dev/null 2>&1 || true
chmod u+w "$W/room/live.jsonl"

# 6: git молчит (снесли .git прямо из-под CLI) — «неизвестно», а не «чисто»
python3 "$RT/executor_run.py" --act e5 --epoch 5 --worktree "$W/wt2" --voice claude \
  --cmd-json '["bash","-c","rm -rf .git"]' >/dev/null 2>&1 || true

# 7: --worktree не корень дерева (подкаталог) — отказ ДО запуска CLI
mkdir -p "$W/wt/sub"
set +e
python3 "$RT/executor_run.py" --act e6 --epoch 6 --worktree "$W/wt/sub" --voice codex \
  --cmd-json '["true"]' >/dev/null 2>&1; echo $? > "$W/rc_sub"
# 7б: ГЛАВНЫЙ checkout вместо пристёгнутого дерева — тоже отказ
python3 "$RT/executor_run.py" --act e9 --epoch 1 --worktree "$W/base" --voice kimi \
  --cmd-json '["true"]' >/dev/null 2>&1; echo $? > "$W/rc_main"
# 7в: повтор ТОЙ ЖЕ эпохи — отказ, улика прошлого хода цела
mkdir -p "$W/leases"; echo "УЛИКА" > "$W/leases/edit-e8.9.log"
python3 "$RT/executor_run.py" --act e8 --epoch 9 --worktree "$W/wt" --voice kimi \
  --cmd-json '["true"]' >/dev/null 2>&1; echo $? > "$W/rc_dup"
# 7г: кривой --act — честный отказ, а не трейсбек
python3 "$RT/executor_run.py" --act ../evil --epoch 1 --worktree "$W/wt" --voice kimi \
  --cmd-json '["true"]' >/dev/null 2>&1; echo $? > "$W/rc_act"
set -e

# 8: ВНУК. PDEATHSIG достаётся только прямому потомку — граница честная,
#    и тест её ЗАКРЕПЛЯЕТ, а не делает вид, что её нет: внук переживает
#    вылет, и снять его окно может только по pgid из meta.
python3 "$RT/executor_run.py" --act e7 --epoch 7 --worktree "$W/wt" --voice grok \
  --cmd-json '["bash","-c","sleep 61.17 & sleep 60"]' >/dev/null 2>&1 & GPID=$!
sleep 2; kill -9 "$GPID" 2>/dev/null || true; wait "$GPID" 2>/dev/null || true
sleep 1

RT="$RT" python3 - "$W" <<'PY'
import json, os, sys
w = sys.argv[1]
sys.path.insert(0, os.environ["RT"])
os.environ["CHOIR_LEASE_DIR"] = f"{w}/leases"
import leases
evs = [json.loads(l) for l in open(f"{w}/room/live.jsonl") if l.strip()]
cl = {e["act"]: e for e in evs if e.get("kind") == "edit_close"}
# Сироту ищем по ЗАПИСАННОМУ pid, а не по шаблону в cmdline: шаблон
# ловит заодно и сам тест, и это ровно тот класс лжи, ради которого мы
# всё это городим. Заодно проверяется write_meta — окну оттуда брать
# pgid для «отозвать».
meta = json.loads(leases.meta_path("e3").read_text())
grandchild = len([1 for ln in os.popen("ps -eo args=").read().splitlines()
                  if ln.strip() == "sleep 61.17"])
try:
    os.kill(meta["cli_pid"], 0)
    orphan = 1
except (ProcessLookupError, PermissionError):
    orphan = 0
marker = leases.LEASE_DIR / "e4.4.close.json"   # имя маркера несёт эпоху
checks = [
    ("штатный успех → close/done", cl.get("e1", {}).get("status") == "done"),
    ("ошибка CLI → close/error rc=7",
     cl.get("e2", {}).get("status") == "error" and cl["e2"].get("rc") == 7),
    ("вылет → close НЕТ, замок свободен",
     "e3" not in cl and not leases.is_held("e3")),
    ("meta хода хранит pid/pgid/starttime CLI (окну — для «отозвать»)",
     isinstance(meta.get("cli_pid"), int) and isinstance(meta.get("cli_pgid"), int)
     and meta["cli_pgid"] == meta["cli_pid"]        # своя сессия: pgid == pid
     and isinstance(meta.get("cli_starttime"), int)),
    ("после вылета не осталось сироты-CLI", orphan == 0),
    ("упавшая лента → close лёг маркером",
     marker.exists() and json.loads(marker.read_text())["status"] == "done"
     and "e4" not in cl),
    ("успешный close маркера не оставляет",
     not list(leases.LEASE_DIR.glob("e1.*.close.json"))),
    ("meta закрытого хода: cli_done ЕСТЬ и starttime НЕ потерян",
     json.loads(leases.meta_path("e1").read_text()).get("cli_done") is True
     and isinstance(json.loads(leases.meta_path("e1").read_text())
                    .get("cli_starttime"), int)),
    ("главный checkout вместо worktree → отказ",
     open(f"{w}/rc_main").read().strip() == "2"),
    ("повтор той же эпохи → отказ, улика прошлого хода цела",
     open(f"{w}/rc_dup").read().strip() == "2"
     and open(f"{w}/leases/edit-e8.9.log").read().strip() == "УЛИКА"),
    ("кривой --act → rc=2, а не трейсбек",
     open(f"{w}/rc_act").read().strip() == "2"),
    ("подкаталог вместо корня worktree → отказ ДО запуска CLI",
     open(f"{w}/rc_sub").read().strip() == "2"),
    ("ВНУК переживает вылет — граница названа, а не спрятана",
     grandchild == 1),
    ("молчащий git → head/dirty = неизвестно (null), а не «чисто»",
     cl.get("e5", {}).get("head") is None and cl.get("e5", {}).get("dirty") is None
     and "неизвестно" in cl.get("e5", {}).get("text", "")),
]
bad = [n for n, c in checks if not c]
for n, c in checks:
    print(("PASS " if c else "FAIL ") + n)
print(f"\nexecutor: PASS {len(checks)-len(bad)} · FAIL {len(bad)}")
os.system("pkill -x -f 'sleep 61.17' >/dev/null 2>&1")   # прибрать внука
sys.exit(1 if bad else 0)
PY
