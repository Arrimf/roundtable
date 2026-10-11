#!/usr/bin/env python3
"""Резервные снимки журналов и стенограмм стола (наказ Автора 2026-10-08:
«унифицированное резервное хранилище копии»).

Повод: прежняя уборка окна по сроку (14 суток) стёрла стенограммы 94 из
96 актов окна RoundTable и актов другого окна — вкладка «дирижёр»
показывает пустоту. Лента и комната версионируются git'ом, стенограммы
актов (~/.cache/choir/rt-acts) — нет: единственная копия жила в кэше.

Что копируется (ОДНО хранилище для всех окон и проектов — журнал у стола
один): каталог журнала (live.jsonl, room.jsonl, debts.jsonl, secretary/,
rounds/, reviews/, notes/, voices/ — состояние нитей без ключей),
стенограммы актов (rt-acts, rt-acts-archive), отметки ревизий
(reviews.jsonl), настройки окна (rt-voices.json, rt-models.json).
Что НЕ копируется никогда: ключи и состояние CLI (~/.config/*, ~/.claude,
~/.codex …), реестр окон с токенами кресла (rt-windows), пропуска Jev
(jev-pass), замки (gates, leases), worktree'ы кресел, карантин.

Как: rsync -a в каталог снимка <DEST>/<UTC-время>/ с --link-dest на
предыдущий снимок — неизменившиеся файлы делят inode (жёсткие ссылки),
снимок стоит столько, сколько изменилось; после — символическая ссылка
latest. Хранится KEEP снимков (умолчание 40), старшие удаляются целиком.
Куда: CHOIR_BACKUP_DIR или ~/.local/share/choir-backup, права 0700.

Безопасность (ответ на вопрос Автора): копия не создаёт нового риска
сверх оригиналов — в ней то, что и так лежит на диске под тем же
пользователем; ключей и токенов в ней нет по списку исключений; каталог
закрыт от клетки голосов (access.hidden_roots) и от других пользователей
(0700). Стенограммы могут содержать вывод инструментов голосов — это их
свойство, не копии; чистка от строк-ключей идёт на выдаче окна
(_scrub_log), снимок хранит сырьё. Снимок на том же диске не спасает от
смерти диска — для этого есть timeshift Автора; он спасает от уборок,
кривых правок кода и `git stash`.

Зовётся окном при старте и далее каждый час с min_age=BACKUP_EVERY (6 ч):
каденс — свойство хранилища, не окна, три окна не делают три снимка;
замок flock на хранилище. Руками — `python3 chamber/backup.py [--list |
--prune]` (без min_age — снимок всегда). Снимки разных журналов
(боевой, стенды) лежат в разных подкаталогах DEST (store()).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live    # noqa: E402

DEST = Path(os.environ.get("CHOIR_BACKUP_DIR") or Path.home() / ".local" / "share" / "choir-backup")
KEEP = int(os.environ.get("CHOIR_BACKUP_KEEP") or 40)      # 0 — хранить все снимки
BACKUP_EVERY = int(os.environ.get("CHOIR_BACKUP_EVERY") or 6 * 3600)
CACHE = Path(os.environ.get("CHOIR_CACHE") or Path.home() / ".cache" / "choir")
INCOMPLETE_TTL = 3600       # полуснимок старше часа убирает prune

# исключения ВНУТРИ копируемых каталогов (rsync --exclude, относительно корня источника)
EXCLUDE = ("prompt.txt", "answer.txt", "*.lock", "*.tmp", "__pycache__")


def sources() -> list[tuple[str, Path]]:
    """(имя в снимке, путь) — что копировать. Порядок значим только для отчёта."""
    acts = Path(os.environ.get("CHOIR_RT_ACTS") or CACHE / "rt-acts")
    arch = Path(os.environ.get("CHOIR_RT_ACTS_ARCHIVE") or CACHE / "rt-acts-archive")
    out = [("journal", live.JOURNAL), ("rt-acts", acts), ("rt-acts-archive", arch),
           ("reviews.jsonl", CACHE / "reviews.jsonl"),
           # те же переопределения, что у окна (стенды): иначе в снимок шёл бы не тот файл (deepseek)
           ("rt-voices.json", Path(os.environ.get("CHOIR_RT_VOICES") or CACHE / "rt-voices.json")),
           ("rt-models.json", Path(os.environ.get("CHOIR_RT_MODELS") or CACHE / "rt-models.json"))]
    return out


def store() -> Path:
    """Хранилище ЭТОГО журнала: DEST/<имя стола>-<sha пути журнала>. Один
    DEST на машину, но снимки разных журналов (боевой, стенд с
    ROUNDTABLE_JOURNAL) не перемешиваются и не переставляют друг другу
    latest (ревизия: субагент)."""
    import hashlib
    key = hashlib.sha1(str(live.JOURNAL.resolve()).encode()).hexdigest()[:8]
    return DEST / f"{live.JOURNAL.resolve().parent.name}-{key}"


def _stamp(now: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(now or time.time(), _dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def snapshots(complete: bool = False) -> list[Path]:
    """Снимки по возрасту. complete=True — только целые: с MANIFEST.json и
    без ошибок (только такие годятся в link-dest и latest)."""
    root = store()
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.is_symlink() or not p.name[:4].isdigit():
            continue
        if complete and not _complete(p):
            continue
        out.append(p)
    return out


def _complete(p: Path) -> bool:
    try:
        m = json.loads((p / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return not m.get("errors")


def latest_at() -> float | None:
    """Момент последнего целого снимка (по манифесту), None — снимков нет."""
    snaps = snapshots(complete=True)
    if not snaps:
        return None
    try:
        m = json.loads((snaps[-1] / "MANIFEST.json").read_text(encoding="utf-8"))
        return _dt.datetime.fromisoformat(m["at"]).timestamp()
    except (OSError, ValueError, KeyError):
        return None


def run(now: float | None = None, *, min_age: float = 0.0) -> dict:
    """Один снимок. min_age — не снимать, если последний целый снимок моложе
    (каденс — свойство ХРАНИЛИЩА, не окна: три окна не делают три снимка).
    Замок на хранилище (flock): параллельный run() другого окна — пропуск.
    Возвращает отчёт: snapshot | skipped_reason, copied, skipped, errors,
    pruned, kept, elapsed_s."""
    import fcntl
    t0 = time.time()
    root = store()
    root.mkdir(parents=True, exist_ok=True)
    for d in (DEST, root):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    lockf = open(root / ".lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockf.close()
        return {"snapshot": None, "skipped_reason": "снимок уже делает другой процесс", "copied": [], "skipped": [],
                "errors": [], "pruned": [], "kept": len(snapshots()), "elapsed_s": 0.0}
    try:
        la = latest_at()
        if min_age and la is not None and (now or time.time()) - la < min_age:
            return {"snapshot": None, "skipped_reason": f"последний снимок моложе {int(min_age)} с", "copied": [],
                    "skipped": [], "errors": [], "pruned": [], "kept": len(snapshots()), "elapsed_s": 0.0}
        return _run_locked(root, now, t0)
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN); lockf.close()
        except OSError:
            pass


def _run_locked(root: Path, now: float | None, t0: float) -> dict:
    prev = snapshots(complete=True)
    link = prev[-1] if prev else None
    snap = root / _stamp(now)
    for suffix in ("", f"-{os.getpid()}", f"-{os.getpid()}-{int(time.time() * 1000) % 1000}"):
        try:
            cand = root / (snap.name + suffix)
            cand.mkdir(mode=0o700)
            snap = cand
            break
        except FileExistsError:
            continue
    else:
        return {"snapshot": None, "skipped_reason": "каталог снимка занят", "copied": [], "skipped": [],
                "errors": ["каталог снимка занят"], "pruned": [], "kept": len(snapshots()), "elapsed_s": 0.0}
    rep: dict = {"snapshot": str(snap), "copied": [], "skipped": [], "errors": [], "warnings": [],
                 "link_dest": str(link) if link else None}
    rsync = shutil.which("rsync")
    if not rsync:
        rep["warnings"].append("rsync не найден — снимок без жёстких ссылок на прошлый (полная копия)")
    for name, src in sources():
        if not src.exists():
            rep["skipped"].append(f"{name}: нет {src}")
            continue
        dst = snap / name
        try:
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                if rsync:
                    cmd = [rsync, "-a", *(f"--exclude={e}" for e in EXCLUDE)]
                    if link and (link / name).is_dir():
                        cmd.append(f"--link-dest={link / name}")
                    cmd += [str(src) + "/", str(dst) + "/"]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                    if r.returncode == 24:       # файл исчез во время копии (живой ход) — не ошибка снимка
                        rep["warnings"].append(f"{name}: файлы менялись во время копии (rsync 24)")
                    elif r.returncode != 0:
                        rep["errors"].append(f"{name}: rsync {r.returncode}: {(r.stderr or '').strip()[-300:]}")
                        continue
                else:
                    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*EXCLUDE))
            else:
                linked = False
                if link and (link / name).exists() and _same(src, link / name):
                    try:
                        os.link(link / name, dst)  # не изменился — жёсткая ссылка на прошлый снимок
                        linked = True
                    except OSError:
                        linked = False             # ФС без жёстких ссылок — копия (deepseek)
                if not linked:
                    shutil.copy2(src, dst)
            rep["copied"].append(name)
        except (OSError, subprocess.TimeoutExpired, shutil.Error) as e:
            rep["errors"].append(f"{name}: {type(e).__name__}: {str(e)[-300:]}")
    # МАНИФЕСТ — до latest: смерть между ними оставила бы latest на снимке без манифеста
    try:
        (snap / "MANIFEST.json").write_text(json.dumps({
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "journal": str(live.JOURNAL.resolve()),
            "sources": {n: str(p) for n, p in sources()}, "copied": rep["copied"], "skipped": rep["skipped"],
            "warnings": rep["warnings"], "errors": rep["errors"], "link_dest": rep["link_dest"]},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        rep["errors"].append(f"MANIFEST: {e}")
    # latest — только на ЦЕЛЫЙ снимок: неполный не вытесняет целый
    if not rep["errors"]:
        tmp = root / f".latest.tmp.{os.getpid()}"
        try:
            tmp.unlink(missing_ok=True)
            os.symlink(snap.name, tmp)
            os.replace(tmp, root / "latest")
        except OSError as e:
            rep["errors"].append(f"latest: {e}")
    rep["pruned"] = prune()
    rep["kept"] = len(snapshots())
    rep["elapsed_s"] = round(time.time() - t0, 1)
    return rep


def _same(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return sa.st_size == sb.st_size and sa.st_mtime_ns == sb.st_mtime_ns


def prune(keep: int | None = None) -> list[str]:
    """Убрать: полуснимки без манифеста старше INCOMPLETE_TTL и целые
    сверх KEEP (0 — хранить все). latest не трогается никогда."""
    keep = KEEP if keep is None else keep
    root = store()
    latest = None
    try:
        latest = (root / "latest").resolve()
    except OSError:
        pass
    gone = []
    now = time.time()
    for p in snapshots():
        if not (p / "MANIFEST.json").exists() and now - p.stat().st_mtime > INCOMPLETE_TTL:
            shutil.rmtree(p, ignore_errors=True); gone.append(p.name)
    full = [p for p in snapshots() if (p / "MANIFEST.json").exists()]
    for p in (full[:-keep] if keep > 0 else []):
        if latest and p.resolve() == latest:
            continue
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p.name)
    return gone


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Резервные снимки журналов и стенограмм стола")
    ap.add_argument("--list", action="store_true", help="перечислить снимки")
    ap.add_argument("--prune", action="store_true", help="только убрать старшие сверх KEEP")
    a = ap.parse_args(argv)
    if a.list:
        for p in snapshots():
            try:
                m = json.loads((p / "MANIFEST.json").read_text(encoding="utf-8"))
                print(f"{p.name}  скопировано: {', '.join(m.get('copied') or [])}"
                      + (f"  ошибки: {len(m['errors'])}" if m.get("errors") else ""))
            except (OSError, ValueError):
                print(f"{p.name}  (без манифеста)")
        print(f"хранилище: {store()} · снимков {len(snapshots())} · держим {KEEP or 'все'}")
        return 0
    if a.prune:
        print("убрано:", ", ".join(prune()) or "ничего")
        return 0
    rep = run()
    if rep.get("skipped_reason"):
        print(f"снимок не снят: {rep['skipped_reason']}")
        return 0
    print(f"снимок {rep['snapshot']}: скопировано {', '.join(rep['copied'])}"
          + (f"; предупреждения: {'; '.join(rep['warnings'])}" if rep.get("warnings") else "")
          + (f"; пропущено {'; '.join(rep['skipped'])}" if rep["skipped"] else "")
          + (f"; ОШИБКИ {'; '.join(rep['errors'])}" if rep["errors"] else "")
          + f"; снимков {rep['kept']} ({rep['elapsed_s']} с)")
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
