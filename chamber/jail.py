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
достать иначе; окно стола на 127.0.0.1 из клетки доступно — токен на
POST окна записан долгом), путь HOME не подменяем (auth-каталоги живут
на своих местах; recover читает сессии снаружи — раунд назвал подмену
упущением всех). ЧТО ЕСТЬ сверх ro-корня: свой pid- и ipc-namespace
(первая редакция их не отделяла — /proc/<pid>/cmdline соседа отдавал
промпт из аргументов, нашёл codex; killpg и cgroup окна работают сквозь
namespace), пустой tmpfs на месте дома и /run/user/<uid> (см. ниже).

Кодекс: своя песочница (landlock+seccomp через writable_roots) — в
кресле её хватает (факт «own», клетки нет), а в раунде и комнате он идёт
В клетку с песочницей внутри (факт «bwrap+own»; вложение замерено
живьём 2026-09-18). Грок в ревизии — своя (`--sandbox read-only`), в
кресле без песочницы — клетка нужна.

Клетка — ФАКТ ЖУРНАЛА, не обещание: обёртка кресла пишет в close
`jail` («bwrap»/«none») и `jail_sha` — свёртку аргументов клетки, по
которой видно, какие пути были открыты (идея kimi: jail_sha рядом с
context_sha). Нет bwrap — запись честно говорит «none», гейт называет
это мягкой причиной, не блокирует: правка без клетки хуже правки в
клетке, но не хуже вчерашней.

