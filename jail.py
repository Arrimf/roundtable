#!/usr/bin/env python3
"""Клетка для CLI голоса: bwrap с корнем только на чтение.

Второй шаг изоляции по порядку стола (раунд «стол-v3-изоляция»,
2026-09-16: сначала systemd-run, потом bwrap). Закрывает два обещания,
которые до сих пор держались на слове CLI (правило 8.5):

  1. исполнитель пишет ТОЛЬКО в свой worktree — Клод, Кими, Грок и dsh в
     кресле получали запись во весь диск, огорожен был один Кодекс;
  2. рецензент не пишет НИЧЕГО, кроме состояния своего CLI (сессии,
     кэш) — read-only был только у codex и grok, Клод и Кими шли по
     фразе в уставе (уже был __pycache__ от ревизии).

Как: `bwrap --ro-bind / /` — весь диск виден, но неизменяем; поверх
точечно rw: worktree и git-корни ветки (кресло), каталоги состояния
самого CLI (сессии, кэш — иначе ни один CLI не стартует); `/tmp` —
свежий tmpfs (CLI туда пишут; чужие пакеты и логи там не видны);
`--die-with-parent` — умерла обёртка, ядро убивает и клетку.

ЧЕГО НЕТ, намеренно: сети не трогаем (ключи и API — снаружи клетки не
достать иначе), pid-namespace не отделяем (окно ищет процессы по pgid и
cgroup, а не по pid внутри клетки), HOME не подменяем (auth-каталоги
живут на своих местах; recover читает сессии снаружи — раунд назвал это
упущением всех).

Кодекс в клетку НЕ идёт: у него своя песочница (landlock+seccomp через
writable_roots), и вложение двух песочниц никем не замерено. Грок в
ревизии тоже (`--sandbox read-only`) — в кресле Грок без песочницы, и
там клетка нужна.

Клетка — ФАКТ ЖУРНАЛА, не обещание: обёртка кресла пишет в close
`jail` («bwrap»/«none») и `jail_sha` — свёртку аргументов клетки, по
которой видно, какие пути были открыты (идея kimi: jail_sha рядом с
context_sha). Нет bwrap — запись честно говорит «none», гейт называет
это мягкой причиной, не блокирует: правка без клетки хуже правки в
клетке, но не хуже вчерашней.

ИЗВЕСТНЫЕ ГРАНИЦЫ (ревизия стола, 2026-09-17): точки монтирования для
отсутствующих ro-каталогов bwrap создаёт на настоящем диске — после
первого запуска в ~/.claude появляются пустые agents/, commands/,
rules/ и projects/*/memory (это запись в HOME от клетки, не от CLI;
названо ценой). rw на refs/heads/act
и objects — это ВСЕ ветки актов и общая база объектов (те же корни,
что у Кодекса): кресло акта A физически может переписать ref акта B —
гейт сверяет головы по sha, и подмена вскроется ревизией, но это
обнаружение, не запрет (codex). Каталоги состояния CLI открыты на
запись целиком, кроме бинаря/хуков/настроек (STATE_RO): полное
закрытие — отдельный HOME на голос, обязательство 3 раунда (deepseek).
Всё под /tmp в клетке — tmpfs: «леса» точек монтирования там
записываемы, запись туда никуда не ложится, но CLI видит успех.

Выключить: CHOIR_RT_NO_BWRAP=1 (тесты со стабами, машины без bwrap).
"""
from __future__ import annotations

import glob
import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

# Каталоги состояния CLI по голосам — то, без чего CLI не стартует или
# теряет сессию. Только СУЩЕСТВУЮЩИЕ из списка попадают в клетку: bwrap
# на несуществующий источник падает. ~/.claude.json — файл: bind файла
# работает, но rename поверх него внутри клетки невозможен (точка
# монтирования) — CLI пишет в него через tmp+rename; проверено живьём
# (см. journal/reviews/review-bwrap-v1.md): claude -p при этом
# отвечает, конфиг просто не обновляется.
STATE = {
    "claude": ["~/.claude", "~/.claude.json", "~/.cache/claude",
               "~/.cache/claude-cli-nodejs", "~/.local/state/claude",
               "~/.config/claude"],
    "codex": ["~/.codex"],
    "kimi": ["~/.kimi-code", "~/.cache/kimi-code", "~/.kimi"],
    "grok": ["~/.grok", "~/.cache/grok", "~/.config/grok"],
    "dsh": ["~/.dsh", "~/.deepseek", "~/.cache/dsh"],
    "deepseek": ["~/.dsh", "~/.deepseek", "~/.cache/dsh"],
    "gemini": ["~/.gemini"],
}

