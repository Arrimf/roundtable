"""Правки проекта из окна: worktree, интент, вердикт, читатель маркеров.

Этап 1 СПЕКА-исполнитель-v1 (v2): окно выдаёт кресло исполнителя ОДНОМУ
голосу на ОДИН акт. Порядок такта:

    открыть   → чеканим эпоху, строим worktree от base_sha, пишем
                интент edit_open В ЛЕНТУ (до запуска — намерение видно,
                даже если дальше всё умрёт);
    работать  → executor_run.py держит аренду и водит CLI (см. его
                докстроку: он не песочница и не сторож зависаний);
    вердикт   → замок пал; close ЭТОЙ ЭПОХИ есть (в ленте или маркером)
                — штатно; нет — ВЫЛЕТ, worktree в карантин.

ЧИТАТЕЛЬ МАРКЕРОВ ЖИВЁТ ЗДЕСЬ — тот самый, которого ревизия ядра
называла недостающим («маркер декоративен, пока его никто не читает»,
codex). Маркер <act>.<epoch>.close.json пишет исполнитель, когда лента
недоступна; наше дело — допостить его в ленту и только потом снять.
Симметричный маркер <act>.<epoch>.crash.json пишет НАБЛЮДАТЕЛЬ, когда
лента недоступна ему самому: вылет при лежащей ленте иначе не оставлял
ни одной записи вовсе (нашёл kimi).

ГРАНИЦЫ ЧЕСТНО, БЕЗ ПРИУКРАС (ревизия 2026-09-01, все пять голосов):
— исполнителю-codex открывается запись в <base>/.git ЦЕЛИКОМ — иначе
  линкованный worktree несовместим с его песочницей (git-метаданные
  дерева лежат в <base>/.git/worktrees/, без этого не создать даже
  index.lock; проверено живым прогоном). Через .git достижимы и чужие
  ветки, и hooks, и config;
— «Bash(git:*)» у клода — это и git push, и `git -c alias.x='!cmd' x`,
  то есть произвольная команда под uid Автора. Узкой эта граница НЕ
  является (grok, codex);
— merge-гейт и пломба refs/heads/main — ЭТАП 3, их ещё НЕТ. Сегодня
  запись исполнителя вне worktree не обнаруживает никто, кроме глаз
  ревизии. Единственная механика прямо сейчас — авторство и намерение
  в ленте плюс изоляция по умолчанию (CLI направлен в своё дерево).
Это осознанная цена этапа 1 (вердикт раунда право-записи-v1:
«контроль = обнаружение, не запрет»), и она НАЗВАНА, а не спрятана.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leases                                            # noqa: E402

# Комната ищется: env → каталог choir/ рядом с этим файлом (раскладка
# публичного репозитория) → путь песочницы Автора. Порядок важен для
# публикации: свежий клон работает без настройки.
CHOIR = Path(os.environ.get("ROUNDTABLE_CHOIR")
             or (Path(__file__).resolve().parent / "choir"
                 if (Path(__file__).resolve().parent / "choir"
                     / "live.py").exists()
                 else Path.home() / "AiSandbox" / "Choir"))
WT_DIR = Path(os.environ.get("CHOIR_WT_DIR")
              or Path.home() / ".cache" / "choir" / "worktrees")

# ── Исполнители этапа 1 ──────────────────────────────────────────────
# Оба проверены живым прогоном 2026-09-01 (файл + коммит в worktree).
# Каждая запись строит argv CLI; cwd будет worktree (его ставит
# executor_run). Дефисная инъекция (задание, начинающееся с «-»,
# читается флагом CLI — вплоть до снятия ограничений; нашли все пять
# ревьюеров) закрывается ПО-РАЗНОМУ, у кого что есть: codex и claude
# понимают `--` (проверено живьём), kimi/grok/deepseek — нет, у них
# щит-префикс «ЗАДАНИЕ СТОЛА.»; у грока поверх — shlex.quote, потому
# что его строка внутри script — это shell.
#
# Авторство коммита: git-автор будет из конфига машины, НЕ голоса —
# честная запись авторства живёт в ленте (edit_open/edit_close несут
# voice), а --author к коммиту поставит merge-гейт на этапе 3.
#
# Кого здесь НЕТ: gemini — файлов не видит вовсе, он только ревьюер
# (спека, роли). Остальные пятеро сидят.
#
# ГРОК: pty даёт script -qec (без терминала его CLI не работает), сама
# командная строка внутри script — shell, поэтому задание уходит через
# shlex.quote — иначе кавычка в тексте исполняла бы команды под uid
# Автора. ПЕСОЧНИЦЫ НЕТ: --sandbox workspace-write на этой машине
# падает («deny list could not be prepared», Landlock) — проверено
# живым прогоном 2026-09-01; граница названа, а не спрятана: грок в
# кресле ограничен только словами задания, как и клод.
#
# DEEPSEEK: в кресле сидит НЕ голос стола (deepseek-http), а dsh
# --profile headless — агентный харнесс той же лаборатории. Обкатан
# живым прогоном 2026-09-01 (файл + коммит с первого раза). Щит-префикс
# и здесь: дефисное задание не должно читаться флагом.
#
# КИМИ СИДИТ С ПОМЕТКОЙ (наказ Автора 2026-09-01: «сажаем Кими с
# пометкой»). Пометка — три цены, названные вслух в подсказке окна:
#   1) МОНОПОЛЬНЫЙ ПО ЛИНИИ: у каждой его организации concurrency=1,
#      обёртка держит ворота ОДНОЙ линии весь акт (EDIT_GATES); вторая
#      линия, если жива её квота, остаётся столу — но всё встаёт в
#      очередь (уточнил codex: «совсем без Кими» было неправдой);
#   2) ДОРОГОЙ: правка — длинная агентная сессия, 15–20 внутренних
#      запросов с перечитыванием контекста (замер 2026-09-01, ~5 USD
#      за ночь ревизий);
#   3) обрывает длинные ответы (грабля CLAUDE.md) — большая правка
#      рискует недоделкой.
# Его лучший случай — НЕБОЛЬШАЯ точная правка, где вдумчивость
# окупается. Разделитель `--` его CLI не понимает (проверено: съедает
# текст как команду), поэтому дефисное задание прикрывает ЩИТ-ПРЕФИКС
# «ЗАДАНИЕ СТОЛА.» — он же даёт голосу контекст.
# Переопределения кресел ИЗ ОКНА (вкладка 🔧 coder): {voice: {model,
# effort, pool}}. Заполняет roundtable по VOICE_CFG ПЕРЕД open_edit —
# лямбды ниже строят argv в процессе окна, env спавна тут ни при чём.
# pool=False выводит голос из random-выбора (kimi дополнительно заперт
# EDIT_COSTLY — дорогой только явно).
EXEC_OVERRIDES: dict = {}


def _exec_over(voice: str, key: str) -> str:
    v = (EXEC_OVERRIDES.get(voice) or {}).get(key)
    return str(v) if v else ""


EDIT_VOICES = {
    "codex": lambda task, base_git: [
        "codex", "exec", "-s", "workspace-write",
        # json.dumps даёт валидную TOML basic string: путь с кавычкой
        # или бэкслешем иначе ломал конфиг или РАСШИРЯЛ песочницу
        # (нашли kimi, codex и grok независимо).
        "-c", f"sandbox_workspace_write.writable_roots="
              f"[{json.dumps(str(base_git))}]",
        *(["-c", f"model={_exec_over('codex', 'model')}"]
          if _exec_over("codex", "model") else []),
        # значение БЕЗ кавычек: -c 'k="v"' вешает codex до таймаута
        # (проверено живьём дважды)
        *(["-c", "model_reasoning_effort="
                 + _exec_over("codex", "effort")]
          if _exec_over("codex", "effort") else []),
        "--", task,
    ],
    "claude": lambda task, base_git: [
        "claude", "-p",
        "--allowedTools", "Write,Edit,Bash(git:*)",
        *(["--model", _exec_over("claude", "model")]
          if _exec_over("claude", "model") else []),
        "--", task,
    ],
    "kimi": lambda task, base_git: [
        str(Path.home() / ".kimi-code" / "bin" / "kimi"),
        # провайдер дописывается, как в live.py: селектор окна даёт
        # «kimi-k3», а CLI ждёт «moonshotai/kimi-k3» — голое имя ломало
        # запуск (нашли codex и grok)
        "-m", ((lambda m: m if "/" in m else "moonshotai/" + m)
               (_exec_over("kimi", "model") or "moonshotai/kimi-k3")),
        "-p",
        "ЗАДАНИЕ СТОЛА. " + task,
    ],
    "grok": lambda task, base_git: [
        "script", "-qec",
        "grok -p " + shlex.quote("ЗАДАНИЕ СТОЛА. " + task) + " --no-plan"
        + (" --effort " + shlex.quote(_exec_over("grok", "effort"))
           if _exec_over("grok", "effort") else ""),
        "/dev/null",
    ],
    # Модель dsh КРУТИТСЯ патч-слоем (нашлось по вопросу Автора «а
    # почему Дипсик с прочерками»): умолчание харнесса — v4-flash,
    # оверлей dsh-model-pro.yaml пересаживает на v4-pro (проверено
    # живьём: агент сам называет модель). Усилия у dsh нет по-прежнему.
    "deepseek": lambda task, base_git: [
        "dsh", "--profile", "headless",
        *(["--patch", str(Path(__file__).resolve().parent
                          / "dsh-model-pro.yaml")]
          if "pro" in (_exec_over("deepseek", "model") or "") else []),
        "ЗАДАНИЕ СТОЛА. " + task,
    ],
}

# Голоса, чей канал сериен: обёртка исполнителя берёт ворота организации
# на весь акт (executor_run --serial-gate). Смотрит /edit при спавне.
EDIT_GATES = {"kimi"}

# Дорогие кресла НЕ разыгрываются случайно — только явный выбор Автора.
# Обещание подсказки обеспечено механикой, а не текстом (правило 8.5):
# random.choice по всем однажды посадил бы Кими на грошовую правку.
EDIT_COSTLY = {"kimi"}


def random_pool() -> list:
    """Кто разыгрывается случайно: умеющие − дорогие − снятые галочкой
    пула (вкладка coder). Пустой список — random невозможен, /edit
    честно откажет."""
    return sorted(v for v in EDIT_VOICES
                  if v not in EDIT_COSTLY
                  and (EXEC_OVERRIDES.get(v) or {}).get("pool") is not False)


def _git(repo: Path, *args, timeout: int = 60):
    """git с rc-проверкой; (None, why) = не ответил."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"git {' '.join(args)}: {e}"
    if r.returncode != 0:
        return None, f"git {' '.join(args)}: rc={r.returncode} " \
                     f"{r.stderr.strip()[:200]}"
    return r.stdout, ""


