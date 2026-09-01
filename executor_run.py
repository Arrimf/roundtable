#!/usr/bin/env python3
"""Обёртка исполнителя: держит аренду, водит CLI, честно закрывает акт.

Роль по СПЕКА-исполнитель-v1.md (v2): «процесс исполнителя» в механике
аренд — ЭТОТ процесс. Он берёт flock на весь акт, запускает CLI-голос в
worktree, ждёт его и пишет событие close В ЛЕНТУ ДО того, как отпустит
замок. Наблюдатель окна различает исходы ровно по этому порядку:

    замок упал, close ЭТОЙ ЭПОХИ есть → штатное завершение (даже если
                               CLI вернул ошибку — это ЕГО ошибка,
                               акт жив)
    замок упал, такого close нет      → ВЫЛЕТ: обёртку убили (kill -9,
                               OOM, выключение) — worktree в карантин

Слова «этой эпохи» не украшение: акт можно продолжить, и close от
прошлой попытки того же акта прочитанный как свой закрыл бы вылет
задним числом. Поэтому и маркер, и стенограф носят эпоху в имени.

Смерть CLI при живой обёртке вылетом НЕ является: обёртка увидит rc и
закроет акт статусом error — правило 4, отказ фиксируется как есть.

ЧЕМ ОБЁРТКА НЕ ЯВЛЯЕТСЯ (потребовали все четверо ревьюеров):
— не песочница: cwd=worktree направляет CLI, но записи ВНЕ дерева не
  ловит — это работа гейта (сверка дифа с заявленным) и пломбы
  refs/heads/main, спека §5, и там это обнаружение, не запрет;
— не сторож зависаний: таймаута у wait() НЕТ НАМЕРЕННО (спор
  deepseek↔codex решён по спеке §4: автоубийства нет, зависание ловит
  наблюдатель прогресса ОКНА — mtime/коммиты worktree — и жёлтый
  сигнал Автору; в этих файлах его нет, он часть интеграции окна);
— не гарант доставки close В ЛЕНТУ: close всегда сперва ложится
  МАРКЕРОМ рядом с замком (<act>.<epoch>.close.json, fsync), и только потом
  идёт в ленту; удачная запись маркер снимает. ЧИТАТЕЛЯ МАРКЕРА ПОКА
  НЕТ — это работа окна на шаге интеграции (нашёл codex: «маркер
  декоративен, пока его никто не читает»). До тех пор отказ ленты
  выглядит для наблюдателя вылетом, а разбор лежит на человеке:
  маркер на диске есть, и по нему видно, что ход закрылся честно.
  Читатель обязан сверять epoch внутри маркера — имя акта может
  повториться, эпоха нет.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
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


def post(kind: str, text: str, **extra):
    """Событие в ленту — через live.post, как всё в этом проекте."""
    sys.path.insert(0, str(CHOIR))
    import live                                          # noqa: PLC0415
    return live.post("choir", kind, text, **extra)


# GIT_DIR/GIT_WORK_TREE из окружения окна перенаправили бы наш git в
# ЧУЖОЙ репозиторий: --show-toplevel остаётся worktree'ом (проверка
# зелёная), а rev-parse HEAD отдаёт sha другого репо — и он уезжает в
# ленту как достоверный (нашёл grok). Поэтому все GIT_* снимаем.
_GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git_at(wt: Path, *args) -> str | None:
    """git в дереве wt: с таймаутом, без stdin, без GIT_*-окружения.

    Таймаут и закрытый stdin: у CLI таймаута нет НАМЕРЕННО, а здесь его
    отсутствие было недосмотром — зависший git (битый индекс, чужой
    lock, pinentry на вводе) держал бы аренду вечно, и акт выглядел
    живым для замка и мёртвым для монитора (субагент). OSError (нет
    бинаря, нет прав) тоже наш: без него успешная работа закрывалась бы
    как «обёртка упала» вместо честного «неизвестно» (deepseek)."""
    try:
        r = subprocess.run(["git", "-C", str(wt), *args],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60,
                           env=_GIT_ENV)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"git {' '.join(args)}: не выполнился ({e})", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"git {' '.join(args)}: rc={r.returncode} "
              f"{r.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return r.stdout


def _close_act(a, *, status: str, rc: int, text: str, **extra):
    """Событие close — в ленту, а ДО того маркером на диск.

    Весь различитель «штатно/вылет» стоит на том, что close вообще
    случится. А live.post пишет в файл журнала и может упасть (диск,
    права, пломба) — и тогда молчание обёртки прочтётся наблюдателем
    как ВЫЛЕТ, хотя работа сделана и закоммичена (нашёл deepseek).

    Поэтому порядок такой: сперва маркер <act>.close.json рядом с
    замком (tmp→replace, чтобы половинки не было), потом лента. Ушло в
    ленту — маркер убираем. Инвариант для окна: **маркер лежит ⇒ close
    в ленте может отсутствовать**, допости его сам.
    """
    ev = dict(act=a.act, epoch=a.epoch, voice=a.voice,
              status=status, rc=rc, **extra)
    # Эпоха — В ИМЕНИ. Прежде маркер звался <act>.close.json, и «маркер
    # прошлой жизни того же акта» приходилось стирать на старте, а
    # уборка стирала его вовсе — так честно закрытый ход с упавшей
    # лентой превращался в вылет без следов (субагент). С эпохой в
    # имени путаницы нет: продолжение того же акта — всегда новая
    # эпоха, значит новый файл; старый остаётся уликой.
    marker = leases.LEASE_DIR / f"{a.act}.{a.epoch}.close.json"
    wrote = False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(marker.name + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(dict(ev, text=text), ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())        # маркер обязан пережить питание
        os.replace(tmp, marker)
        dfd = os.open(str(marker.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)               # и само имя тоже
        finally:
            os.close(dfd)
        wrote = True
    except OSError as e:
        # Маркер не лёг — это не повод молчать в ленту: остаётся шанс,
        # что журнал жив. Но и делать вид, что «close записан всегда»,
        # нельзя — про отказ говорим вслух (codex).
        print(f"маркер close не записан: {e}", file=sys.stderr)
    try:
        post("edit_close", text, **ev)
    except Exception as e:              # noqa: BLE001 — лента шире OSError
        print(f"close в ленту не ушёл ({e}); маркер: {marker}",
              file=sys.stderr)
        return
    if wrote:
        try:
            marker.unlink()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--act", required=True, help="act_id правки")
    ap.add_argument("--epoch", required=True, type=int,
                    help="эпоха аренды (чеканит окно)")
    ap.add_argument("--worktree", required=True, help="каталог worktree")
    ap.add_argument("--voice", required=True, help="имя голоса-исполнителя")
    ap.add_argument("--cmd-json", required=True,
                    help="argv CLI исполнителя, JSON-массивом")
    ap.add_argument("--serial-gate", default="",
                    help="имя голоса, чей канал сериен (kimi): обёртка "
                         "займёт ворота организации НА ВЕСЬ акт")
    a = ap.parse_args()

    # --act проверяем ПЕРВЫМ: кривое имя роняло lease_path трейсбеком
    # мимо всех обработчиков — аренда не взята, close не написан, окно
    # видит молчание (нашёл субагент-ревьюер).
    try:
        leases.lease_path(a.act)
    except ValueError as e:
        print(f"--act: {e}", file=sys.stderr)
        return 2
    if a.epoch <= 0:
        print("--epoch: положительное целое (эпоху чеканит окно)",
              file=sys.stderr)
        return 2

    wt = Path(a.worktree)
    if not wt.is_dir():
        print(f"worktree не существует: {wt}", file=sys.stderr)
        return 2
    # Каталог обязан быть корнем ПРИСТЁГНУТОГО worktree. Проверка
    # «это top-level дерева» была недостаточной: главный checkout ей
    # удовлетворяет полностью, и субагент-ревьюер прогнал это на живом
    # живом репозитории Автора — head и dirty его дерева уехали в
    # ленту как «результат правки». Линкованный worktree отличается от
    # главного тем, что --git-dir у него свой, а --git-common-dir общий.
    top = _git_at(wt, "rev-parse", "--show-toplevel")
    if top is None or Path(top.strip() or ".").resolve() != wt.resolve():
        print(f"--worktree должен быть корнем git-дерева: {wt} → "
              f"{(top or '').strip() or 'git не ответил'}", file=sys.stderr)
        return 2
    gd = _git_at(wt, "rev-parse", "--absolute-git-dir")
    gc = _git_at(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if gd is None or gc is None:
        print("git не сказал, worktree это или главный checkout — "
              "отказ (лучше не начать, чем править main)", file=sys.stderr)
        return 2
    if Path(gd.strip()).resolve() == Path(gc.strip()).resolve():
        print(f"{wt} — ГЛАВНЫЙ checkout, а не отдельный worktree. Правка "
              f"пошла бы прямо в рабочее дерево Автора; гейту нечего "
              f"было бы сверять", file=sys.stderr)
        return 2
    try:
        cmd = json.loads(a.cmd_json)
    except ValueError:
        cmd = None
    # if, а не assert: под `python -O` assert вырезается, и в Popen ушёл
    # бы мусор из аргумента (нашёл codex).
    if not (isinstance(cmd, list) and cmd
            and all(isinstance(x, str) for x in cmd)):
        print("cmd-json: нужен непустой JSON-массив строк", file=sys.stderr)
        return 2

    # Аренда — ДО запуска CLI и на весь акт. Занято — второй исполнитель
    # на тот же акт невозможен, и это не гонка, а ответ: кресло одно.
    try:
        lease = leases.acquire(a.act)
    except leases.LeaseBusy:
        print(f"аренда {a.act} занята — кресло не свободно", file=sys.stderr)
        return 3

    # Эпоха в имени бережёт улики ровно до тех пор, пока окно не
    # ошибётся числом. Ошибка должна быть ГРОМКОЙ: маркер или стенограф
    # этой же эпохи означают, что такой ход уже был, и второй запуск
    # затёр бы его следы (нашёл субагент-ревьюер).
    stale = ([leases.LEASE_DIR / f"{a.act}.{a.epoch}.close.json"]
             + [leases.LEASE_DIR / f"edit-{a.act}.{a.epoch}.log"])
    busy = [f for f in stale if f.exists()]
    if busy:
        print(f"эпоха {a.epoch} акта {a.act} уже использована: "
              f"{', '.join(f.name for f in busy)}. Чеканьте новую — "
              f"иначе улики прошлого хода будут затёрты", file=sys.stderr)
        lease.close()
        return 2

    # С этой точки замок наш, и выйти молча нельзя НИ ПРИ КАКОМ исходе:
    # молчание обёртки читается наблюдателем как вылет. Своя поломка —
    # тоже исход, и он известен, значит закрывается честно (правило 4),
    # а не притворяется загадочной смертью: worktree останется гейту, а
    # не уедет в карантин «неизвестно что» (потребовал deepseek).
    # Ctrl-C и SystemExit — тоже исходы, а они BaseException: ловя одну
    # Exception, комментарий выше врал (нашёл kimi). Прерванный акт
    # закрывается статусом error и НЕ уезжает в карантин: причина
    # известна, работа в worktree цела. Дальше исключение летит своим
    # ходом — прятать прерывание нельзя.
    try:
        return _run(a, wt, cmd, lease)
    except BaseException as e:          # noqa: BLE001
        import traceback
        _close_act(a, status="error", rc=-2,
                   text=f"правка {a.act}: обёртка прервана/упала: "
                        f"{type(e).__name__}: {e}",
                   tail=traceback.format_exc()[-1500:])
        if isinstance(e, Exception):
            return 1
        raise
    finally:
        lease.close()                   # идемпотентен


def _run(a, wt: Path, cmd: list, lease) -> int:
    t0 = time.monotonic()
    # Каталог — У LEASES, не своей формулой: две формулы одного пути
    # однажды разъедутся молча (нашёл kimi).
    leases.LEASE_DIR.mkdir(parents=True, exist_ok=True)
    # Стенограф — тоже с эпохой: «продолжить» после вылета затирало
    # единственную улику того, что делал упавший CLI, ровно в момент
    # разбора вылета (субагент).
    log_path = leases.LEASE_DIR / f"edit-{a.act}.{a.epoch}.log"

    my_pid = os.getpid()

    # libc резолвим ЗАРАНЕЕ, в родителе: import и CDLL внутри
    # преexec_fn идут ПОСЛЕ fork, где чужая нить могла унести
    # import-lock или замок буфера stderr — и потомок висит до вечности
    # (нашёл kimi). По той же причине в _child_prep нет ни одного
    # print: между fork и exec печатать нельзя.
    try:
        import ctypes
        _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError as e:
        _libc = None
        print(f"libc не открылась ({e}): PDEATHSIG не будет, сирота "
              f"переживёт обёртку", file=sys.stderr)

    def _child_prep():
        # СВОЯ сессия — прежний комментарий это обещал, а флага не было:
        # «отозвать» по pgid прибил бы группу обёртки вместе с окном
        # (нашёл kimi). Плюс PDEATHSIG: умерла обёртка (kill -9) —
        # ядро добивает CLI. ВНУКИ ЭТИМ НЕ ЗАКРЫТЫ: PDEATHSIG достаётся
        # только прямому потомку, а настоящий голос плодит детей —
        # проверено прогоном (grok, субагент). Для них у окна есть pgid
        # в meta: своя сессия делает killpg точным, и звать его —
        # работа окна, здесь этого нет.
        #
        # ЧЕСТНО О ГРАНИЦАХ (потребовал codex): PDEATHSIG достаётся
        # ТОЛЬКО прямому потомку — внуки, которых CLI наплодил сам,
        # переживут его. Для них у окна есть pgid из meta-файла: своя
        # сессия делает killpg точным. Второе: между fork и prctl есть
        # щель — родитель может умереть раньше, чем флаг поставлен, и
        # сигнала уже не будет; поэтому сразу после prctl сверяем
        # getppid и уходим сами, если родитель сменился.
        os.setsid()
        if _libc is not None:
            _libc.prctl(1, 9, 0, 0, 0)      # PR_SET_PDEATHSIG, SIGKILL
        if os.getppid() != my_pid:
            os._exit(127)       # родитель умер до prctl — не сиротеть

    # ВОРОТА СЕРИЙНОГО КАНАЛА — на весь акт, ПОСЛЕ аренды. У организации
    # Кими concurrency=1: CLI в кресле делает десятки внутренних
    # запросов, и чужой раунд, влезший между ними, получил бы 429 с
    # списанием токенов за отбитые запросы (грабля трёх суток «медленного
    # Кими», CLAUDE.md). Порядок «кресло → ворота» намеренный: наоборот
    # мы держали бы единственную линию Кими, ещё не имея права работать.
    # Ожидание — вслух в stderr (лог акта): молчащая очередь неотличима
    # от зависшего вызова. Вылет в очереди роняет и flock ворот, и
    # аренду — ядро снимает оба, наблюдатель окна видит вылет штатно.
    gate_ctx = None

    def _gate_release():
        # Идемпотентно и зовётся из finally ниже: прежний код отпускал
        # ворота только на удачном пути и в OSError — любое другое
        # исключение оставляло канал занятым до смерти процесса, а
        # вместе с ним живой CLI (нашли codex и deepseek в один голос).
        nonlocal gate_ctx
        if gate_ctx is not None:
            g, gate_ctx = gate_ctx, None
            try:
                g.__exit__(None, None, None)
            except Exception as e:                   # noqa: BLE001
                print(f"ворота не отпустились чисто: {e}", file=sys.stderr)

    if a.serial_gate:
        sys.path.insert(0, str(CHOIR))
        from serial_gate import first_free_gate      # noqa: PLC0415
        gates = [a.serial_gate]
        if a.serial_gate == "kimi":
            # FAIL-CLOSED: не прочитались каналы — кресло НЕ выдаётся.
            # Прежний фолбэк на ворота с именем «kimi» брал замок, с
            # которым раунды не пересекаются, — серийность деградировала
            # молча, и 429 вернулись бы под видом починенного (deepseek).
            try:
                from channels import kimi_channels   # noqa: PLC0415
                gates = sorted({c["gate"] for c in kimi_channels()})
                if not gates:
                    raise RuntimeError("пустой список каналов")
            except Exception as e:                   # noqa: BLE001
                _close_act(a, status="error", rc=-3,
                           text=f"правка {a.act}: каналы Кими не "
                                f"прочитаны ({e}) — кресло не выдано, "
                                f"чтобы не жечь чужой раунд 429-ми")
                return 1
        gate_ctx = first_free_gate(
            gates,
            on_wait=lambda sec, who: print(
                f"кресло ждёт ворота {gates} уже {sec} с (занято: {who})",
                file=sys.stderr))
        gate_ctx.__enter__()

    try:
        # Вывод — В ФАЙЛ, не в память: communicate с PIPE копил весь
        # стенограф болтливого CLI и падал по памяти — ложный вылет
        # хорошей работы (нашёл kimi).
        with log_path.open("wb") as lf:
            proc = subprocess.Popen(cmd, cwd=str(wt),
                                    stdin=subprocess.DEVNULL,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    preexec_fn=_child_prep)
            # pgid НЕ спрашиваем у ядра: getpgid сразу после Popen
            # может успеть до setsid ребёнка и вернуть группу ОБЁРТКИ —
            # «отозвать» снесло бы группу окна (нашёл grok). После
            # setsid у сессии-лидера pgid тождественно равен pid, и это
            # знание надёжнее гонки. starttime — чтобы «отозвать» не
            # било по переиспользованному номеру (субагент).
            lease.write_meta(cli_pid=proc.pid, cli_pgid=proc.pid,
                             cli_starttime=leases.proc_starttime(proc.pid))
            proc.wait()
            rc = proc.returncode
            # CLI кончился — отметить ОБЯЗАТЕЛЬНО: иначе окно часами
            # (пока Автор разбирает вылет) видит в meta живой pgid и
            # шлёт killpg по номеру, который ядро давно отдало другим.
            # Этот инцидент в доме уже был — CLAUDE.md, 2026-08-26.
            lease.write_meta(cli_pid=proc.pid, cli_pgid=proc.pid,
                             cli_done=True, cli_rc=rc)
        # Канал освобождается СРАЗУ после смерти CLI: git-снимок и
        # close канала Кими не трогают, а чужой раунд уже может идти.
        _gate_release()
        out = ""
        try:
            with log_path.open("rb") as lf:
                lf.seek(max(0, log_path.stat().st_size - 4000))
                out = lf.read().decode("utf-8", "replace")
        except OSError:
            pass
    except OSError as e:
        _close_act(a, status="error", rc=-1,
                   text=f"правка {a.act}: CLI не запустился: {e}")
        return 1
    finally:
        _gate_release()

    el = round(time.monotonic() - t0, 1)

    # git-снимок результата — гейту и ревизии. rc проверяется: молчащий
    # git (снесли worktree, чужой владелец, битый .git) отдал бы пустой
    # stdout, и в ленту ушло бы head=None, dirty=False — «чисто, ничего
    # не наработано» вместо «мы не знаем» (нашёл grok). Неизвестность
    # называется неизвестностью: dirty=None.
    head = _git_at(wt, "rev-parse", "HEAD")
    head = head.strip() if head else None
    st = _git_at(wt, "status", "--porcelain")
    dirty = None if st is None else bool(st.strip())

    # CLOSE ПИШЕТСЯ ДО ОСВОБОЖДЕНИЯ ЗАМКА — на этом порядке стоит вся
    # различимость «штатно/вылет». Упади мы между close и release —
    # наблюдатель увидит close и поверит штатности: это честно, работа
    # записана.
    note = ""
    if dirty:
        note = "; в worktree НЕзакоммиченные правки"
    elif dirty is None:
        note = "; состояние worktree неизвестно (git не ответил)"
    _close_act(a, status="done" if rc == 0 else "error", rc=rc,
               text=f"правка {a.act} [{a.voice}]: CLI завершился rc={rc}"
                    f" за {el} с{note}",
               head=head, dirty=dirty,
               elapsed_s=el, tail=out[-1500:] if out else "")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