# Внутри rw-состояния — снова ro: бинарь CLI, хуки, настройки, ключи.
# Иначе обещание 2 дырявое в постоянное состояние: неаккуратный агент
# переписывает ~/.kimi-code/bin/kimi или ~/.claude/hooks, и следующий
# НЕзаключённый запуск — уже он (нашёл grok). Только существующее.
# Каталоги (в т.ч. по glob) — если их НЕТ, на их место ro-подкладывается
# пустой /var/empty: иначе клетка позволяла бы их СОЗДАТЬ, и следующая
# незаключённая сессия подхватила бы подложенные agents/rules (субагент;
# цена — bwrap создаёт точку монтирования, пустой каталог в настоящем
# HOME). Файлы — только существующие: пустой файл как точка монтирования
# сломал бы разбор настроек снаружи клетки; отсутствующий файл настроек
# кресло создать МОЖЕТ — названная граница.
STATE_RO_DIRS = {
    # projects/*/memory — авто-память, инструкции будущим сессиям; для
    # проекта без памяти на её место ложится пустой ro-каталог, чтобы
    # кресло не подложило MEMORY.md (codex). Новый каталог проекта в
    # projects/ кресло создать может — названная граница.
    "claude": ["~/.claude/hooks", "~/.claude/skills", "~/.claude/plugins",
               "~/.claude/agents", "~/.claude/commands", "~/.claude/rules",
               "~/.claude/projects/*/memory"],
    "kimi": ["~/.kimi-code/bin"],
    "grok": ["~/.grok/bin", "~/.grok/bundled", "~/.grok/vendor",
             "~/.grok/hooks", "~/.grok/skills"],
    # dsh пишет profiles/<имя>/cordis.yml при старте (EROFS в пробнике —
    # профиль ro нельзя); ro — только код: ОБЩИЙ profiles/node_modules
    # (у профилей своих node_modules нет — glob «*/node_modules» целил
    # мимо и плодил пустые каталоги на диске, нашёл субагент).
    "dsh": ["~/.dsh/profiles/node_modules"],
    "deepseek": ["~/.dsh/profiles/node_modules"],
    "gemini": [],
}
STATE_RO_FILES = {
    "claude": ["~/.claude/settings.json", "~/.claude/settings.local.json",
               "~/.claude/CLAUDE.md", "~/.claude/statusline-command.sh"],
    "kimi": ["~/.kimi-code/config.toml", "~/.kimi-code/tui.toml"],
    # hooks-paths — ФАЙЛ (пустой): в списке каталогов он молча выпадал,
    # и кресло Грока могло вписать туда путь на свой скрипт (grok).
    "grok": ["~/.grok/config.toml", "~/.grok/sandbox.toml",
             "~/.grok/hooks-paths"],
    "dsh": ["~/.dsh/settings.yaml"],
    "deepseek": ["~/.dsh/settings.yaml"],
    "gemini": ["~/.gemini/keys.txt"],
}


def empty_dir() -> str:
    """Пустой ro-источник для отсутствующих каталогов: /var/empty, а нет
    его — свой пустой каталог в кэше стола (без источника защита молча
    исчезала бы — codex). Пустая строка — не из чего собрать."""
    if os.path.isdir("/var/empty"):
        return "/var/empty"
    return _cache_empty()


def _cache_empty() -> str:
    """Свой пустой каталог; НЕ пустой (кто-то положил файлы) — не
    источник: его содержимое стало бы содержимым «пустых» agents/rules
    (grok)."""
    d = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() \
        / "choir" / "jail-empty"
    try:
        d.mkdir(parents=True, exist_ok=True)
        if os.listdir(d):
            return ""
        os.chmod(d, 0o555)
        return str(d)
    except OSError:
        return ""


EMPTY = empty_dir()

# Голоса со СВОЕЙ песочницей — их не вкладываем (см. докстринг).
OWN_SANDBOX = {"codex"}

# Кэш пробника: True — навсегда (bwrap не исчезает), False — на
# _RETRY_S секунд: окно живёт часами, и один таймаут пробника не должен
# на всю жизнь окна помечать кресла «none» при живом bwrap (grok).
# Под замком: первый набросок писал _AVAIL=False ДО пробы, и второй
# поток в этот миг получал «клетки нет» (воспроизвёл codex).
_AVAIL: bool | None = None
_AVAIL_AT = 0.0
_RETRY_S = 30.0
_LOCK = threading.Lock()


def disabled() -> bool:
    return os.environ.get("CHOIR_RT_NO_BWRAP", "") == "1"