СВОЙ HOME НА ГОЛОС (обязательство 3 раунда, 2026-09-18). Под одним
пользователем каждый голос видел сессии, истории адаптеров и ключи
остальных: ответ слепой фазы ложится в ~/.claude/projects/…/*.jsonl,
~/.codex/sessions, ~/.kimi-code/sessions, ~/.grok/sessions,
~/.gemini/choir-http и ~/.deepseek/choir-http СРАЗУ, ещё до закрытия
фазы. Путь HOME не подменяется (auth-каталоги и recover остаются на
своих местах — раунд назвал подмену упущением всех), но САМ ДОМ — пустой
tmpfs: в нём есть только своё состояние (STATE, rw), бинари CLI
(~/.local/bin, ~/.local/lib, HOME_RO по голосу — ro), git-identity
(~/.gitconfig, ro) и то, что вызывающий открыл явно (песочница, проект,
журнал — ro; свой каталог голоса — rw). Чужое не «скрыто», его НЕТ —
включая файлы и каталоги, которые появятся при живой клетке (первая
редакция закрывала соседей поимённо, и новый ~/.claude.json.bak-*
оставался виден — codex). Переменные окружения с чужими и ничьими
ключами снимаются (`unset_env()`). Слепота — отсутствием данных, не
запретом смотреть (правило 8.5). Канарейка слепоты идёт тем же путём и
мерит ровно то, что видит голос.

ИЗВЕСТНЫЕ ГРАНИЦЫ (ревизия стола, 2026-09-17): точки монтирования для
отсутствующих ro-каталогов bwrap создаёт на настоящем диске — после
первого запуска в ~/.claude появляются пустые agents/, commands/,
rules/ и projects/*/memory (это запись в HOME от клетки, не от CLI;
названо ценой). rw на refs/heads/act и objects — это ВСЕ ветки актов и
общая база объектов (те же корни, что у Кодекса): кресло акта A
физически может переписать ref акта B — гейт сверяет головы по sha, и
подмена вскроется ревизией, но это обнаружение, не запрет (codex).
Каталоги состояния CLI открыты на запись целиком, кроме
бинаря/хуков/настроек (STATE_RO). Всё под /tmp в клетке — tmpfs: «леса»
точек монтирования там записываемы, запись туда никуда не ложится, но
CLI видит успех. Точку монтирования под ro-родителем bwrap создать не
может («Can't create file: Read-only file system») — поэтому родители
чужого (voices/, карантин, choir-voices) вызывающий создаёт ДО wrap, а
hidden() пропускает отсутствующее молча (grok). Секреты окружения — по
известным именам, образцам и префиксам. dsh и deepseek — два кресла
одного голоса, дом общий намеренно. Строгий режим
CHOIR_RT_JAIL_REQUIRED=1 не зовёт голос без клетки.

Выключить: CHOIR_RT_NO_BWRAP=1 (тесты со стабами, машины без bwrap).
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import os
import re
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
    # ~/.claude.json — только сам файл: дом пустой, резервные копии
    # .bak-* никому не видны, а glob с временными файлами tmp+rename
    # ронял бы bwrap («Can't find source path») — субагент.
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
    "codex": ["~/.codex/packages"],       # бинарь codex (симметрия с kimi/grok)
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

# Секреты в окружении по голосам: голосу X снимаются переменные всех
# остальных (свои остаются — deepseek-http без DEEPSEEK_API_KEY не
# ответит). Список — известные имена; неизвестная переменная с ключом
# пройдёт — названная граница.
SECRET_ENV = {
    "claude": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_MESSAGING_TOKEN"],
    "codex": ["OPENAI_API_KEY", "CODEX_API_KEY"],
    "grok": ["XAI_API_KEY", "GROK_API_KEY"],
    "kimi": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
    "dsh": ["DEEPSEEK_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEYS_FILE",
               "GOOGLE_APPLICATION_CREDENTIALS"],
}
# Свои по ПРЕФИКСУ — образец SECRET_ENV_RE их не снимает (субагент:
# GOOGLE_APPLICATION_CREDENTIALS у gemini, ANTHROPIC_AUTH_TOKEN у claude
# при переезде на gateway ломались бы молча).
OWN_PREFIX = {
    "claude": ("ANTHROPIC_", "CLAUDE_"), "codex": ("OPENAI_", "CODEX_"),
    "grok": ("XAI_", "GROK_"), "kimi": ("MOONSHOT_", "KIMI_"),
    "dsh": ("DEEPSEEK_",), "deepseek": ("DEEPSEEK_",),
    "gemini": ("GEMINI_", "GOOGLE_"),
}

# Что из дома нужно ВСЕМ голосам поверх пустого tmpfs (ro): бинари и
# библиотеки CLI, git-identity для коммитов кресла.
HOME_RO_ALL = ["~/.local/bin", "~/.local/lib", "~/.gitconfig",
               "~/.config/git"]
# Бинарь голоса вне его STATE: launcher claude — симлинк в
# ~/.local/share/claude (пробник с tmpfs-домом: «claude: command not
# found»); codex/grok/dsh лежат в своих STATE или ~/.local/lib.
HOME_RO = {
    "claude": ["~/.local/share/claude"],
}

# Голоса со своей песочницей — см. докстринг (own / bwrap+own).
OWN_SANDBOX = {"codex"}

# Ключи в окружении, которые не принадлежат ни одному голосу: снимаются
# ВСЕМ (GITHUB_TOKEN, AWS_*, …). Имя по образцу — известная граница:
# нестандартно названный секрет пройдёт (deepseek).
SECRET_ENV_RE = re.compile(r"(_API_KEY|_TOKEN|_SECRET|_SECRET_ACCESS_KEY|"
                           r"_PASSWORD|_PASSWD|_CREDENTIALS)$|"
                           r"^(AWS|GITHUB|GH|NPM|PYPI|HF|HUGGINGFACE|"
                           r"OPENROUTER|AZURE|GOOGLE_APPLICATION)_")

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
    """Те же флаги, что у боевой клетки (pid/ipc namespace, tmpfs дома):
    пробник с урезанным набором говорил «есть», а exec падал — поле
    jail=bwrap врало бы (grok)."""
    if not shutil.which("bwrap"):
        return False
    try:
        r = subprocess.run(
            # голос «probe» — вне STATE: prefix("codex") создавал бы
            # ~/.codex на машине без codex (субагент)
            [*prefix("probe"), "--", "sh", "-c",
             "touch /rt-jail-probe 2>/dev/null && exit 3; "
             "touch \"$HOME/rt-jail-probe\" && touch /tmp/rt-jail-probe"],
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


def base_voice(name: str) -> str:
    """Имя голоса без линии: kimi-alt → kimi (две линии — один голос,
    один дом)."""
    if name in STATE:
        return name
    head = name.split("-", 1)[0]
    return head if head in STATE else name


def home() -> str:
    return os.path.expanduser("~")


def _under(p: str, roots) -> bool:
    p = str(p).rstrip("/")
    return any(p == r.rstrip("/") or p.startswith(r.rstrip("/") + "/")
               for r in roots if r)


def hidden(voice: str, extra=()) -> list[tuple[str, str, str]]:
    """Что закрыть от голоса: [(вид, источник, путь)] — extra (родители
    чужого, открытые вызывающим ro поверх пустого дома: voices/ журнала,
    карантин, нейтральные cwd) и, на всякий случай, каталоги состояния
    остальных голосов ВНЕ дома (в доме их и так нет — tmpfs). Своё не
    закрывается никогда, даже если совпадает с чужим (dsh и deepseek
    делят каталоги)."""
    own = set(state_dirs(voice))
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    others: list[str] = []
    for v in STATE:
        if v == voice:
            continue
        others += [d for d in state_dirs(v) if not _under(d, [home()])]
    for q in [*others, *[str(x) for x in extra]]:
        if q in own or q in seen or not q:
            continue
        seen.add(q)
        if os.path.isdir(q):
            out.append(("--tmpfs", "", q))
        elif os.path.exists(q):
            out.append(("--ro-bind", "/dev/null", q))
    return out


def unset_env(voice: str) -> list[str]:
    """Чужие ключи (SECRET_ENV других голосов) и ничьи (SECRET_ENV_RE);
    свои — никогда. DBUS_SESSION_BUS_ADDRESS — всем: шина сессии
    закрыта вместе с /run/user (см. prefix)."""
    own = set(SECRET_ENV.get(voice, []))
    out: list[str] = []
    for v, names in SECRET_ENV.items():
        if v == voice:
            continue
        out += [n for n in names if n not in own and n in os.environ
                and n not in out]
    pref = OWN_PREFIX.get(voice, ())
    for n in sorted(os.environ):
        if n not in own and n not in out and SECRET_ENV_RE.search(n) \
                and not n.startswith(pref):
            out.append(n)
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        out.append("DBUS_SESSION_BUS_ADDRESS")
    return out


def runtime_dir() -> str:
    """XDG_RUNTIME_DIR (/run/user/<uid>): сокеты D-Bus, KWallet, буфера
    обмена, порталов — из клетки «рецензент не пишет ничего» иначе ложь
    (субагент: busctl --user list работал). Ни один CLI голоса шиной не
    пользуется (kimi, grok, claude — проверено живьём с tmpfs поверх)."""
    d = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return d if os.path.isdir(d) else ""


def path_tmp_dirs() -> list[str]:
    """Каталоги PATH под /tmp — видны в клетке (ro): PATH — дирижёра, и
    стабы CLI тестов лежат там; tmpfs клетки иначе прятал бы их."""
    return [p for p in os.environ.get("PATH", "").split(":")
            if p.startswith("/tmp/") and os.path.isdir(p)]


def _existing(paths) -> list[str]:
    out = []
    for p in paths:
        ex = os.path.expanduser(p)
        if "*" in ex:
            out += sorted(q for q in glob.glob(ex) if os.path.lexists(q))
            continue
        q = Path(ex)
        if q.exists() or q.is_symlink():
            out.append(str(q))
    return out


def state_dirs(voice: str) -> list[str]:
    return _existing(STATE.get(voice, []))


def ensure_primary(voice: str) -> None:
    """ГЛАВНЫЙ каталог голоса (первый в STATE) создаётся до клетки, если
    его нет: иначе первый запуск сложил бы сессию в tmpfs дома, и она
    пропала бы вместе с клеткой — продолжение нити и recover без данных
    (codex, kimi). Только для СВОЕГО голоса, из prefix(): вызов в
    hidden()/state_dirs() плодил бы чужие пустые каталоги (первый
    прогон теста)."""
    lst = STATE.get(voice, [])
    if lst and "*" not in lst[0]:
        with contextlib.suppress(OSError):
            Path(os.path.expanduser(lst[0])).mkdir(parents=True, exist_ok=True)


def argv_paths(cmd, voice: str = "") -> list[str]:
    """Существующие абсолютные пути из аргументов команды (и вида
    --flag=/путь): файл патча dsh в ~/.cache/choir/dsh-patches — в
    пустом доме его не было бы (codex: кресло dsh теряло --patch).
    Открываются ro. ПОД ДОМОМ — только кэш стола (~/.cache/choir) и своё
    состояние: промпт «-p ~/.kimi-code» иначе открыл бы чужие
    сессии (субагент). Песочницу и проект вызывающий открывает сам."""
    h = home()
    ok_home = [os.path.join(h, ".cache", "choir", "dsh-patches"),
               *state_dirs(voice)] if voice else []
    out: list[str] = []
    for a in cmd:
        a = str(a)
        cand = [a] + ([a.split("=", 1)[1]] if a.startswith("-") and "=" in a else [])
        for c in cand:
            if not (c.startswith("/") and len(c) < 512 and "\n" not in c):
                continue
            if _under(c, [h]) and not _under(c, ok_home):
                continue
            if os.path.lexists(c) and c not in out:
                out.append(c)
    return out


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


def prefix(voice: str, *, rw=(), ro=(), ro_after=(), hide=(),
           cwd: str | None = None) -> list[str]:
    """Аргументы bwrap без самой команды. rw — что открыть на запись
    (worktree, git-корни), ro — что должно быть видно, хотя лежит под
    tmpfs /tmp (пакет ревизии, тестовый репозиторий), ro_after — что
    снова закрыть ВНУТРИ rw (бинарь и хуки CLI, см. STATE_RO). Порядок
    bind'ов значим: позднее перекрывает раннее, поэтому ro идёт ПЕРЕД rw
    — общий .git целиком ro, его objects/refs поверх rw; обратный
    порядок закрыл бы rw-корни ro-родителем (первый пробник так и упал:
    index.lock — Read-only file system).

    ПОРЯДОК: корень, tmpfs /tmp и tmpfs HOME → ro (бинари из дома, PATH
    под /tmp, ro вызывающего, cwd) → СОКРЫТИЯ (hidden(voice, hide):
    РОДИТЕЛЬСКИЕ каталоги чужого, открытые ro выше — voices/, карантин,
    нейтральные cwd) → rw своё → ro_after (STATE_RO и своё, что лежит
    ПОД скрытым родителем: каталог своего вызова в карантине, cwd).
    Сокрытия после ro: ro-bind песочницы/журнала иначе заново открыл бы
    voices/ соседей; своё — после сокрытий, потому что родитель скрыт
    целиком. Скрывать РОДИТЕЛЯ, а не перечислять соседей: сосед, чей
    каталог появится уже при живой клетке, иначе был бы виден (codex).
    --unshare-pid: без него /proc/<pid>/cmdline соседа отдавал промпт
    из аргументов (codex, пробник); killpg и cgroup окна работают
    сквозь pid-namespace (пробник: группа снята целиком); внук CLI
    теперь умирает вместе с namespace — граница «внук переживает
    вылет» закрыта для голосов в клетке. --unshare-ipc и tmpfs поверх
    /run/user/<uid>: шина сессии и сокеты рабочего стола недоступны.
    --unsetenv чужих и ничьих ключей; --chdir последним."""
    h = home()
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", h,
            "--unshare-pid", "--unshare-ipc", "--die-with-parent"]
    rd = runtime_dir()
    if rd:
        args += ["--tmpfs", rd]
    ensure_primary(voice)
    # Пути НОРМАЛИЗУЮТСЯ: «~/<песочница>/..» проходил проверку
    # предка дома строкой и открывал дом заново (codex, пробник).
    norm = os.path.normpath
    hide_l = [norm(str(p)) for p in hide]
    ro_n = [norm(str(p)) for p in ro]
    # ro РОВНО на скрытом корне — отказ: bind поверх tmpfs открыл бы
    # всех соседей (codex: «проектом выбран journal/voices»); ro ПОД
    # скрытым родителем — после сокрытия, иначе молча пропадает
    # (субагент: пакет под ~/.cache/choir не читался бы)
    for q in ro_n:
        if q in hide_l:
            raise RuntimeError(f"клетка: путь {q} — скрываемый корень; "
                               f"открыть его значит открыть соседей")
    ro_all = [q for q in ro_n if not _under(q, hide_l)]
    after = [norm(str(p)) for p in ro_after] + [q for q in ro_n if _under(q, hide_l)]
    rw_all = [*state_dirs(voice), *[norm(str(p)) for p in rw]]
    cwd = norm(str(cwd)) if cwd else cwd
    # Дом и его предки открывать нельзя: --ro-bind ~ после
    # --tmpfs ~ вернул бы чужие сессии и ключи целиком (codex:
    # «--project ~»). Сверка по realpath — симлинк на дом или
    # его предка тоже отказ. Это отказ, не тихий пропуск.
    hr = os.path.realpath(h)
    for q in [*ro_all, *rw_all, *after, *([cwd] if cwd else [])]:
        if _under(hr, [os.path.realpath(q)]):
            raise RuntimeError(f"клетка: путь {q} — дом голоса или его "
                               f"предок; открыть его значит открыть чужое")
    # cwd — куда его класть, решается здесь, а не у вызывающего: под
    # скрытым родителем (нейтральный cwd Клода) — поверх сокрытий (ro);
    # под своим rw или уже открытым ro — ничего; иначе — в ro ДО
    # сокрытий. Первая редакция клала cwd после сокрытий всегда, и
    # ro-bind RoundTable/ заново открывал journal/voices/ соседей, а
    # cwd-предок закрывал ro свой rw-каталог (codex, grok, субагент).
    if cwd:
        c = cwd
        if _under(c, rw_all) or _under(c, ["/tmp"]):
            # своё rw уже открыто; /tmp — tmpfs клетки: bind поверх сделал
            # бы /tmp ro (пробник dsh: mkdtemp EROFS), а chdir в
            # несуществующий каталог упадёт вслух — вызывающий обязан
            # открыть cwd под /tmp сам (все четыре так и делают)
            pass
        elif _under(c, hide_l):
            after.append(c)
        elif not _under(c, ro_all):
            ro_all.append(c)
    args += _bind("--ro-bind", [*_existing(HOME_RO_ALL),
                                *_existing(HOME_RO.get(voice, [])),
                                *path_tmp_dirs(), *ro_all])
    for kind, src, dst in hidden(voice, hide):
        args += [kind, dst] if kind == "--tmpfs" else [kind, src, dst]
    args += _bind("--bind", rw_all)
    args += _bind("--ro-bind", [*state_ro(voice), *after])
    for n in unset_env(voice):
        args += ["--unsetenv", n]
    if cwd:
        args += ["--chdir", cwd]
    return args


def sha(pref: list[str]) -> str:
    return hashlib.sha256("\0".join(pref).encode()).hexdigest()[:16]


def wrap(cmd: list[str], voice: str, *, rw=(), ro=(), ro_after=(),
         hide=(), cwd: str | None = None,
         nest_own: bool = False) -> tuple[list[str], dict]:
    """(команда, факт для журнала). Факт: {"jail": "bwrap", "jail_sha":
    …} либо {"jail": "none", "jail_why": …} — почему без клетки. Голос
    со своей песочницей — "own" (без клетки), а с nest_own=True —
    "bwrap+own": его песочница внутри клетки (раунд и комната, где
    важно сокрытие чужого; в кресле Кодексу writable_roots хватает)."""
    voice = base_voice(voice)
    if voice in OWN_SANDBOX and not nest_own:
        return list(cmd), {"jail": "own"}
    # Пути из argv под скрытым родителем открываются только СВОИ (под
    # ro_after/rw/состоянием): промпт, равный пути чужого вызова в
    # карантине, иначе переоткрыл бы его через after (субагент, пробник)
    own = [str(p) for p in ro_after] + [str(p) for p in rw] + state_dirs(voice)
    hide_l = [os.path.normpath(str(p)) for p in hide]
    ro = [*ro, *[p for p in argv_paths(cmd, voice)
                 if not (_under(p, hide_l) and not _under(p, own))]]
    if disabled():
        return list(cmd), {"jail": "none", "jail_why": "CHOIR_RT_NO_BWRAP=1"}
    if not available():
        if os.environ.get("CHOIR_RT_JAIL_REQUIRED") == "1":
            # Строгий режим: без клетки голос не зовём вовсе (codex:
            # «слепая фаза с jail=none — ход с доступом ко всем чужим
            # сессиям»). Умолчание мягкое: факт none в записи, стол
            # работает, как вчера.
            raise RuntimeError("клетка bwrap недоступна, а "
                               "CHOIR_RT_JAIL_REQUIRED=1 — ход не выдан")
        return list(cmd), {"jail": "none", "jail_why": "bwrap недоступен"}
    pref = prefix(voice, rw=rw, ro=ro, ro_after=ro_after, hide=hide, cwd=cwd)
    kind = "bwrap+own" if voice in OWN_SANDBOX else "bwrap"
    return [*pref, "--", *cmd], {"jail": kind, "jail_sha": sha(pref)}


def mark(fact: dict, hide=()) -> str:
    """Строка для стенограммы: чем и от чего огорожен ход."""
    j = fact.get("jail")
    if j in ("bwrap", "bwrap+own"):
        return (f"🔒 клетка {j} ({fact.get('jail_sha')}): корень ro, "
                f"чужие каталоги скрыты ({len(list(hide))} + состояние "
                f"других голосов)")
    if j == "own":
        return "🔒 своя песочница CLI, клетки нет"
    return f"⚠ БЕЗ клетки: {fact.get('jail_why', '?')}"