def post(kind: str, text: str, **extra) -> dict:
    sys.path.insert(0, str(CHOIR))
    import live                                          # noqa: PLC0415
    return live.post("roundtable", kind, text, **extra)


class EditRefused(RuntimeError):
    """Правка не открыта — причина в тексте, следов не осталось."""


def open_edit(project: Path, task: str, voice: str,
              files: list[str] | None = None) -> dict:
    """Открыть акт правки: эпоха → worktree → интент в ленту.

    Порядок именно такой. Эпоха первой — она дешёвая и монотонная,
    брошенная эпоха ничего не ломает. Worktree до интента — если git
    откажет, в ленте не останется намерения без дерева. Интент
    ПОСЛЕДНИМ и ДО запуска исполнителя; не записался — дерево и ветка
    УБИРАЮТСЯ: worktree без интента не существует ни для кого, и
    оставить его значило бы копить безымянные деревья (нашли grok и
    codex). Восстановление после рестарта окна — recover_edits().
    """
    if voice not in EDIT_VOICES:
        raise EditRefused(f"исполнитель {voice!r} не умеет правки "
                          f"(умеют: {', '.join(EDIT_VOICES)})")
    task = (task or "").strip()
    if not task:
        raise EditRefused("пустое задание")
    files = [f.strip() for f in (files or []) if f and f.strip()]
    for f in files:
        if f.startswith("/") or ".." in f.split("/"):
            raise EditRefused(f"скоуп — пути ВНУТРИ проекта, без /"
                              f" в начале и без ..: {f!r}")
    project = Path(project).resolve()
    if not project.is_dir():
        raise EditRefused(f"нет такого каталога: {project}")
    top, err = _git(project, "rev-parse", "--show-toplevel")
    if top is None or Path(top.strip()).resolve() != project:
        raise EditRefused(f"--project должен быть корнем git-репозитория: "
                          f"{err or top}")
    # Ветка базы — В ИНТЕНТ: приёмка двигает ИМЕННО её по имени.
    # Прежде гейт брал «текущую ветку checkout» в момент merge — detached
    # HEAD или переключение Автора на другую ветку с тем же sha двинуло
    # бы не то, вплоть до создания refs/heads/HEAD (нашли codex, grok и
    # deepseek независимо).
    branch, err = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch or "").strip()
    if not branch or branch == "HEAD":
        raise EditRefused("checkout проекта в detached HEAD — правке "
                          "некуда возвращаться; встаньте на ветку")
    base_sha, err = _git(project, "rev-parse", "HEAD")
    if base_sha is None:
        raise EditRefused(f"base_sha не взят: {err}")
    base_sha = base_sha.strip()

    act = uuid.uuid4().hex[:12]
    epoch = leases.mint_epoch()          # EpochCorrupt летит наружу: без
    #                                      эпох кресло не выдаётся вовсе
    WT_DIR.mkdir(parents=True, exist_ok=True)
    wt = WT_DIR / act
    out, err = _git(project, "worktree", "add", "-b", f"act/{act}",
                    str(wt), base_sha, timeout=120)
    if out is None:
        raise EditRefused(f"worktree не построен: {err}")

    def _undo():
        _git(project, "worktree", "remove", "--force", str(wt))
        _git(project, "branch", "-D", f"act/{act}")

    base_git, err = _git(project, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
    if base_git is None:
        _undo()
        raise EditRefused(f"git-common-dir не взят: {err}")

    cmd = EDIT_VOICES[voice](task, base_git.strip())
    # seat — ЧЕМ исполняется голос (бинарь кресла): у deepseek в кресле
    # dsh-харнесс, а не HTTP-голос стола, и без метки читатель ленты их
    # не различит (нашли deepseek и kimi). cmd в событии полный, seat —
    # его читаемая выжимка.
    seat = Path(cmd[0]).name
    try:
        post("edit_open",
             f"правка {act} [{voice}]: {task[:200]}",
             act=act, epoch=epoch, voice=voice, project=str(project),
             base_sha=base_sha, worktree=str(wt), task=task[:1000],
             branch=branch, seat=seat,
             # Скоуп — ЗАЯВЛЕНИЕ исполнительского намерения (спека п.1):
             # гейт сверит диф с ним при приёмке. Пусто — «не заявлен»,
             # и событие merge честно скажет, что сверки не было.
             files=sorted(files) if files else None,
             # Команда — в ленту: через месяц «что именно запускали» не
             # восстановить из памяти окна (правило 8.5).
             cmd=" ".join(shlex.quote(c) for c in cmd))
    except Exception as e:               # noqa: BLE001
        _undo()
        raise EditRefused(f"интент не записался в ленту ({e}) — "
                          f"акт не открыт, дерево убрано") from e
    return {"act": act, "epoch": epoch, "worktree": wt,
            "base_sha": base_sha, "voice": voice, "cmd": cmd,
            "project": project}


# ── Вердикт и читатель маркеров ──────────────────────────────────────
# Авторы, чьим event'ам close верит вердикт. Под одним uid подделать
# можно любую строку (обнаружение, не запрет) — но случайный чужой
# kind=edit_close от голоса в разговоре не должен закрывать акт.
_CLOSE_AUTHORS = {"choir", "roundtable"}


def _feed_events(tail_bytes: int | None):
    """События ленты, свежие первыми. None = читать ЦЕЛИКОМ.

    Хвост годится для быстрых проверок; вердикт и репост обязаны
    читать всё: close, уехавший за обрезку при болтливой ленте, делал
    штатный акт «вылетом», а репост — дублём (нашли deepseek, kimi,
    grok — каждый со своей стороны).
    """
    live_path = CHOIR / "live.jsonl"
    try:
        with live_path.open("rb") as f:
            if tail_bytes is not None:
                size = live_path.stat().st_size
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    f.readline()         # добить обрезанную строку: json
                    #                      на границе seek иначе терялся
                    #                      молча (gemini)
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return
    for line in reversed(chunk.splitlines()):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def close_in_feed(act: str, epoch: int, *, whole: bool = True):
    """Событие edit_close ЭТОЙ ЭПОХИ от доверенного автора, или None."""
    for e in _feed_events(None if whole else 512_000):
        if (e.get("kind") == "edit_close" and e.get("act") == act
                and e.get("epoch") == epoch
                and e.get("author") in _CLOSE_AUTHORS):
            return e
    return None


def marker_path(act: str, epoch: int, kind: str = "close") -> Path:
    return leases.LEASE_DIR / f"{act}.{epoch}.{kind}.json"


# Поля события, которые никогда не берутся из файла маркера: их ставит
# лента или репост. Иначе kind/text в маркере (мусор, подделка, будущий
# формат) роняли post TypeError'ом, и sweep бился об этот маркер каждые
# 60 с вечно (нашли deepseek и gemini).
_MARKER_DROP = {"kind", "text", "author", "id", "ts", "schema", "live",
                "reposted_from_marker"}


def repost_marker(act: str, epoch: int):
    """Маркер → лента → только потом снять. Возвращает событие или None.

    Порядок незыблем: снять маркер до удачного поста значит потерять
    единственную запись о честном закрытии — ровно то, что ревизия ядра
    ловила у уборки дважды (правило 4: отказ фиксируется, не стирается).
    """
    m = marker_path(act, epoch)
    try:
        raw = json.loads(m.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if raw.get("epoch") != epoch or raw.get("act") != act:
        # Имя и содержимое разошлись — это не наш close, а мусор;
        # трогать не нам. Читатель обязан сверять эпоху (докстрока
        # executor_run) — вот эта сверка.
        return None
    already = close_in_feed(act, epoch)
    if already is None:
        text = str(raw.get("text") or f"правка {act}: close из маркера")
        ev = {k: v for k, v in raw.items() if k not in _MARKER_DROP}
        try:
            already = post("edit_close", text + " [из маркера: лента "
                           "была недоступна исполнителю]",
                           reposted_from_marker=True, **ev)
        except Exception as e:           # noqa: BLE001 — лента всё ещё
            print(f"репост маркера {m.name} не удался: {e}",
                  file=sys.stderr)      # лежит: маркер НЕ снимаем
            return None
    try:
        m.unlink()
    except OSError:
        pass
    return already


def _post_or_marker(kind: str, text: str, **ev):
    """Пост в ленту; лента лежит — маркер crash рядом с замком.

    Вылет при недоступной ленте иначе не оставлял НИ ОДНОЙ записи:
    post падал, исключение глотала нить наблюдателя, sweep читает
    только close-маркеры — и акт исчезал бесследно (нашёл kimi).
    """
    try:
        return post(kind, text, **ev)
    except Exception as e:               # noqa: BLE001
        m = marker_path(ev.get("act", "x"), ev.get("epoch", 0), "crash")
        try:
            m.parent.mkdir(parents=True, exist_ok=True)
            m.write_text(json.dumps(dict(ev, text=text),
                                    ensure_ascii=False), encoding="utf-8")
            print(f"лента недоступна ({e}); вылет записан маркером {m.name}",
                  file=sys.stderr)
        except OSError as e2:
            print(f"ни лента, ни маркер вылета: {e} / {e2}", file=sys.stderr)
        return None


def verdict_on_drop(act: str, epoch: int, worktree: Path,
                    voice: str, why: str | None):
    """Замок пал — сказать ленте, чем кончился акт.

    Возвращает записанное событие (или None, если и лента, и маркер
    отказали). Сам НИЧЕГО не решает о worktree: штатный или нет, дерево
    остаётся на диске — его судьбу (merge, продолжить, выбросить)
    решает Автор через гейт. «Карантин» — статус в ленте, а не
    перемещение файлов.
    """
    if why:
        # Наблюдатель не дождался захвата или сломался. Это ещё НЕ
        # вердикт: медленный старт executor (три git-проверки до
        # аренды) мог просто не уложиться в грацию — тогда замок уже
        # держится, и кричать «кресло не занято» значило бы дать два
        # противоречивых вердикта одному акту (нашли grok и gemini).
        # Решение принимает вызывающий (re-watch); мы честно скажем
        # только про случай, когда замка действительно нет.
        if leases.is_held(act):
            return None                  # живой — пусть перевзводят
        return _post_or_marker("edit_crash",
                               f"правка {act} [{voice}]: кресло не занято "
                               f"({why}); worktree {worktree} остаётся "
                               f"для разбора",
                               act=act, epoch=epoch, voice=voice,
                               status="no_start", worktree=str(worktree))
    ev = close_in_feed(act, epoch) or repost_marker(act, epoch)
    if ev is not None:
        return ev                        # штатно: close уже в ленте
    prev = _crash_in_feed(act, epoch)
    if prev is not None:
        return prev                      # вылет уже записан — не дублировать
    out = _post_or_marker("edit_crash",
                           f"правка {act} [{voice}]: ВЫЛЕТ — замок пал, "
                           f"close эпохи {epoch} нет ни в ленте, ни "
                           f"маркером. Worktree {worktree} в карантине "
                           f"(на диске, не тронут)",
                           act=act, epoch=epoch, voice=voice,
                           status="crash", worktree=str(worktree))
    if out is not None:
        # Вердикт в ленте — черновик-маркер (если успел лечь при
        # прежней попытке с лежащей лентой) больше не нужен: sweep
        # иначе донёс бы его вторым edit_crash той же эпохи (codex).
        marker_path(act, epoch, "crash").unlink(missing_ok=True)
    return out


def _crash_in_feed(act: str, epoch: int):
    """Уже записанный edit_crash этой эпохи, или None."""
    for e in _feed_events(None):
        if (e.get("kind") == "edit_crash" and e.get("act") == act
                and e.get("epoch") == epoch
                and e.get("author") in _CLOSE_AUTHORS):
            return e
    return None


# ── Восстановление после рестарта окна ───────────────────────────────

def open_acts_in_feed():
    """Акты с edit_open БЕЗ парного edit_close/edit_crash той же эпохи.

    Лента читается целиком: это восстановление, оно редкое и обязано
    быть полным — «хвоста достаточно» здесь стоило бы вечно открытых
    актов (первый набросок этой механики так и врал, нашли все пятеро
    ревьюеров: после рестарта окна вердикт не доносил никто).
    """
    closed, opened = set(), {}
    for e in _feed_events(None):         # свежие первыми
        k = e.get("kind")
        if k in ("edit_close", "edit_crash"):
            closed.add((e.get("act"), e.get("epoch")))
        elif k == "edit_open":
            key = (e.get("act"), e.get("epoch"))
            if key not in closed and key not in opened:
                opened[key] = e
    return list(opened.values())


def recover_edits(watch_fn, *, grace_s: float = 240.0,
                  now=None) -> int:
    """Донести вердикты актам, которых окно не сторожит.

    watch_fn(act, epoch, worktree, voice) — взводит наблюдателя окна
    (передаётся снаружи, чтобы edits не знал про нити roundtable).
    Для каждого открытого акта из ленты:
      замок держится      → взвести наблюдателя заново (окно перезапущено,
                            исполнитель жив и работает);
      замок пал, акт СТАР → вердикт прямо сейчас (close/маркер/crash);
      замок пал, акт МОЛОД (моложе grace_s) → взвести наблюдателя и
                            ждать: executor до аренды делает git-пробы,
                            и рестарт окна в эту минуту иначе объявлял
                            вылет ЖИВОМУ исполнителю, который затем
                            писал close — два вердикта одному акту
                            (нашёл codex вторым кругом).

    Ошибка на одном акте не рвёт цикл: одно кривое событие иначе
    оставляло без вердикта все остальные (нашёл deepseek).
    """
    n = 0
    now = now if now is not None else __import__("time").time()
    for e in open_acts_in_feed():
        try:
            act, epoch = e.get("act"), e.get("epoch")
            voice = e.get("voice", "?")
            wt = Path(e.get("worktree", ""))
            if not act or not isinstance(epoch, int):
                continue
            young = False
            ts = e.get("ts")
            if isinstance(ts, str):
                try:
                    from datetime import datetime
                    t0 = datetime.fromisoformat(ts).timestamp()
                    young = (now - t0) < grace_s
                except ValueError:
                    pass
            if leases.is_held(act) or young:
                try:
                    watch_fn(act, epoch, wt, voice)
                    n += 1
                except ValueError:
                    pass                 # наблюдатель уже стоит
            else:
                verdict_on_drop(act, epoch, wt, voice, None)
                n += 1
        except Exception as err:         # noqa: BLE001
            print(f"recover {e.get('act')}: {err}", file=sys.stderr)
    return n


def sweep(active_ids) -> int:
    """Уборка закрытых актов + репост осиротевших маркеров (close и crash).

    Осиротевший маркер = исполнитель (или наблюдатель) писал при
    недоступной ленте. Репостим и снимаем; после этого следующий
    проход уборки унесёт и стенограф.
    """
    n = leases.sweep_closed(active_ids)
    if not leases.LEASE_DIR.exists():
        return n
    for m in (list(leases.LEASE_DIR.glob("*.close.json"))
              + list(leases.LEASE_DIR.glob("*.crash.json"))
              + list(leases.LEASE_DIR.glob("*.merge.json"))
              + list(leases.LEASE_DIR.glob("*.rebase.json"))):
        mm = re.fullmatch(r"(.+)\.(\d+)\.(close|crash|merge|rebase)\.json",
                          m.name)
        if not mm:
            continue
        act, epoch, kind = mm.group(1), int(mm.group(2)), mm.group(3)
        if act in (active_ids or ()):
            continue
        if leases.is_held(act):
            continue                     # акт ещё живёт — не наш ход
        if kind == "close":
            repost_marker(act, epoch)
        elif kind in ("merge", "rebase"):
            # merge случился при лежащей ленте (гейт оставил маркер) —
            # донести как edit_merge, той же дисциплиной, что close.
            try:
                raw = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if raw.get("act") != act or raw.get("epoch") != epoch:
                continue
            text = str(raw.get("text") or f"правка {act}: событие гейта")
            ev = {k: v for k, v in raw.items() if k not in _MARKER_DROP}
            try:
                post(f"edit_{kind}", text + " [из маркера: лента была "
                     "недоступна гейту]", reposted_from_marker=True, **ev)
                m.unlink(missing_ok=True)
            except Exception as e:       # noqa: BLE001
                print(f"репост merge-маркера {m.name}: {e}",
                      file=sys.stderr)
        else:
            try:
                raw = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if raw.get("act") != act or raw.get("epoch") != epoch:
                continue                 # имя и содержимое разошлись —
                #                          мусор, не наш (та же сверка,
                #                          что у close; нашёл codex)
            if _crash_in_feed(act, epoch) is not None:
                m.unlink(missing_ok=True)
                continue                 # вылет уже в ленте — не дублировать
            text = str(raw.get("text") or f"правка {act}: вылет")
            ev = {k: v for k, v in raw.items() if k not in _MARKER_DROP}
            try:
                post("edit_crash", text + " [из маркера: лента была "
                     "недоступна наблюдателю]",
                     reposted_from_marker=True, **ev)
                m.unlink(missing_ok=True)
            except Exception as e:       # noqa: BLE001
                print(f"репост crash-маркера {m.name}: {e}", file=sys.stderr)
    return n