def _probe() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        r = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
             "--proc", "/proc", "--tmpfs", "/tmp", "--die-with-parent",
             "--", "sh", "-c",
             "touch /rt-jail-probe 2>/dev/null && exit 3; "
             "touch /tmp/rt-jail-probe"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def available() -> bool:
    """bwrap есть и умеет ro-корень (пробник: запись в / отбита,
    в tmpfs /tmp — прошла). Под замком, см. _LOCK."""
    global _AVAIL, _AVAIL_AT
    if disabled():
        return False
    with _LOCK:
        if _AVAIL is True:
            return True
        if _AVAIL is False and time.monotonic() - _AVAIL_AT < _RETRY_S:
            return False
        _AVAIL = _probe()
        _AVAIL_AT = time.monotonic()
        return _AVAIL


def _existing(paths) -> list[str]:
    out = []
    for p in paths:
        q = Path(os.path.expanduser(p))
        if q.exists() or q.is_symlink():
            out.append(str(q))
    return out


def state_dirs(voice: str) -> list[str]:
    return _existing(STATE.get(voice, []))


def state_ro(voice: str) -> list[tuple[str, str]]:
    """[(источник, путь)]: существующие каталоги и файлы — сами на себя,
    отсутствующие каталоги — пустой /var/empty (если он есть)."""
    out: list[tuple[str, str]] = []
    def one(ex: str):
        if os.path.isdir(ex):
            out.append((ex, ex))
        elif not os.path.lexists(ex) and EMPTY and os.path.isdir(EMPTY) \
                and os.path.isdir(os.path.dirname(ex)):
            out.append((EMPTY, ex))

    for p in STATE_RO_DIRS.get(voice, []):
        ex = os.path.expanduser(p)
        if "*" in ex:
            # glob по родителю: «*/memory» должен закрыть и проекты БЕЗ
            # памяти, а не только те, где она уже есть (codex)
            head, tail = ex.rsplit("/", 1) if "*" in ex.rsplit("/", 1)[0] \
                else (ex, "")
            parents = sorted(q for q in glob.glob(head) if os.path.isdir(q))
            for q in parents:
                one(os.path.join(q, tail) if tail else q)
            continue
        one(ex)
    out += [(q, q) for q in _existing(STATE_RO_FILES.get(voice, []))
            if os.path.isfile(q)]
    return out


def _bind(kind: str, paths) -> list[str]:
    """paths — пути (источник = точка) или пары (источник, точка)."""
    args: list[str] = []
    seen = set()
    for p in paths:
        src, dst = (str(p[0]), str(p[1])) if isinstance(p, tuple) else (str(p), str(p))
        if not dst or dst in seen:
            continue
        seen.add(dst)
        args += [kind, src, dst]
    return args


def prefix(voice: str, *, rw=(), ro=(), ro_after=(),
           cwd: str | None = None) -> list[str]:
    """Аргументы bwrap без самой команды. rw — что открыть на запись
    (worktree, git-корни), ro — что должно быть видно, хотя лежит под
    tmpfs /tmp (пакет ревизии, тестовый репозиторий), ro_after — что
    снова закрыть ВНУТРИ rw (бинарь и хуки CLI, см. STATE_RO). Порядок
    bind'ов значим: позднее перекрывает раннее, поэтому ro идёт ПЕРЕД rw
    — общий .git целиком ro, его objects/refs поверх rw; обратный
    порядок закрыл бы rw-корни ro-родителем (первый пробник так и упал:
    index.lock — Read-only file system). ro_after — последним."""
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--die-with-parent"]
    args += _bind("--ro-bind", [str(p) for p in ro])
    args += _bind("--bind", [*state_dirs(voice), *rw])
    args += _bind("--ro-bind", [*state_ro(voice), *[str(p) for p in ro_after]])
    if cwd:
        args += ["--chdir", str(cwd)]
    return args


def sha(pref: list[str]) -> str:
    return hashlib.sha256("\0".join(pref).encode()).hexdigest()[:16]


def wrap(cmd: list[str], voice: str, *, rw=(), ro=(), ro_after=(),
         cwd: str | None = None) -> tuple[list[str], dict]:
    """(команда, факт для журнала). Факт: {"jail": "bwrap", "jail_sha":
    …} либо {"jail": "none", "jail_why": …} — почему без клетки. Голос
    со своей песочницей — "own"."""
    if voice in OWN_SANDBOX:
        return list(cmd), {"jail": "own"}
    if disabled():
        return list(cmd), {"jail": "none", "jail_why": "CHOIR_RT_NO_BWRAP=1"}
    if not available():
        return list(cmd), {"jail": "none", "jail_why": "bwrap недоступен"}
    pref = prefix(voice, rw=rw, ro=ro, ro_after=ro_after, cwd=cwd)
    return [*pref, "--", *cmd], {"jail": "bwrap", "jail_sha": sha(pref)}
