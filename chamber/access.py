#!/usr/bin/env python3
"""Доступ голосов вне каталога проекта — ACCESS.txt проекта.

Зачем (наказ Автора 2026-09-20). Клетка bwrap с 2026-09-18 держит дом
голоса пустым tmpfs: виден каталог проекта, журнал стола и своё
состояние — и больше ничего. Для проекта, чей предмет —
соседний каталог, это значило, что ни один голос его не видит: первый
раунд такого проекта прошёл до клетки, и агентные голоса читали каталог
напрямую; повторить их проверку стало нельзя (ход 845acace: «chamber/ мне
не смонтирован, виден только journal/»; предмет проекта не видел никто). Правило 14.5 требует
контролёра на первоисточнике — ему нужен первоисточник, а не пересказ
дирижёра, который клетки не имеет и видит всё.

Как. Файл `<проект>/ACCESS.txt`, одна запись — одна строка:

    r  /абсолютный/путь     чтение всем голосам: комната, раунды,
                            ревизия, кресло
    rw /абсолютный/путь     запись — ТОЛЬКО креслу исполнителя;
                            остальным чтение

Пустые строки и `#` — комментарии. Читает вызывающий (live.py,
choir.py, merge_gate.py, executor_run.py, edits.py) и отдаёт пути в
jail.wrap (ro/rw); голосам в пакет уходит строка `notice()` — что
именно открыто. Правило 8.5 в обе стороны: слово в пакете обеспечено
bind'ом, а bind без слова голос не найдёт (он не знает, что смотреть).

ЧТО ОТВЕРГАЕТСЯ — поимённо, строкой, а не молча и не падением хода:
кривой режим; относительный путь; несуществующий путь; путь через
ссылку (между разбором и bwrap цель ссылки подменяема — grok); дом
голоса и его предки (jail.prefix открыл бы чужие сессии и ключи заново
— codex); СКРЫТЫЕ записи дома `~/.*` (~/.ssh, ~/.gnupg, ~/.aws,
~/.config… — ревизия 20.09: grok, kimi, субагент; проекты в точках не
живут, а bind после `--tmpfs ~` вернул бы ключи всем шести провайдерам);
состояние CLI голосов (~/.claude, ~/.codex, …); виртуальные деревья
(/dev /proc /sys /run — bind поверх `--proc` вернул бы cmdline соседей,
поверх tmpfs /run — шину D-Bus; codex, kimi, субагент) и корни общих
каталогов целиком (/tmp, /var/tmp, /dev/shm — bind поверх `--tmpfs /tmp`
вернул бы пакеты чужих ревизий и уронил CLI с EROFS; подкаталог — явный
выбор Автора; deepseek, kimi); каталоги стола — карантин,
worktree и аренды кресел, voices/ журнала (безусловно, не только по
`hide` вызывающего: кресло `hide` не передаёт — codex, grok, kimi); сам
каталог проекта и всё под ним; `rw` на проект, его предка или внутрь
него (кресло пишет только в worktree — обещание 1); `rw` на стол
(журнал и код — правило 5); повтор пути с другим режимом (какой
действует — не должен решать порядок строк; grok). Отвергнутая строка
идёт в `rejects`, печатается в стенограмму и в ответ окна; в пакет — нет.

Откуда читается: из каталога проекта, как его знает ОКНО, не из
worktree кресла. Кресло правит ACCESS.txt в своей ветке как любой
файл, и правка проходит ревизию; merge_gate называет диф, трогающий
ACCESS.txt, отдельной строкой. `rw` вне worktree — запись, которой в
дифе ревизии не будет и которую канарейки прав не стерегут; поэтому
кресло пишет `access_rw` в close, а гейт ставит мягкую причину.

Файл читается при КАЖДОМ вызове (комната — на ход, раунд — раз на
процесс через project_notice): правка файла посреди слепой фазы дала
бы голосам разные пакеты; дирижёр читает один раз на процесс по той же
причине, по которой так читается PROJECT.md.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import jail

FILE = "ACCESS.txt"
MODES = ("r", "rw")
# Виртуальные деревья (тот же список, что у jail.argv_paths): bind
# сюда или под них снимает изоляцию, а не открывает данные (/proc —
# cmdline соседей, /run — шина D-Bus, /dev — устройства).
VIRTUAL_ROOTS = ("/dev", "/proc", "/sys", "/run")
# tmpfs клетки и общие записываемые каталоги: сам корень не выдаётся
# (bind /tmp поверх tmpfs вернул бы чужие пакеты и уронил CLI с EROFS),
# а ПОДкаталог — явный выбор Автора, названный в пакете: так же клетка
# сама кладёт под /tmp пакет ревизии и стабы тестов.
SHARED_EXACT = ("/tmp", "/var/tmp", "/dev/shm")


def hidden_roots() -> list[str]:
    """Каталоги стола, куда голосу нельзя ни в каком режиме: карантин
    раундов и кэш стола (~/.cache/choir — промпты, fifo, reviews.jsonl,
    логи актов), нейтральные cwd, worktree и аренды кресел (чужие акты
    до ревизии), voices/ журнала (state.json с id сессий, prompt/answer
    хода). Безусловно — вызывающий может не передать `hide` (кресло)."""
    h = jail.home()
    roots = [os.path.join(h, ".cache", "choir"),
             os.path.join(h, ".cache", "choir-voices")]
    for env, default in (("CHOIR_WT_DIR", os.path.join(h, ".cache", "choir", "worktrees")),
                         ("CHOIR_LEASE_DIR", os.path.join(h, ".cache", "choir", "leases")),
                         ("CHOIR_RT_ACTS", os.path.join(h, ".cache", "choir", "rt-acts")),
                         # архив стенограмм (2026-10-06): нескрёбленные логи,
                         # чистка ключей — на выдаче; вынесенный архив иначе
                         # открылся бы голосам по `r /родитель` (субагент-ревьюер)
                         ("CHOIR_RT_ACTS_ARCHIVE", os.path.join(h, ".cache", "choir", "rt-acts-archive")),
                         # пропуска Jev и удержанные события открытой фазы (jevproxy, 2026-10-08):
                         # вынесенный каталог иначе был бы виден голосам под ro-корнем
                         ("CHOIR_JEV_PASS_DIR", os.path.join(h, ".cache", "choir", "jev-pass")),
                         # реестр окон с токеном кресла секретаря (roundtable.py SEC_TOKEN)
                         ("CHOIR_RT_WINDOWS", os.path.join(h, ".cache", "choir", "rt-windows")),
                         # резервные снимки журналов и стенограмм (chamber/backup.py): копия стенограмм,
                         # которых голосу не видно и в оригинале
                         ("CHOIR_BACKUP_DIR", os.path.join(h, ".local", "share", "choir-backup"))):
        roots.append(os.path.normpath(os.path.expanduser(os.environ.get(env) or default)))
    here = Path(__file__).resolve().parent
    journals = [str(here.parent / "journal")]
    for env in ("ROUNDTABLE_JOURNAL", "CHOIR_JOURNAL"):
        j = os.environ.get(env)
        if j:
            journals.append(os.path.normpath(os.path.expanduser(j)))
    roots += [os.path.join(j, "voices") for j in journals]
    # нить кресла секретаря (id сессии полноправного агента) — скрыта во всех
    # клетках: раунд, кресло, ревизия (второй проход ревизии: grok, субагент)
    roots += [os.path.join(j, "secretary") for j in journals]
    return roots


def table_roots() -> list[str]:
    """Стол — код и журнал: `rw` сюда не выдаётся никому. Журнал — как
    его знает вызывающий (ROUNDTABLE_JOURNAL / CHOIR_JOURNAL: окно и
    edits.py ставят его детям), код — этот каталог chamber/; родитель —
    только если он и есть стол (рядом лежит roundtable.py). Родитель «по
    положению» не годится: тесты гоняют КОПИЮ chamber/ во временном
    каталоге, и весь каталог теста стал бы «столом» (первый прогон
    voices_http_test так и упал)."""
    here = Path(__file__).resolve().parent
    roots = [str(here)]
    if (here.parent / "roundtable.py").is_file():
        roots.append(str(here.parent))
    else:
        roots.append(str(here.parent / "journal"))
    for env in ("ROUNDTABLE_JOURNAL", "CHOIR_JOURNAL"):
        j = os.environ.get(env)
        if j:
            roots.append(os.path.normpath(os.path.expanduser(j)))
    return roots

TEMPLATE = """# Доступ голосов вне каталога проекта (читают комната, раунды,
# ревизия и кресло стола). Одна запись — одна строка, путь абсолютный:
#   r  /путь    чтение всем голосам
#   rw /путь    запись креслу исполнителя; остальным чтение
# Каталог самого проекта открыт всегда. Дом пользователя, состояние
# CLI голосов и стол (журнал, код) на запись не выдаются — такие строки
# отвергаются поимённо, остальные действуют.
"""


@dataclass
class Access:
    project: str | None = None
    file: str | None = None            # путь к ACCESS.txt, если он есть
    text: str = ""
    ro: list[str] = field(default_factory=list)
    rw: list[str] = field(default_factory=list)
    rejects: list[str] = field(default_factory=list)
    sha: str | None = None             # sha256 текста файла, 16 hex

    @property
    def empty(self) -> bool:
        return not (self.ro or self.rw)


def _norm(p: str) -> str:
    return os.path.normpath(str(p))


def _state_roots() -> list[str]:
    """Каталоги состояния ВСЕХ голосов (jail.STATE), без glob-масок:
    открыть их на чтение значит открыть чужие сессии всем."""
    out: list[str] = []
    for lst in jail.STATE.values():
        for d in lst:
            if "*" in d:
                continue
            out.append(_norm(os.path.expanduser(d)))
    return out


def _why_reject(mode: str, path: str, *, project: str | None,
                hide) -> str | None:
    """Причина отказа строке или None, если строка допустима."""
    if mode not in MODES:
        return f"режим «{mode}» — допустимы r и rw"
    if not os.path.isabs(path):
        return "путь не абсолютный"
    q = _norm(path)
    if not os.path.exists(q):
        return "путь не существует"
    real = os.path.realpath(q)
    if real != q:
        # Ссылка (или ссылка в предках): проверяем цель сейчас, bwrap
        # разрешит её потом — между ними цель подменяема (grok).
        return f"путь ведёт через ссылку — укажите настоящий: {real}"
    if jail._under(real, list(VIRTUAL_ROOTS)):
        return "виртуальное дерево (/dev /proc /sys /run) — bind снял бы изоляцию"
    if real in SHARED_EXACT:
        return "общий каталог целиком (/tmp, /var/tmp, /dev/shm) — не выдаётся; укажите подкаталог"
    home = os.path.realpath(jail.home())
    # Дом или его предок: bind поверх tmpfs дома вернул бы чужие
    # сессии и ключи целиком — ровно то, что jail.prefix отвергает
    # исключением; здесь — строкой, чтобы не ронять ход.
    if jail._under(home, [real]):
        return "дом голоса или его предок — открыл бы чужие сессии и ключи"
    if jail._under(real, [home]):
        rel = os.path.relpath(real, home)
        if rel.split(os.sep, 1)[0].startswith("."):
            return ("скрытая запись дома (~/.*) — ключи и состояние; "
                    "выдаётся только состояние своего CLI, и не отсюда")
    for root in hidden_roots():
        rr = os.path.realpath(root)
        if jail._under(real, [rr]):
            return f"каталог стола ({root}) — не выдаётся ни в каком режиме"
    for s in _state_roots():
        rs = os.path.realpath(s)
        if jail._under(real, [rs]) or jail._under(rs, [real]):
            return f"состояние CLI голосов ({s}) — не выдаётся"
    for h in hide:
        rh = os.path.realpath(str(h))
        # Внутри скрытого корня jail открыл бы путь ПОВЕРХ сокрытия
        # (ветка after) — чужие промпты и ответы. Предок скрытого —
        # допустим: сокрытие накладывается после ro (journal/ так и
        # открывается).
        if jail._under(real, [rh]):
            return f"внутри скрытого каталога стола ({h})"
    if project:
        rp = os.path.realpath(_norm(project))
        # rw — раньше «открыт и так»: rw на сам проект — это запись в
        # главный checkout мимо worktree, а не лишнее чтение.
        if mode == "rw" and jail._under(rp, [real]):
            return ("rw на каталог проекта или его предка — кресло пишет "
                    "только в worktree")
        if jail._under(real, [rp]):
            return ("rw внутри каталога проекта — запись в главный checkout "
                    "мимо worktree" if mode == "rw"
                    else "внутри каталога проекта — открыт и так")
    if mode == "rw":
        for t in table_roots():
            rt = os.path.realpath(t)
            if jail._under(real, [rt]) or jail._under(rt, [real]):
                return "rw на стол (журнал, код) — право записи голосу не выдаётся"
        for root in hidden_roots():
            # предок worktree/аренд/карантина: запись в чужие акты
            if jail._under(os.path.realpath(root), [real]):
                return f"rw на предка каталога стола ({root}) — запись в чужие акты и карантин"
    return None


def parse(text: str, *, project: str | None = None,
          hide=()) -> tuple[list[str], list[str], list[str]]:
    """(ro, rw, rejects). Пути нормализованы, без повторов; отвергнутые
    строки — с номером, текстом и причиной."""
    ro: list[str] = []
    rw: list[str] = []
    rejects: list[str] = []
    seen: dict[str, tuple[str, int]] = {}      # путь → (режим, строка)
    for n, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        mode = parts[0].lower()
        path = parts[1].strip() if len(parts) > 1 else ""
        if not path:
            rejects.append(f"строка {n} «{line}»: нет пути")
            continue
        why = _why_reject(mode, path, project=project, hide=hide)
        if why:
            rejects.append(f"строка {n} «{line}»: {why}")
            continue
        q = _norm(path)
        if q in seen:
            m0, n0 = seen[q]
            if m0 == mode:
                continue                    # чистый повтор — один bind
            # Разные режимы одного пути: кто победил, решал бы порядок
            # строк, и молча (grok, субагент). Отвергаем позднюю.
            rejects.append(f"строка {n} «{line}»: путь уже задан строкой {n0} "
                           f"как {m0} — уберите одну из строк")
            continue
        seen[q] = (mode, n)
        (rw if mode == "rw" else ro).append(q)
    return ro, rw, rejects


def from_snapshot(d: dict | None) -> Access:
    """Access из снимка, который окно передало креслу (--access-json):
    кресло НЕ перечитывает файл — иначе права Кодекса (writable_roots,
    сняты при сборке argv) и запись close разошлись бы при правке файла
    между ними (codex)."""
    d = d or {}
    return Access(project=d.get("project"), file=d.get("file"),
                  ro=[str(x) for x in d.get("ro") or []],
                  rw=[str(x) for x in d.get("rw") or []],
                  rejects=[str(x) for x in d.get("rejects") or []],
                  sha=d.get("sha") or None)


def snapshot(acc: Access) -> dict:
    return {"project": acc.project, "file": acc.file, "ro": list(acc.ro),
            "rw": list(acc.rw), "rejects": list(acc.rejects), "sha": acc.sha}


def load(project, *, hide=()) -> Access:
    """ACCESS.txt проекта; нет проекта или файла — пустой доступ."""
    if not project:
        return Access()
    p = Path(project)
    acc = Access(project=str(p))
    f = p / FILE
    if f.is_symlink() or not f.is_file():
        if f.is_symlink():
            acc.rejects.append(f"{FILE} — ссылка, а не файл: не читаю")
        return acc
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        acc.rejects.append(f"{FILE} не читается: {e}")
        return acc
    acc.file = str(f)
    acc.text = text
    acc.sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    acc.ro, acc.rw, rej = parse(text, project=str(p), hide=hide)
    acc.rejects.extend(rej)
    return acc


def ro_paths(acc: Access, *, chair: bool = False) -> list[str]:
    """Что открыть на чтение: вне кресла `rw` тоже читается — запись
    выдаётся только исполнителю."""
    return list(acc.ro) if chair else [*acc.ro, *acc.rw]


def rw_paths(acc: Access, *, chair: bool = False) -> list[str]:
    return list(acc.rw) if chair else []


def fact(acc: Access) -> dict:
    """Поле события: отпечаток действовавшего ACCESS.txt (как jail_sha —
    отпечаток аргументов клетки). Пустой доступ поля не даёт."""
    return {"access_sha": acc.sha} if (acc.sha and not acc.empty) else {}


def notice(acc: Access, *, chair: bool = False) -> str:
    """Строка пакета — всем одинаково (правило 1). Отвергнутых строк
    здесь нет: голосу говорят только то, что действительно открыто."""
    if acc.empty:
        return ""
    ro = ro_paths(acc, chair=chair)
    s = "ДОСТУП ПО ACCESS.txt ПРОЕКТА: "
    if chair:
        if ro:
            s += "вам открыто на чтение также: " + ", ".join(ro)
        if acc.rw:
            s += (("; " if ro else "") + "на ЗАПИСЬ (вне worktree — в диф "
                  "ревизии эти правки не попадут, называйте их в отчёте): "
                  + ", ".join(acc.rw))
        return s + ".\n\n"
    if ro:
        s += "агентным голосам открыто на чтение также: " + ", ".join(ro)
    if acc.rw:
        s += (("; " if ro else "") + "креслу исполнителя — на запись: "
              + ", ".join(acc.rw) + " (остальным на чтение)")
    return s + ". Голоса без диска отвечают по пакету и говорят об этом.\n\n"


def mark(acc: Access) -> str:
    """Строка стенограммы (после jail.mark): что дал ACCESS.txt и что в
    нём отвергнуто. Файла нет — пусто."""
    if acc.file is None and not acc.rejects:
        return ""
    items = [f"r {p}" for p in acc.ro] + [f"rw {p}" for p in acc.rw]
    s = "\n📂 ACCESS.txt: " + ("; ".join(items) if items else "записей нет")
    if acc.rejects:
        s += " · ОТВЕРГНУТО: " + "; ".join(acc.rejects)
    return s
