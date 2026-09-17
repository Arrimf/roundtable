#!/usr/bin/env python3
"""Канарейки прав — измерение обещаний 1 и 2 (раунд стол-v3-изоляция,
идея kimi, 2026-09-16): исполнитель пишет только в свой worktree,
рецензент не пишет ничего. Изоляции пока нет; но нарушение можно
ОБНАРУЖИТЬ: перед актом раскладываются файлы-приманки там, куда обещания
запрещают писать (корень проекта, HOME, каталог worktree'ов, кэш стола),
с пломбой sha256 + mtime + inode; по закрытии акта пломбы сверяются, и
итог ложится в ленту событием `canary`. Гейт при нарушении не принимает
акт. Угроза названа «неаккуратный CLI», не злоумышленник: против неё
детект дешевле превенции и даёт то, чего изоляция не даёт, — запись в
журнал, что обещание нарушалось.

Файл-приманка: `.rt-canary-<акт>` со случайной строкой. В корне проекта
он untracked; в `.git/info/exclude` добавляется маска, чтобы не пачкать
status. Пломбы — <store>/<акт>.canary.json."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat as stat_mod
import time
from pathlib import Path


def _seal(p: Path) -> dict:
    """Пломба РЕГУЛЯРНОГО файла по lstat: FIFO или симлинк на /dev/zero на
    месте приманки иначе вешали бы read() навсегда (ревизия: субагент)."""
    st = os.lstat(p)
    if not stat_mod.S_ISREG(st.st_mode):
        raise OSError(f"не регулярный файл (mode {oct(st.st_mode)})")
    with p.open("rb") as f:
        data = f.read(1 << 20)
    return {"sha": hashlib.sha256(data).hexdigest(),
            "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "size": st.st_size}


def default_spots(project: Path | None, wt_dir: Path | None) -> list[Path]:
    spots: list[Path] = []
    if project:
        spots.append(Path(project))
    # HOME подменяется в тестах (CHOIR_CANARY_HOME), чтобы приманки не
    # ложились в настоящий дом Автора
    spots.append(Path(os.environ.get("CHOIR_CANARY_HOME") or Path.home()))
    if wt_dir:
        spots.append(Path(wt_dir))
    spots.append(Path(os.environ.get("CHOIR_CANARY_HOME") or Path.home())
                 / ".cache" / "choir" / "canary")
    return spots


def _exclude_in_git(project: Path) -> None:
    """Маска приманки — в .git/info/exclude проекта (не в .gitignore:
    это не правка репозитория)."""
    try:
        import subprocess
        gd = subprocess.run(["git", "-C", str(project), "rev-parse",
                             "--git-common-dir"], capture_output=True,
                            text=True, timeout=10).stdout.strip()
        if not gd:
            return
        ex = Path(gd if os.path.isabs(gd) else str(Path(project) / gd)) / "info" / "exclude"
        line = ".rt-canary-*"
        cur = ex.read_text(encoding="utf-8") if ex.exists() else ""
        if line not in cur.splitlines():
            ex.parent.mkdir(parents=True, exist_ok=True)
            with ex.open("a", encoding="utf-8") as f:
                f.write(("" if cur.endswith("\n") or not cur else "\n") + line + "\n")
    except Exception:                                    # noqa: BLE001
        pass                        # приманка важнее исключения из status


def lay(act: str, spots: list[Path], store: Path,
        project: Path | None = None, owner: dict | None = None) -> dict:
    """Разложить приманки и запомнить пломбы. Возвращает запись пломб;
    место, куда положить не удалось, названо в `skipped`. owner — окно,
    которому принадлежит акт (pid, start_tick): чужая уборка его не
    трогает (ревизия: субагент). Пломба пишется атомарно (tmp+replace)."""
    rec: dict = {"act": act, "ts": time.time(), "files": {}, "skipped": [],
                 "owner": owner or {}}
    token = secrets.token_hex(16)
    for d in spots:
        p = Path(d) / f".rt-canary-{act}"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"канарейка прав акта {act}: {token}\n", encoding="utf-8")
            try:
                rec["files"][str(p)] = _seal(p)
            except OSError:
                p.unlink(missing_ok=True)      # без пломбы файл — сирота
                raise
        except OSError as e:
            rec["skipped"].append(f"{p}: {e}")
    if project:
        _exclude_in_git(Path(project))
    try:
        store.mkdir(parents=True, exist_ok=True)
        tmp = store / f"{act}.canary.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, store / f"{act}.canary.json")
    except OSError:
        # пломба не легла — приманки без следа никто не уберёт: снять сразу
        for path in list(rec["files"]):
            try:
                Path(path).unlink()
            except OSError:
                pass
        raise
    return rec


def owner_of(act: str, store: Path) -> dict | None:
    """Владелец пломбы (окно), если записан."""
    try:
        return (json.loads((store / f"{act}.canary.json").read_text(encoding="utf-8"))
                .get("owner") or None)
    except (OSError, ValueError):
        return None


def check(act: str, store: Path, *, remove: bool = True) -> dict | None:
    """Сверить пломбы. None — приманок не было. Иначе {clean, broken:
    [что и как], laid: N}. Приманки убираются (remove), чтобы не
    копились."""
    f = store / f"{act}.canary.json"
    if not f.exists():
        return None
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # битая пломба — не нарушение исполнителем, а сбой окна: убрать
        # файл (иначе уборка сыпала бы заметки каждую минуту) и сказать
        # «сверки нет» отдельным статусом (ревизия: субагент)
        if remove:
            try:
                f.unlink()
            except OSError:
                pass
        return {"clean": False, "broken": [], "laid": 0, "unreadable": True}
    broken: list[str] = []
    for path, seal in (rec.get("files") or {}).items():
        p = Path(path)
        if not p.exists():
            broken.append(f"{path}: удалён")
            continue
        try:
            now = _seal(p)
        except OSError as e:
            broken.append(f"{path}: не прочитан ({e})")
            continue
        diff = [k for k in ("sha", "ino", "size") if now.get(k) != seal.get(k)]
        if now.get("mtime_ns") != seal.get("mtime_ns"):
            diff.append("mtime")
        if diff:
            # улика — что именно записали: приманка ниже убирается, и без
            # этого Автору нечего разбирать (ревизия: субагент)
            try:
                with p.open("rb") as fh:
                    head = fh.read(200).decode("utf-8", "replace")
            except OSError:
                head = ""
            broken.append(f"{path}: изменён ({', '.join(diff)})"
                          + (f"; содержимое: {head!r}" if head else ""))
        if remove:
            try:
                p.unlink()
            except OSError:
                pass
    if remove:
        try:
            f.unlink()
        except OSError:
            pass
    return {"clean": not broken, "broken": broken,
            "laid": len(rec.get("files") or {}), "skipped": rec.get("skipped") or []}
