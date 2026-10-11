#!/usr/bin/env python3
"""Список актов, ждущих приёмки (/state.pending_acts), и итог изменений в
событии закрытия кресла (executor_run._change_summary). Окно импортируется
на копии каталога, как в access_guard_test. Запуск: python3 test/pending_acts_test.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parents[1]
T = Path(tempfile.mkdtemp(prefix="pending-"))
shutil.copytree(RT / "chamber", T / "chamber")
(T / "journal").mkdir()
for n in ("live.jsonl", "room.jsonl"):
    (T / "journal" / n).write_text("", encoding="utf-8")
for d in ("acts", "leases"):
    (T / d).mkdir()
os.environ.update(ROUNDTABLE_CHAMBER=str(T / "chamber"), ROUNDTABLE_JOURNAL=str(T / "journal"),
                  CHOIR_JOURNAL=str(T / "journal"), CHOIR_ROOM=str(T / "journal" / "room.jsonl"),
                  CHOIR_RT_ACTS=str(T / "acts"), CHOIR_RT_ACTS_ARCHIVE=str(T / "acts-archive"),
                  CHOIR_LEASE_DIR=str(T / "leases"), CHOIR_WT_DIR=str(T / "wts"), CHOIR_RT_NO_DISCOVERY="1",
                  CHOIR_RT_MODELS=str(T / "models.json"), CHOIR_RT_VOICES=str(T / "voices.json"),
                  CHOIR_CANARY_HOME=str(T / "chome"), CHOIR_RT_NO_BWRAP="1")
sys.path.insert(0, str(RT)); sys.path.insert(0, str(T / "chamber"))
import roundtable as rt                                       # noqa: E402
import executor_run as xr                                     # noqa: E402

N_OK = 0


def check(name, cond):
    global N_OK
    if not cond:
        print("ПРОВАЛ:", name)
        sys.exit(1)
    N_OK += 1


summary = [
    {"act": "aaaa", "voice": "codex", "ts": "2026-09-23T11:00:00+00:00", "task": "макет", "stage": "closed",
     "approvals": ["claude", "grok"], "refused": [], "project": "/p"},
    {"act": "bbbb", "voice": "kimi", "ts": "2026-09-24T11:00:00+00:00", "task": "макет", "stage": "closed",
     "empty": True, "project": "/p"},
    {"act": "mmmm", "voice": "grok", "stage": "merged"},
]
rows = rt._pending_rows(["bbbb", "aaaa", "zzzz"], summary)
check("порядок — как в pending", [r["act"] for r in rows] == ["bbbb", "aaaa", "zzzz"])
check("счёт ревизии переносится", rows[1]["approvals"] == ["claude", "grok"] and rows[1]["refused"] == [])
check("пустой акт: признак empty переносится, списки ревизий пустые", rows[0]["empty"] is True and rows[0]["approvals"] == [])
check("у акта с правками empty не истина", rows[1].get("empty") is None)
check("акт вне окна сводки — строка с пустыми полями, не пропуск", rows[2]["voice"] is None and rows[2]["act"] == "zzzz")
check("принятый акт в список не попадает (его нет в pending)", all(r["act"] != "mmmm" for r in rows))
check("пусто → пусто", rt._pending_rows([], summary) == [])

# _change_summary на временном репозитории
repo = Path(tempfile.mkdtemp(prefix="chg-"))
def git(*a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=False)
git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
(repo / "a.txt").write_text("1\n"); git("add", "."); git("commit", "-qm", "base")
head0 = git("rev-parse", "HEAD").stdout.strip()
check("без головы при старте — итог не считается", xr._change_summary(repo) == "")
check("голова не сдвинулась — сказано прямо", "не изменилась" in xr._change_summary(repo, head0))
git("checkout", "-qb", "act/x"); (repo / "a.txt").write_text("1\n2\n3\n"); (repo / "b.txt").write_text("x\n")
git("add", "."); git("commit", "-qm", "kimi: правка двух файлов")
s = xr._change_summary(repo, head0)
check("итог за ход: файлы по-русски, +/−, заголовок коммита",
      "2 файла" in s and "+3" in s and "«kimi: правка двух файлов»" in s)
head1 = git("rev-parse", "HEAD").stdout.strip()
(repo / "c.txt").write_text("y\n"); git("add", "."); git("commit", "-qm", "второй ход")
s2 = xr._change_summary(repo, head1)
check("итог продолжения — только этот ход, не вся ветка", "1 файл" in s2 and "«второй ход»" in s2 and "2 файла" not in s2)
check("не-репозиторий — пусто, без исключения", xr._change_summary(Path(tempfile.mkdtemp()), "abc") == "")
check("склонение файлов", xr._ru_stat("5 files changed, 1 insertion(+)") == "5 файлов, +1" and xr._ru_stat("21 files changed") == "21 файл")
print(f"{N_OK} проверок ок")
