#!/usr/bin/env python3
"""Резервные снимки (chamber/backup.py): снимок с манифестом, latest,
жёсткие ссылки на неизменившиеся файлы, исключения, обрезка старших,
права 0700, журнал и стенограммы из переменных стенда."""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="rt-backup."))
os.environ.update(CHOIR_JOURNAL=str(T / "journal"), ROUNDTABLE_JOURNAL=str(T / "journal"),
                  CHOIR_RT_ACTS=str(T / "acts"), CHOIR_RT_ACTS_ARCHIVE=str(T / "acts-archive"),
                  CHOIR_CACHE=str(T / "cache"), CHOIR_BACKUP_DIR=str(T / "backup"), CHOIR_BACKUP_KEEP="3")
RT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RT / "chamber"))
import backup   # noqa: E402
import shutil as _which
HAS_RSYNC = bool(_which.which("rsync"))      # без rsync жёстких ссылок на каталоги нет — те проверки пропускаются

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"PASS  {name}")
    else:
        FAIL += 1; print(f"FAIL  {name}")


for d in ("journal/secretary", "acts", "acts-archive", "cache"):
    (T / d).mkdir(parents=True, exist_ok=True)
(T / "journal" / "live.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
(T / "journal" / "secretary" / "state.abc.json").write_text("{}", encoding="utf-8")
(T / "journal" / "secretary" / "state.abc.lock").write_text("", encoding="utf-8")
(T / "journal" / "secretary" / "grok.abc").mkdir()
(T / "journal" / "secretary" / "grok.abc" / "prompt.txt").write_text("промпт", encoding="utf-8")
(T / "acts" / "a1.raw.log").write_text("стенограмма\n", encoding="utf-8")
(T / "acts-archive" / "old.log").write_text("архив\n", encoding="utf-8")
(T / "cache" / "reviews.jsonl").write_text('{"r": 1}\n', encoding="utf-8")

r1 = backup.run()
s1 = Path(r1["snapshot"])
STORE = backup.store()
check(not r1["errors"] and sorted(r1["copied"]) == ["journal", "reviews.jsonl", "rt-acts", "rt-acts-archive"], f"первый снимок: журнал, стенограммы, архив, reviews.jsonl; пропущено {r1['skipped']}")
check(s1.parent == STORE and STORE.parent == T / "backup" and STORE.name.startswith("rt-backup."[:0] + T.name + "-"), f"снимок — в подкаталоге хранилища этого журнала ({STORE.name})")
check((s1 / "journal" / "live.jsonl").read_text() == '{"id": 1}\n' and (s1 / "rt-acts" / "a1.raw.log").exists() and (s1 / "rt-acts-archive" / "old.log").exists(),
      "содержимое скопировано")
check(not (s1 / "journal" / "secretary" / "state.abc.lock").exists() and not (s1 / "journal" / "secretary" / "grok.abc" / "prompt.txt").exists()
      and (s1 / "journal" / "secretary" / "state.abc.json").exists(), "исключения: замки и промпты ходов не копируются, состояние — да")
check(oct((T / "backup").stat().st_mode & 0o777) == "0o700" and oct(STORE.stat().st_mode & 0o777) == "0o700" and oct(s1.stat().st_mode & 0o777) == "0o700", "каталоги хранилища и снимка — 0700")
check((STORE / "latest").is_symlink() and (STORE / "latest").resolve() == s1.resolve(), "latest → снимок")
m = json.loads((s1 / "MANIFEST.json").read_text())
check(m["copied"] == r1["copied"] and "journal" in m["sources"] and m["journal"] == str((T / "journal").resolve()), "манифест снимка: источники, журнал")
# каденс хранилища: min_age — снимок не снимается, пока последний целый моложе
r_skip = backup.run(min_age=3600)
check(r_skip["snapshot"] is None and "моложе" in r_skip["skipped_reason"] and len(backup.snapshots()) == 1, "run(min_age): последний снимок моложе — пропуск без нового каталога")
# замок хранилища: пока держит другой процесс — пропуск
import fcntl
lf = open(STORE / ".lock", "w"); fcntl.flock(lf, fcntl.LOCK_EX)
r_lock = backup.run()
fcntl.flock(lf, fcntl.LOCK_UN); lf.close()
check(r_lock["snapshot"] is None and "другой процесс" in r_lock["skipped_reason"], "run() при занятом замке — пропуск")
check("rt-voices.json: нет" in " ".join(r1["skipped"]), "отсутствующий источник назван пропущенным, не ошибкой")

time.sleep(1.1)
(T / "journal" / "live.jsonl").write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
r2 = backup.run(); s2 = Path(r2["snapshot"])
check(r2["link_dest"] == str(s1) and (s2 / "journal" / "live.jsonl").read_text().count("\n") == 2, "второй снимок: --link-dest на первый, изменённый файл — новый")
check((not HAS_RSYNC) or (s2 / "rt-acts" / "a1.raw.log").stat().st_ino == (s1 / "rt-acts" / "a1.raw.log").stat().st_ino, "неизменившаяся стенограмма — жёсткая ссылка (один inode)" + ("" if HAS_RSYNC else " [пропуск: нет rsync]"))
check((s2 / "reviews.jsonl").stat().st_ino == (s1 / "reviews.jsonl").stat().st_ino, "неизменившийся одиночный файл — жёсткая ссылка")
check((STORE / "latest").resolve() == s2.resolve(), "latest переставлен")

# полуснимок (без манифеста, старый) убирается; свежий — нет; в link-dest не идёт
half = STORE / "2020-01-01T00-00-00Z"; half.mkdir(); os.utime(half, (time.time() - 7200, time.time() - 7200))
fresh_half = STORE / "2099-01-01T00-00-00Z"; fresh_half.mkdir()
time.sleep(1.1); r3 = backup.run()
check(half.name in r3["pruned"] and not half.exists() and fresh_half.exists() and r3["link_dest"] == str(s2), "prune: старый полуснимок убран, свежий оставлен; link-dest — последний ЦЕЛЫЙ")
import shutil as _sh; _sh.rmtree(fresh_half)
time.sleep(1.1); r4 = backup.run()
snaps = backup.snapshots()
check(len(snaps) == 3 and not s1.exists() and r4["pruned"] == [s1.name], "KEEP=3: четвёртый снимок убрал первый")
check((STORE / "latest").resolve() == Path(r4["snapshot"]).resolve(), "latest после обрезки — свежий")
# снимок с ошибкой не становится latest и не идёт в link-dest
os.chmod(T / "acts", 0o000)                              # источник не читается → ошибка rsync
time.sleep(1.1); r_err = backup.run()
os.chmod(T / "acts", 0o755)
check(bool(r_err["errors"]) and (STORE / "latest").resolve() == Path(r4["snapshot"]).resolve(), f"снимок с ошибкой: latest не переставлен ({r_err['errors'][:1]})")
time.sleep(1.1); r5 = backup.run()
check(not r5["errors"] and r5["link_dest"] == r4["snapshot"], "следующий целый снимок берёт link-dest с последнего целого, не с битого")
# KEEP=0 — хранить все
backup.KEEP = 0; time.sleep(1.1); r6 = backup.run()
check(r6["pruned"] == [] and len(backup.snapshots()) >= 4, "KEEP=0: ничего не убирается")
backup.KEEP = 3

# CLI
import subprocess
p = subprocess.run([sys.executable, str(RT / "chamber" / "backup.py"), "--list"], capture_output=True, text=True, env=dict(os.environ))
check(p.returncode == 0 and "держим 3" in p.stdout and STORE.name in p.stdout, "CLI --list")

shutil.rmtree(T, ignore_errors=True)
print(f"\nbackup: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
