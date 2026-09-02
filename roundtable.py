#!/usr/bin/env python3
"""RoundTable — окно Автора в живую комнату. Прототип, раунд roundtable-v1.

Зачем. Сейчас Автор говорит со столом через Клода: и реплики, и код, и
быстрые ответы идут одним каналом — перекос по построению (затравка
Автора, 2026-08-24). Это окно даёт Автору прямой вход в ленту: реплика
уходит от имени `arr`, а не через дирижёра.

Что это НЕ. Не вторая база и не второй журнал: единственная истина —
`live.jsonl` (вердикт стола, раунд `оболочки-v1`). Сервер только читает
ленту хвостом и зовёт `live.py` подпроцессом — весь протокол (слепой
первый ход, очередь drand, остывание, карта покрытия) остаётся в live.py.

Что тут из решений стола:
  • жребий дирижёра — commit-reveal НА БУДУЩИЙ drand-раунд (идея kimi,
    дыры v1 нашли claude/codex/kimi в ревью В1): commit публикуется до
    существования подписи, имя не вычислимо без соли, соль раскрывается
    при закрытии — сверить может любой;
  • маяка нет — жребия нет (правило 11, fail-closed);
  • «тест безрукого голоса»: окно не требует от голосов ничего сверх
    live.py — HTTP-голоса (gemini, deepseek) равноправны;
  • панель галочек «из кого рандом» — просьба Автора дословно.

Запуск:  python3 roundtable.py            # http://127.0.0.1:8770
  Порт по умолчанию — 8770, не 8765: 8765 на этой машине занят чужим
  сервисом, отвечающим "forbidden"/"not found" на любой запрос, —
  обнаружено при живом прогоне, когда окно «открывалось», а отвечал
  не наш процесс. Переопределяется через ROUNDTABLE_PORT.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import edits                             # правки: worktree + вердикт
import leases                            # аренды кресла исполнителя
import merge_gate                        # этап 3: ревизия дифа + приёмка

# Комната остаётся в Choir — RoundTable лишь окно в неё. Путь можно
# переопределить (ROUNDTABLE_CHOIR), чтобы окно смотрело в другую комнату.
# Комната ищется: env → каталог choir/ рядом с этим файлом (раскладка
# публичного репозитория) → путь песочницы Автора. Порядок важен для
# публикации: свежий клон работает без настройки.
CHOIR = Path(os.environ.get("ROUNDTABLE_CHOIR")
             or (Path(__file__).resolve().parent / "choir"
                 if (Path(__file__).resolve().parent / "choir"
                     / "live.py").exists()
                 else Path.home() / "AiSandbox" / "Choir"))
FEED = CHOIR / "live.jsonl"
PORT = int(os.environ.get("ROUNDTABLE_PORT", "8770"))
# Каталог, о котором идёт разговор. Умолчание — ТОТ, ИЗ КОТОРОГО ОКНО
# ЗАПУСТИЛИ: Автор набирает `roundtable` в папке проекта и ждёт, что
# стол уже смотрит туда, а не заставляет копировать путь руками.
# Переопределяется флагом --project и переменной ROUNDTABLE_PROJECT;
# `-c/--continue` берёт путь прошлого запуска (LAST_RUN).
PROJECT = os.environ.get("ROUNDTABLE_PROJECT") or os.getcwd()
LAST_RUN = Path.home() / ".cache" / "choir" / "rt-last.json"

# Каталоги, которые НЕЛЬЗЯ подставлять молча, и файлы, выдающие такой
# каталог. Причина найдена ревьюером дифа и она денежная, а не
# теоретическая: `--project` уходит голосам как `--add-dir` и как cwd,
# то есть шесть чужих CLI получают каталог НА ЧТЕНИЕ. «Набираю
# roundtable в папке проекта» — но домашний каталог тоже папка, и запуск
# оттуда отдал бы им ~/.claude/.credentials.json, ~/.codex/auth.json и
# остальные ключи, которые сборщик лимитов рядом старательно не пускает
# наружу. Умолчание — удобство; удобство не стоит ключей Автора.
SECRET_MARKS = (".credentials.json", "auth.json", ".env", "keys.txt",
                "credentials", ".netrc", "id_rsa", ".ssh")


def project_risk(path: str) -> str | None:
    """Почему этот каталог опасно подставлять умолчанием (или None)."""
    if not path:
        return None
    p = Path(path)
    if p == Path.home():
        return "домашний каталог: в нём лежат ключи всех голосов"
    if str(p) in ("/", "/tmp", "/etc", "/var"):
        return f"{p} — системный каталог"
    try:
        for mark in SECRET_MARKS:
            if (p / mark).exists():
                return f"в каталоге есть {mark} — это похоже на секреты"
    except OSError:
        pass
    return None
DRAND = "https://api.drand.sh/v2/beacons/quicknet/rounds/latest"

# Голоса берём у live.py, не дублируем список руками: две копии однажды
# разойдутся, и панель начнёт врать (тем же образом из канона выпал Грок).
sys.path.insert(0, str(CHOIR))
import live  # noqa: E402

VOICES = list(live.VOICES)

# ── реестр запущенных ходов: индикатор «кто думает» ──────────────────
# Свой реестр, а не парсинг `live.py who`: индикатор должен гаснуть и
# при аварии подпроцесса, а это видно только его родителю.
RUNNING: dict[str, dict] = {}
RUN_LOCK = threading.Lock()
# Проекты, чьё кресло исполнителя прямо сейчас ОТКРЫВАЕТСЯ (между
# резервом в /edit и записью спавна в RUNNING). Под RUN_LOCK.
_EDIT_RESERVED: set[str] = set()

# Жребий. Схема v2 — после ревью В1 (2026-08-25), три дыры v1:
#   • имя было ВЫЧИСЛИМО из commit-события: индекс считался только от
#     подписи и кандидатов, а оба публиковались (нашёл claude);
#   • commit публиковался ПОСЛЕ получения маяка — сервер знал победителя
#     до объявления, перебрасывай пока не понравится (нашёл codex);
#   • соль жила в памяти: рестарт вешал в ленте навсегда нераскрытый
#     commit (нашли kimi и codex независимо).
# Теперь: commit = sha256(salt:candidates:R) публикуется, когда подписи
# раунда R ещё НЕ СУЩЕСТВУЕТ (R — будущий); победитель =
# H(sig_R:salt:candidates) — без соли не вычислим никем даже после
# наступления R; соль до раскрытия лежит в приватном сейфе Автора на
# диске (это секрет по построению, а не вторая истина: в истине — commit).
LOT_LOCK = threading.Lock()
LOT_SAFE = Path.home() / ".cache" / "choir" / "roundtable-lot.json"
LOT_AHEAD = 10          # раундов вперёд ≈ 30 с: подписи ещё нет ни у кого

DRAND_ROUND = "https://api.drand.sh/v2/beacons/quicknet/rounds/{r}"


def _lot_load() -> dict | None:
    try:
        return json.loads(LOT_SAFE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _lot_store(lot: dict | None) -> None:
    """Соль — секрет: создаём файл сразу с правами 0600, а не write_text
    под umask с последующим chmod (окно, в котором секрет читаем всеми)."""
    LOT_SAFE.parent.mkdir(parents=True, exist_ok=True)
    if lot is None:
        LOT_SAFE.unlink(missing_ok=True)
        return
    fd = os.open(LOT_SAFE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(lot, f)


class _SafeGate:
    """Межпроцессный замок на сейф жребия. LOT_LOCK — threading, а сейф
    один на машину: два окна (порт задан переменной, так и задумано)
    проходили проверку «жребий уже есть» одновременно, оба публиковали
    commit, второй затирал соль первого — и первый commit оставался
    нераскрываемым навсегда (нашёл ревьюер дифа)."""

    def __enter__(self):
        LOT_SAFE.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(LOT_SAFE.with_suffix(".gate"), "a+")
        live.fcntl.flock(self._fh, live.fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            live.fcntl.flock(self._fh, live.fcntl.LOCK_UN)
        finally:
            self._fh.close()


def drand_latest() -> dict:
    with urllib.request.urlopen(DRAND, timeout=15) as r:
        return json.load(r)


_ROUND_CACHE: dict[int, dict] = {}
_ROUND_CACHE_LOCK = threading.Lock()


_ROUND_MISS: dict[int, float] = {}      # когда раунда ещё не было


def drand_round(r: int) -> dict | None:
    """Подпись раунда. Кэшируем НАВСЕГДА: раунд drand неизменяем, а
    /state опрашивается раз в 2 с — без кэша это ~43k запросов в сутки
    к чужому маяку и до 15 с под LOT_LOCK на каждом тормозе сети
    (нашёл ревьюер дифа)."""
    with _ROUND_CACHE_LOCK:
        hit = _ROUND_CACHE.get(r)
        # Положительный ответ кэшируется навсегда, а вот ОТРИЦАТЕЛЬНЫЙ не
        # кэшировался вовсе — и пока целевой раунд не наступил (а это и
        # есть нормальное состояние commit-фазы), каждый /state раз в
        # 2 с уходил в сеть на 15 с под LOT_LOCK, вешая вкладку и вместе
        # с ней /lot и /reveal (нашёл kimi). Отрицательный держим
        # секунды: раунд drand выходит каждые 3 с.
        miss_at = _ROUND_MISS.get(r, 0.0)
    if hit is not None:
        return hit
    if time.time() - miss_at < 3.0:
        return None
    try:
        with urllib.request.urlopen(DRAND_ROUND.format(r=r),
                                    timeout=NET_TIMEOUT) as x:
            b = json.load(x)
    except Exception:               # noqa: BLE001 — раунда ещё нет
        with _ROUND_CACHE_LOCK:
            _ROUND_MISS[r] = time.time()
        return None
    with _ROUND_CACHE_LOCK:
        _ROUND_CACHE[r] = b
    return b


def cast_lot(candidates: list[str]) -> dict:
    """Обязательство на БУДУЩИЙ раунд. Маяк недоступен — жребия нет."""
    beacon = drand_latest()
    target = beacon["round"] + LOT_AHEAD
    order = sorted(candidates)
    salt = secrets.token_hex(16)
    cands = ",".join(order)
    commit = hashlib.sha256(f"{salt}:{cands}:{target}".encode()).hexdigest()
    lot = {"salt": salt, "candidates": order, "target": target,
           "commit": commit}
    # СНАЧАЛА публикация, потом сейф. Обратный порядок оставлял в сейфе
    # жребий, чей commit никто не видел: /lot дальше отвечал 409, а
    # /reveal раскрывал несуществующее обязательство — зеркало дыры v1,
    # ради которой делали v2 (нашёл ревьюер дифа).
    feed_append(
        "lot_commit",
        f"жребий дирижёра: commit {commit[:16]}… на drand-раунд {target} "
        f"(его подписи ещё не существует); кандидаты {{{cands}}}",
        commit=commit, candidates=order, drand_target=target)
    _lot_store(lot)
    return lot


def lot_winner(lot: dict) -> str | None:
    """Имя есть только когда наступил целевой раунд. До reveal его может
    вычислить лишь держатель соли — то есть это окно Автора."""
    b = drand_round(lot["target"])
    if not b:
        return None
    cands = ",".join(lot["candidates"])
    idx = int(hashlib.sha256(
        f"{b['signature']}:{lot['salt']}:{cands}".encode()).hexdigest(),
        16) % len(lot["candidates"])
    return lot["candidates"][idx]


def reveal_lot(lot: dict) -> dict | None:
    winner = lot_winner(lot)
    if winner is None:
        return None
    ev = feed_append(
        "lot_reveal",
        f"дирижёром был {winner}: соль {lot['salt']}, раунд "
        f"{lot['target']} — проверка: sha256(соль:кандидаты:раунд) == "
        f"commit {lot['commit'][:16]}…; индекс = "
        f"sha256(подпись:соль:кандидаты) mod {len(lot['candidates'])}",
        conductor=winner, salt=lot["salt"], commit=lot["commit"],
        candidates=lot["candidates"], drand_target=lot["target"])
    _lot_store(None)
    return ev


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def feed_append(kind: str, text: str, **extra) -> dict:
    """Дописать событие в ленту — ЧЕРЕЗ live.post, не своим форматом.

    Урок первого смока (2026-08-25, пойман дважды): запись со строковым
    id ломала `read_events` всей комнаты — live.py живёт на числовых id.
    Формат журнала принадлежит live.py; второй писатель со своим
    форматом — вторая истина в миниатюре (раунд `оболочки-v1`).
    Межпроцессную гонку двух писателей закрывает flock в самом
    live.post — правка по ревью В1.
    """
    return live.post("roundtable", kind, text, **extra)


# Вывод подпроцессов. Раньше глотался в DEVNULL (нашли kimi и grok):
# упавший ход не оставлял следов, а судьба действия жила только в памяти
# RUNNING — рестарт окна её терял. Теперь stdout+stderr каждого действия
# собираются в файл по act_id, файл остаётся на диске для разбора.
# Это диагностика, не вторая истина: итог (done/error + хвост) — в ленте.
ACT_DIR = Path.home() / ".cache" / "choir" / "rt-acts"

# Имя раунда — ОДНО правило на сервер и на страницу (константа, а не
# литерал в обработчике): вторая копия правила живёт в PAGE (RE_ROUND),
# и когда правило меняют в одном месте, а во втором нет, окно начинает
# ловить 400 уже ПОСЛЕ того, как Автор набрал вопрос и имя.
ROUND_RE = r"[\w][\w.-]{0,59}"

# Флаг-стоп идущего раунда. Каталог общий с choir.py (~/.cache/choir):
# окно только ставит файл, читает его дирижёр между шагами такта.
# Файл, а не сигнал: окно и такт — разные процессы и переживают друг
# друга, а файл переживает и рестарт окна.
STOP_DIR = Path.home() / ".cache" / "choir"


def stop_file(name: str) -> Path:
    """Путь флага-стопа. Имя обязано пройти ROUND_RE ДО вызова: в классе
    нет `/`, а первым символом не может быть точка, поэтому `../` в путь
    не пролезет — но проверять это надо на входе, а не надеяться на
    «имя же пришло из нашего окна»: /stop открыт всем, кто дотянулся до
    порта."""
    return STOP_DIR / f"stop-{name}"


def _log_tail(log_path: Path, limit: int = 400) -> str:
    """Хвост объединённого stdout+stderr, до 400 символов — в act_status."""
    try:
        return log_path.read_bytes()[-4 * limit:].decode(
            "utf-8", errors="replace")[-limit:].strip()
    except OSError:
        return ""


def spawn(cmd: list[str], label: str, voices: list[str],
          cwd: Path | None = None, meta: dict | None = None,
          note: str = "", fields: dict | None = None,
          env_extra: dict | None = None) -> str:
    """Запустить действие фоном; лента обновится сама — мы следим, когда
    ход закончился, чтобы погасить индикатор.

    Каждому действию — act_id и события act_status в ленте (просили
    codex, kimi и grok): «принят» при приёме, «done»/«error» по
    завершении. Статус пишется в ленту, а не только в RUNNING: рестарт
    окна не должен терять судьбу действия.

    `meta` кладётся в запись RUNNING (например, имя раунда — чтобы
    кнопка «Стоп» знала, что именно останавливать, а не выковыривала
    имя из подписи). `note` и `fields` дописываются в событие
    `accepted`: режим запуска обязан быть виден В ЛЕНТЕ, иначе через
    месяц «раунд вёл человек по шагам» и «прокрутил автомат» выглядят
    одинаково — а это разные вещи по правилу 10 (то, что оркестрация
    сделала молча, читается как решение стола)."""
    act_id = uuid.uuid4().hex[:8]
    ACT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = ACT_DIR / f"{act_id}.log"
    feed_append("act_status",
                f"act {act_id} принят: {label}" + (f"\n{note}" if note else ""),
                act_id=act_id, status="accepted", **(fields or {}))
    log = open(log_path, "wb")
    try:
        # start_new_session: подпроцесс в СВОЕЙ группе. Окно знает pid
        # только live.py, а тот запускает CLI голосов — при остановке
        # окна внуки оставались сиротами и жгли квоту дальше (нашёл
        # codex как «то, что упустили все», раунд переезд-v1). Своя
        # группа позволяет снять всё дерево одним killpg при выходе.
        # env — для ВСЕХ голосов, не только отмеченных галочками:
        # live.py после первого хода продолжает разговор сам, по всему
        # ростеру (pick_voices), и голос, снятый галочкой, всё равно может
        # заговорить. Сужение списка не давало изоляции (переменные и так
        # именованы по голосу), зато панель показывала одну модель, а голос
        # шёл другой — поле врало (нашёл ревьюер 2026-08-26).
        proc = subprocess.Popen(
            cmd, cwd=str(cwd or CHOIR), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
            # RT_ACT_ID — чтобы заметки live.py («слово → …») несли
            # идентификатор акта: без него два параллельных действия
            # склеивались в один глобальный список «кто думает»
            # (нашли codex и grok).
            # ОКРУЖЕНИЕ ПЕРЕДАЁТСЯ ЯВНО: os.environ плюс модель и усилие,
            # заданные через POST /voices. Без этой строки настройка окна
            # была бы декорацией — значение лежит в памяти сервера, а
            # подпроцесс берёт умолчания и отвечает не той моделью,
            # которая показана в панели (правило 8.5: поле, не
            # обеспеченное механикой, врёт).
            # env_extra — переключатели ОДНОГО акта (например
            # CHOIR_BRIEF): в VOICE_CFG им не место, они не про голос и
            # не должны переживать такт.
            env={**_spawn_env(VOICES), "RT_ACT_ID": act_id,
                 **(env_extra or {})})
    except OSError as e:
        log.close()
        feed_append("act_status",
                    f"act {act_id} error: {label} — запуск не удался: {e}",
                    act_id=act_id, status="error", rc=-1)
        return act_id
    # pgid берём СЕЙЧАС, пока процесс заведомо жив. Позже os.getpgid(pid)
    # опасен: после wait() ядро освобождает pid и может отдать его чужому
    # процессу — сигнал уйдёт постороннему дереву, вплоть до оболочки
    # Автора (нашёл ревьюер дифа 2026-08-25).
    with RUN_LOCK:
        # meta первым: свои поля должны ПЕРЕКРЫВАТЬ переданные, иначе
        # случайный ключ "proc" в meta подменит объект процесса, и
        # killpg при закрытии окна пойдёт мимо.
        RUNNING[act_id] = {**(meta or {}),
                           "label": label, "voices": voices,
                           "since": time.time(), "pid": proc.pid,
                           "proc": proc, "pgid": os.getpgid(proc.pid)}

    def reap():
        rc = proc.wait()
        log.close()
        # Итог пишет ТОТ, КТО ВЫНУЛ запись из RUNNING. Иначе при закрытии
        # окна main пишет `interrupted`, а проснувшаяся reap — второй,
        # противоречивый `error(rc=-15)`: в append-only истине у одного
        # действия два разных финала (нашёл ревьюер дифа).
        with RUN_LOCK:
            mine = RUNNING.pop(act_id, None)
        if mine is None:
            return
        try:
            if rc == 0:
                feed_append("act_status", f"act {act_id} done: {label}",
                            act_id=act_id, status="done")
            else:
                tail = _log_tail(log_path)
                feed_append(
                    "act_status",
                    f"act {act_id} error (rc={rc}): {label}"
                    + (f"\n{tail}" if tail else ""),
                    act_id=act_id, status="error", rc=rc)
        except Exception as e:              # noqa: BLE001
            # статус не записался — хотя бы след в терминале, не молча
            print(f"act {act_id}: статус не записан: {e}", file=sys.stderr)
    threading.Thread(target=reap, daemon=True).start()
    return act_id


def _line_id(line: str) -> int:
    """Числовой id события ленты; кривая строка — -1 (без "id:" в SSE,
    и при resume не отдаётся: сравнивать её id не с чем).

    Ловим ЛЮБОЕ исключение, а не только JSONDecodeError: строки `null`,
    `123`, `"abc"`, `[1,2]` — валидный json, но `.get` на них бросает
    AttributeError, а _stream ловит лишь ошибки сокета. Такое исключение
    рвало соединение, браузер переподключался с тем же Last-Event-ID,
    снова доходил до той же строки — и лента в окне умирала навсегда
    (нашёл ревьюер дифа 2026-08-25; до появления "id:" такую строку
    молча глотал catch в JS).
    """
    try:
        o = json.loads(line)
        v = o.get("id") if isinstance(o, dict) else None
        return v if isinstance(v, int) else -1
    except Exception:                       # noqa: BLE001 — см. выше
        return -1


# ── модель, усилие и лимиты голосов ──────────────────────────────────
# Зачем это в окне. Правило 1 требует одинаковых условий у всех, правило
# 8.5 — чтобы объявленное поле было обеспечено механикой. Голос,
# отвечающий не той моделью, которой подписан, ломает оба разом: ровно
# за это Джемини увели с CLI на прямой HTTP (CLI молча откатывал 3.7 на
# 3.5, и увидеть подмену можно было только в json-статистике). Окно
# поэтому обязано ПОКАЗЫВАТЬ действующие модель и усилие — и менять их
# там, где смена реально доедет до CLI.
#
# Чего тут нет намеренно: выдуманных чисел. Провайдеры сообщают об
# остатке очень по-разному, и «неизвестно» — отдельное состояние, а не
# ноль и не сто процентов. Пустое поле, прочитанное как «всё хорошо», —
# та же ошибка, что «ПАС читается как согласие».

CODEX_CFG = Path.home() / ".codex" / "config.toml"
KIMI_CFG = Path.home() / ".kimi-code" / "config.toml"
DS_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DS_KEY = Path.home() / ".deepseek" / "key"

# Лестницы усилий берём у choir.py, а не переписываем: вторая копия
# однажды разойдётся молча — тем же образом из правила 7 выпал Грок
# (и по той же причине VOICES выше берутся у live.py). Импорт под
# защитой: окно не должно падать оттого, что у соседнего модуля
# насморк, — тогда работает запасная таблица, и это видно в поле
# efforts_source.
try:
    import choir                                    # noqa: E402
    EFFORTS = {k: list(v) for k, v in choir.EFFORT_LADDER.items()}
    EFFORTS_SRC = "choir.EFFORT_LADDER"
except Exception:                                   # noqa: BLE001
    # Запасная копия. Держится ровно потому, что без неё POST /voices
    # отверг бы вообще любой уровень — а это хуже, чем устаревшая копия
    # с честной подписью источника.
    EFFORTS = {"claude": ["low", "medium", "high", "xhigh", "max"],
               "codex": ["low", "medium", "high", "xhigh"],
               "grok": ["low", "medium", "high"],
               "kimi": [], "gemini": []}
    EFFORTS_SRC = "запасная копия в roundtable.py (choir не импортировался)"

# У кого лестница НЕ оттуда — называем свой источник. deepseek в
# choir.EFFORT_LADDER отсутствует вовсе, его список приходит из choices
# самого адаптера; подписывать его чужим источником значит врать ровно
# в том поле, которое заведено против догадок (нашёл ревьюер 2026-08-26).
def _grok_cache() -> dict:
    """Разобранный ~/.grok/models_cache.json или {} — кэш пишет сам CLI,
    и наша копия имён разошлась бы с живым сервером молча (тот же довод,
    по которому VOICES берутся у live.py).

    Except широкий и с проверками типов НАМЕРЕННО: это читается на
    ИМПОРТЕ окна, и валидный json неожиданной формы (models списком)
    ронял бы окно целиком ещё до первой страницы (нашли deepseek и
    grok независимо).
    """
    try:
        mc = json.loads((Path.home() / ".grok" / "models_cache.json")
                        .read_text(encoding="utf-8"))
        models = mc.get("models") if isinstance(mc, dict) else None
        if isinstance(models, dict) and models:
            return models
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def _grok_model_efforts() -> dict[str, list[str]]:
    """Усилия ПО МОДЕЛЯМ: union по всем моделям пропускал пару
    grok-4.5+xhigh, которую CLI роняет с 'unknown effort level' —
    проверка пары обязана знать, у какой модели что есть (нашли все
    четверо ревьюеров)."""
    out: dict[str, list[str]] = {}
    order = ["low", "medium", "high", "xhigh", "max"]
    for mid, m in _grok_cache().items():
        effs = []
        try:
            for e in (m.get("info") or {}).get("reasoning_efforts") or []:
                v = e.get("value")
                if v and v not in effs:
                    effs.append(v)
        except (TypeError, AttributeError):
            continue
        if effs:
            out[str(mid)] = sorted(
                effs, key=lambda x: order.index(x) if x in order else 99)
    return out


_GROK_BY_MODEL = _grok_model_efforts()
GROK_CACHE_OK = bool(_GROK_BY_MODEL)


def _grok_models() -> list[str]:
    if GROK_CACHE_OK:
        return sorted(_GROK_BY_MODEL.keys(), reverse=True)
    return ["grok-4.6", "grok-4.5"]


def _grok_efforts() -> list[str]:
    if GROK_CACHE_OK:
        seen: list[str] = []
        for effs in _GROK_BY_MODEL.values():
            for v in effs:
                if v not in seen:
                    seen.append(v)
        order = ["low", "medium", "high", "xhigh", "max"]
        return sorted(seen, key=lambda x: order.index(x)
                      if x in order else 99)
    # Фолбэк БЕЗ xhigh: он есть не у всех моделей, а пары проверить
    # нечем — кэша нет.
    return ["low", "medium", "high"]



EFFORTS_SRC_BY = {"deepseek": "deepseek-http: choices у --effort",
                  "grok": ("~/.grok/models_cache.json: reasoning_efforts "
                           "(пишет сам CLI с сервера)" if GROK_CACHE_OK else
                           "кэш моделей НЕ прочитался — запасной список "
                           "по памяти, без xhigh"),
                  "gemini": "thinkingLevel Gemini API через gemini-http "
                            "--thinking. По medium стол разошёлся "
                            "(codex и grok: поддержан обеими моделями; "
                            "kimi: даст 400) — оставлен по большинству и "
                            "докам, опыт снимет спор, когда шлюз оживёт"}


def _bin_path(name: str) -> Path | None:
    """Где лежит адаптер. PATH окна может не содержать ~/.local/bin
    (окно запускают из systemd/nohup), поэтому явный запасной путь."""
    p = shutil.which(name)
    if p:
        return Path(p)
    guess = Path.home() / ".local" / "bin" / name
    return guess if guess.exists() else None


def _src_default(binname: str, pattern: str) -> str | None:
    """Достать умолчание ИЗ ИСХОДНИКА адаптера регэкспом.

    Не «мы знаем, что там max», а «в файле написано max»: адаптеры —
    stdlib-скрипты рядом, и это дешевле копии значения в окне, которая
    молча протухнет. Не нашли — возвращаем None и говорим «неизвестно»,
    а не подставляем правдоподобное."""
    p = _bin_path(binname)
    if not p:
        return None
    try:
        m = re.search(pattern, p.read_text(encoding="utf-8", errors="replace"),
                      re.DOTALL)
    except OSError:
        return None
    return m.group(1) if m else None


def _toml_get(path: Path, key: str) -> str | None:
    try:
        import tomllib
        with path.open("rb") as f:
            v = tomllib.load(f).get(key)
        return v if isinstance(v, str) else None
    except Exception:                               # noqa: BLE001
        return None


# У deepseek лестницы в choir.EFFORT_LADDER нет вовсе (голос добавлен
# позже), зато ручка у адаптера есть — берём допустимые уровни из его
# же argparse-choices, чтобы окно не выдумало свой список.
_ds_choices = _src_default(
    "deepseek-http", r'"--effort".*?choices=\(([^)]*)\)')
EFFORTS["deepseek"] = ([s.strip().strip('"\'') for s in _ds_choices.split(",")
                        if s.strip()] if _ds_choices else [])

# ЧТО РЕАЛЬНО МОЖНО ПОМЕНЯТЬ НА ЛЕТУ (разведка по коду, 2026-08-25).
# Переменные окружения во всей цепочке читают ровно два места:
#   live.py:176–177      CHOIR_CLAUDE_MODEL / CHOIR_CLAUDE_EFFORT
#   deepseek-http:87,94  DEEPSEEK_MODEL / DEEPSEEK_EFFORT
# У остальных рычага нет: codex берёт модель и усилие из
# ~/.codex/config.toml (глобально, на все инструменты машины), у grok
# усилие захардкожено в live.py (GROK_EFFORT), а модель не задаётся
# вовсе — отвечает серверный дефолт xAI; у kimi флага усилия нет в
# самом CLI; у gemini модель — литерал в live.py, а thinkingLevel зашит
# в gemini-http. Поэтому POST /voices на них отвечает 400 с
# объяснением, а не «принято»: ручка, которая молча ни на что не
# влияет, — ровно то враньё поля, которое запрещает правило 8.5.
# Отказ честнее зелёной галочки ни о чём.
VOICE_CTL: dict[str, dict] = {
    "claude": {
        "rounds_default": "умолчание choir.py: fable/max (наказ arr 24.08)",
        "model_env": "CHOIR_CLAUDE_MODEL",
        "effort_env": "CHOIR_CLAUDE_EFFORT",
        # Алиасы из `claude --help`; полное имя вида claude-fable-5 тоже
        # принимается — поэтому рядом стоит model_re.
        "models": ["fable", "opus", "sonnet", "haiku"],
        "model_re": r"claude-[\w.-]{1,48}",
        # ПОСЛЕ закрытия утечки (07aaa42) эта пара правит ТОЛЬКО
        # комнату: наследный фолбэк в choir.py снят, раундами Клода
        # правит CHOIR_ROUND_CLAUDE_*. Подпись «и комната, и раунды»
        # осталась от прошлой редакции и врала ровно про то, ради чего
        # заводились две области.
        "applies_to": ["live.py"],
        "scope": "живая комната (live.py). Раунды — отдельная пара, "
                 "вкладка 🎼; их умолчание fable/max (наказ arr 24.08)",
    },
    "deepseek": {
        "rounds_default": "умолчание адаптера: deepseek-v4-pro/max",
        "model_env": "DEEPSEEK_MODEL",
        "effort_env": "DEEPSEEK_EFFORT",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "model_re": r"deepseek-[\w.-]{1,48}",
        # Раундам deepseek-http теперь получает ЯВНЫЕ флаги (choir.py),
        # и они сильнее env — комнатная пара в раунды не утекает.
        "applies_to": ["live.py"],
        "scope": "живая комната (live.py → env адаптера deepseek-http). "
                 "Раунды берут свою пару флагами, вкладка 🎼",
    },
    # ЗАПЕРТЫХ ГОЛОСОВ БОЛЬШЕ НЕТ (наказ Автора 2026-08-25: «выбор
    # модели и усилия должен быть для всех вендоров»). live.py читает
    # CHOIR_<ГОЛОС>_MODEL/_EFFORT и передаёт флагами CLI; исключение —
    # усилие Кими: у kimi-code такого флага физически нет (в CLI только
    # -m/--model), и рычаг, за которым ничего не стоит, был бы полем,
    # которое врёт (правило 8.5).
    "codex": {
        "rounds_default": "умолчание: модель из ~/.codex/config.toml, усилие high (ступень ниже потолка)",
        "model_env": "CHOIR_CODEX_MODEL",
        "effort_env": "CHOIR_CODEX_EFFORT",
        "models": ["gpt-5.6-sol"],
        "model_re": r"gpt-[\w.-]{1,48}",
        "applies_to": ["live.py"],
        "scope": "живая комната (live.py: -m и -c model_reasoning_effort "
                 "у exec и resume). Без настройки — ~/.codex/config.toml, "
                 "как раньше; choir.py в раундах зовёт codex сам",
    },
    "grok": {
        "rounds_default": "умолчание: модель — серверный дефолт xAI, усилие medium",
        "model_env": "CHOIR_GROK_MODEL",
        "effort_env": "CHOIR_GROK_EFFORT",
        "models": _grok_models(),
        "model_re": r"grok-[\w.-]{1,48}",
        "applies_to": ["live.py"],
        "scope": "живая комната (live.py: --model и --effort). Пустая "
                 "модель — серверный дефолт xAI, как раньше. xhigh есть "
                 "только у grok-4.6 (reasoning_efforts в models_cache); "
                 "grok-4.5 на xhigh падает с 'unknown effort level'",
    },
    "kimi": {
        "rounds_default": "умолчание: модель линии из ~/.kimi-code/config.toml",
        "model_env": "CHOIR_KIMI_MODEL",
        "models": ["kimi-k3"],
        "model_re": r"kimi-[\w.-]{1,40}",
        "applies_to": ["live.py"],
        "scope": "живая комната; имя БЕЗ провайдера (kimi-k3, не "
                 "moonshotai/kimi-k3): провайдер выбирает КЛЮЧ линии, и "
                 "live.py подставляет его сам каждой из двух линий. "
                 "Усилия у kimi-code нет вовсе — этот рычаг не показан "
                 "честно, а не по забывчивости",
    },
    "gemini": {
        "rounds_default": "умолчание choir.py: gemini-3.7-flash/high",
        "model_env": "CHOIR_GEMINI_MODEL",
        "effort_env": "CHOIR_GEMINI_EFFORT",
        "models": ["gemini-3.7-flash", "gemini-3.1-pro"],
        "model_re": r"gemini-[\w.-]{1,48}",
        "applies_to": ["live.py"],
        "scope": "живая комната (live.py → gemini-http "
                 "--model/--thinking). Усилие — thinkingLevel API",
    },
}

# У Грока лестница из его же кэша моделей (у choir.EFFORT_LADDER потолок
# high — это про grok-4.5); у Джемини усилий в choir нет вовсе.
EFFORTS["grok"] = _grok_efforts()
EFFORTS["gemini"] = ["low", "medium", "high"]

# Имя модели уходит в env, оттуда — аргументом CLI. Оболочки в этом
# пути нет (subprocess со списком), но у Грока команда собирается
# строкой для `script -qec`, и привычка пропускать в argv что попало
# однажды встретится с ней. Пускаем только безобидный класс символов.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")

# Настройка ПЕРЕЖИВАЕТ перезапуск окна (наказ Автора 2026-08-27:
# «последнее установленное — по умолчанию»). Прежний довод «файл был бы
# второй истиной» остаётся в силе и решается так: в файл попадают ТОЛЬКО
# значения, прошедшие через POST /voices, а каждый такой POST пишет
# voice_config в ленту — значит восстановленное значение всегда равно
# последнему событию ленты, и вопрос «какой моделью сказано» по-прежнему
# решается её чтением. Файл — кэш последних событий, не второй канон.
#
# Структура: {голос: {"room": {model,effort}, "rounds": {model,effort}}}.
# room — живая комната (live.py), rounds — раунды choir.py («модель
# дирижёра» в терминах Автора).
VOICE_CFG: dict[str, dict] = {}


def _sync_exec_overrides() -> None:
    """VOICE_CFG[…]["exec"] → edits.EXEC_OVERRIDES (модель/усилие/пул
    кресел). Одна истина у окна — VOICE_CFG; edits просто читает свой
    словарь при сборке argv.

    ПОДМЕНА ССЫЛКИ, не clear+refill: читатели (random_pool, лямбды
    argv) ходят без замка, и в окно между clear и заполнением /edit
    видел ПУСТОЙ пул — 409 «все сняты» или argv без модели (нашли grok
    и deepseek; kimi поставил на этом ОТКАЗ). Присваивание атрибута
    модуля атомарно под GIL."""
    fresh: dict = {}
    with CFG_LOCK:
        for n, v in VOICE_CFG.items():
            ex = (v or {}).get("exec")
            if isinstance(ex, dict) and ex:
                fresh[n] = dict(ex)
    edits.EXEC_OVERRIDES = fresh
CFG_LOCK = threading.Lock()
# env-переопределение — ДЛЯ ТЕСТОВ: изоляция комнатой (ROUNDTABLE_CHOIR)
# не покрывала кэш настроек, и тестовый сервер писал в живой файл
# Автора (поймано живой проверкой вкладки coder, 2026-09-01).
CFG_FILE = Path(os.environ.get("CHOIR_RT_VOICES")
                or Path.home() / ".cache" / "choir" / "rt-voices.json")
# exec — вкладка 🔧 coder: пара модель+усилие КРЕСЛА исполнителя и
# галочка пула random (наказ Автора 2026-09-01: «те же агенты со своими
# моделями и усилиями — чтобы можно было выбирать»).
SCOPES = ("room", "rounds", "exec")


def _load_voice_cfg() -> None:
    """Восстановить настройки, НЕ доверяя файлу больше, чем POST-запросу.

    Файл правят руками — это названо штатным, значит валидация обязана
    быть той же строгости, что у /voices. Ревизия сняла с первой
    редакции три шкуры: `str(m)` проверялся, а хранился ОРИГИНАЛ — int
    из файла ронял Popen ЛЮБОГО акта TypeError'ом (субагент, замером);
    effort не проверялся вовсе — кавычка в нём была TOML-инъекцией в
    -c Кодекса (kimi); пара Грока не сверялась (grok). Кривое значение
    отбрасывается с объяснением в stderr — молча терять то, что человек
    писал руками, тоже нельзя.
    """
    try:
        raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠ {CFG_FILE.name} не прочитался ({e}) — настройки "
              f"голосов начинаются с умолчаний", file=sys.stderr)
        return
    if not isinstance(raw, dict):
        print(f"⚠ {CFG_FILE.name}: не словарь — пропущен", file=sys.stderr)
        return

    def drop(name: str, sc: str, what: str, why: str) -> None:
        print(f"⚠ {CFG_FILE.name}: {name}/{sc}/{what} отброшен ({why})",
              file=sys.stderr)

    for name, v in raw.items():
        if name not in VOICES or not isinstance(v, dict):
            continue
        if any(sc in v for sc in SCOPES):
            ent = {sc: dict(v[sc]) for sc in SCOPES
                   if isinstance(v.get(sc), dict)}
        else:
            # Файл первой редакции был плоским — читаем как комнату.
            ent = {"room": dict(v)}
        ctl = VOICE_CTL.get(name) or {}
        for sc in list(ent):
            keys = ("model", "effort", "pool") if sc == "exec" \
                else ("model", "effort")
            pair = {k: ent[sc][k] for k in keys
                    if k in ent[sc]}          # белый список ключей
            if "pool" in pair and not isinstance(pair["pool"], bool):
                drop(name, sc, "pool", "не bool")
                pair.pop("pool", None)
            m = pair.get("model")
            if m is not None and (not isinstance(m, str)
                                  or not MODEL_RE.match(m)):
                drop(name, sc, "model", "не строка или кривая форма")
                pair.pop("model", None)
            e = pair.get("effort")
            allowed = EFFORTS.get(name) or []
            if e is not None and (not isinstance(e, str)
                                  or e not in allowed):
                drop(name, sc, "effort",
                     f"не из списка {allowed or '(рычага нет)'}")
                pair.pop("effort", None)
            if (name == "grok" and GROK_CACHE_OK
                    and pair.get("model") in _GROK_BY_MODEL
                    and pair.get("effort")
                    and pair["effort"] not in _GROK_BY_MODEL[pair["model"]]):
                drop(name, sc, "effort",
                     f"пара с {pair['model']} недопустима")
                pair.pop("effort", None)
            if pair:
                ent[sc] = pair
            else:
                ent.pop(sc, None)
        if ent:
            VOICE_CFG[name] = ent


def _save_voice_cfg(name: str, vscope: str) -> None:
    """Записать В ФАЙЛ одну изменённую пару, слив с тем, что там лежит.

    Не «сериализовать всю память»: два окна на одном HOME так затирали
    правки друг друга — A ставил gemini, B через минуту codex, и gemini
    исчезал (замерил субагент двумя окнами). flock + перечитка + правка
    ровно одного голоса/области; свой снимок памяти файлом не считаем.
    Не смогли — окно живёт дальше: файл лишь кэш, истина в ленте.
    """
    try:
        CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
        lock = CFG_FILE.with_name(CFG_FILE.name + ".lock")
        with lock.open("a+") as lk:
            try:
                fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            try:
                disk = json.loads(CFG_FILE.read_text(encoding="utf-8"))
                if not isinstance(disk, dict):
                    disk = {}
            except (OSError, ValueError):
                disk = {}
            ent = disk.setdefault(name, {})
            if not isinstance(ent, dict) or (
                    ent and "room" not in ent and "rounds" not in ent):
                ent = {}
                disk[name] = ent
            pair = (VOICE_CFG.get(name) or {}).get(vscope) or {}
            if pair:
                ent[vscope] = dict(pair)
            else:
                ent.pop(vscope, None)
            tmp = CFG_FILE.with_name(CFG_FILE.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(CFG_FILE)
    except OSError as e:
        print(f"настройки голосов не сохранились: {e}", file=sys.stderr)


def _spawn_env(voices: list[str]) -> dict:
    """См. также CHOIR_NO_DING ниже: звонит окно, а не подпроцесс."""
    """os.environ плюс переопределения окна для этих голосов.

    Без этого POST /voices был бы декорацией: значение лежит в памяти
    сервера, а подпроцесс запускается с чистым окружением родителя и
    берёт умолчания. live.py читает CHOIR_CLAUDE_* на импорте, а он
    импортируется заново на каждый ход — значит переменная доезжает.
    Для deepseek переменную читает уже внук (адаптер), и это работает
    ровно потому, что live.py зовёт subprocess.run без env= и наследует
    окружение целиком."""
    env = dict(os.environ)
    # Такт, запущенный окном, НЕ звонит paplay сам: сокет звука он
    # наследует (окружение передаётся целиком), и без этой строки
    # каждый такт звонил дважды — paplay изнутри плюс WebAudio окна
    # по act_status (нашёл субагент, ревьюя звонок).
    env["CHOIR_NO_DING"] = "1"
    with CFG_LOCK:
        for name in voices:
            cfg = VOICE_CFG.get(name) or {}
            ctl = VOICE_CTL.get(name) or {}
            room = cfg.get("room") or {}
            if room.get("model") and ctl.get("model_env"):
                env[ctl["model_env"]] = room["model"]
            if room.get("effort") and ctl.get("effort_env"):
                env[ctl["effort_env"]] = room["effort"]
            # Раунды — своя семья переменных, читает её choir.py
            # («модель дирижёра», наказ 2026-08-27). У deepseek комнатой
            # правит env самого адаптера, раундами — эти же ROUND-флаги.
            rounds = cfg.get("rounds") or {}
            up = name.upper()
            if rounds.get("model"):
                env[f"CHOIR_ROUND_{up}_MODEL"] = rounds["model"]
            if rounds.get("effort"):
                env[f"CHOIR_ROUND_{up}_EFFORT"] = rounds["effort"]
    return env


def _arm_edit_watch(act: str, epoch: int, wt: Path, voice: str) -> None:
    """Взвести наблюдателя кресла. Вердикт — В ОТДЕЛЬНОЙ НИТИ.

    Контракт leases.watch велит колбэку быть коротким: чтение ленты
    целиком и пост под её flock — не короткое (нашёл kimi). Колбэк
    только передаёт эстафету нити; гонки эпох тут нет — вердикт ищет
    close СВОЕЙ эпохи, а новое кресло того же акта получит новую.

    Грация 180 с, не 30: executor до аренды делает несколько git-проб
    с таймаутом 60 с каждая, и на медленном диске наблюдатель успевал
    крикнуть «кресло не занято» ЖИВОМУ исполнителю (нашли grok и
    gemini). Если no_start всё же случился, а замок уже держится —
    verdict_on_drop смолчит, и мы перевзводим наблюдателя.
    """
    def _verdict(why):
        ev = edits.verdict_on_drop(act, epoch, wt, voice, why)
        if why and ev is None and leases.is_held(act):
            # Рукопожатие всё же состоялось — сторожим дальше. Ретраи
            # обязательны: старый наблюдатель снимается из _WATCHED в
            # finally СВОЕЙ нити, и мгновенный перевзвод мог поймать
            # ValueError и молча оставить живой акт без сторожа
            # (нашёл codex).
            for _ in range(20):
                try:
                    _arm_edit_watch(act, epoch, wt, voice)
                    return
                except ValueError:
                    time.sleep(0.1)
            print(f"перевзвод наблюдателя {act} не удался: слот занят",
                  file=sys.stderr)
    leases.watch(act,
                 lambda why: threading.Thread(
                     target=_verdict, args=(why,), daemon=True,
                     name=f"edit-verdict-{act}").start(),
                 grace=180)


def _argv_probe(name: str) -> list[str]:
    """Настоящая командная строка голоса — из самих лямбд live.VOICES.

    Не вторая таблица моделей в окне: копия однажды разойдётся с живым
    кодом молча, и панель начнёт врать (та же причина, по которой
    VOICES берутся у live.py, а не переписываются). Здесь argv собирает
    ровно тот код, который потом и запустит голос, а мы лишь читаем из
    него --model и --effort. У Грока команда — `script -qec "<строка>"
    /dev/null`: строку разбираем shlex'ом, иначе флаги внутри неё не
    видны вовсе."""
    v = live.VOICES.get(name) or {}
    start = v.get("start")
    if not callable(start):
        return []
    try:
        argv = [str(t) for t in start("PROBE", Path("/dev/null"),
                                      Path("/dev/null"), "probe", None)]
    except Exception:                               # noqa: BLE001
        return []
    out: list[str] = []
    for tok in argv:
        if " -" in tok:                 # склеенная командная строка
            try:
                out.extend(shlex.split(tok))
                continue
            except ValueError:
                pass
        out.append(tok)
    return out


def _flag(argv: list[str], *flags: str) -> str | None:
    for i, t in enumerate(argv):
        for fl in flags:
            if t == fl and i + 1 < len(argv):
                return argv[i + 1]
            if t.startswith(fl + "="):
                return t.split("=", 1)[1]
    return None


def voice_report(name: str, limit: dict) -> dict:
    """Карточка голоса: чем он отвечает сейчас и что о его лимите
    известно. Источник каждого значения называется рядом — «opus»
    без подписи «откуда» через месяц неотличимо от догадки."""
    ctl = VOICE_CTL.get(name) or {}
    argv = _argv_probe(name)
    model = _flag(argv, "--model", "-m")
    # --thinking — усилие Джемини: без него панель показывала ПУСТОЕ
    # усилие с подписью «зашито в gemini-http», хотя вызов шёл с
    # --thinking high — поле и источник врали разом; мёртвый regex по
    # исходнику адаптера снят вместе с самим литералом (нашли codex,
    # grok и субагент, у субагента — замером voice_report).
    effort = _flag(argv, "--effort", "--reasoning-effort", "--thinking")
    msrc = "флаг в командной строке live.py"
    esrc = "флаг в командной строке live.py"

    if name == "codex":
        # Флаги появляются в argv только при настройке из окна; без неё
        # codex по-прежнему берёт модель и усилие из своего конфига.
        model = model or _toml_get(CODEX_CFG, "model")
        effort = effort or _toml_get(CODEX_CFG, "model_reasoning_effort")
        if not _flag(argv, "-m"):
            msrc = f"{CODEX_CFG} (окно модель не задавало)"
        if not _flag(argv, "-c"):
            esrc = f"{CODEX_CFG} (окно усилие не задавало)"
    elif name == "kimi":
        model = model or _toml_get(KIMI_CFG, "default_model")
        msrc = (msrc if model and _flag(argv, "-m", "--model")
                else f"{KIMI_CFG}: default_model")
        esrc = "у CLI нет флага усилия; уровень — клиентская настройка"
    elif name == "gemini":
        esrc = "live.py → gemini-http --thinking (thinkingLevel API)"
    elif name == "deepseek":
        model = model or _src_default(
            "deepseek-http", r'DEFAULT_MODEL\s*=\s*"([^"]+)"')
        effort = effort or _src_default(
            "deepseek-http", r'"DEEPSEEK_EFFORT",\s*"(\w+)"')
        msrc = "deepseek-http: DEEPSEEK_MODEL, иначе DEFAULT_MODEL"
        esrc = "deepseek-http: DEEPSEEK_EFFORT, иначе умолчание адаптера"
    elif name == "grok":
        if not model:
            # Флага нет = окно модель не задавало: отвечает дефолт xAI.
            msrc = "флага нет — отвечает серверный дефолт xAI"

    with CFG_LOCK:
        cfg = {sc: dict((VOICE_CFG.get(name) or {}).get(sc) or {})
               for sc in SCOPES}
    # (exec-блок собирается ниже из cfg["exec"])
    room = cfg["room"]
    if room.get("model"):
        model = room["model"]
        msrc = f"задано в окне: {ctl.get('model_env')}={model}"
    if room.get("effort"):
        effort = room["effort"]
        esrc = f"задано в окне: {ctl.get('effort_env')}={effort}"
    # Раунды («дирижёр»): настройка из окна либо умолчание choir.py —
    # второе описано строкой в VOICE_CTL, живого зонда для choir нет.
    rd = cfg["rounds"]
    rounds_card = {
        "model": rd.get("model"),
        "effort": rd.get("effort"),
        "source": ("задано в окне (CHOIR_ROUND_*)" if rd else
                   ctl.get("rounds_default", "умолчание choir.py")),
    }

    card = {
        "name": name,
        # None, а не «неизвестно» строкой: пустое значение обязано быть
        # отличимо от значения по имени «неизвестно».
        "model": model, "model_source": msrc,
        "effort": effort, "effort_source": esrc,
        "efforts": EFFORTS.get(name, []),
        "efforts_source": EFFORTS_SRC_BY.get(name, EFFORTS_SRC),
        "models": ctl.get("models", []),
        "can_set_model": bool(ctl.get("model_env")),
        "can_set_effort": bool(ctl.get("effort_env")),
        "set_in_window": (cfg if any(cfg.get(sc) for sc in SCOPES)
                          else None),
        "rounds": rounds_card,
        # Кресло (вкладка 🔧): рычаги только там, где за ними механика
        # (правило 8.5): у dsh их нет вовсе, у claude — только модель,
        # у kimi — только модель, у grok — только усилие.
        "exec": ({
            "model": (cfg.get("exec") or {}).get("model"),
            "effort": (cfg.get("exec") or {}).get("effort"),
            "pool": (cfg.get("exec") or {}).get("pool",
                     name not in edits.EDIT_COSTLY),
            "costly": name in edits.EDIT_COSTLY,
            "can_model": name in ("codex", "claude", "kimi", "deepseek"),
            "can_effort": name in ("codex", "grok"),
            "seat": {"deepseek": "dsh"}.get(name, name),
        } if name in edits.EDIT_VOICES else None),
        "applies_to": ctl.get("applies_to", []),
        "scope": ctl.get("scope", ""),
        "locked_why": ctl.get("locked", ""),
        "limit": limit,
    }
    # Шкал у голоса может быть НЕСКОЛЬКО (у claude их три). Список
    # кладём отдельным полем и ТОЛЬКО когда он непуст: пустой массив
    # читается как «сервер шкал не прислал», а это другое утверждение,
    # чем «этот канал остатка не сообщает» — и второе живёт в `limit`.
    gauges = limit.get("limits") if isinstance(limit, dict) else None
    if isinstance(gauges, list) and gauges:
        card["limits"] = gauges
    return card


# ── лимиты: только то, что провайдер сказал сам ──────────────────────
# Четыре состояния, и они РАЗНЫЕ:
#   number  — есть живое число (сколько израсходовано / сколько
#             осталось) и время замера;
#   refusal — числа нет, но есть последний отказ по квоте: когда и
#             какими словами. Иногда провайдер приложил цифры прямо к
#             отказу (kimi: current/limit) — тогда они здесь же, но со
#             stale: true, потому что это снимок момента отказа, а не
#             остаток сейчас;
#   none    — провайдер не сообщает ничего;
#   unknown — замер ещё не сделан этим окном (не путать с none).
# Оговорка, без которой индикатор врёт: ОТСУТСТВИЕ ЗАПИСИ ОБ ОТКАЗЕ НЕ
# ЗНАЧИТ, ЧТО КВОТА ЦЕЛА. Пустое поле, прочитанное как зелёный свет, —
# та же ошибка, что «ПАС читается как согласие», поэтому в каждом none
# стоит эта оговорка текстом.
#
# ЧТО ИЗМЕНИЛОСЬ 2026-08-25 (разведка по всем шести каналам, проверено
# живыми запросами, а не предположено). Раньше окно читало только диск,
# и половина стола висела в «none». Оказалось, четыре провайдера отдают
# состояние БЕСПЛАТНЫМ GET'ом, без единого модельного вызова:
#   claude    api.anthropic.com/api/oauth/usage       — ТРИ окна сразу
#   codex     chatgpt.com/backend-api/codex/usage     — used_percent + сброс
#   kimi      api.moonshot.ai/v1/users/me{,/balance}  — баланс + потолки
#   deepseek  api.deepseek.com/user/balance           — баланс (было и раньше)
# А два не отдают, и это тоже ИЗМЕРЕНО:
#   grok      шкала существует, но живёт за веб-сессией grok.com: POST
#             /rest/rate-limits токену CLI отвечает 403 «Action cannot be
#             performed by OAuth2 token users [WKE=unauthorized:
#             oauth2-auth-forbidden]» — отказ адресный и намеренный.
#             Показываем тариф и конец оплаченного периода; шкалу — нет.
#   gemini    /v1beta/quota и /v1beta/usage → 404, заголовков
#             x-ratelimit-* на успешном ответе нет вовсе. Известно
#             только, сколько ключей в ротации и какой активен.
#
# ШКАЛ У ГОЛОСА МОЖЕТ БЫТЬ НЕСКОЛЬКО, И СЛИВАТЬ ИХ НЕЛЬЗЯ. У Клода их
# три одновременно: 5-часовое окно, недельное по всем моделям и
# отдельное недельное по модели Fable (замер 2026-08-25: 92 %, 84 % и
# 100 %). Среднее между ними не значит ничего, «худшее из» скрывает,
# какое именно окно кончилось, а решение «звать ли голос» принимают по
# конкретному окну. Поэтому наружу идёт СПИСОК шкал, у каждой своё имя,
# своё окно и своё время сброса.
#
# ЧЕГО ТУТ НЕТ НАМЕРЕННО: выдуманных чисел. Ни одна цифра ниже не
# вычисляется окном — все приходят от провайдера или лежат на диске, и
# у каждой рядом стоит source.
#
# Источник отказов — room.jsonl, где choir.py пишет status="quota" с
# цифрами провайдера. live.jsonl намеренно НЕ читается: там отказ по
# квоте записан как kind="error", detail="код 1" — механики различения
# в live.py нет, и индикатор, гадающий по тексту, был бы полем,
# необеспеченным механикой (правило 8.5).
# 180 с, а не 30: эндпоинт расхода Клода сам ограничивает частоту
# опроса и на 30-секундном шаге отвечает 429 — то есть частый замер не
# уточнял шкалу, а ГАСИЛ её (нашёл grok, я поймал 429 живьём). Диску
# и логам чаще тоже незачем.
LIMITS_TTL = 180.0
FIRST_WAIT = 1.5            # сколько ждёт САМЫЙ ПЕРВЫЙ запрос, потом ноль
NET_TIMEOUT = 6.0           # короткий: шкала не стоит того, чтобы ждать
ROOM = CHOIR / "room.jsonl"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Где лежат ключи и токены. ЧИТАЮТСЯ НА КАЖДОМ ЗАМЕРЕ, а не один раз при
# старте: токен Клода живёт ~8 часов и обновляется самим Claude Code,
# кэш в памяти окна протух бы молча и показал бы «токен не принят» при
# живом токене.
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CLAUDE_USAGE = "https://api.anthropic.com/api/oauth/usage"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_USAGE = "https://chatgpt.com/backend-api/codex/usage"
KIMI_API = "https://api.moonshot.ai/v1"
GROK_AUTH = Path.home() / ".grok" / "auth.json"
GROK_SETTINGS = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_SUBS = "https://grok.com/rest/subscriptions"
GEMINI_KEYS = Path.home() / ".gemini" / "keys.txt"
GEMINI_ROT = Path.home() / ".gemini" / ".rot_last_http"

_LIM = {"ts": 0.0, "data": {}, "busy": False}
_LIM_LOCK = threading.Lock()
_LIM_READY = threading.Event()


def _iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, timezone.utc).isoformat(
        timespec="seconds")


_SECRET_RE = re.compile(
    r"(Bearer\s+\S+|sk-[A-Za-z0-9_\-]{8,}|ak-[A-Za-z0-9_\-]{8,}|"
    r"xai-[A-Za-z0-9_\-]{8,}|AIza[A-Za-z0-9_\-]{8,})")


def _no_secrets(text: str) -> str:
    """Вырезать похожее на ключ из ЧУЖОГО текста, прежде чем показывать.

    Тело ответа эндпоинта уходит в `note` карточки, то есть в /voices и
    в подсказку панели. Ревьюер дифа поднял локальный эндпоинт,
    отражающий заголовок запроса, и получил в карточке
    `HTTP 401: {"a": "Bearer sk-FAKE-SECRET-…"}`. Достижимо не только в
    лаборатории: адрес DeepSeek берётся из DEEPSEEK_BASE_URL.
    """
    return _SECRET_RE.sub("‹вырезано›", text or "")


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект только в пределах того же хоста.

    В заголовках здесь ездят oauth-токен Клода и ключи Кими; urllib по
    умолчанию идёт за 301/302 и УНОСИТ Authorization на новый хост.
    Тогда сохранность секретов Автора зависела бы от честности чужого
    эндпоинта — а она не наша (нашёл kimi).
    """

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        from urllib.parse import urlparse
        if urlparse(newurl).netloc != urlparse(req.full_url).netloc:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"редирект на другой хост ({urlparse(newurl).netloc}) — "
                f"не иду, там заголовок с секретом", hdrs, fp)
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)


_SAFE_OPENER = urllib.request.build_opener(_SameHostRedirect)


def _get_json(url: str, headers: dict, timeout: float = NET_TIMEOUT
              ) -> tuple[int, object, str]:
    """GET → (код, тело, короткая причина отказа).

    Ошибку возвращаем, а не бросаем: «эндпоинт ответил 401» и «сети нет»
    — разные факты, и оба обязаны доехать до карточки. Молчаливый
    except превратил бы их в одинаковое «none», то есть в поле, которое
    врёт (правило 8.5).
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        with _SAFE_OPENER.open(req, timeout=timeout) as r:
            return r.status, json.load(r), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:200].decode("utf-8", errors="replace")
        except Exception:                           # noqa: BLE001
            body = ""
        return e.code, None, f"HTTP {e.code}: {_no_secrets(body)[:160]}"
    except Exception as e:                          # noqa: BLE001
        return 0, None, f"{type(e).__name__}: {e}"


def _tail(path: Path, nbytes: int) -> str:
    """Хвост файла. Именно хвост: логи CLI растут десятками мегабайт, а
    индикатор не имеет права стоить секунду чтения на каждый опрос."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _newest(paths, n: int) -> list[Path]:
    got = []
    for p in paths:
        try:
            got.append((p.stat().st_mtime, p))
        except OSError:
            continue
    got.sort(reverse=True)
    return [p for _, p in got[:n]]


NO_DATA_CAVEAT = ("отсутствие записи об отказе НЕ значит, что квота "
                  "цела: у части голосов отказ так просто не выглядит")


def _no_data(why: str) -> dict:
    return {"kind": "none", "note": why, "caveat": NO_DATA_CAVEAT}


def _gauge(name: str, **kw) -> dict:
    """Одна шкала. `name` — короткая подпись (страница режет до 20
    символов), дальше числа провайдера как есть.

    Обязательное поле — `source`: «86 %» без подписи «откуда» через
    месяц неотличимо от догадки. Ключи оставлены плоскими (known /
    meaning / total / unit / window_minutes / resets_at) — ровно те,
    что уже читает страница; переименование ради красоты стоило бы
    молчаливой потери показаний.
    """
    g = {"name": name, "kind": "number"}
    g.update(kw)
    return g


def _pack(gauges: list[dict], **extra) -> dict:
    """Карточка лимита голоса из списка шкал.

    Наружу уходит И список (`limits`), И плоская главная шкала — второе
    для читателя, знающего только старую одиночную форму. Список
    пустым в ответ не кладём вовсе: пустой массив читается как «шкал
    нет, потому что сервер их не прислал», а это другое утверждение,
    чем «этот канал остатка не сообщает».
    """
    gauges = [g for g in gauges if g]
    if not gauges:
        return {"kind": "none", "caveat": NO_DATA_CAVEAT, **extra}
    head = gauges[0]
    out = {k: v for k, v in head.items() if k != "name"}
    out["limits"] = gauges
    out.update(extra)
    # note главной шкалы не должен затирать общее пояснение канала
    if "note" in extra:
        out["note"] = extra["note"]
    return out


def _room_quota() -> dict[str, dict]:
    """Последний отказ по квоте на голос — из room.jsonl. Файл в
    единицах мегабайт и читается целиком: он и есть журнал отказов,
    а хвост потерял бы старые отказы тех голосов, кто давно не падал."""
    out: dict[str, dict] = {}
    try:
        with ROOM.open("r", encoding="utf-8") as f:
            for line in f:
                if '"quota"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:               # noqa: BLE001
                    continue
                if o.get("status") != "quota":
                    continue
                v = o.get("voice")
                if not isinstance(v, str):
                    continue
                out[v] = {"at": o.get("ts"),
                          "text": ANSI_RE.sub("", (o.get("detail") or ""))[:300],
                          "round": o.get("round")}
    except OSError:
        pass
    return out


# ── claude: три окна одним бесплатным GET'ом ─────────────────────────
# Эндпоинт тот же, который CLI дёргает сам, чтобы нарисовать /usage
# (найден в бандле 2.1.243: `fetchUtilization: GET /api/oauth/usage`).
# Модель при этом не вызывается — платы нет.
#
# ТОКЕН ПЕРЕЧИТЫВАЕМ С ДИСКА КАЖДЫЙ РАЗ и НИКОГДА не обновляем сами:
# accessToken живёт ~8 часов, ротацию ведёт сам Claude Code, и вторая
# ротация наперегонки с ним разлогинит Автора. На 401 честно пишем
# «токен не принят», а не молчим.
#
# Отдельно: файлу ~/.claude/daemon-auth-status.json верить нельзя — там
# лежало {"status":"auth_required"} при заведомо живом токене (замер
# 2026-08-25). Спрашиваем эндпоинт, а не чужой кэш.
#
# ЧЕРЕЗ CLI НЕ ХОДИМ И НЕ ПОЙДЁМ. `claude auth status --json` отдаёт
# subscriptionType (тариф, а не остаток), но стоит 5.8 секунды на запуск
# и с stdin=DEVNULL — как его и надо звать из сервера — падает с SIGSEGV
# (rc=-11, замер 2026-08-25 на claude 2.1.233). Весь сбор лимитов
# остался без единого запуска CLI: чтение диска плюс бесплатные GET'ы.
# Подписи РАЗНЫЕ намеренно. Две строки «неделя 84 %» и «неделя 100 %»
# рядом — это потеря ровно того, ради чего список и заведён: какое
# именно окно кончилось. У Автора три лимита (5 часов, неделя общая,
# неделя отдельно на Fable), и «Fable» видно только в scoped-строке
# (нашёл ревьюер дифа, воспроизвёл на ответе без display_name).
_CL_NAMES = {"session": "5 часов", "weekly_all": "неделя (всё)",
             "weekly_scoped": "неделя (модель)"}
# Длину окна берём по ПРЕФИКСУ вида: раньше искали по полю `group`, а
# там приходит weekly_all / weekly_scoped, и словарь с ключом `weekly`
# не находил ничего — window_minutes уходил None, страница теряла
# длину окна. Комментарий при этом описывал не тот механизм.
_CL_WINDOW = {"session": 300, "weekly": 10080}


def _cl_window(kind: str, group: str) -> int | None:
    for key in (group, kind):
        if key in _CL_WINDOW:
            return _CL_WINDOW[key]
    if str(kind).startswith("weekly") or str(group).startswith("weekly"):
        return _CL_WINDOW["weekly"]
    return None


def _lim_claude_api() -> tuple[list[dict], dict] | None:
    try:
        creds = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not token:
        return None
    # Заголовки как у самого CLI. Без User-Agent и anthropic-beta
    # эндпоинт отвечает стойким 429, и панель показывала «отказ по
    # квоте» там, где квота ни при чём: это был отказ МОНИТОРА, а не
    # голоса — то есть поле врало ровно про то, ради чего заведено
    # (нашёл grok; 429 я и сам поймал живьём).
    code, d, err = _get_json(CLAUDE_USAGE,
                             {"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json",
                              "anthropic-beta": "oauth-2025-04-20",
                              "User-Agent": "claude-cli/roundtable"})
    if not isinstance(d, dict):
        why = ("токен не принят (401): его обновляет сам Claude Code, "
               "окно в ротацию не лезет" if code == 401 else
               "429 от эндпоинта расхода: это ограничение НА ОПРОС "
               "монитора, а не квота голоса — шкала просто не измерена"
               if code == 429 else
               f"эндпоинт расхода не ответил — {err}")
        return [], {"kind": "none", "note": why, "caveat": NO_DATA_CAVEAT,
                    # Отказ ОПРОСА, не квоты голоса: по этому флагу
                    # collect_limits вправе показать последний удачный
                    # замер вместо пустоты («Клод не видит опять»).
                    "monitor_refusal": True,
                    "source": CLAUDE_USAGE,
                    "plan": oauth.get("subscriptionType") or ""}
    now = _iso(time.time())
    gauges: list[dict] = []
    for row in (d.get("limits") or []):
        if not isinstance(row, dict):
            continue
        pct = row.get("percent")
        if not isinstance(pct, (int, float)):
            continue
        kind = str(row.get("kind") or "")
        name = _CL_NAMES.get(kind, kind or "окно")
        model = (((row.get("scope") or {}).get("model") or {})
                 .get("display_name"))
        if model:
            name = f"{name} · {model}"
        g = _gauge(name, meaning="used", known=float(pct), total=100.0,
                   unit="percent", measured_at=now, source=CLAUDE_USAGE,
                   note="процент израсходованного окна, как его считает "
                        "провайдер: сюда входит вся работа Автора этим "
                        "аккаунтом, не только стол")
        win = _cl_window(str(kind), str(row.get("group") or ""))
        if win:
            g["window_minutes"] = win
        if isinstance(row.get("resets_at"), str):
            g["resets_at"] = row["resets_at"]
        if row.get("severity"):
            g["severity"] = row["severity"]
        g["active"] = bool(row.get("is_active"))
        gauges.append(g)
    if not gauges:
        # limits[] не пришёл — берём то же самое из именованных полей.
        for key, name, win in (("five_hour", "5 часов", 300),
                               ("seven_day", "неделя", 10080)):
            w = d.get(key)
            if isinstance(w, dict) and isinstance(
                    w.get("utilization"), (int, float)):
                g = _gauge(name, meaning="used",
                           known=float(w["utilization"]), total=100.0,
                           unit="percent", window_minutes=win,
                           measured_at=now, source=CLAUDE_USAGE)
                if isinstance(w.get("resets_at"), str):
                    g["resets_at"] = w["resets_at"]
                gauges.append(g)
    extra = {"plan": oauth.get("subscriptionType") or "",
             "note": f"источник — {CLAUDE_USAGE}, тот же запрос, которым "
                     f"CLI рисует /usage; модель не вызывается, платы нет"}
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)):
        extra["token_expires_at"] = _iso(exp / 1000.0)
    return gauges, extra


def _find_key(obj, key: str):
    """Первое вхождение ключа в дереве json. Транскрипты Клода меняют
    форму между версиями CLI, и жёсткий путь к полю ломается тихо."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            got = _find_key(v, key)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_key(v, key)
            if got is not None:
                return got
    return None


def _lim_claude_disk(room: dict) -> dict | None:
    """Запасной путь, когда эндпоинт не ответил: наш собственный журнал
    room.jsonl (там отказ записан как status=quota со словами
    провайдера) плюс resetsAt из транскрипта CLI, если найдётся.

    Оставлен намеренно: сеть падает, токен протухает, а «когда сбросится»
    из последнего отказа — единственное, что остаётся. Порядок такой,
    потому что room.jsonl — про НАШ вызов, транскрипт — про любой,
    включая ручную работу Автора в другом окне."""
    reset = kind_of = at = src = None
    base = Path.home() / ".claude" / "projects"
    for pth in _newest(base.rglob("*.jsonl"), 12):
        txt = _tail(pth, 256 * 1024)
        if '"quotaLimits"' not in txt:
            continue
        for line in reversed(txt.splitlines()):
            if '"quotaLimits"' not in line or '"rejected"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:                   # noqa: BLE001
                continue
            q = _find_key(o, "quotaLimits")
            if not isinstance(q, dict) or q.get("status") != "rejected":
                continue
            ts = o.get("timestamp") or ""
            if at and ts <= at:
                break
            at, src, reset, kind_of = (ts, str(pth), q.get("resetsAt"),
                                       q.get("rateLimitType"))
            break
    r = room.get("claude")
    if not at and not r:
        return None
    lim = {"kind": "refusal",
           "at": (r or {}).get("at") or at,
           "text": (r or {}).get("text")
                   or f"отказ по лимиту ({kind_of or 'тип не назван'})",
           "source": str(ROOM) if r else src,
           "note": "живой расход не запросился; это последний отказ, а не "
                   "остаток сейчас"}
    if isinstance(reset, (int, float)):
        # Сброс — из транскрипта CLI, и это МОЖЕТ БЫТЬ другой отказ, не
        # тот, что в room.jsonl. Поэтому у него своё время рядом.
        lim["resets_at"] = _iso(reset)
        lim["rate_limit_type"] = kind_of
        lim["reset_from"] = {"at": at, "source": src}
    return lim


def _lim_claude(room: dict) -> dict:
    got = _lim_claude_api()
    if got is None:
        return (_lim_claude_disk(room)
                or _no_data(f"нет {CLAUDE_CREDS} — окно не знает, чьим "
                            f"токеном спрашивать расход"))
    gauges, extra = got
    if gauges:
        return _pack(gauges, **extra)
    # Эндпоинт не дал чисел — падаем на диск, но причину называем.
    disk = _lim_claude_disk(room)
    if disk:
        # Причину дописываем ОТДЕЛЬНЫМ ключом, а не поверх пояснения
        # диска. Раньше note диска затирался — вместе с его главной
        # оговоркой «это ПОСЛЕДНИЙ отказ, а не остаток сейчас», — и в
        # карточке оставалось утверждение про эндпоинт, который чисел не
        # дал: два противоречащих источника подряд (нашёл ревьюер дифа;
        # так же уже сделано у codex).
        out = {**extra, **disk}
        api_note = f"{extra.get('note') or ''}".strip()
        if api_note:
            out["api_note"] = api_note
        return out
    return {**extra, "caveat": NO_DATA_CAVEAT,
            "kind": extra.get("kind", "none")}


# ── codex: одно окно, живой эндпоинт вместо ковыряния в логах ────────
# ЗАГОЛОВКИ `originator` И `User-Agent` ОБЯЗАТЕЛЬНЫ: без любого из них
# Cloudflare перед chatgpt.com отдаёт 403 с html-заглушкой (проверено
# порознь 2026-08-25). Точность версии в UA не важна — с заведомо
# несуществующей 9.9.9 ответ тот же 200; важен сам вид строки.
def _codex_ua_version() -> str:
    """Версия для User-Agent. Берём из ~/.codex/version.json, если он
    есть; иначе — последняя проверенная. Это строка для Cloudflare, а не
    утверждение о том, какой CLI установлен, поэтому запускать `codex
    --version` ради неё (полсекунды на каждый замер) не стоит."""
    try:
        v = json.loads(CODEX_AUTH.with_name("version.json").read_text(
            encoding="utf-8")).get("latest_version")
        if isinstance(v, str) and re.fullmatch(r"[\d.]{1,16}", v):
            return v
    except Exception:                               # noqa: BLE001
        pass
    return "0.147.0"


def _lim_codex_api() -> dict | None:
    try:
        tk = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))["tokens"]
        token, acc = tk["access_token"], tk["account_id"]
    except Exception:                               # noqa: BLE001
        return None
    ver = _codex_ua_version()
    code, d, err = _get_json(CODEX_USAGE, {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": acc,
        "originator": "codex_cli_rs",
        "version": ver,
        "User-Agent": f"codex_cli_rs/{ver} (RoundTable; limits probe)"})
    if not isinstance(d, dict):
        return {"kind": "none", "source": CODEX_USAGE,
                "note": f"живой расход не запросился — {err}",
                "caveat": NO_DATA_CAVEAT}
    rl = d.get("rate_limit") or {}
    now = _iso(time.time())
    gauges: list[dict] = []
    # Окон может быть два (primary/secondary). На тарифе plus secondary
    # приходит null — но КОД ЭТОГО НЕ ПРЕДПОЛАГАЕТ: появится второе
    # окно, оно просто станет второй шкалой, а не потеряется молча.
    for key, label in (("primary_window", "окно"),
                       ("secondary_window", "второе окно")):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percent")
        if not isinstance(pct, (int, float)):
            continue
        secs = w.get("limit_window_seconds")
        name = label
        if isinstance(secs, (int, float)) and secs >= 60:
            mins = int(secs // 60)
            name = (f"{mins // 1440} сут" if mins % 1440 == 0 else
                    f"{mins // 60} ч" if mins % 60 == 0 else f"{mins} мин")
        g = _gauge(name, meaning="used", known=float(pct), total=100.0,
                   unit="percent", measured_at=now, source=CODEX_USAGE,
                   note="процент израсходованного окна по данным "
                        "провайдера — вся работа этим аккаунтом, не "
                        "только стол")
        if isinstance(secs, (int, float)) and secs >= 60:
            g["window_minutes"] = int(secs // 60)
        if isinstance(w.get("reset_at"), (int, float)):
            g["resets_at"] = _iso(w["reset_at"])
        gauges.append(g)
    extra: dict = {"plan": d.get("plan_type") or "", "source": CODEX_USAGE}
    cr = d.get("credits") or {}
    bal = cr.get("balance")
    if bal not in (None, ""):
        extra["credits"] = str(bal)
    if rl.get("limit_reached"):
        extra["reached"] = True
    if not gauges:
        extra.update({"kind": "none", "caveat": NO_DATA_CAVEAT,
                      "note": "эндпоинт ответил, но окна расхода в ответе "
                              "не оказалось"})
        return extra
    extra["note"] = (f"источник — {CODEX_USAGE} (бесплатный GET, модель не "
                     f"вызывается)")
    return _pack(gauges, **extra)


def _lim_codex_disk() -> dict | None:
    """Запасной путь: на каждый ход Кодекс пишет в свой rollout событие
    token_count с rate_limits. Это СНИМОК последнего вызова, а не
    состояние сейчас, — так и подписан.

    Свежесть файла (mtime) и свежесть ЗАПИСИ — разные вещи: сессию можно
    возобновить, и тогда самый новый по mtime файл содержит запись
    позавчерашнюю. Поэтому смотрим пять новейших файлов и выбираем
    запись с максимальным timestamp."""
    base = Path.home() / ".codex" / "sessions"
    best: dict | None = None
    for pth in _newest(base.rglob("rollout-*.jsonl"), 5):
        for line in reversed(_tail(pth, 512 * 1024).splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:                   # noqa: BLE001
                continue                        # обрезанная первая строка
            pr = (((o.get("payload") or {}).get("rate_limits")
                   or {}).get("primary") or {})
            if not isinstance(pr.get("used_percent"), (int, float)):
                continue
            g = _gauge("окно", meaning="used",
                       known=float(pr["used_percent"]), total=100.0,
                       unit="percent", stale=True,
                       measured_at=o.get("timestamp") or "",
                       source=str(pth),
                       note="снимок НА МОМЕНТ последнего вызова codex, а "
                            "не расход сейчас: живой эндпоинт не ответил")
            if isinstance(pr.get("window_minutes"), int):
                g["window_minutes"] = pr["window_minutes"]
                g["name"] = (f"{pr['window_minutes'] // 1440} сут"
                             if pr["window_minutes"] % 1440 == 0
                             else g["name"])
            if isinstance(pr.get("resets_at"), (int, float)):
                g["resets_at"] = _iso(pr["resets_at"])
            if best is None or g["measured_at"] > best["measured_at"]:
                best = g
            break
    return _pack([best]) if best else None


def _lim_codex() -> dict:
    api = _lim_codex_api()
    if api and api.get("limits"):
        return api
    disk = _lim_codex_disk()
    if disk:
        if api:
            disk["api_note"] = api.get("note", "")
        return disk
    return api or _no_data(
        "codex пишет rate_limits на каждый ход, но ни живой эндпоинт, ни "
        "записи на диске сейчас недоступны")


# ── deepseek: предоплаченный баланс ──────────────────────────────────
def _lim_deepseek() -> dict | None:
    """Живое число: предоплаченный баланс. Запрос бесплатный (не
    модельный вызов). Связь с отказом прямая и измеренная: адаптер
    `deepseek-http` ставит `exit 3` ровно на HTTP 402/429 — то есть
    обнуление этого баланса и есть будущий `quota` в журнале. Поэтому
    предупредить можно заранее, а не по факту отказа."""
    try:
        key = DS_KEY.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not key:
        return None
    code, d, err = _get_json(f"{DS_BASE}/user/balance",
                             {"Authorization": f"Bearer {key}"})
    if not isinstance(d, dict):
        return {"kind": "none", "source": f"{DS_BASE}/user/balance",
                "note": f"баланс не запросился — {err}",
                "caveat": NO_DATA_CAVEAT}
    infos = d.get("balance_infos") or []
    usd = next((i for i in infos if i.get("currency") == "USD"),
               infos[0] if infos else None)
    if not usd:
        return None
    try:
        left = float(usd.get("total_balance"))
    except (TypeError, ValueError):
        return None
    g = _gauge("баланс", meaning="left", known=left,
               unit=usd.get("currency", "USD").lower(),
               measured_at=_iso(time.time()),
               source=f"{DS_BASE}/user/balance",
               note="предоплаченный баланс; общего потолка нет, поэтому "
                    "и поля total здесь нет — шкалу не из чего строить")
    return _pack([g], available=bool(d.get("is_available")),
                 note="обнуление баланса даст 402 → adapter exit 3 → "
                      "статус quota в журнале")


# ── kimi: баланс живой, расход провайдер не отдаёт ───────────────────
_KIMI_429 = re.compile(
    r"^(\d{4}-\d\d-\d\dT[\d:.]+Z).*?current: (\d+), limit: (\d+)")


def _kimi_key() -> str | None:
    """Ключ первой ПО АЛФАВИТУ линии из ~/.kimi-code/config.toml.

    Именно по алфавиту, а не «первой в файле»: сейчас это совпадает
    (`moonshotai` < `moonshotai2`), но провайдер с именем `alt` или
    `backup` сместил бы выбор молча (ревьюер дифа поправил докстроку —
    подпись обязана описывать механику, а не намерение).

    Спрашиваем ровно одну линию намеренно. У Кими в конфиге два
    провайдера (`moonshotai`, `moonshotai2`) с РАЗНЫМИ ключами, но
    /v1/users/me по обоим возвращает один и тот же org/project/user и
    один и тот же баланс (сверено 2026-08-25). Вторая линия даёт вторую
    параллельную сессию — но не второй кошелёк и не второй лимит.
    Показать два баланса значило бы показать один и тот же дважды:
    поле, которое врёт (правило 8.5)."""
    try:
        import tomllib
        with KIMI_CFG.open("rb") as f:
            provs = tomllib.load(f).get("providers") or {}
    except Exception:                               # noqa: BLE001
        return None
    for _, p in sorted(provs.items()):
        if isinstance(p, dict) and isinstance(p.get("api_key"), str):
            return p["api_key"]
    return None


def _lim_kimi_log() -> dict | None:
    """Расход у Кими виден только в момент 429 — и лежит в его же логе
    сессии. Это снимок момента отказа, а не остаток сейчас, и подписан
    он именно так: stale. Тот же счётчик, из-за которого Кими трое суток
    числился «медленным», хотя упирался в лимит организации.

    Не «двадцать пять новейших»: последний 429 лежал в логе с 28-м
    местом по mtime — сессия отказала и больше не трогалась, пока рядом
    дописывались живые."""
    base = Path.home() / ".kimi-code" / "sessions"
    best: tuple[str, str, int, int] | None = None
    for pth in _newest(base.glob("*/*/logs/kimi-code.log"), 200):
        for line in reversed(_tail(pth, 128 * 1024).splitlines()):
            m = _KIMI_429.match(line)
            if m:
                cand = (m.group(1), str(pth), int(m.group(2)),
                        int(m.group(3)))
                if best is None or cand[0] > best[0]:
                    best = cand
                break
    if best is None:
        return None
    at, src, cur, tot = best
    return _gauge("последний 429", kind="refusal", meaning="used",
                  known=cur, total=tot, unit="tokens", at=at,
                  measured_at=at, stale=True, source=src,
                  text=f"429: израсходовано {cur} из {tot} токенов",
                  note="число известно НА МОМЕНТ ОТКАЗА, не сейчас: на "
                       "успешном вызове Кими счётчика не сообщает")


def _lim_kimi(room: dict) -> dict:
    key = _kimi_key()
    gauges: list[dict] = []
    extra: dict = {}
    if key:
        hdr = {"Authorization": f"Bearer {key}"}
        code, d, err = _get_json(f"{KIMI_API}/users/me/balance", hdr)
        data = (d or {}).get("data") if isinstance(d, dict) else None
        bal = (data or {}).get("available_balance")
        if isinstance(bal, (int, float)):
            gauges.append(_gauge(
                "баланс", meaning="left", known=float(bal), unit="usd",
                measured_at=_iso(time.time()),
                source=f"{KIMI_API}/users/me/balance",
                note="ОДИН кошелёк на обе линии (moonshotai и "
                     "moonshotai2 — один org/project/user); потолка у "
                     "баланса нет, поэтому нет и шкалы"))
        else:
            extra["note"] = f"баланс не запросился — {err}"
        code, d, err = _get_json(f"{KIMI_API}/users/me", hdr)
        org = (((d or {}).get("data") or {}).get("organization")
               if isinstance(d, dict) else None)
        if isinstance(org, dict):
            # Потолки — НЕ шкала: израсходованное сервер не отдаёт
            # (organization_usage приходит пустым объектом). Кладём их
            # как справку, чтобы никто не построил из потолка процент.
            extra["ceilings"] = {
                "max_concurrency": org.get("max_concurrency"),
                "max_request_per_minute": org.get("max_request_per_minute"),
                "max_token_per_minute": org.get("max_token_per_minute"),
                "max_token_quota": org.get("max_token_quota")}
            extra["ceilings_source"] = f"{KIMI_API}/users/me"
            # Расхождение с каноном называем вслух, а НЕ чиним молча:
            # в Choir/CLAUDE.md и в шапке serial_gate.py записано
            # «concurrency = 1», сервер сейчас отвечает другое. Проверить
            # это может только живой параллельный вызов, а не наш GET, —
            # поэтому очередь трогать нельзя, а промолчать нельзя тем
            # более (правило 4).
            if org.get("max_concurrency") not in (None, 1):
                extra["canon_mismatch"] = (
                    f"сервер отвечает max_concurrency="
                    f"{org.get('max_concurrency')}, а в Choir/CLAUDE.md и "
                    f"serial_gate.py записано 1. Не проверено живым "
                    f"параллельным вызовом — очередь оставлена как есть")
    else:
        extra["note"] = f"ключа не нашлось в {KIMI_CFG}"
    snap = _lim_kimi_log()
    if snap:
        gauges.append(snap)
    r = room.get("kimi")
    if r:
        extra["room_at"], extra["room_text"] = r.get("at"), r.get("text")
    if not gauges and not r:
        return {**extra, **_no_data(
            extra.get("note") or "Кими сообщает расход только в момент "
                                 "429; отказов в логах нет")}
    if not gauges:
        # `**extra` ПЕРВЫМ: иначе его note («баланс не запросился —
        # HTTP 401…») затирал пояснение к отказу из room.jsonl, и
        # карточка показывала одно, а поясняла другое (ревьюер дифа).
        return {**extra, "kind": "refusal", **r, "source": str(ROOM),
                "note": "цифр провайдера в логах не нашлось"}
    return _pack(gauges, **extra)


# ── grok: шкалы у CLI-учётки нет, и это ПРОВЕРЕНО ────────────────────
def _grok_spent() -> list[dict]:
    """Расход канала Грока за 5 часов и за сутки — из его же логов.

    Читаем хвост unified.jsonl (он растёт десятками мегабайт, поэтому
    именно хвост) и считаем ответы модели. Величина честная, но чужая
    квоте: это работа ЭТОЙ машины через CLI, а не всё, что Автор
    потратил аккаунтом. Так и подписано.
    """
    log = Path.home() / ".grok" / "logs" / "unified.jsonl"
    now = time.time()
    win = {"за 5 ч": 5 * 3600, "за сутки": 24 * 3600}
    steps = {k: 0 for k in win}
    toks = {k: 0 for k in win}
    sess: dict[str, set] = {k: set() for k in win}
    try:
        tail = _tail(log, 4_000_000)
    except OSError:
        return []
    oldest = None
    for line in tail.splitlines():
        if '"shell.turn.inference_done"' not in line:
            continue
        try:
            d = json.loads(line)
            ts = datetime.fromisoformat(
                str(d.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            continue
        if oldest is None or ts < oldest:
            oldest = ts
        c = d.get("ctx") if isinstance(d.get("ctx"), dict) else {}
        for name, span in win.items():
            if now - ts <= span:
                steps[name] += 1
                sess[name].add(str(d.get("sid") or ""))
                for k in ("prompt_tokens", "completion_tokens"):
                    try:
                        toks[name] += int(c.get(k) or 0)
                    except (TypeError, ValueError):
                        pass        # мусор в поле не должен ронять замер
    if not any(steps.values()):
        return []
    at = _iso(now)
    out = []
    for name, span in win.items():
        # ПОЛНОТА ОКНА: хвост 4 МБ может не покрыть сутки, а ротация
        # лога — тем более. Тогда число — «не менее», и так подписано
        # (нашли kimi и codex независимо).
        full = oldest is not None and now - oldest >= span
        out.append(_gauge(
            name + ("" if full else " (не менее)"),
            meaning="used", known=float(steps[name]),
            # Это ШАГИ МОДЕЛИ, а не вопросы: один ход Грока даёт
            # несколько inference_done по loop_index, и назвать их
            # «вызовами» значило соврать в поле (посчитал codex:
            # 1083 события на 126 сессий).
            unit="шагов модели", measured_at=at, source=str(log),
            note=f"шагов модели за окно; сессий {len(sess[name])}, "
                 f"токенов {toks[name]:,}".replace(",", " ")
                 + ". Считано по логам ЭТОЙ машины — включая работу "
                   "Грока вне стола. Это РАСХОД, а не доля квоты: "
                   "потолка провайдер не сообщает"
                 + ("" if full else ". ОКНО НЕПОЛНОЕ: прочитан хвост "
                    "лога, более старое не учтено")))
    return out


def _lim_grok() -> dict:
    """Тариф и конец оплаченного периода — есть. Шкалы — нет.

    Автор видит проценты в настройках grok.com, и они там правда есть:
    маршрут POST https://grok.com/rest/rate-limits живой. Но токену CLI
    (OIDC из ~/.grok/auth.json) сервер отвечает
    403 «Action cannot be performed by OAuth2 token users
    [WKE=unauthorized:oauth2-auth-forbidden]» — отказ адресный и
    намеренный; подстановка того же токена в куку sso даёт 401. Нужна
    браузерная сессия grok.com, то есть чужой секрет с полными правами
    на аккаунт. Поэтому здесь честное «шкалы нет», а не выдуманный
    процент.

    В самом CLI шкалы тоже нет: единственная проверка подписки —
    булево `x.ai/auth/check_subscription`, про лимит у Грока только
    тексты ошибок постфактум (usage_limit_reached, usage_pool_exhausted).

    Перепроверено 2026-08-25 по просьбе Автора («у остальных должны
    быть»), третьим способом: рабочий эндпоинт cli-chat-proxy.grok.com
    отвечает 200 БЕЗ каких-либо x-ratelimit-заголовков, на диске
    (~/.grok: логи, кэши, auth) остатка нет. ЧЕТЫРЕ независимых прохода
    (последний — 2026-08-31, со свежим токеном сразу после перелогина
    Автора, плюс разбор строк самого бинаря) — итог один: остаток живёт
    только за браузерной сессией. В бинаре есть лишь маркеры отказов
    (`usage_limit_reached`, `usage_pool_exhausted`, `rate_limited`) и
    hook-событие `rate_limit` — то есть момент, КОГДА кончилось, узнать
    можно, а «сколько осталось» — нет.

    Зато на диске есть РАСХОД (см. _grok_spent): его и показываем.
    """
    try:
        auth = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
        ent = next(iter(auth.values()))
        token = ent["key"]
    except Exception:                               # noqa: BLE001
        # Расход из ЛОГОВ токену не нужен: без auth.json он всё равно
        # известен, и терять его вместе с тарифом незачем (нашёл grok).
        spent = _grok_spent()
        if spent:
            return _pack(spent, note="тариф не спрошен: файла "
                         "авторизации нет. Ниже — расход по логам CLI, "
                         "он от токена не зависит",
                         caveat=NO_DATA_CAVEAT)
        return _no_data("grok: файла авторизации нет — ни тарифа, ни "
                        "расхода спросить нечем")
    # РАСХОД ИЗ СОБСТВЕННЫХ ЛОГОВ CLI. Остатка провайдер не отдаёт, но
    # «сколько потрачено» лежит на диске: каждый ответ Грок пишет в
    # ~/.grok/logs/unified.jsonl событием shell.turn.inference_done с
    # prompt_tokens/completion_tokens. Это НЕ доля квоты — потолка мы не
    # знаем и выдумывать его нельзя (правило 8.5), — но это живое число
    # того же канала, растущее с работой стола, вместо пустого места.
    # Наказ Автора 2026-08-31: «посмотри ооочень внимательно на лимиты
    # Грока — в вебинтерфейсе я их вижу».
    gauges = _grok_spent()
    out: dict = {"kind": "number" if gauges else "none",
                 "caveat": NO_DATA_CAVEAT,
                 "source": GROK_SETTINGS}
    if gauges:
        out["limits"] = gauges
        out.update({k: gauges[0][k] for k in
                    ("known", "meaning", "unit", "measured_at")
                    if k in gauges[0]})
    code, d, err = _get_json(GROK_SETTINGS, {"Authorization": f"Bearer {token}"})
    if isinstance(d, dict):
        out["plan"] = d.get("subscription_tier_display") or ""
        out["available"] = bool(d.get("allow_access", True))
        out["measured_at"] = _iso(time.time())
    else:
        out["settings_error"] = err
    # Конец оплаченного периода — не шкала, но единственная величина со
    # временем, которую этот канал вообще отдаёт. UA обязателен: без него
    # Cloudflare отвечает 403 «error code: 1010» (проверено).
    code, d, err = _get_json(GROK_SUBS, {
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "User-Agent": "grok-cli/1.0.3"})
    if isinstance(d, dict):
        act = [s for s in (d.get("subscriptions") or [])
               if isinstance(s, dict)
               and s.get("status") == "SUBSCRIPTION_STATUS_ACTIVE"]
        if act:
            pe = act[0].get("billingPeriodEnd")
            # Число (epoch) — к ISO: чип-таймер на странице молча гас
            # на сыром числе (нашёл субагент прогоном fmtLeft).
            out["period_end"] = (_iso(pe / (1000 if pe > 2e10 else 1))
                                 if isinstance(pe, (int, float)) else pe)
            out["tier"] = act[0].get("tier")
    out["note"] = (
        ("расход посчитан по СОБСТВЕННЫМ логам CLI (~/.grok/logs): "
         "шаги модели и токены этой машины, включая работу Грока вне "
         "стола. Доли квоты здесь нет — потолка провайдер не сообщает. "
         if gauges else "")
        + "остатка нет: проценты у Грока живут за веб-сессией grok.com, а "
        "токену CLI сервер отказывает адресно (403 oauth2-auth-forbidden, "
        "проверено 2026-08-25). Известны тариф"
        + (f" «{out.get('plan')}»" if out.get("plan") else "")
        + (f" и конец периода {out['period_end']}" if out.get("period_end")
           else "") + ".")
    return out


# ── gemini: остатка не отдают вовсе; считаем ключи в ротации ─────────
def _lim_gemini(room: dict) -> dict:
    """Единственное, что можно показать честно: сколько ключей в
    ротации и какой сейчас активен.

    Проверено 2026-08-25: /v1beta/quota и /v1beta/usage → 404,
    serviceusage.googleapis.com API-ключом не читается в принципе
    («API keys are not supported by this API»), а на успешном ответе
    /v1beta/models нет ни одного заголовка x-ratelimit-*. В истории
    ~/.gemini/choir-http/ нет ни quotaMetric, ни quotaValue — то есть
    даже из прошлых 429 остаток не восстановить.

    `exit 3` у адаптера означает «все ключи получили 429», так что
    шкала здесь может быть только дискретной и только постфактум."""
    # Правило отбора ключей копируем у самого адаптера (gemini-http:
    # load_keys) — и минимальную длину читаем ИЗ ЕГО ИСХОДНИКА, чтобы
    # единственное число, которое может разойтись, бралось из файла, а
    # не из памяти этого окна.
    try:
        minlen = int(_src_default("gemini-http", r"len\(s\)\s*>=\s*(\d+)")
                     or 30)
    except (TypeError, ValueError):
        minlen = 30
    keys = 0
    try:
        for line in GEMINI_KEYS.read_text(encoding="utf-8").splitlines():
            s = line.strip().strip('"').replace(" ", "")
            if s and not s.startswith("#") and len(s) >= minlen:
                keys += 1
    except OSError:
        keys = 0
    out: dict = {"kind": "none", "caveat": NO_DATA_CAVEAT,
                 "source": str(GEMINI_KEYS)}
    if keys:
        try:
            idx = int(GEMINI_ROT.read_text(encoding="utf-8").strip()) % keys
        except (OSError, ValueError, ZeroDivisionError):
            idx = None
        out["keys"] = keys
        out["key_index"] = idx
        out["note"] = (
            f"остатка Generative Language API не отдаёт вовсе (проверено: "
            f"/v1beta/quota и /v1beta/usage → 404, x-ratelimit-* нет). "
            f"Известно только: ключей в ротации {keys}"
            + (f", активен №{idx + 1}" if idx is not None else "")
            + f"; exit 3 у gemini-http = все {keys} получили 429.")
    else:
        out["note"] = (f"остатка провайдер не сообщает, а ключей в "
                       f"{GEMINI_KEYS} не нашлось")
    if "gemini" in room:
        out["room_at"] = room["gemini"].get("at")
        out["room_text"] = room["gemini"].get("text")
    return out


# Последний УДАЧНЫЙ замер по голосу. Отказ эндпоинта (429 на опрос,
# сеть, протухший токен) больше не стирает показания с экрана: Автор
# видел «Клод не видит опять», хотя час назад числа были — показываем
# их со СТАРЫМ временем замера (возраст и так подписан) и причиной,
# почему свежих нет. Наказ 2026-08-27.
_LAST_GOOD: dict[str, dict] = {}


def _has_numbers(res: dict) -> bool:
    """Есть ли в замере ЖИВОЕ число. Ревизия сняла две дыры: пустой
    kind=number («объявлено, не прислано») затирал настоящий последний
    удачный (kimi), а любой непустой limits — даже сплошные отказы —
    считался успехом (codex)."""
    if not res or res.get("kind") != "number" or res.get("stale_reason"):
        return False
    rows = res.get("limits") if isinstance(res.get("limits"), list) else [res]
    return any(isinstance(r, dict)
               and any(isinstance(r.get(k), (int, float))
                       for k in ("known", "used", "left"))
               for r in rows)


def collect_limits() -> dict[str, dict]:
    """Один проход по всем источникам.

    Сетевые запросы идут ПАРАЛЛЕЛЬНО и с коротким таймаутом: их шесть,
    последовательно они складывались бы в полминуты на одном тормозящем
    провайдере. Вызывается только из фоновой нити (см. limits_now) —
    в обработчике запроса этого нет никогда, чтобы /voices отвечал
    быстро и при мёртвой сети.

    Платных вызовов здесь нет ни одного: всё либо чтение диска, либо
    бесплатный информационный GET."""
    room = _room_quota()
    jobs = {
        "claude": lambda: _lim_claude(room),
        "codex": _lim_codex,
        "deepseek": lambda: _lim_deepseek() or (
            {"kind": "refusal", **room["deepseek"], "source": str(ROOM),
             "note": "баланс сейчас не запросился; показан последний отказ"}
            if "deepseek" in room else
            _no_data("баланс не запросился (нет ключа или сети)")),
        "kimi": lambda: _lim_kimi(room),
        "grok": _lim_grok,
        "gemini": lambda: _lim_gemini(room),
    }
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs.items()}
        for f in as_completed(futs):
            name = futs[f]
            try:
                out[name] = f.result() or _no_data(
                    "источник этого голоса ничего не вернул")
            except Exception as e:                  # noqa: BLE001
                # Падение сборщика — свойство ОКНА, а не квоты голоса, и
                # называется оно именно так.
                out[name] = _no_data(f"сборщик лимита упал: "
                                     f"{type(e).__name__}: {e}")
    for name in VOICES:
        out.setdefault(name, _no_data("источник для этого голоса неизвестен"))
    # Память последнего удачного: свежий отказ не стирает показания.
    # У голосов, где чисел не бывает в принципе (grok, gemini), памяти
    # не заводится — их карточки честны сами по себе.
    for name, res in out.items():
        if _has_numbers(res):
            _LAST_GOOD[name] = res
        elif name in _LAST_GOOD and res.get("monitor_refusal"):
            # Маскируется ТОЛЬКО отказ САМОГО МОНИТОРА (429 на опрос,
            # сеть): настоящий отказ квоты голоса — факт о канале, и
            # прятать его за старыми шкалами значило бы врать ровно в
            # том месте, ради которого шкалы заведены (grok, deepseek).
            good = dict(_LAST_GOOD[name])
            good["stale_reason"] = (res.get("note")
                                    or res.get("error")
                                    or f"свежий замер: {res.get('kind')}")
            out[name] = good
    return out


def _refresh() -> None:
    data = None
    try:
        data = collect_limits()
    except Exception as e:                      # noqa: BLE001
        print(f"/voices: сбор лимитов упал: {e}", file=sys.stderr)
    finally:
        # finally, а не хвост тела: при BaseException (или падении до
        # входа в try) флаг busy оставался поднятым НАВСЕГДА, и
        # limits_now больше никогда не запускала обновление — шкалы
        # застывали, а подпись «замер N назад» честно старела
        # (нашёл ревьюер дифа).
        with _LIM_LOCK:
            if data is not None:
                _LIM["data"], _LIM["ts"] = data, time.time()
            _LIM["busy"] = False
        _LIM_READY.set()


def limits_now() -> tuple[dict, float]:
    """Снимок лимитов и время замера. /voices НЕ ЖДЁТ сбора: устаревший
    снимок отдаётся сразу, обновление уходит в фоновую нить. Ждёт
    только самый первый запрос за жизнь окна и не дольше FIRST_WAIT —
    иначе первая же карточка была бы сплошным «unknown», а окно,
    задумавшееся на секунды, читается как зависшее."""
    with _LIM_LOCK:
        stale = time.time() - _LIM["ts"] > LIMITS_TTL
        if stale and not _LIM["busy"]:
            _LIM["busy"] = True
            threading.Thread(target=_refresh, daemon=True).start()
        first = _LIM["ts"] == 0.0
    if first:
        _LIM_READY.wait(FIRST_WAIT)
    with _LIM_LOCK:
        return dict(_LIM["data"]), _LIM["ts"]


# ── HTTP ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                      # тишина в терминале
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            # Значение подставляем ПРИ ОТДАЧЕ, а не в исходнике страницы:
            # PAGE — константа модуля, а путь известен только после
            # разбора аргументов. html-экранирование обязательно: в имени
            # каталога может стоять кавычка, и она разорвала бы атрибут.
            body = PAGE.replace("__PROJECT_DEFAULT__",
                                html.escape(PROJECT, quote=True)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.partition("?")[0] == "/events":
            # SSE resume (просил codex): после обрыва клиент шлёт
            # Last-Event-ID (EventSource — сам, благодаря "id:" в _emit)
            # или ?after=<id>, и мы продолжаем с непросмотренного места,
            # а не с «последних 80» — иначе разрыв теряет события.
            raw = self.headers.get("Last-Event-ID")
            if raw is None:
                q = parse_qs(self.path.partition("?")[2])
                raw = (q.get("after") or [None])[0]
            try:
                after = int(raw) if raw is not None else None
            except ValueError:
                after = None
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._stream(after)
        elif self.path == "/state":
            with RUN_LOCK:
                # round/auto отдаём отдельными полями, а не оставляем
                # странице разбирать подпись: подпись — текст для
                # человека, и её правка не должна ломать кнопку «Стоп».
                running = [{"id": aid,
                            "label": t["label"], "voices": t["voices"],
                            "for_s": int(time.time() - t["since"]),
                            "round": t.get("round"),
                            "auto": bool(t.get("auto"))}
                           for aid, t in RUNNING.items()]
            with LOT_LOCK:
                lot = _lot_load()
                winner = lot_winner(lot) if lot else None
            try:
                acts = merge_gate.acts_summary()
            except Exception as e:              # noqa: BLE001
                print(f"/state acts: {e}", file=sys.stderr)
                acts = None                     # None ≠ [] — «не собралось»
            self._json(200, {"voices": VOICES, "running": running,
                             "edit_voices": sorted(edits.EDIT_VOICES),
                             "exec_pool": edits.random_pool(),
                             "acts": acts,
                             # Режим объявлен только теперь, когда он
                             # ДЕЙСТВИТЕЛЬНО обслуживается сервером:
                             # раньше страница тянула голос сама, потому
                             # что сервер о quick молчал.
                             "modes": ["say", "ask", "quick"],
                             "lot": ({"commit": lot["commit"][:16],
                                      "target": lot["target"],
                                      "conductor": winner} if lot else None)})
        elif self.path.partition("?")[0] == "/voices":
            # Карточки голосов: чем отвечает каждый и что о его лимите
            # известно. Лимиты — из кэша (см. limits_now): окно не имеет
            # права задумываться на секунды, когда его опрашивают.
            limits, lts = limits_now()
            self._json(200, {
                "ts": _now(),
                "limits_measured_at": _iso(lts) if lts else None,
                "limits_age_s": int(time.time() - lts) if lts else None,
                "voices": [voice_report(
                    n, limits.get(n) or {"kind": "unknown",
                                         "note": "замер ещё не сделан "
                                                 "этим окном"})
                           for n in VOICES]})
        else:
            self._json(404, {"error": "нет такого пути"})

    def _stream(self, after: int | None = None):
        """Хвост ленты в SSE. Без after — последние 80 событий; с after —
        всё, что новее (id > after): переподключение продолжает чтение,
        а не перечитывает. Ротации у live.jsonl нет, инода стабильна."""
        try:
            with open(FEED, "r", encoding="utf-8") as f:
                lines = f.readlines()
                if after is None:
                    tail = lines[-80:]
                else:
                    tail = [ln for ln in lines if _line_id(ln) > after]
                for line in tail:
                    self._emit(line)
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if line.endswith("\n"):
                        self._emit(line)
                    elif line:
                        # хвост ещё дописывается вторым процессом —
                        # отдать полстроки значит скормить UI битый json
                        f.seek(pos)
                        time.sleep(0.3)
                    else:
                        self.wfile.write(b": keep\n\n")   # heartbeat
                        self.wfile.flush()
                        time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _emit(self, line: str):
        line = line.strip()
        if not line:
            return
        # "id:" перед data — стандарт SSE: браузерный EventSource сам
        # запомнит последний id и пришлёт Last-Event-ID при реконнекте
        eid = _line_id(line)
        head = f"id: {eid}\n".encode() if eid >= 0 else b""
        self.wfile.write(head + b"data: " + line.encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "кривой json"})

        if self.path == "/act":
            text = (req.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "пустая реплика"})
            # Пустой список ≠ «поле не задано». Снять все галочки и
            # нажать «Сказать» означало рассылку ВСЕМ шести — панель
            # подписана «кому уйдёт», а эффект был противоположный, и
            # платный (нашёл ревьюер дифа).
            raw_voices = req.get("voices")
            voices = [v for v in (raw_voices or []) if v in VOICES]
            if raw_voices is not None and not voices:
                return self._json(400, {"error": "не выбран ни один голос "
                                        "— отметьте хотя бы одного"})
            # «Быстрый вопрос» — НЕ синоним say. Подпись кнопки обещает
            # один канал и дешевизну именно от этого; без --once
            # live.py после первого ответа продолжал разговор по ВСЕМУ
            # составу платными вызовами, а поле mode="quick" сервер
            # просто не читал (нашли grok и kimi независимо).
            quick = (req.get("mode") or "") == "quick"
            if quick and req.get("blind"):
                return self._json(400, {"error": "быстрый вопрос и слепой "
                                        "ход — разные режимы, вместе не "
                                        "бывают"})
            picked_by_server = None
            if quick and not voices:
                # Голос не назван — тянет СЕРВЕР, и говорит об этом в
                # ленту. Это не жребий drand (правило 11 про ведущего, а
                # не про мелочь), и называть его жребием нельзя.
                picked_by_server = secrets.choice(VOICES)
                voices = [picked_by_server]
            if quick and len(voices) != 1:
                # Веер по всем шести под подписью «один канал» — это
                # деньги Автора, потраченные не на то, что он нажал.
                return self._json(400, {
                    "error": "быстрый вопрос — ровно к одному голосу",
                    "got": voices})
            mode = "ask" if req.get("blind") else "say"
            if mode == "ask":
                # Барьер слепоты ДВУсторонний: /edit не выдаёт кресло
                # при слепой фазе, но и слепая фаза не начинается при
                # выданном кресле — исполнитель с диском может читать
                # всё, что фаза прячет (нашёл codex: прежний барьер был
                # односторонним).
                with RUN_LOCK:
                    editing = ([t for t in RUNNING.values() if t.get("edit")]
                               or _EDIT_RESERVED)
                if editing:
                    return self._json(409, {"error": "идёт правка — "
                                            "слепой ход не начинается, "
                                            "пока кресло исполнителя "
                                            "занято"})
            args = [mode, text]
            if quick:
                args.append("--once")
            if voices:
                args += ["--voices", ",".join(voices)]
            # --project (просил grok): комната обсуждает конкретный
            # каталог, live.py выдаст его голосам на чтение. Опечатка в
            # пути возвращается сразу 400-кой, а не событием
            # act_status/error спустя запуск.
            project = (req.get("project") or "").strip()
            if project:
                # resolve + is_dir: live.py требует каталог и резолвит
                # путь от СВОЕГО cwd — иначе проверка и использование
                # смотрят в разные места, а файл проходил как «путь есть».
                p = Path(project).expanduser()
                if not p.is_absolute():
                    p = (CHOIR / p)
                p = p.resolve()
                if not p.is_dir():
                    return self._json(400,
                                      {"error": f"не каталог: {p}"})
                args += ["--project", str(p)]
            # Два хода на один голос одновременно портят его состояние:
            # live.py читает-меняет-пишет voices/<имя>/state.json без
            # межпроцессного замка, и в ленте окажутся две реплики от
            # РАЗНЫХ сессий одного голоса. Окно делало это доступным в
            # два клика (нашёл ревьюер дифа).
            want = set(voices or VOICES)
            with RUN_LOCK:
                busy = {v for t in RUNNING.values() for v in t["voices"]}
            clash = sorted(want & busy)
            if clash:
                return self._json(409, {"error": "уже отвечают: "
                                        + ", ".join(clash)})
            # Фактический project — полем в запись accepted: панель и CLI
            # могут разойтись (симлинк, относительный путь), и без поля
            # в журнале расхождение невосстановимо (просил ревьюер дифа).
            acc_fields: dict = {}
            if project:
                acc_fields["project"] = str(p)
            # Краткость — рычаг Автора, а не устав (наказ 2026-09-01).
            # В ленту пишем ОБЯЗАТЕЛЬНО: иначе через месяц короткий
            # ответ и урезанный ответ выглядят одинаково, а это разные
            # вещи — правило 8.5 про поля, не обеспеченные механикой,
            # работает и в обратную сторону: механика без записи в
            # журнал делает ленту неверной.
            brief = bool(req.get("brief"))
            if brief:
                acc_fields["brief"] = True
            if picked_by_server:
                acc_fields.update(picked_by="server-random",
                                  voice=picked_by_server)
            act = spawn([sys.executable, str(CHOIR / "live.py"), *args],
                        f"{'быстрый' if quick else mode}: {text[:60]}",
                        voices or VOICES,
                        # blind в meta — чтобы /edit видел слепую фазу
                        # и не выдавал кресло, пока она идёт (спека §5).
                        meta={"blind": True} if mode == "ask" else None,
                        note=(f"голос выбрал сервер случайно: "
                              f"{picked_by_server} (НЕ жребий drand — "
                              f"random.choice по составу)"
                              if picked_by_server else ""),
                        fields=acc_fields or None,
                        env_extra={"CHOIR_BRIEF": "1"} if brief else None)
            return self._json(200, {"act": act})

        if self.path == "/round":
            # Раунды choir.py из окна (просил grok: «иначе ежедневные
            # раунды остаются в чате с Клодом»). Вопрос — в файл, дальше
            # два режима, и они РАЗНЫЕ по цене и по смыслу:
            #   auto=false — цепочка pick → expand → ask одним bash -c:
            #     шаги строго последовательны (expand читает жребий pick,
            #     ask — затравку expand), и такт СТОИТ после слепой фазы:
            #     rebut и summarize запускает человек, посмотрев ответы;
            #   auto=true — одна команда `choir.py run --rebuts N`: весь
            #     такт до карточки крутит дирижёр. Здесь намеренно НЕ
            #     собирается своя цепочка витков в bash -c: два места,
            #     знающих порядок такта, разойдутся молча — и окно начнёт
            #     гонять «свой» протокол вместо протокола стола
            #     (правило 10: молчаливое решение оркестрации однажды
            #     прочитается как решение стола).
            # Наказ Автора 2026-08-25: «давайте с кнопкой, но к ней нужна
            # галочка автонажимание».
            question = (req.get("question") or "").strip()
            name = (req.get("name") or "").strip()
            if not question or not name:
                return self._json(400, {"error": "нужны question и name"})
            # Ведущий дефис делал имя похожим на флаг: `--round '-x'` →
            # argparse «expected one argument», цепочка падала на pick.
            if not re.fullmatch(ROUND_RE, name):
                return self._json(400, {"error": "имя раунда: буквы, "
                                        "цифры, точка, дефис; до 60"})
            auto = bool(req.get("auto"))
            # Витков критики 1..3. Потолок не наш, а правила 12: три
            # витка, дальше «вежливость моделей бесконечна, токены нет».
            # Проверяем и при auto=false: контракт один, иначе кривое
            # поле пролезет молча и всплывёт при следующем нажатии — уже
            # с включённой галочкой и оплаченными вызовами.
            try:
                rebuts = int(req.get("rebuts", 1))
            except (TypeError, ValueError):
                return self._json(400, {"error": "rebuts — целое 1..3"})
            if not 1 <= rebuts <= 3:
                return self._json(400, {"error": "витков 1..3 (правило 12: "
                                        "потолок — три витка)"})
            qfile = CHOIR / f"ВОПРОС-{name}.md"
            # Не переписываем молча: в room.jsonl уже лежит pick с
            # question_sha от старого текста, и файл разошёлся бы с
            # журналом беззвучно (нашёл ревьюер дифа).
            if qfile.exists() and not req.get("force"):
                return self._json(409, {"error": f"{qfile.name} уже есть — "
                                        "выберите другое имя раунда"})
            qfile.write_text(question + "\n", encoding="utf-8")
            # Флаг-стоп с прошлого раза снимаем ЗДЕСЬ и ВСЛУХ. Файл живёт
            # дольше раунда: забытый стоп остановил бы новый такт с тем
            # же именем на первой же паузе — молча, будто дирижёр сам
            # решил не продолжать. Но и молчаливое снятие не годится: в
            # ленте уже стоит round_stop, и без парной записи она врёт,
            # что раунд остановлен.
            stale = stop_file(name)
            if stale.exists():
                stale.unlink(missing_ok=True)
                feed_append("round_stop",
                            f"флаг-стоп раунда {name} снят: начат новый такт "
                            f"с тем же именем",
                            round=name, status="cleared")
            if auto:
                # Аргументы списком, без bash -c: имя уже проверено, но
                # лишний слой кавычек — лишний способ ошибиться.
                cmd = [sys.executable, "choir.py", "run", "--round", name,
                       "--seed", qfile.name, "--rebuts", str(rebuts)]
                label = f"round: {name} [авто, витков: {rebuts}]"
                note = (f"АВТОПРОГОН: такт идёт сам — pick → expand → ask → "
                        f"rebut ×{rebuts} → summarize, без остановки на "
                        f"человека. Остановить: кнопка «Стоп» "
                        f"(флаг {stale.name}).")
            else:
                py = shlex.quote(sys.executable)
                rn = shlex.quote(name)
                seed = shlex.quote(qfile.name)
                zt = shlex.quote(f"ЗАТРАВКА-{name}.md")  # его создаст expand
                cmd = ["bash", "-c",
                       f"{py} choir.py pick --round {rn} --seed {seed} && "
                       f"{py} choir.py expand --round {rn} --seed {seed} && "
                       f"{py} choir.py ask --round {rn} --seed {zt}"]
                label = f"round: {name}"
                note = ("ПО ШАГАМ: pick → expand → ask; после слепой фазы "
                        "такт останавливается — rebut и summarize "
                        "запускает человек.")
            # Режим — отдельными полями события, а не только словами в
            # тексте: читателю ленты нужна пометка, разбору журнала —
            # поле (правило 8.5: свойство, объявленное полем, должно
            # быть обеспечено механикой, и наоборот — механика без поля
            # в журнале невидима).
            fields = {"round": name, "auto": auto}
            if auto:
                fields["rebuts"] = rebuts
            # Краткость — та же опция, что и в комнате: choir.py читает
            # CHOIR_BRIEF при импорте и подставляет жёсткие рамки в
            # затравку и свод. Умолчание — полный вывод.
            brief = bool(req.get("brief"))
            if brief:
                fields["brief"] = True
            act = spawn(cmd, label, VOICES, cwd=CHOIR,
                        meta={"round": name, "auto": auto},
                        note=note, fields=fields,
                        env_extra={"CHOIR_BRIEF": "1"} if brief else None)
            return self._json(200, {"act": act, "auto": auto,
                                    "rebuts": rebuts if auto else None,
                                    "question_file": qfile.name})

        if self.path == "/edit":
            # Правка проекта: кресло исполнителя ОДНОМУ голосу на ОДИН
            # акт (СПЕКА-исполнитель-v1, этап 1). Задание уходит CLI
            # голоса в отдельном worktree; замок держит обёртка
            # executor_run; вердикт по падению замка выносит
            # edits.verdict_on_drop — по close ЭТОЙ эпохи.
            task = (req.get("task") or req.get("text") or "").strip()
            voice = (req.get("voice") or "").strip() or "random"
            project = (req.get("project") or "").strip() or str(PROJECT or "")
            if not task:
                return self._json(400, {"error": "пустое задание"})
            if not project:
                return self._json(400, {"error": "нужен project: правка "
                                        "без репозитория некуда"})
            # Два кресла на один project — два CLI в ОДНОМ .git
            # (writable_roots общий): они ломали бы ветки друг друга
            # (нашли codex и grok). Кресло одно на проект, и резерв
            # берётся ПОД ЗАМКОМ: ThreadingHTTPServer гонит два /edit
            # параллельно, и проверка без резерва оставляла щель между
            # «свободно» и spawn (codex). Резерв снимается в finally —
            # к этому моменту spawn уже положил запись в RUNNING.
            proj_key = str(Path(project).resolve())
            # Занятость смотрим и В ЛЕНТЕ: RUNNING пуст после рестарта
            # окна, а исполнитель прежнего акта может быть жив — второй
            # /edit открыл бы второе кресло в том же .git (нашёл codex;
            # deepseek добавил: recover в фоне, и до его прохода
            # эта проверка — единственный заслон).
            try:
                for e0 in edits.open_acts_in_feed():
                    if (e0.get("project") == proj_key
                            and leases.is_held(e0.get("act") or "")):
                        return self._json(409, {
                            "error": "проект уже под правкой "
                                     f"(акт {e0.get('act')}, по ленте)"})
            except Exception as e0:             # noqa: BLE001
                print(f"/edit: проверка ленты: {e0}", file=sys.stderr)
            with RUN_LOCK:
                busy_edit = [t for t in RUNNING.values()
                             if t.get("edit")
                             and t.get("edit_project") == proj_key]
                blind_now = [t for t in RUNNING.values() if t.get("blind")]
                if not busy_edit and not blind_now:
                    if proj_key in _EDIT_RESERVED:
                        busy_edit = [{"edit": "открывается"}]
                    else:
                        _EDIT_RESERVED.add(proj_key)
            if busy_edit:
                return self._json(409, {"error": "проект уже под правкой "
                                        f"(акт {busy_edit[0]['edit']})"})
            if blind_now:
                # Спека §5: слепые фазы блокируют выдачу кресла —
                # исполнитель с доступом к диску мог бы прочитать
                # чужие ответы фазы (правило 8.5: слепота — отсутствием
                # данных; здесь данных нет, пока кресло не выдано).
                return self._json(409, {"error": "идёт слепая фаза — "
                                        "кресло не выдаётся до её конца"})
            if voice == "random":
                pool = edits.random_pool()
                if not pool:
                    return self._json(409, {"error": "пул random пуст — "
                                            "все кресла сняты галочками; "
                                            "выберите исполнителя явно"})
                voice = secrets.choice(pool)
                picked_note = (f"исполнителя выбрал сервер случайно: "
                               f"{voice} (random.choice по умеющим "
                               f"правки — НЕ жребий drand)")
            else:
                picked_note = ""
            files = req.get("files")
            if isinstance(files, str):
                files = [x.strip() for x in files.split(",") if x.strip()]
            try:
                try:
                    ed = edits.open_edit(Path(project), task, voice,
                                         files=files)
                except edits.EditRefused as e:
                    return self._json(400, {"error": str(e)})
                except leases.EpochCorrupt as e:
                    # Без эпох кресло не выдаётся вовсе — fail-closed.
                    return self._json(503, {"error": f"эпохи сломаны: {e}"})
                act, epoch, wt = ed["act"], ed["epoch"], ed["worktree"]
                try:
                    _arm_edit_watch(act, epoch, wt, voice)
                except Exception as e:          # noqa: BLE001
                    # Интент уже в ленте — спавним всё равно: без
                    # наблюдателя вердикт донесёт recover/sweep, а вот
                    # 500 здесь оставил бы дерево брошенным (deepseek).
                    print(f"/edit: наблюдатель {act} не взведён: {e}",
                          file=sys.stderr)
                spawn([sys.executable,
                       str(Path(__file__).resolve().parent
                           / "executor_run.py"),
                       "--act", act, "--epoch", str(epoch),
                       "--worktree", str(wt), "--voice", voice,
                       *(["--serial-gate", voice]
                         if voice in edits.EDIT_GATES else []),
                       "--cmd-json",
                       json.dumps(ed["cmd"], ensure_ascii=False)],
                      f"edit: [{voice}] {task[:60]}", [voice],
                      meta={"edit": act, "epoch": epoch,
                            "edit_project": str(ed["project"])},
                      note=picked_note,
                      fields={"edit": act, "epoch": epoch, "voice": voice})
            finally:
                with RUN_LOCK:
                    _EDIT_RESERVED.discard(proj_key)
            return self._json(200, {"act": act, "epoch": epoch,
                                    "voice": voice, "worktree": str(wt),
                                    "base_sha": ed["base_sha"]})

        if self.path == "/edit_review":
            # Ревизия дифа столом: долгая (все голоса параллельно, до
            # 10 мин) — уходит через spawn, судьба видна как у любого
            # действия. Платная: подтверждение — на стороне окна.
            batch = bool(req.get("batch"))
            act = (req.get("act") or "").strip()
            if not batch and not re.fullmatch(r"[0-9a-f]{8,32}", act):
                return self._json(400, {"error": "кривой act"})
            if batch:
                # Пустая пачка — отказ ДО спавна: иначе процесс стартует,
                # тут же падает GateRefused, и в ленте остаётся шумное
                # act_status error на ровном месте (нашёл deepseek).
                try:
                    if not merge_gate.pending_acts():
                        return self._json(409, {"error": "нет актов, "
                                                "ждущих ревизии"})
                except Exception as e:          # noqa: BLE001
                    print(f"/edit_review: pending: {e}", file=sys.stderr)
            # ОДИН резерв на обе формы: батч и поштучная ревизия видят
            # друг друга (акт мог бы ревизоваться дважды параллельно —
            # два платных веера и дубли событий), а проверка и спавн
            # атомарны — двойной клик в щель между ними спавнил второй
            # веер (нашёл codex). Резерв снимается в finally: к этому
            # моменту spawn уже положил запись в RUNNING.
            with RUN_LOCK:
                dup = ([t for t in RUNNING.values()
                        if t.get("gate") == "review"]
                       or ("review" in _EDIT_RESERVED))
                if not dup:
                    _EDIT_RESERVED.add("review")
            if dup:
                return self._json(409, {"error": "ревизия уже идёт — "
                                        "дождитесь вердиктов"})
            try:
                if batch:
                    spawn([sys.executable,
                           str(Path(__file__).resolve().parent
                               / "merge_gate.py"), "review-batch"],
                          "review: пачка ждущих актов", VOICES,
                          meta={"edit": "batch", "gate": "review"},
                          fields={"gate": "review", "batch": True})
                else:
                    spawn([sys.executable,
                           str(Path(__file__).resolve().parent
                               / "merge_gate.py"), "review", act],
                          f"review: акт {act}", VOICES,
                          meta={"edit": act, "gate": "review"},
                          fields={"edit": act, "gate": "review"})
            finally:
                with RUN_LOCK:
                    _EDIT_RESERVED.discard("review")
            return self._json(200, {"batch": batch, "act": act or None,
                                    "started": True})

        if self.path == "/edit_merge":
            # Приёмка быстрая (git локально) — синхронно; слепую фазу
            # смотрим в RUNNING на месте.
            act = (req.get("act") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{8,32}", act):
                return self._json(400, {"error": "кривой act"})
            def _blind_now():
                # Зовётся гейтом ПОД его flock, вплотную к update-ref:
                # снимок до вызова оставлял окно, в котором слепая фаза
                # успевала стартовать (нашли deepseek, gemini, codex).
                with RUN_LOCK:
                    return any(t.get("blind") for t in RUNNING.values())
            try:
                ev = merge_gate.merge(act, blind_check=_blind_now)
            except merge_gate.GateRefused as e:
                # Сдвиг main кнопка чинит САМА (спека п.7): rebase →
                # честный ответ «одобрения сгорели, нужна ревизия
                # дельты». Гейт при этом ничего не решает — только
                # выравнивает базу; merge всё равно не пройдёт до
                # новой ревизии.
                if merge_gate.checks(act).get("stale_base"):
                    try:
                        rb = merge_gate.rebase_act(act)
                        return self._json(409, {
                            "error": "main уехал — гейт перенёс ветку "
                                     f"(база {rb['base_sha'][:12]}); "
                                     "прежние одобрения сгорели: "
                                     "запустите ревизию (пойдёт дельта)",
                            "rebased": True, "head": rb["head"]})
                    except merge_gate.GateRefused as e2:
                        return self._json(409, {"error": f"{e}; авто-"
                                                f"rebase: {e2}"})
                return self._json(409, {"error": str(e)})
            except Exception as e:              # noqa: BLE001
                return self._json(500, {"error": f"гейт упал: {e}"})
            return self._json(200, {
                "act": act, "result_sha": ev.get("result_sha"),
                "reviewed_by": ev.get("reviewed_by"),
                "worktree_copy": ev.get("worktree_copy")})

        if self.path == "/edit_adopt":
            act = (req.get("act") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{8,32}", act):
                return self._json(400, {"error": "кривой act"})
            try:
                merge_gate.adopt(act)
            except merge_gate.GateRefused as e:
                return self._json(409, {"error": str(e)})
            return self._json(200, {"act": act, "adopted": True})

        if self.path == "/stop":
            # Стоп — это ФЛАГ, а не убийство. Файл ~/.cache/choir/stop-<имя>
            # дирижёр читает между шагами такта; дерево процессов окно
            # намеренно не снимает: голос, срезанный на полуслове, теряет
            # уже оплаченный ответ, а раунд остаётся с половиной фазы в
            # журнале. Плата за мягкость честная — текущий вызов голоса
            # доживёт, стоп сработает на ближайшей паузе, и ровно это
            # пишется в ленту: иначе «нажал, а оно ещё думает» читается
            # как поломка кнопки.
            name = (req.get("round") or "").strip()
            if not re.fullmatch(ROUND_RE, name):
                return self._json(400, {"error": "имя раунда: буквы, "
                                        "цифры, точка, дефис; до 60"})
            with RUN_LOCK:
                acts = [aid for aid, t in RUNNING.items()
                        if t.get("round") == name
                        or t["label"].startswith(f"round: {name}")]
            path = stop_file(name)
            try:
                STOP_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"{_now()} стоп из окна RoundTable (arr)\n",
                    encoding="utf-8")
            except OSError as e:
                return self._json(500, {"error": f"флаг не записан: {e}"})
            feed_append(
                "round_stop",
                f"стоп раунда {name}: флаг {path.name} выставлен — дирижёр "
                f"остановит такт на ближайшей паузе между шагами, идущий "
                f"вызов голоса доживёт"
                + (f"; акты окна: {', '.join(acts)}" if acts else
                   "; идущего раунда с этим именем окно не видит — флаг "
                   "останется лежать до следующего запуска с тем же именем"),
                round=name, status="requested", acts=acts,
                stop_file=str(path))
            return self._json(200, {"round": name, "stop_file": str(path),
                                    "acts": acts})

        if self.path == "/abort":
            # ЖЁСТКАЯ остановка — в отличие от «Стоп», который лишь
            # выставляет флаг между шагами. Две кнопки существуют именно
            # потому, что цены у них разные, и смешивать их нельзя:
            # мягкий «Стоп» бережёт оплаченный вызов, «Прервать всё»
            # платит за немедленность потерей слепой фазы. Если бы abort
            # тоже ставил флаг, подпись кнопки врала бы, а Автор,
            # нажавший её в панике, продолжал бы ждать.
            #
            # До этой правки маршрута не было вовсе: страница слала сюда
            # POST, получала 404 и показывала ошибку — свойство было
            # объявлено в подсказке кнопки и не обеспечено механикой
            # (правило 8.5, найдено при сверке круга правок).
            want = req.get("acts")
            if want is not None and not isinstance(want, list):
                return self._json(400, {"error": "acts: список id или "
                                        "поле вовсе не задавать"})
            if want is not None and not want:
                # Дословно тот же дефект, что уже пойман в /act: пустой
                # список ≠ «поле не задано». Там цена — платная рассылка
                # всем шести, здесь выше: убийство ВСЕХ идущих ходов
                # вместо ни одного (нашёл ревьюер дифа, проверено).
                return self._json(200, {"aborted": [], "finished": [],
                                        "note": "acts пуст — прерывать "
                                                "нечего"})
            with RUN_LOCK:
                # Забираем записи СЕБЕ, как это делает выход из окна:
                # итог у действия ровно один, и пишет его тот, кто вынул
                # запись из RUNNING. Проснувшаяся reap увидит пустоту.
                if want:
                    mine = [(a, RUNNING.pop(a)) for a in list(want)
                            if a in RUNNING]
                else:
                    mine = list(RUNNING.items())
                    RUNNING.clear()
            if not mine:
                with RUN_LOCK:
                    others = list(RUNNING)
                # status="noop", а не "aborted": в append-only журнале
                # запись «прервано» там, где ничего не прерывали, через
                # месяц прочтётся как факт отмены (нашёл ревьюер дифа).
                feed_append("act_status",
                            "прервать нечего: "
                            + (f"названных актов окно не видит, идут "
                               f"другие ({', '.join(others)})" if others
                               else "идущих ходов нет"),
                            status="noop", acts=[])
                return self._json(200, {
                    "aborted": [], "finished": [], "running": others,
                    "note": ("этих актов окно не видит, но другие идут"
                             if others else "идущих ходов нет")})
            aborted, finished = [], []
            for aid, t in mine:
                rc = None
                try:
                    rc = t["proc"].poll()
                except Exception:                       # noqa: BLE001
                    pass
                if rc is not None:
                    # Успел закончиться, пока летел запрос. Писать над
                    # прозвучавшим ответом «прерван» значит врать журналу
                    # (нашли grok и kimi на живом «ОК» от deepseek).
                    feed_append("act_status",
                                f"act {aid} {'done' if rc == 0 else 'error'} "
                                f"(успел до «прервать всё»): {t['label']}",
                                act_id=aid,
                                status="done" if rc == 0 else "error", rc=rc)
                    finished.append(aid)
                    continue
                rnd = t.get("round")
                killed = False
                try:
                    # ПОВТОРНЫЙ poll вплотную к сигналу. Раньше между
                    # первым poll и killpg стояла дисковая запись
                    # флага-стопа — за эти миллисекунды процесс успевал
                    # завершиться, ядро освобождало pid И pgid (лидер
                    # группы — он сам), и сигнал мог уйти чужому дереву.
                    # Окно здесь было ШИРЕ, чем при закрытии окна, где
                    # между проверкой и сигналом нет ничего (нашёл
                    # ревьюер дифа, показал освобождение группы).
                    if t["proc"].poll() is not None:
                        raise ProcessLookupError
                    # Те же три предохранителя, что при закрытии окна:
                    # живость, сохранённый pgid, сверка со своей группой.
                    # Без последней сигнал мог уйти в оболочку Автора.
                    if t["pgid"] != os.getpgrp():
                        os.killpg(t["pgid"], signal.SIGTERM)
                        killed = True
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                if rnd:
                    # Флаг-стоп ПОСЛЕ сигнала: такт уведён в свою сессию
                    # и мог SIGTERM пережить — тогда флаг остановит его
                    # на ближайшей паузе между шагами.
                    try:
                        STOP_DIR.mkdir(parents=True, exist_ok=True)
                        stop_file(rnd).write_text(
                            f"{_now()} прервать всё из окна RoundTable\n",
                            encoding="utf-8")
                    except OSError:
                        pass
                # ВТОРОЙ poll, уже после сигнала. Между первым poll и
                # killpg процесс мог доиграть с rc=0: тогда в ленте уже
                # лежит ответ голоса, а мы писали бы над ним «прерван» —
                # ровно та ложь, что ловили на живом «ОК» от deepseek
                # (нашли grok и kimi независимо).
                try:
                    rc = t["proc"].wait(timeout=1.5)
                except Exception:                       # noqa: BLE001
                    rc = None
                if rc == 0:
                    feed_append("act_status",
                                f"act {aid} done (успел завершиться, пока "
                                f"шло «прервать всё»): {t['label']}",
                                act_id=aid, status="done", rc=0)
                    finished.append(aid)
                    continue
                if rc is not None and not killed:
                    # Умер САМ с ненулевым кодом в зазоре между первым
                    # poll и попыткой сигнала — сигнала не было, значит
                    # «прерван» был бы ложью: честный статус — error с
                    # его собственным rc (перепроход Fable 2026-08-25;
                    # тот же класс, что «прерван» над прозвучавшим
                    # ответом).
                    feed_append("act_status",
                                f"act {aid} error (rc={rc}, упал сам, "
                                f"пока шло «прервать всё»): {t['label']}",
                                act_id=aid, status="error", rc=rc)
                    finished.append(aid)
                    continue
                feed_append(
                    "act_status",
                    f"act {aid} прерван по «прервать всё»: {t['label']}"
                    "; уже оплаченные вызовы и частичные результаты могли "
                    "попасть в ленту — но не обязательно ВЫШЕ этой "
                    "записи: реплика живого голоса ляжет после неё"
                    + (f"; автопрогон «{rnd}» получил ещё и флаг-стоп"
                       if rnd else "")
                    + ("" if killed else "; группа процессов не снята "
                       "(уже завершилась или своя)"),
                    act_id=aid, status="aborted", round=rnd)
                aborted.append(aid)
            return self._json(200, {"aborted": aborted,
                                    "finished": finished})

        if self.path == "/voices":
            # Модель и усилие голосу НА ВРЕМЯ РАБОТЫ ОКНА. Хранится в
            # памяти, доезжает до CLI переменной окружения (см.
            # _spawn_env), и каждое изменение пишется в ленту событием
            # voice_config — чтобы через месяц было видно, каким
            # голосом и какой моделью сказана конкретная реплика.
            # Это прямое требование правила 1 (одинаковый вопрос —
            # одинаковые условия) и правила 8.5: условие, не названное
            # в журнале, через месяц прочтётся как разница позиций,
            # хотя это была разница моделей.
            name = (req.get("voice") or "").strip()
            # Область настройки: room — живая комната (по умолчанию),
            # rounds — раунды choir.py («модель дирижёра»).
            vscope = (req.get("scope") or "room").strip()
            if vscope not in SCOPES:
                return self._json(400, {"error": "scope: room, rounds "
                                        "или exec"})
            if name not in VOICES:
                # ДО exec-ветки: pool с мусорным именем иначе писался в
                # VOICE_CFG (нашёл gemini).
                return self._json(400, {"error": "нет такого голоса: "
                                        + (name or "(пусто)")})
            if vscope == "exec":
                # Вкладка coder: pool-галочка и пара кресла. gemini
                # кресла не имеет — отказ по существу, не по форме.
                if name not in edits.EDIT_VOICES:
                    return self._json(400, {"error": f"{name} кресла не "
                                            "имеет (только ревьюер)"})
                # pool применяется ТОЛЬКО в чистом виде: вместе с
                # model/effort он записывался ДО их валидации, и 400 по
                # паре оставлял галочку применённой — клиент откатывал
                # её и показывал противоположное серверу (нашёл codex).
                if "pool" in req and (req.get("model") or req.get("effort")
                                      or "model" in req or "effort" in req):
                    return self._json(400, {"error": "pool — отдельным "
                                            "запросом, без model/effort"})
                if "pool" in req:
                    if not isinstance(req["pool"], bool):
                        return self._json(400, {"error": "pool — bool"})
                    with CFG_LOCK:
                        ent = VOICE_CFG.setdefault(name, {})
                        pair = dict(ent.get("exec") or {})
                        pair["pool"] = req["pool"]
                        ent["exec"] = pair
                        _save_voice_cfg(name, "exec")
                    _sync_exec_overrides()
                    ev = feed_append(
                        "voice_config",
                        f"голос {name} [кресло]: "
                        + ("в пуле random" if req["pool"]
                           else "снят с пула random"),
                        voice=name, cfg_scope="exec",
                        pool=req["pool"], by="arr")
                    if not (req.get("model") or req.get("effort")
                            or "model" in req or "effort" in req):
                        return self._json(200, {"ok": True,
                                                "event": ev["id"]})
            ctl = VOICE_CTL.get(name) or {}
            model = (req.get("model") or "").strip()
            effort = (req.get("effort") or "").strip()
            # Пустое значение при ПРИСУТСТВУЮЩЕМ ключе — «вернуть
            # умолчание»: раньше сбросить настройку было нельзя вовсе
            # (пустое поле = 400; заметил kimi).
            clear_model = "model" in req and not model
            clear_effort = "effort" in req and not effort
            if clear_model or clear_effort:
                with CFG_LOCK:
                    ent = VOICE_CFG.setdefault(name, {})
                    pair = dict(ent.get(vscope) or {})
                    if clear_model:
                        pair.pop("model", None)
                    if clear_effort:
                        pair.pop("effort", None)
                    if pair:
                        ent[vscope] = pair
                    else:
                        ent.pop(vscope, None)
                if vscope == "exec":
                    _sync_exec_overrides()
                ev = feed_append(
                    "voice_config",
                    f"голос {name} "
                    + ("[раунды/дирижёр]" if vscope == "rounds"
                       else "[кресло]" if vscope == "exec"
                       else "[комната]") + ": "
                    + ", ".join(k for k, c in (("model", clear_model),
                                               ("effort", clear_effort)) if c)
                    + " → умолчание канала (сброс из окна Автора)",
                    voice=name, cfg_scope=vscope, reset=True, by="arr")
                with CFG_LOCK:
                    _save_voice_cfg(name, vscope)
                limits, _ = limits_now()
                return self._json(200, {
                    "voice": voice_report(name, limits.get(name)
                                          or {"kind": "unknown"}),
                    "event": ev["id"], "reset": True})
            if not model and not effort:
                return self._json(400, {"error": "нечего менять: нужны "
                                        "model и/или effort"})
            # Голос без рычага — 400 с объяснением, а не молчаливое
            # «принято»: настройка, которая никуда не доедет, хуже
            # отсутствия настройки.
            if vscope == "exec":
                # Рычаги КРЕСЛА свои: у claude/kimi только модель, у
                # grok только усилие, у dsh — ничего. Комнатные can_*
                # тут принимали мёртвые настройки, которые argv молча
                # игнорировал (нашли codex и grok).
                if model and name not in ("codex", "claude", "kimi",
                                          "deepseek"):
                    return self._json(400, {"error": f"модель кресла "
                                            f"{name} не крутится"})
                if effort and name not in ("codex", "grok"):
                    return self._json(400, {"error": f"усилие кресла "
                                            f"{name} не крутится"})
            elif model and not ctl.get("model_env"):
                return self._json(400, {"error": f"модель {name} из окна "
                                        "не меняется",
                                        "why": ctl.get("locked", "")})
            elif effort and not ctl.get("effort_env"):
                return self._json(400, {"error": f"усилие {name} из окна "
                                        "не меняется",
                                        "why": ctl.get("locked", "")})
            allowed = EFFORTS.get(name) or []
            if effort and effort not in allowed:
                # Уровень вне лестницы — отказ, а НЕ посадка на потолок
                # (как делает eff() в choir.py). Там подмена уместна:
                # раунд уже идёт, ронять его дороже. Здесь человек
                # только что нажал кнопку и обязан узнать, что его
                # выбор не принят, — иначе панель покажет одно, а голос
                # пойдёт с другим. Цена ошибки измерена: xhigh роняет
                # Грока за 4 секунды с 'unknown effort level'.
                return self._json(400, {
                    "error": f"усилие «{effort}» для {name} недопустимо",
                    "allowed": allowed,
                    # `name`, а не `voice`: такой переменной здесь нет
                    # вовсе, и отказ по недопустимому усилию падал
                    # NameError — то есть вместо честного 400 с
                    # объяснением человек получал 500 и трейсбек.
                    "source": EFFORTS_SRC_BY.get(name, EFFORTS_SRC)})
            unverified = False
            if model:
                known = model in (ctl.get("models") or [])
                shaped = bool(ctl.get("model_re")
                              and re.fullmatch(ctl["model_re"], model))
                if not MODEL_RE.match(model) or not (known or shaped):
                    return self._json(400, {
                        "error": f"модель «{model}» для {name} не "
                                 "распознана",
                        "known": ctl.get("models") or [],
                        "also_ok": ctl.get("model_re", "")})
                # Имя подходит по форме, но в известном списке его нет.
                # Запрещать нельзя — список зашит в коде и устареет раньше
                # провайдера. Но и выдавать за действующую нельзя: с
                # опечаткой голос выпадет целиком, и в ленте это ляжет как
                # `error`, то есть как свойство участника, а не как промах
                # окна (правило 4; нашёл ревьюер 2026-08-26).
                unverified = known is False and shaped
            with CFG_LOCK:
                ent = VOICE_CFG.setdefault(name, {})
                prev = dict(ent.get(vscope) or {})
                cur = dict(prev)
                if model:
                    cur["model"] = model
                if effort:
                    cur["effort"] = effort
                if name == "grok" and GROK_CACHE_OK:
                    # Пара проверяется ПОСЛЕ слияния: до него смена одной
                    # модели при сохранённом xhigh проскакивала — общий
                    # список пропускал grok-4.5+xhigh, и CLI падал уже на
                    # платном ходу (нашли все четверо ревьюеров; перенос
                    # за слияние — при сборке тумблера областей).
                    pm, pe = cur.get("model"), cur.get("effort")
                    ok = _GROK_BY_MODEL.get(pm)
                    if pm and pe and ok and pe not in ok:
                        return self._json(400, {
                            "error": f"пара {pm}+{pe} недопустима",
                            "allowed": ok,
                            "why": "у этой модели такой ступени нет "
                                   "(reasoning_efforts в кэше CLI)"})
                ent[vscope] = cur
            up = name.upper()
            envs = {}
            # exec — не env вовсе: лямбды кресел строят argv в процессе
            # окна и читают EXEC_OVERRIDES напрямую.
            if vscope == "exec":
                _sync_exec_overrides()
            if cur.get("model") and vscope != "exec":
                envs[ctl["model_env"] if vscope == "room"
                     else f"CHOIR_ROUND_{up}_MODEL"] = cur["model"]
            if cur.get("effort") and vscope != "exec":
                envs[ctl["effort_env"] if vscope == "room"
                     else f"CHOIR_ROUND_{up}_EFFORT"] = cur["effort"]
            # Идущие ходы уже запущены со старым окружением — сказать
            # это вслух, иначе «поменял и сразу спросил» прочтётся как
            # ответ новой модели.
            with RUN_LOCK:
                busy = sorted({v for t in RUNNING.values()
                               for v in t["voices"] if v == name})
            ev = feed_append(
                "voice_config",
                f"голос {name} "
                + ("[раунды/дирижёр]" if vscope == "rounds" else "[комната]")
                + ": "
                + ", ".join(
                    f"{k} {prev.get(k) or '(по умолчанию)'} → {cur[k]}"
                    for k in ("model", "effort") if k in cur)
                + " — задано из окна Автора"
                + ("" if vscope == "rounds" else "; "
                   + (ctl.get("scope")
                      or ", ".join(ctl.get("applies_to", []))))
                + ("; идущий ход этого голоса запущен ещё со старым "
                   "окружением" if busy else "")
                + ("; ⚠ модель НЕ ПРОВЕРЕНА: подходит по форме, но в "
                   "известном списке её нет — если имя с опечаткой, голос "
                   "выпадет целиком" if unverified else ""),
                voice=name, cfg_scope=vscope,
                model=cur.get("model"), effort=cur.get("effort"),
                model_verified=(not unverified) if cur.get("model") else None,
                prev_model=prev.get("model"), prev_effort=prev.get("effort"),
                env=envs,
                applies_to=(["choir.py"] if vscope == "rounds"
                            else ["edits.EDIT_VOICES (кресло)"]
                            if vscope == "exec"
                            else ctl.get("applies_to", [])),
                by="arr")
            with CFG_LOCK:
                # Кэш ПОСЛЕ канона: настройка, не попавшая в ленту, не
                # имеет права пережить перезапуск (порядок — codex).
                _save_voice_cfg(name, vscope)
            limits, _ = limits_now()
            return self._json(200, {
                "voice": voice_report(name, limits.get(name)
                                      or {"kind": "unknown"}),
                "env": envs, "event": ev["id"],
                "note": ctl.get("scope", ""),
                "running_with_old_env": busy,
                "model_verified": (not unverified) if cur.get("model") else None,
                "warning": ("модель не в известном списке — принята по форме "
                            "имени. Если это опечатка, голос выпадет "
                            "целиком, и в ленте отказ будет выглядеть его "
                            "свойством. Проверьте первым же ходом."
                            if unverified else "")})

        if self.path == "/lot":
            cands = [v for v in (req.get("candidates") or []) if v in VOICES]
            if len(cands) < 2:
                return self._json(400, {"error": "жребий — минимум из двух"})
            with LOT_LOCK, _SafeGate():
                if _lot_load():
                    return self._json(409, {"error": "жребий уже брошен и "
                                            "не раскрыт — сначала /reveal"})
                try:
                    lot = cast_lot(cands)
                except Exception as e:                  # noqa: BLE001
                    # маяк упал → жребия нет; отказ, а не тихий random()
                    return self._json(502, {"error": f"маяк недоступен: {e}"})
            # имени ещё НЕ СУЩЕСТВУЕТ — подписи целевого раунда нет
            return self._json(200, {"commit": lot["commit"],
                                    "target": lot["target"],
                                    "wait_s": LOT_AHEAD * 3})

        if self.path == "/reveal":
            with LOT_LOCK, _SafeGate():
                lot = _lot_load()
                if not lot:
                    return self._json(409, {"error": "нераскрытого жребия нет"})
                ev = reveal_lot(lot)
            if ev is None:
                return self._json(425, {"error": "целевой drand-раунд ещё "
                                        "не наступил — подождите"})
            return self._json(200, {"event": ev["id"]})

        return self._json(404, {"error": "нет такого пути"})


# ── страница ─────────────────────────────────────────────────────────
PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>RoundTable</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#14161a;--panel:#1c1f25;--ink:#e8e6df;--dim:#8b897f;
--rule:#2c3038;--acc:#d6a24a;--me:#7fa3c9;--err:#c97563}
*{box-sizing:border-box}
/* Правая колонка 344px, была 240: в строку голоса встали галочка, имя,
   модель и усилие (наказ Автора 2026-08-25). Уже 344 — селекторы
   схлопываются в многоточие, и «opus» с «opus-4-mini» становятся
   неразличимы; шире — лента теряет свои 72ch. Ниже 760px панель
   по-прежнему уходит целиком: там места нет и подавно. */
/* Границы двигаются и запоминаются (наказ Автора 2026-08-31). Размеры
   держим переменными, а не числами в правилах: перетаскивание меняет
   одну переменную, и раскладка идёт за ней сама — без пересчёта в JS
   того, что уже умеет grid. */
:root{--sidew:344px;--actsw:17rem}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 "Golos Text",system-ui,sans-serif;height:100vh;
display:grid;grid-template-columns:1fr var(--sidew);
grid-template-rows:1fr auto}
#feed{grid-row:1;overflow-y:auto;padding:1rem 1.2rem}
/* Панелька быстрых переключателей — правый верх ЛЕНТЫ (наказ Автора
   2026-08-31). position:fixed с отступом от правой панели через
   var(--sidew): двигаете границу — панелька едет вместе с ней. */
/* Sticky-ряд ВНУТРИ ленты, а не fixed поверх неё: fixed перекрывал
   правый верх реплик при прокрутке — текст уходил под непрозрачную
   панель и не читался (нашли codex и deepseek). Sticky занимает свою
   строку в потоке: лента начинается ниже, перекрытий нет, а при
   прокрутке ряд остаётся прижат к верху. */
#feedtop{position:sticky;top:-1rem;z-index:6;display:flex;
justify-content:flex-end;margin:-1rem -1.2rem .4rem;
padding:.35rem 1.2rem .25rem;
background:linear-gradient(var(--bg) 65%,transparent)}
#quickbar{display:flex;gap:.55rem;align-items:center;
background:var(--panel);border:1px solid var(--rule);border-radius:6px;
padding:.2rem .5rem}
.ev{margin:0 0 .7rem;max-width:72ch}
.ev .who{font:600 .78rem/1 ui-monospace,monospace;letter-spacing:.06em;
color:var(--acc)}
.ev.arr .who{color:var(--me)}
.ev .t{white-space:pre-wrap;word-wrap:break-word;margin-top:.15rem}
.ev.sys .t{color:var(--dim);font-size:.86rem}
.ev.err .who{color:var(--err)}
.ev.err .t{color:var(--err)}
.ev .kind{font:600 .66rem/1 ui-monospace,monospace;letter-spacing:.08em;
color:var(--dim);border:1px solid var(--rule);border-radius:3px;
padding:.1em .3em;margin-left:.45rem}
.ev .det{color:var(--dim);font-size:.82rem;margin-top:.2rem;
white-space:pre-wrap;border-left:2px solid var(--rule);padding-left:.5rem}
/* Моноширинный и с секундами: время реплики сверяют с журналом, а
   пропорциональные цифры прыгают и мешают читать колонку глазами.
   cursor:help — знак, что в title лежит полная метка с поясом и UTC. */
.ev .ts{color:var(--dim);font:.72rem ui-monospace,monospace;
margin-left:.5rem;cursor:help}
#side{position:relative;
grid-row:1/3;border-left:1px solid var(--rule);background:var(--panel);
padding:1rem;display:flex;flex-direction:column;gap:.9rem;overflow-y:auto}
#side h3{margin:0;font:600 .78rem/1 ui-monospace,monospace;
letter-spacing:.1em;color:var(--dim);text-transform:uppercase}
/* Строка голоса. Селекторы НЕ внутри <label>: клик по вложенному
   контролу браузер label'у не пересылает, но это тонкость спецификации,
   а цена ошибки — снятая галочка вместо смены модели, то есть реплика
   уйдёт не тем. Поэтому label обнимает ровно галочку и имя. */
.vrow{padding:.3rem 0;border-bottom:1px solid var(--rule)}
.vrow:last-child{border-bottom:0}
.vhead{display:flex;gap:.35rem;align-items:center}
label.vpick{display:flex;gap:.4rem;align-items:center;font-size:.92rem;
cursor:pointer;flex:1;min-width:4.2rem;overflow:hidden}
label.vpick .nm{overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
/* ШИРИНА ЯЧЕЙКИ НЕ ЗАВИСИТ ОТ ЗНАЧЕНИЯ. Раньше .mdl/.eff не имели
   размера вовсе: select мерился по самому длинному пункту, а .vhead с
   flex-wrap переносил строку по-разному — переключение вкладки
   💬/🎼 меняло длину значений, и вся панель съезжала (наказ Автора
   2026-08-31: «при смене комнаты съезжает вёрстка»). Теперь значение
   любой длины живёт внутри своей колонки и обрезается многоточием. */
.vrow .cell{flex:none;width:6.6rem;display:flex;min-width:0}
.vrow .cell>*{width:100%;min-width:0;max-width:100%;box-sizing:border-box}
.vrow select,.vrow input.free{background:var(--bg);color:var(--ink);
border:1px solid var(--rule);border-radius:4px;
font:.72rem ui-monospace,monospace;padding:.05rem .15rem}
.vrow select:focus-visible,.vrow input.free:focus-visible{
outline:2px solid var(--acc);outline-offset:1px}

/* Незменяемое значение — текстом в пунктирной рамке. Селектор там, где
   менять нечего, врёт руками: у kimi усилия нет вовсе, и выпадающий
   список создал бы ручку, которой не существует (правило 8.5). */
.vrow .fix{font:.72rem ui-monospace,monospace;color:var(--dim);
border:1px dashed var(--rule);border-radius:4px;padding:.05rem .25rem;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vrow .cap{display:flex;gap:.4rem;align-items:baseline;
font-size:.7rem;color:var(--dim);line-height:1.25}
.vrow .capt{flex:1;min-width:0}
.vrow .capt[title]:not([title=""]){cursor:help}
.vrow .st{margin-left:auto;font-size:.7rem;color:var(--dim);
white-space:nowrap}
.vrow .verr{color:var(--err);font-size:.72rem;margin-top:.15rem}
/* Заметка — не ошибка: «идущий ход запущен со старым окружением» надо
   сказать, но красным оно читалось бы как поломка. */
.vrow .verr.note{color:var(--dim)}
/* Значение, заданное из этого окна, обведено акцентом: иначе через час
   не отличить «так настроено сейчас» от «так стоит по умолчанию», а
   разница в том, чем именно ответит голос. */
.vrow .set{border-color:var(--acc)}
.vrow .verr[hidden]{display:none}
/* Полоса — ТОЛЬКО там, где есть число. Заполнение = израсходовано.
   .bar.none — тонкая линия-заглушка: место занято, но ничего не
   утверждает; отказ по квоте полосы не получает вовсе (числа нет). */
.bar{height:5px;border-radius:3px;background:var(--rule);overflow:hidden;
margin:.25rem 0 .1rem;cursor:help}
.bar i{display:block;height:100%;background:var(--acc)}
.bar i.hot{background:var(--err)}
.bar.none{height:2px;border-radius:0;background:var(--rule);opacity:.45}
.bar.pad{height:5px;background:none;cursor:default}
.ref{color:var(--err)}
#project{width:100%;background:var(--bg);color:var(--ink);
border:1px solid var(--rule);border-radius:4px;padding:.35rem .5rem;
font:.8rem ui-monospace,monospace}
#project:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
.vrow .st.busy{color:var(--acc)}
button{background:var(--rule);border:1px solid #3a3f49;color:var(--ink);
padding:.45rem .7rem;border-radius:4px;cursor:pointer;font-size:.86rem}
button:hover{border-color:var(--acc)}
button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#lotbox{border:1px solid var(--rule);border-radius:6px;padding:.6rem;
font-size:.85rem;color:var(--dim)}
#lotbox b{color:var(--acc)}
#bar{position:relative;grid-row:2;display:flex;gap:.6rem;padding:.8rem 1.2rem;
border-top:1px solid var(--rule);align-items:stretch}
/* Высота поля СОРАЗМЕРНА колонке кнопок (наказ Автора 2026-08-25).
   Кнопок стало четыре, столбик вырос до ~9rem, и поле в 2.6rem рядом
   с ним читалось как щель для одной строки — хотя сюда набирают и
   вопрос раунда, и абзац реплики. 6rem — это примерно четыре строки:
   видно начало и конец набранного. Потолок 18rem, а не «сколько
   потянешь»: поле растёт вверх за счёт ленты, ради которой окно и
   открыто. resize:vertical оставлен — растянуть руками можно. */
/* Поле тянется на ВСЮ высоту колонки кнопок (align-items:stretch у
   #bar): раньше было 3–4 строки при столбике в девять — набранный
   абзац прятался за прокруткой. min-height остаётся нижней границей
   на случай, если колонка кнопок однажды сожмётся сильнее поля. */
#msg{flex:1;background:var(--panel);color:var(--ink);
border:1px solid var(--rule);border-radius:6px;padding:.55rem .7rem;
font:inherit;resize:vertical;min-height:6rem;max-height:18rem}
#msg:focus-visible{outline:2px solid var(--me);outline-offset:1px}
.hint{color:var(--dim);font-size:.78rem}
/* Галочка hint снята — пояснительные абзацы убраны вместе с
   всплывающими подсказками: тихий интерфейс без лишнего текста. */
body.nohints .hint{display:none}
#hintlab{cursor:pointer;user-select:none;display:inline-flex;
align-items:center;gap:.25rem;opacity:.85;font-size:.76rem;
color:var(--dim)}
#hintlab input{accent-color:var(--acc)}
/* Колонка действий фиксированной ширины: подпись галочки длинная и
   переносится, а прыгающая от переноса ширина сдвигала бы кнопки под
   курсором — «Раунд стола» уезжает туда, где секунду назад был «Стоп». */
#acts{position:relative;flex:none;width:var(--actsw);
display:flex;flex-direction:column;gap:.4rem}
/* Ручка — полоска ПОВЕРХ границы, а не отдельная колонка: колонка
   сдвинула бы всю сетку на свою ширину, и «подвинуть на ноль» стало бы
   невозможно. Зона захвата шире видимой линии — по линии в 1px попасть
   мышью трудно. */
.grip{position:absolute;z-index:5;background:transparent}
.grip:hover,.grip.on{background:var(--acc);opacity:.55}
.grip-v{top:0;bottom:0;left:-4px;width:8px;cursor:col-resize}
.grip-h{left:0;right:0;top:-4px;height:8px;cursor:row-resize}
/* Пока тянут — не выделять текст и не менять курсор над чужими
   элементами: иначе перетаскивание превращается в выделение ленты. */
body.dragging{user-select:none}
body.dragging *{cursor:inherit!important}
label.auto{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;
font-size:.76rem;line-height:1.3;color:var(--dim);cursor:pointer}
label.auto select{background:var(--bg);color:var(--ink);
border:1px solid var(--rule);border-radius:4px;font:inherit;
padding:.05rem .2rem}
label.auto:hover{color:var(--ink)}
/* ×N — короткий счётчик, не полноразмерный селект: наследованные
   7.2rem давили кнопку «Раунд стола» почти в ноль (codex, deepseek). */
#rebuts{width:3.3rem}
#round{min-width:6.5rem}
/* «Стоп» красный не для красоты: он единственный тут ломает уже
   запущенное и оплаченное — цена ошибочного нажатия выше, чем у
   соседей, и это должно быть видно до клика, а не после. */
#stop{border-color:var(--err);color:var(--err)}
#stop[hidden]{display:none}
/* «Прервать всё» тоже красная и по той же причине, что «Стоп»: она
   ломает уже запущенное и оплаченное. Разница между ними не в цвете,
   а в охвате, и охват назван прямо в подписи — «всё». */
#abort{border-color:var(--err);color:var(--err)}
#belllab{cursor:pointer;user-select:none;display:inline-flex;
align-items:center;gap:.25rem;opacity:.85}
#belllab input{accent-color:var(--acc)}
#scopebar{display:flex;gap:.3rem;margin:.1rem 0 .45rem}
.scopetab{flex:1;background:var(--bg);border:1px solid var(--rule);
border-radius:5px;cursor:pointer;font:.74rem inherit;padding:.2rem .3rem;
color:var(--dim)}
.scopetab:hover{border-color:var(--acc)}
/* Выбранная область — заметно: весь список ниже показывает ЕЁ пару, и
   спутать «поменял в комнате» с «поменял в раундах» нельзя. */
.scopetab.on{background:var(--panel);color:var(--ink);
border-color:var(--acc);font-weight:600}
.eta{flex:none;font:.68rem ui-monospace,monospace;color:var(--dim);
cursor:help;white-space:nowrap}
#abort[hidden]{display:none}
/* Две строки по две ячейки: «Быстрый вопрос» + кем спросить и
   «Стоп» + «Прервать всё». Кнопка тянется, селектор фиксирован:
   иначе длинное имя голоса растянуло бы ячейку и сдвинуло кнопку
   прямо под курсором — тот же довод, по которому у #acts вообще
   фиксированная ширина. */
.qrow{display:flex;gap:.4rem;align-items:stretch}
/* !important обязателен: [hidden] и .qrow равны по специфичности, и
   flex, идущий в файле позже, ПОБЕЖДАЛ скрытие — переключение вкладок
   не прятало ряды вовсе (поймал Автор первым же взглядом). */
[hidden]{display:none!important}
.qrow>button{flex:1;min-width:0}
.qrow>select{flex:none;width:7.2rem;box-sizing:border-box;background:var(--bg);color:var(--ink);
border:1px solid var(--rule);border-radius:4px;
font:.76rem ui-monospace,monospace;padding:.05rem .25rem}
.qrow>select:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
/* Ошибка отправки — строкой под кнопками, а не alert'ом: alert
   перекрывает ленту, требует второго клика и легко ловится вслепую
   по Enter. Заметка (не ошибка) — приглушённым, красным она читалась
   бы как поломка. */
#acterr{font-size:.74rem;line-height:1.3;color:var(--err);
white-space:pre-wrap}
#acterr.note{color:var(--dim)}
#acterr[hidden]{display:none}
/* НЕСКОЛЬКО ОКОН ЛИМИТА — НЕСКОЛЬКО ПОЛОС. У claude их три: 5-часовое,
   недельное по всем моделям и отдельное недельное для Fable. Свести их
   в одну шкалу нельзя ни средним, ни максимумом: получится число,
   которого не сообщал никто, а по нему будут решать, звать голос или
   нет (правило 8.5). Поэтому у каждого окна своя подпись, своя полоса
   и свой процент. */
.lim2{display:flex;gap:.4rem;align-items:center;margin:.18rem 0}
.lim2 .nm2{font:.66rem ui-monospace,monospace;color:var(--dim);
flex:none;width:4.8rem;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;cursor:help}
.lim2 .bar{flex:1;margin:0}
.lim2 .pc{font:.66rem ui-monospace,monospace;color:var(--dim);
flex:none;min-width:2.4rem;text-align:right;cursor:help}
/* Отдельным правилом, а не одним .ref: у «.lim2 .pc» специфичность
   выше, и общий .ref был бы перекрыт молча — полоса горит красным, а
   процент рядом остаётся серым. */
.lim2 .pc.ref{color:var(--err)}
/* Число без шкалы (баланс в валюте) шире процента — даём ему место,
   иначе «остаток 4.55 USD» обрежется до неузнаваемости. */
.lim2 .pc.num{min-width:7.5rem;color:var(--ink);font-variant-numeric:tabular-nums}
/* Баланс на исходе (< 2 USD): ярко и жирно — наказ Автора 2026-09-01. */
.lim2 .pc.num.low{color:var(--err);font-weight:700}
.lim2 .bar.low{border-color:var(--err);box-shadow:0 0 0 1px var(--err)}
@media(max-width:760px){body{grid-template-columns:1fr}
#side{display:none}
/* Узкий экран: колонка действий под полем, а не рядом — 17rem сбоку
   не помещаются (нашёл grok). */
#bar{flex-direction:column}
#acts{width:100%}}
#actlist{font:.74rem ui-monospace,monospace;border-top:1px solid var(--rule);
margin-top:.3rem;padding-top:.3rem;max-height:9rem;overflow-y:auto}
.actrow{padding:.14rem 0;border-bottom:1px dashed var(--rule)}
.actrow button{font-size:.7rem;padding:0 .45rem;margin-left:.3rem}
#coderblk{border-left:2px solid var(--acc);padding-left:.45rem}
</style></head><body>
<div id="feed" aria-live="polite"><div id="feedtop"><div id="quickbar" title="Быстрые переключатели окна. Живут поверх ленты, чтобы не занимать место в колонке действий.">
  <label id="belllab" title="Звонок по завершении действия — как у микроволновки: короткое тройное «дзынь», когда ход или такт закончился (done, error или прерван). Работает, пока вкладка открыта; браузер разрешает звук после первого клика по странице. Выключается здесь же, выбор запоминается."><input type="checkbox" id="bell">🔔</label>
  <label id="brieflab" title="Краткость. Снята (умолчание) — голоса отвечают столько, сколько нужно доводу: ответ приходит целиком. Поставлена — возвращаются прежние жёсткие рамки правила 16: реплика 120–250 слов в комнате, ответ 250–400 и свод до 400 слов в раундах. Переключатель уходит в ленту полем brief, чтобы короткий ответ не путался с урезанным. Выбор запоминается."><input type="checkbox" id="brief">кратко</label>
  <label id="hintlab" title="Всплывающие подсказки: у кнопок, галочек, шкал и панелей — при наведении. Жёлтая рамка у модели/усилия значит «комната и раунды настроены по-разному». Снята — все подсказки скрыты, интерфейс тихий; выбор запоминается."><input type="checkbox" id="hints">hint</label>
</div></div></div>
<div id="side">
  <div class="grip grip-v" id="grip-side" title="Ширина правой панели. Тяните; положение запоминается. Двойной щелчок — вернуть по умолчанию."></div>
  <h3 id="vhead3">Голоса · кому уйдёт</h3>
  <div id="scopebar" title="ОДИН переключатель на весь список (наказ Автора 2026-08-31: «чтобы видно было»). Он выбирает, ЧЬЮ пару модель+усилие показывают и меняют ячейки ниже — у каждого голоса их две, независимые.
💬 комната: живой разговор, запускает live.py — «Сказать», «Слепой ход», «Быстрый вопрос».
🎼 раунды: протокол стола, запускает choir.py — «Раунд стола» (жребий, затравка, слепая фаза, витки, свод).
🔧 coder: кресло исполнителя — задание уходит правкой в git-worktree, ревизия дифа столом, приёмка через гейт. У голосов здесь СВОЯ пара модель+усилие (только там, где рычаг существует) и галочка участия в random-пуле.
Настройки хранятся раздельно и переживают перезапуск окна.">
    <button id="sc-room" class="scopetab on">💬 комната</button>
    <button id="sc-rounds" class="scopetab">🎼 раунды</button>
    <button id="sc-exec" class="scopetab">🔧 coder</button>
  </div>
  <div id="voices" title="Полоса — израсходованная доля окна; ◇ — был отказ по квоте, числа нет; тонкая линия — канал остаток не сообщает. Пустое место ≠ «квота цела». Окон у голоса может быть несколько — каждое своей полосой, среднего между окнами не бывает; имя окна и срок сброса — в подсказке самой полосы."></div>

  <h3>Проект (--project)</h3>
  <input id="project" value="__PROJECT_DEFAULT__" placeholder="путь к каталогу; пусто — без него" title="Каталог, который голоса получат на чтение. Подставлен тот, из которого запущено окно; очистить поле — значит спрашивать без проекта.">
  <h3>Жребий дирижёра</h3>
  <div id="lotbox" title="Имя ведущего видите только вы — в ленте до раскрытия лишь хеш-обязательство. «Раскрыть» допишет соль, и sha256 пересчитывается по журналу: проверить может каждый, у кого есть лента.">не брошен</div>
  <button id="lot" title="Бросить жребий дирижёра по протоколу commit-reveal: обязательство (sha256 соли, кандидатов и БУДУЩЕГО раунда drand) публикуется в ленту ДО того, как подпись раунда существует — подогнать выбор под вопрос нельзя. Имя ведущего увидите только вы, до раскрытия.">Бросить (commit)</button>
  <button id="reveal" title="Раскрыть жребий: соль дописывается в ленту, и любой голос пересчитает sha256(подпись:соль:кандидаты) и проверит честность. До целевого раунда drand кнопка ответит «рано».">Раскрыть</button>

</div>
<div id="bar">
  <div class="grip grip-h" id="grip-bar" title="Высота поля ввода. Тяните; положение запоминается. Двойной щелчок — вернуть по умолчанию."></div>
  <textarea id="msg" placeholder="Реплика в комнату (Enter — отправить, Alt+Enter — перенос строки; @имя — адресно)"></textarea>
  <div id="acts">
    <div class="grip grip-v" id="grip-acts" title="Ширина колонки кнопок. Тяните; положение запоминается. Двойной щелчок — вернуть по умолчанию."></div>
    <div class="qrow" id="sendrow">
    <button id="say" title="Реплика в общий разговор. Отвечают НЕ все и не по очереди: кому говорить, решает протокол live.py — адресат «@имя», поднявшие руку («ХОЧУ СЛОВО») и тот, кто дольше всех молчал (правило 7.5). Нить у каждого голоса своя, в неё уходит только дельта ленты. Enter — отправить, Alt+Enter — перенос строки.">Сказать</button>
    <button id="blind" title="Новая тема слепым ходом: отмеченные голоса отвечают ОДНОВРЕМЕННО и не видя друг друга. Это единственный барьер против каскада — когда голос смещается к первому уверенному чужому ответу, даже если свой был точнее (правило 9). Ответы копятся в памяти дирижёра и раскрываются разом, на диск до раскрытия не попадая.">Слепой ход</button>
    </div>
    <div class="qrow" id="roundrow">
      <button id="round" title="Полный протокол стола, а не просто вопрос: жребий ведущего по drand → ведущий разворачивает вопрос в затравку → слепая фаза → открытая критика витками → свод карточкой. Долго и платно: шесть голосов на каждом шаге. Вопрос берётся из поля слева, имя раунда спросят следом. Без галочки «авто» такт встанет после слепой фазы — посмотреть ответы.">Раунд стола</button>
      <label class="auto" title="Без галочки такт останавливается после слепой фазы: rebut и summarize запускает человек, посмотрев ответы. С галочкой choir.py run сам крутит витки критики и свод — до карточки, без вопросов; остановить можно кнопкой «Стоп», она снимет такт на ближайшей паузе.">
        <input type="checkbox" id="auto"><span>авто</span>
      </label>
      <select id="rebuts" title="витков открытой критики в раунде; потолок 3 — правило 12">
        <option value="1" selected>×1</option>
        <option value="2">×2</option>
        <option value="3">×3</option>
      </select>
    </div>
    <div class="qrow" id="quickrow">
      <button id="quick" title="Один голос, без протокола: ни слепой фазы, ни витков критики, ни свода. Для мелочей — уточнить факт, проверить связь, спросить того, кто в теме. Дёшево именно потому, что зовётся один канал; чтобы спросить всех — «Слепой ход», чтобы получить решение стола — «Раунд стола».">Быстрый вопрос</button>
      <select id="qvoice" title="Кого спросить. «случайно» — выбор оставлен серверу; пока сервер режим не объявил, голос тянет само окно и говорит об этом строкой ниже (это не жребий drand — просто случайный выбор для мелочи)">
        <option value="">случайно</option>
      </select>
      <label id="qexeclab" title="coder: текст уходит не вопросом, а ЗАДАНИЕМ НА ПРАВКУ тому же голосу — с worktree, арендой и гейтом, как во вкладке 🔧. Если выбранный голос правок не умеет (умеющих объявляет сервер; gemini не умеет — файлов не видит), кресло уходит случайному из умеющих — и подмена называется вслух. ВЫКЛ по умолчанию — решение стола (раунд вкладки-v1, 6/6): самый частый жест окна не должен по умолчанию быть платной правкой с правом записи. Платно, с подтверждением."><input type="checkbox" id="qexec">coder</label>
    </div>
    <div id="coderblk">
    <div class="qrow">
      <button id="editbtn" title="Выдать кресло исполнителя: текст из поля уходит ЗАДАНИЕМ одному голосу, тот правит код в отдельном git-worktree (ветка act/…). Кресло одно на проект; слепой ход и правка взаимно блокируются. Итог — событиями edit_open/edit_close в ленте; при вылете worktree остаётся на диске карантином. Merge в main — пока руками через гейт (этап 3).">Правка</button>
      <select id="evoice" title="Кто исполняет. codex — единственный с песочницей (workspace-write + .git базы); claude — набор инструментов Write/Edit/Bash(git:*), но git-alias пробивает и его, граница словесная; kimi ⚠ ДОРОГОЙ И МОНОПОЛЬНЫЙ ПО ЛИНИИ: правка — длинная агентная сессия (15–20 внутренних запросов), и обёртка держит ОДНУ из его линий весь акт; вторая линия (если жива её квота) остаётся столу — ревизии и комната идут по ней, но медленнее, в очередь. Его лучший случай — небольшая точная правка. «случайно» — random.choice сервера по умеющим, НЕ жребий drand; kimi в случайный выбор не входит — только явно. grok — через pty, ПЕСОЧНИЦЫ НЕТ (Landlock на этой машине не готовится): ограничен только словами задания. deepseek — в кресле dsh (агентный харнесс), не HTTP-голос стола. gemini файлов не видит — только ревьюер.">
        <option value="">случайно</option>
        <option value="codex">codex</option>
        <option value="claude">claude</option>
        <option value="grok">grok</option>
        <option value="deepseek">deepseek (dsh)</option>
        <option value="kimi">kimi ⚠ дорогой</option>
      </select>
    </div>
    <div class="qrow">
      <input id="scopefiles" placeholder="скоуп: файлы через запятую (пусто — не заявлен)" title="Заявка исполнительского намерения (спека п.1): какие файлы правка ИМЕЕТ ПРАВО трогать (можно маски: src/*.py). Гейт при приёмке сверит диф с заявкой — вышедший за скоуп акт не пройдёт. Пусто — скоуп не заявлен, сверки не будет, и событие merge честно это скажет." style="flex:1;min-width:12rem">
    </div>
    <div class="qrow">
      <input id="actid" placeholder="акт" title="Идентификатор акта правки (hex из события edit_open; после кнопки «Правка» подставляется сам). Кнопки ниже действуют на него." style="width:7.5rem">
      <label id="batchlab" title="Тумблер этапа 5 (умолчание — ПОШТУЧНО: проверено, доказано, работает). Включён — кнопка «Ревизия» ревизует ВСЕ ждущие акты одной пачкой: один веер вызовов вместо N, вердикты по каждому акту отдельной строкой «ВЕРДИКТ <act>: …». Потолки честные: до 4 актов и 60К символов дифа — внимание ревьюера не резина, большие правки только поштучно. Выбор запоминается."><input type="checkbox" id="revbatch">батчем</label>
      <button id="revbtn" title="Разослать диф ветки акта ВСЕМ голосам, кроме исполнителя (правило 13 в миниатюре). Каждый ответ — событие edit_review в ленте с вердиктом первой строкой (ОДОБРЯЮ/ОТКАЗ) и sha, на который он дан. Платно: до пяти вызовов. Доправка после ревизии меняет sha — старые одобрения гаснут.">Ревизия</button>
      <button id="mrgbtn" title="Принять правку в main через гейт: аренда закрыта (или adopt) · база не сдвинута · одобрений ≥2 и ни одного ОТКАЗА — всё на текущий sha ветки. Merge-коммит несёт Reviewed-by и тройку sha; автор коммита — голос-исполнитель. Истина — ref: рабочая копия обновляется после и только чистая.">Принять</button>
      <button id="adoptbtn" title="Для ВЫЛЕТЕВШЕГО акта: явное решение Автора рассмотреть работу из карантина (событие edit_adopt). Снимает одно условие гейта — закрытую аренду; ревизия, кворум и база остаются в силе (спека п.9).">Adopt</button>
      <label id="actfeedlab" title="Опциональная ЛЕНТА АКТОВ (идея голоса claude из раунда вкладки-v1): каждый акт — строка со стадиями «кресло → диф → ревизия N/M → merge», и кнопка существует только на той стадии, где она легальна: Ревизия у закрытого, Принять при кворуме, Adopt у вылетевшего. Выбор запоминается."><input type="checkbox" id="actfeed">лента актов</label>
    </div>
    <div id="actlist" hidden></div>
    </div>
    <div class="qrow" id="ctlrow">
    </div>
    <div class="qrow">
      <button id="stop" hidden title="Выставить флаг-стоп: дирижёр закончит текущий шаг и не начнёт следующий">Стоп</button>
      <button id="abort" hidden title="Прервать ВСЕ идущие ходы (POST /abort). Что уже записано в ленту — останется. НО: ответы слепой фазы идущего раунда живут только в памяти дирижёра и попадают на диск лишь при закрытии фазы — прерывание стирает их бесследно, вместе с оплатой. Мягкий путь — «Стоп»: он даёт дирижёру дойти до конца шага. То же самое делает Esc в поле ввода, оба пути спрашивают подтверждение.">Прервать всё</button>
    </div>
    <div id="acterr" hidden></div>
  </div>
</div>
<script>
const feed=document.getElementById('feed'),msg=document.getElementById('msg');
const qsel=document.getElementById('qvoice');
// ── ПРАВКА: кресло исполнителя одному голосу ────────────────────────
// ── гейт этапа 3: ревизия дифа и приёмка ──────────────────────────
function actId(){return document.getElementById('actid').value.trim()}
async function gatePost(path,body,okMsg){
  let r;
  try{
    r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
  }catch(e){return acterr('сервер не ответил: '+e)}
  let j={};try{j=await r.json()}catch(_){}
  if(!r.ok)return acterr(((j&&j.error)||('ошибка '+r.status)));
  acterr(okMsg(j),true);
}
const batchBox=document.getElementById('revbatch');
try{batchBox.checked=localStorage.getItem('rt-revbatch')==='1'}catch(_){}
batchBox.onchange=function(){try{localStorage.setItem('rt-revbatch',batchBox.checked?'1':'0')}catch(_){}}
document.getElementById('revbtn').onclick=function(){
  if(batchBox.checked){
    if(!confirm('Ревизия ПАЧКОЙ: все ждущие акты одним веером вызовов '+
        '(потолки: 4 акта, 60К дифа). Платно. Пускаем?'))return;
    gatePost('/edit_review',{batch:true},function(j){return 'ревизия пачки запущена — вердикты придут в ленту'});
    return;
  }
  const a=actId(); if(!a)return acterr('нужен акт (поле слева)');
  if(!confirm('Ревизия акта '+a+': диф уйдёт всем голосам, кроме исполнителя. Платно. Пускаем?'))return;
  gatePost('/edit_review',{act:a},function(j){return 'ревизия запущена: акт '+j.act+' — вердикты придут в ленту'});
};
document.getElementById('mrgbtn').onclick=function(){
  const a=actId(); if(!a)return acterr('нужен акт (поле слева)');
  if(!confirm('Принять акт '+a+' в main? Гейт проверит кворум, базу и аренду; main сдвинется.'))return;
  gatePost('/edit_merge',{act:a},function(j){
    return 'принято: '+(j.result_sha||'').slice(0,12)+' (reviewed-by: '+
      ((j.reviewed_by||[]).join(', '))+'); копия: '+j.worktree_copy});
};
document.getElementById('adoptbtn').onclick=function(){
  const a=actId(); if(!a)return acterr('нужен акт (поле слева)');
  if(!confirm('Adopt акта '+a+': рассмотреть ВЫЛЕТЕВШУЮ работу из карантина?'))return;
  gatePost('/edit_adopt',{act:a},function(j){return 'adopt записан: '+j.act});
};
// ── ЛЕНТА АКТОВ (опция; идея голоса claude, раунд вкладки-v1) ──────
const actFeedBox=document.getElementById('actfeed');
try{actFeedBox.checked=localStorage.getItem('rt-actfeed')==='1'}catch(_){}
actFeedBox.onchange=function(){
  try{localStorage.setItem('rt-actfeed',actFeedBox.checked?'1':'0')}catch(_){}
  renderActs();
};
const STAGE_RU={working:'в кресле',opening:'открывается',closed:'закрыт',
  crashed:'ВЫЛЕТ',adopted:'adopt',merged:'принят',lost:'ветка потеряна'};
function renderActs(){
  const box=document.getElementById('actlist');
  const acts=(window.STATE&&window.STATE.acts)||null;
  box.hidden=!actFeedBox.checked;
  if(!actFeedBox.checked)return;
  if(acts===null){box.textContent='акты: сервер сводку не прислал';return}
  if(!acts.length){box.textContent='актов пока нет';box.dataset.sig='';return}
  // Перерисовка только при ИЗМЕНЕНИИ данных: опрос 15 с иначе срывал
  // клик по стадийной кнопке ровно в момент rebuild (нашёл gemini).
  const sig=JSON.stringify(acts);
  if(box.dataset.sig===sig)return;
  box.dataset.sig=sig;
  box.innerHTML='';
  acts.forEach(function(a){
    const row=document.createElement('div');
    row.className='actrow';
    const ap=(a.approvals||[]).length,q=a.quorum||2,
      ref=(a.refused||[]).length;
    let stagebar='кресло';
    if(a.stage==='merged')
      stagebar='кресло → диф → ревизия → merge '+(a.result||'');
    else if(a.stage==='lost')
      stagebar='ветка исчезла из репозитория';
    else if(a.stage!=='working'&&a.stage!=='opening')
      stagebar='кресло → диф → ревизия '+ap+'/'+q+
        (ref?(' (ОТКАЗ: '+a.refused.join(',')+')'):'');
    row.title='акт '+a.act+' ['+a.voice+(a.seat&&a.seat!==a.voice?
      '/'+a.seat:'')+']\n'+(a.task||'')+'\nпроект: '+(a.project||'');
    row.innerHTML='<code>'+esc(a.act.slice(0,8))+'</code> ['+esc(a.voice)+
      '] <b>'+esc(STAGE_RU[a.stage]||a.stage)+'</b> · '+esc(stagebar)+' ';
    // Кнопка существует только на легальной стадии — перегруз лечится
    // не переносом кнопок, а их отсутствием вне момента.
    function btn(label,fn){
      const b=document.createElement('button');
      b.textContent=label;b.onclick=fn;row.appendChild(b);
    }
    if(a.stage==='closed'||a.stage==='adopted'){
      if(!ref&&ap<q)btn('Ревизия',function(){
        if(!confirm('Ревизия акта '+a.act+': платный веер всем, кроме '+
          'исполнителя. Пускаем?'))return;
        gatePost('/edit_review',{act:a.act},function(){return 'ревизия '+
          a.act+' запущена — вердикты придут в ленту'});
      });
      if(ap>=q&&!ref)btn('Принять',function(){
        if(!confirm('Принять акт '+a.act+' в main? Кворум '+ap+'/'+q+
          '. Ветка сольётся, main сдвинется.'))return;
        gatePost('/edit_merge',{act:a.act},function(j){
          return 'принято: '+(j.result_sha||'').slice(0,12)});
      });
    }
    if(a.stage==='crashed')btn('Adopt',function(){
      if(!confirm('Adopt акта '+a.act+': рассмотреть вылетевшую работу '+
        'из карантина?'))return;
      gatePost('/edit_adopt',{act:a.act},function(){return 'adopt: '+a.act});
    });
    box.appendChild(row);
  });
}
const editBtn=document.getElementById('editbtn');
editBtn.onclick=async function(){
  const task=msg.value.trim(); if(!task)return acterr('пустое задание');
  const project=document.getElementById('project').value.trim();
  if(!project)return acterr('правке нужен проект: укажите путь к репозиторию');
  const voice=document.getElementById('evoice').value||'random';
  // Подтверждение всегда: исполнитель получает ПРАВО ЗАПИСИ в worktree
  // проекта, и это платный агентный вызов — не реплика в чат.
  if(!confirm('Выдать кресло исполнителя «'+(voice==='random'?'случайно':voice)+
      '»?\nПроект: '+project+'\nЗадание: '+task.slice(0,200)+
      '\n\nПравка пойдёт в отдельный worktree; merge в main — отдельно.'))
    return acterr('правка отменена', true);
  acterr('');
  // Кнопка гаснет на время запроса: открытие кресла — секунды (git
  // worktree), и второй клик слал бы второй платный POST; сервер один
  // отклонит, но гонка ответов путала отказ с успехом (нашли codex и
  // deepseek независимо).
  editBtn.disabled=true;
  let r;
  try{
    r=await fetch('/edit',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task:task,voice:voice==='random'?null:voice,
                           project:project,
                           files:document.getElementById('scopefiles').value})});
  }catch(e){editBtn.disabled=false;return acterr('сервер не ответил: '+e)}
  finally{editBtn.disabled=false}
  let j={};try{j=await r.json()}catch(_){}
  if(!r.ok)return acterr('правка не открыта: '+((j&&j.error)||('ошибка '+r.status)));
  if(!j||!j.act)return acterr('сервер ответил 200 без акта — поле не чищу');
  // Чистим ТОЛЬКО если Автор не дописал текст за время запроса: иначе
  // стирается свеженабранное (нашёл codex).
  if(msg.value.trim()===task)msg.value='';
  try{document.getElementById('actid').value=j.act}catch(_){}
  acterr('кресло выдано: акт '+j.act+' ['+(j.voice||'?')+'], ветка act/'+j.act,true);
};
const seen=new Set();
// ОДНА область на весь список (наказ 2026-08-31). Была карта по
// строкам: тумблер в каждой строке терялся среди ячеек, и по виду
// панели нельзя было сказать, чью пару ты сейчас правишь.
let SCOPE='room';
// 🔧 из прошлой сессии НЕ поднимаем: Автор перезапустил окно и увидел
// «кнопки чата пропали совсем» — стартуем в 💬 (или 🎼, если так было),
// кодер — только по клику (2026-09-02).
try{const v=localStorage.getItem('rt-scope');if(v==='rounds')SCOPE=v}catch(_){}
// Что сервер объявил про себя. Оба поля молодые, и «не объявил» здесь
// НЕ значит «не умеет» — значит «окно не знает». Разница важна: пока
// не знаем, окно выбирает осторожный путь (см. sendQuick), а не
// уверенный (правило 8.5: свойство, не обеспеченное механикой, врёт).
let MODES=null;      // /state.modes — какие режимы отправки есть у /act
let LAST_RUN=[];     // /state.running — что идёт прямо сейчас
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
// Время реплики. В ленте ts — UTC (live.py пишет isoformat с +00:00), а
// человек живёт в своём поясе: на виду МЕСТНОЕ и обязательно с секундами
// (наказ Автора 2026-08-25 — «точный таймстамп»), а полная метка с
// поясом и исходный UTC — в title. Без UTC в подсказке сверка реплики со
// строкой журнала становится гаданием, а сверяют их постоянно: порядок
// drand, context_sha, «кто когда ответил».
function isoUTC(s){
  s=String(s).trim();
  // Пояса нет — объявляем UTC ЯВНО: иначе Date прочтёт строку как
  // местное время и метка молча уедет на разницу поясов.
  return /(?:[zZ]|[+\-]\d{2}:?\d{2})$/.test(s)?s:s+'Z';
}
function tsView(raw){
  if(!raw)return {short:'',full:'времени нет в событии'};
  const d=new Date(isoUTC(raw));
  // Непонятную метку НЕ режем по позициям: кусок чужой строки выглядит
  // как время и читается как время, хотя им не является.
  if(isNaN(d.getTime()))
    return {short:'—',full:'метка времени не разобрана: '+String(raw)};
  const p=n=>String(n).padStart(2,'0');
  const hms=p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
  const off=-d.getTimezoneOffset(),sg=off<0?'-':'+',ab=Math.abs(off);
  const zone='UTC'+sg+p(Math.floor(ab/60))+(ab%60?':'+p(ab%60):'');
  const day=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());
  const utc=d.getUTCFullYear()+'-'+p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate())+
    ' '+p(d.getUTCHours())+':'+p(d.getUTCMinutes())+':'+p(d.getUTCSeconds());
  return {short:hms,
    full:'местное: '+day+' '+hms+' '+zone+'\nв журнале (UTC): '+utc+
         '\nсырое поле ts: '+String(raw)};
}
function add(ev){
  if(ev.id&&seen.has(ev.id))return; if(ev.id)seen.add(ev.id);
  const el=document.createElement('div');
  const a=ev.author||'?',k=ev.kind||'';
  // kind различаем ОБЯЗАТЕЛЬНО: live.py пишет отказ голоса событием
  // kind=error с текстом в detail (нашёл kimi, переезд-v1). Рендер по
  // одному лишь text рисовал такой отказ ПУСТОЙ СТРОКОЙ — интерфейс
  // молча врал, что голос ответил ничем.
  const sys=(a==='choir'||a==='roundtable'||k==='act_status'||
             k==='lot_commit'||k==='lot_reveal');
  el.className='ev '+(k==='error'?'err':(a==='arr'?'arr':(sys?'sys':'')));
  const body=(ev.text||'')||(k==='error'?'(пусто)':'');
  const det=ev.detail?'<div class="det">'+esc(ev.detail)+'</div>':'';
  const badge=k&&k!=='say'?'<span class="kind">'+esc(k)+'</span>':'';
  const tv=tsView(ev.ts);
  el.innerHTML='<span class="who">'+esc(a)+'</span>'+badge+
    '<span class="ts">'+esc(tv.short)+'</span>'+
    '<div class="t">'+esc(body)+'</div>'+det;
  // Полная метка — СВОЙСТВОМ, а не внутрь строки html: esc() экранирует
  // < > &, но не кавычку, а тут значение попадало бы в атрибут. Поле ts
  // приходит из ленты, то есть снаружи; строка вида `x" onmouseover=…`
  // вырвалась бы из атрибута. Свойству разметка не страшна вовсе.
  const tsEl=el.querySelector('.ts'); if(tsEl)tsEl.title=tv.full;
  const stick=feed.scrollHeight-feed.scrollTop-feed.clientHeight<60;
  feed.appendChild(el); if(stick)feed.scrollTop=feed.scrollHeight;
}
// ── звонок «как у микроволновки» (просьба Автора 2026-08-25) ─────────
// Три коротких синусовых «дзынь» на завершение действия. BOOT-порог —
// чтобы хвост ленты, приходящий при открытии страницы, не устраивал
// перезвон: звоним только о событиях, случившихся при живой вкладке.
// (порог по возрасту СТРАНИЦЫ убран: SSE-реконнект после сна ноутбука
// доносил пачку старых done/error, и все звонили разом; а настоящее
// событие в первые секунды после загрузки молчало. Теперь: только
// события, которых лента ещё не видела (fresh), не старше минуты по
// их собственному ts, и не чаще раза в 2 с.)
let lastDing=0;
const bellBox=document.getElementById('bell');
try{bellBox.checked=localStorage.getItem('rt-bell')!=='0'}catch(_){bellBox.checked=true}
bellBox.onchange=function(){try{localStorage.setItem('rt-bell',bellBox.checked?'1':'0')}catch(_){}}
// ── подсказки под выключателем ───────────────────────────────────────
// CSS отключить title не умеет, поэтому механика двухходовая: при
// снятой галочке подсказка снимается с элемента В МОМЕНТ наведения
// (mouseover ловит и элементы, дорисованные позже — шкалы лимитов
// перерисовываются каждый опрос), текст прячется в data-t; при
// включении всё возвращается на место одним проходом.
// «кратко» — умолчание СНЯТО: наказ Автора 2026-09-01 «выводи весь
// вывод от каждого агента, но опцию оставим». Поэтому здесь ==='1', а
// не !=='0', как у звонка и подсказок: те по умолчанию включены.
const briefBox=document.getElementById('brief');
try{briefBox.checked=localStorage.getItem('rt-brief')==='1'}catch(_){briefBox.checked=false}
briefBox.onchange=function(){try{localStorage.setItem('rt-brief',briefBox.checked?'1':'0')}catch(_){}}
function briefOn(){return !!(briefBox&&briefBox.checked)}
const hintBox=document.getElementById('hints');
try{hintBox.checked=localStorage.getItem('rt-hints')!=='0'}catch(_){hintBox.checked=true}
function stripTitle(el){
  if(el&&el.getAttribute&&el.getAttribute('title')){
    el.dataset.t=el.getAttribute('title');el.removeAttribute('title')}
}
function applyHints(){
  document.body.classList.toggle('nohints',!hintBox.checked);
  try{localStorage.setItem('rt-hints',hintBox.checked?'1':'0')}catch(_){}
  if(hintBox.checked){
    document.querySelectorAll('[data-t]').forEach(function(el){
      // Свежий title (шкалу перерисовали, пока подсказки были
      // выключены) главнее припрятанного: восстановление поверх него
      // вернуло бы протухший текст (нашли grok и субагент).
      if(!el.getAttribute('title'))el.title=el.dataset.t;
      delete el.dataset.t});
  }else{
    document.querySelectorAll('[title]').forEach(stripTitle);
  }
}
hintBox.onchange=applyHints;applyHints();
// Перерисовка шкал ставит title заново каждые ~15 с; курсор, неподвижно
// стоящий над шкалой, mouseover при замене узла не получает — родная
// подсказка всплывала при выключенном hint (нашёл субагент). Наблюдатель
// ловит КАЖДОЕ появление атрибута, откуда бы оно ни пришло.
new MutationObserver(function(ms){
  if(hintBox.checked)return;
  ms.forEach(function(m){
    if(m.type==='attributes'){stripTitle(m.target);return}
    // Узлы, собранные ЦЕЛИКОМ до вставки (шкалы, ячейки), приходят с
    // уже выставленным title — атрибутной мутации нет, и подсказки
    // возвращались при выключенном hint (нашли codex и deepseek).
    m.addedNodes.forEach(function(n){
      if(n.nodeType!==1)return;
      stripTitle(n);
      if(n.querySelectorAll)
        n.querySelectorAll('[title]').forEach(stripTitle);
    });
  });
}).observe(document.body,{attributes:true,attributeFilter:['title'],
                          childList:true,subtree:true});

// ── кто ДЕЙСТВИТЕЛЬНО думает ─────────────────────────────────────────
// Карта акт → множество позванных. Первая редакция держала ОДНО
// глобальное множество — и чужой act_status сбрасывал фильтр, а после
// реплики голоса пустое множество гасило всех при живом акте (нашли
// grok, codex и deepseek). Акт без записи в карте показывается по
// своему списку из /state — так слепой ход (word_to не пишет) честно
// показывает всех отмеченных: там одновременно думают действительно
// все.
const SPEAK=new Map();
let AC=null;
function ding(){
  if(!bellBox.checked)return;
  try{
    AC=AC||new (window.AudioContext||window.webkitAudioContext)();
    if(AC.state==='suspended')AC.resume();
    const t0=AC.currentTime;
    [0,0.19,0.38].forEach(function(off){
      const o=AC.createOscillator(),g=AC.createGain();
      o.type='sine';o.frequency.value=1568;
      g.gain.setValueAtTime(0.0001,t0+off);
      g.gain.exponentialRampToValueAtTime(0.12,t0+off+0.012);
      g.gain.exponentialRampToValueAtTime(0.0001,t0+off+0.16);
      o.connect(g);g.connect(AC.destination);
      o.start(t0+off);o.stop(t0+off+0.17);
    });
  }catch(_){}
}
// Браузер держит AudioContext «suspended» до первого жеста — греем заранее.
document.addEventListener('pointerdown',function(){try{
  AC=AC||new (window.AudioContext||window.webkitAudioContext)();
  if(AC.state==='suspended')AC.resume();
}catch(_){}});
new EventSource('/events').onmessage=e=>{try{
  const ev=JSON.parse(e.data);
  const fresh=!(ev.id&&seen.has(ev.id));
  add(ev);
  if(fresh){
    if(ev.kind==='note'&&Array.isArray(ev.word_to)&&ev.act)
      SPEAK.set(ev.act,new Set(ev.word_to));
    else if(['say','pass','error','verdict'].indexOf(ev.kind)>=0)
      // Реплика голоса не несёт акта — снимаем его во всех множествах:
      // говорить дважды одновременно один голос не может. Опустевшее
      // множество УДАЛЯЕТСЯ, а не остаётся: пустой Set в первой
      // редакции читался как «никто не думает» и гасил индикатор при
      // живом акте (нашёл grok).
      SPEAK.forEach(function(set,k){
        set.delete(ev.author); if(!set.size)SPEAK.delete(k)});
    if(ev.kind==='act_status'&&ev.act_id&&
       ['done','error','aborted','interrupted','detached','unknown','noop']
       .indexOf(ev.status)>=0)SPEAK.delete(ev.act_id);
  }
  const young=ev.ts?(Date.now()-Date.parse(ev.ts)<60000):false;
  if(fresh&&young&&ev.kind==='act_status'&&
     ['done','error','aborted','interrupted'].indexOf(ev.status)>=0&&
     Date.now()-lastDing>2000){lastDing=Date.now();ding()}
}catch(_){}}
// ── строки голосов: галочка · модель · усилие · полоса лимита ────────
// Наказ Автора 2026-08-25: «рядом с галочкой выбора вендора можно было
// выбирать модель и эффорт… заодно подчеркнем каждое из полей
// прогрессбаром лимитов».
//
// Контракт GET /voices (серверную часть пишет второй агент):
//   {"voices":[{"name":"claude","model":"opus","models":["opus","fable"],
//               "effort":"high","efforts":["low","medium","high"],
//               "can_set_model":true,
//               "limit":{"kind":"number"|"refusal"|"none", …}}]}
// Окон лимита у голоса может быть НЕСКОЛЬКО (у claude — 5-часовое,
// недельное по всем моделям и отдельное недельное для Fable). Читаем
// в трёх видах: список в `limits`, список внутри `limit.limits` или
// `limit.windows`, одиночный `limit` — как сейчас. Сливать их в одно
// число окно не будет ни при каком виде: см. renderMulti.
// Читаем МЯГКО: /voices может ещё не существовать, вернуть голые имена
// (как /state) или прислать голос без части полей — окно обязано
// работать во всех трёх случаях и ничего не достраивать за сервер.
// Чего нет, то и рисуется как «нет»: выдуманная полоса, селектор без
// ручки и «0 %» вместо «неизвестно» — это поля, которые врут
// (правило 8.5), а у квоты цена такой лжи прямая — сожжённый лимит.
const VMETA={}, VROW={};
let VOICES_SRC=false;      // /voices ответил хоть раз
let LIM_AGE=null;          // возраст ОБЩЕГО замера лимитов, секунд

function num(x){return typeof x==='number'&&isFinite(x)?x:null}
function human(n){
  const a=Math.abs(n);
  if(a>=1e9)return (n/1e9).toFixed(2)+'G';
  if(a>=1e6)return (n/1e6).toFixed(2)+'M';
  if(a>=1e4)return (n/1e3).toFixed(1)+'k';
  return String(Math.round(n*100)/100);
}
function agoS(l,got){
  // Возраст замера. К age_s прибавляем то, что натикало у нас с момента
  // ответа сервера: без этого «замер 2 мин назад» замирает между
  // опросами и стареет молча — подпись про свежесть сама становится
  // несвежей.
  if(!l)return null;
  const a=num(l.age_s!=null?l.age_s:l.ago_s);
  if(a!=null)return a+Math.max(0,(Date.now()-(got||Date.now()))/1000);
  const raw=l.measured_at||l.at||l.ts||l.when||l.checked_at;
  if(raw){
    const d=new Date(isoUTC(raw));
    if(!isNaN(d.getTime()))return Math.max(0,(Date.now()-d.getTime())/1000);
  }
  // Запасной путь — общий момент замера из /voices (limits_age_s): у
  // самого лимита времени может не быть, а сказать «замер минуту назад»
  // всё же честнее, чем молчать о свежести.
  if(LIM_AGE!=null)return LIM_AGE+Math.max(0,(Date.now()-(got||Date.now()))/1000);
  return null;
}
function agoTxt(s){
  if(s==null)return null;
  if(s<90)return Math.round(s)+' с назад';
  if(s<5400)return Math.round(s/60)+' мин назад';
  if(s<172800)return Math.round(s/3600)+' ч назад';
  return Math.round(s/86400)+' сут назад';
}
function clip(s,n){s=String(s).replace(/\s+/g,' ').trim();
  return s.length>n?s.slice(0,n-1)+'…':s}
function unitTxt(u){
  if(!u)return '';
  const s=String(u).toLowerCase();
  if(s==='percent'||s==='%')return '%';
  if(s==='tokens')return ' токенов';
  if(s==='usd')return ' USD';
  return ' '+u;
}
function winTxt(mi){
  if(mi%1440===0)return (mi/1440)+' сут';
  if(mi%60===0)return (mi/60)+' ч';
  return mi+' мин';
}
function limNums(l){
  // Словарь сервера: known + meaning («used» — израсходовано, «left» —
  // остаток) + total. Плоские имена держим рядом: контракт молодой, и
  // читатель не должен ломаться оттого, что поле назвали иначе.
  let used=num(l.used),
      left=num(l.left!=null?l.left:(l.remaining!=null?l.remaining:l.balance));
  const known=num(l.known),total=num(l.total);
  if(known!=null){if(String(l.meaning)==='left')left=known;else used=known}
  let pct=num(l.used_percent);
  if(pct==null)pct=num(l.percent);
  if(pct==null&&total!=null&&total>0){
    if(used!=null)pct=used/total*100;
    else if(left!=null)pct=(total-left)/total*100;
  }
  if(pct!=null)pct=Math.max(0,Math.min(100,pct));
  return {used:used,left:left,total:total,pct:pct,
          some:(used!=null||left!=null)};
}
function fmtLeft(v){
  // «↻ 2д 4ч» — сколько осталось до обнуления окна (наказ 2026-08-27).
  // Принимает ISO и epoch (сек/мс). ISO без зоны считается UTC:
  // Date.parse трактовал бы его как местное время, и в Москве сброс
  // уезжал на три часа (нашли codex, grok и субагент). Прошедший срок
  // чипом не рисуем: у устаревшего замера он был бы вечным враньём.
  let t=NaN;
  if(typeof v==='number')t=v>2e10?v:v*1000;
  else if(v){let x=String(v);
    if(/T\d\d:\d\d/.test(x)&&!/[Zz]|[+-]\d\d:?\d\d$/.test(x))x+='Z';
    t=Date.parse(x);}
  if(!t||isNaN(t))return'';
  let s=Math.floor((t-Date.now())/1000);if(s<=0)return'';
  const d=Math.floor(s/86400);s%=86400;
  const h=Math.floor(s/3600);const mn=Math.floor(s%3600/60);
  if(d)return d+'д '+h+'ч';
  if(h)return h+'ч '+String(mn).padStart(2,'0')+'м';
  return mn+'м';
}
function limLabel(l,n){
  if(l.label)return String(l.label);
  const u=unitTxt(l.unit);
  if(String(l.unit).toLowerCase()==='percent'&&n.used!=null)
    return human(n.used)+'% окна'+
      (num(l.window_minutes)?' за '+winTxt(l.window_minutes):'');
  if(n.used!=null&&n.total!=null)return human(n.used)+' из '+human(n.total)+u;
  if(n.left!=null&&n.total!=null)
    return 'остаток '+human(n.left)+' из '+human(n.total)+u;
  if(n.left!=null)return 'остаток '+human(n.left)+u;
  if(n.used!=null)return 'израсходовано '+human(n.used)+u;
  return n.pct==null?'':'израсходовано '+n.pct.toFixed(0)+'%';
}
function limTail(l){
  // Всё, что уточняет число, но не влезает в подпись, — в title. Резать
  // приходится: у claude в поле text лежит 300 символов сырого json
  // отказа, и в подсказке это стена, а не сведения.
  const x=[];
  if(l.note)x.push(clip(l.note,240));
  if(l.text)x.push(clip(l.text,200));
  if(num(l.window_minutes))x.push('окно: '+winTxt(l.window_minutes));
  if(l.resets_at)x.push('сброс: '+l.resets_at);
  if(typeof l.plan==='string'&&l.plan)
    x.push('тариф: '+l.plan+' — тариф, а не остаток');
  if(l.available===false)x.push('провайдер отвечает: канал недоступен');
  if(l.round)x.push('раунд: '+l.round);
  if(l.room_text)x.push('в room.jsonl: '+clip(l.room_text,160)+
    (l.room_at?' ('+l.room_at+')':''));
  if(l.stale)x.push('значение НЕ текущее: оно из момента отказа');
  if(l.caveat)x.push(clip(l.caveat,240));
  if(l.source||l.src)x.push('источник: '+(l.source||l.src));
  return x.length?'\n'+x.join('\n'):'';
}
function limsOf(m){
  // Все окна лимита этого голоса — списком. Форму контракта читаем
  // мягко (см. комментарий выше): пришёл список — берём список, пришёл
  // один объект — список из одного. Пустое и не-объекты выбрасываем
  // молча: «лимит есть, но это строка» — не лимит.
  const out=[];
  const add=function(x){if(x&&typeof x==='object'&&!Array.isArray(x))out.push(x)};
  const l=m?m.limit:null;
  if(Array.isArray(m&&m.limits))m.limits.forEach(add);
  else if(l&&Array.isArray(l.limits))l.limits.forEach(add);
  else if(l&&Array.isArray(l.windows))l.windows.forEach(add);
  else add(l);
  return out;
}
function limName(l,i){
  // Короткая подпись окна. Придумывать её нельзя: «5 ч» рядом с
  // недельной шкалой — это уже утверждение о том, что за окно, а мы
  // его не знаем. Поэтому только то, что сказал сервер, и лишь в
  // крайнем случае безымянное «окно N».
  // Только СТРОКИ: в scope у Клода лежит объект (scope.model.display_name),
  // а в window вполне может прийти число минут — «300» без единицы
  // подписью окна не является, это цифра непонятно чего.
  const c=[l.name,l.label_short,l.label,l.title,l.window,l.scope];
  for(let k=0;k<c.length;k++)
    if(typeof c[k]==='string'&&c[k].trim())return clip(c[k],20);
  if(num(l.window_minutes))return winTxt(l.window_minutes);
  return 'окно '+(i+1);
}
function renderMulti(row,m,L){
  // Несколько окон — несколько полос, каждая со своей подписью и своим
  // процентом. Ни среднего, ни «худшего из», ни суммы: у 86 % за пять
  // часов и 100 % недельного Fable нет общей величины, а решение
  // «звать ли голос» принимают по конкретному окну.
  const box=row.querySelector('.limbox'),cap=row.querySelector('.capt');
  box.innerHTML='';cap.innerHTML='';cap.title='';
  let when0=null,hot=0,lowMoney=0;
  L.forEach(function(l,i){
    const kind=l.kind?String(l.kind):'',n=limNums(l),
          a=agoTxt(agoS(l,m._got)),tail=limTail(l);
    const when=a?'замер '+a:'время замера неизвестно';
    if(when0===null&&a)when0=a;
    const r=document.createElement('div');r.className='lim2';
    const nmt=limName(l,i),lab=limLabel(l,n);
    // Имя окна («5 часов», «неделя», «за сутки») из строки убрано —
    // наказ Автора 2026-08-31 «и так понятно»: полоса+таймер+процент.
    // Имя живёт в подсказке строки (t ниже) — наведите на полосу.
    // Обратный таймер ПЕРЕД полосой (наказ 2026-08-27): когда окно
    // обнулится. У баланса сброса нет — чипа нет.
    const lf=l.resets_at?fmtLeft(l.resets_at):'';
    if(lf){const e=document.createElement('span');e.className='eta';
      e.textContent='↻'+lf;
      e.title='до обнуления этого окна: '+lf+'\nмомент сброса: '+l.resets_at;
      r.appendChild(e);}
    // Подпись в подсказку идёт, только если она НЕ повторяет имя окна:
    // сервер часто кладёт одно и то же в label и в name, и «5 ч / 5 ч»
    // двумя строками читается как два разных факта.
    const t=nmt+(lab&&lab!==nmt?'\n'+lab:'')+'\n'+when+tail;
    const pc=document.createElement('span');pc.className='pc';
    let b;
    if(kind==='number'&&n.pct!=null){
      b=document.createElement('div');b.className='bar';
      const fill=document.createElement('i');
      fill.style.width=n.pct.toFixed(1)+'%';
      if(n.pct>=85){fill.className='hot';hot++}
      b.appendChild(fill);
      b.title='израсходовано '+n.pct.toFixed(1)+'%\n'+t;
      pc.textContent=n.pct.toFixed(0)+'%';
      if(n.pct>=85)pc.className='pc ref';
    }else if(kind==='refusal'){
      // Полосы нет намеренно, как и в одиночном случае: остатка не
      // существует, известен только факт отказа и его время.
      b=document.createElement('div');b.className='bar none';
      b.title='◇ отказ по квоте — остаток неизвестен\n'+t;
      pc.textContent='◇';pc.className='pc ref';
    }else{
      b=document.createElement('div');b.className='bar none';
      b.title=(kind==='number'
        ?(n.some?'число без потолка — шкалу не из чего строить'
                :'канал объявил число, но сервер его не прислал')
        :(kind==='none'?'остаток этот канал не сообщает'
        :(kind==='unknown'||!kind?'замер этим окном ещё не сделан'
        :'неизвестный вид лимита: '+kind)))+'\n'+t;
      // Число без потолка (баланс Кими в USD) шкалой не рисуется, но
      // ПОКАЗАТЬ его обязаны: раньше на виду стоял прочерк, а
      // единственное живое число канала пряталось в подсказку — то есть
      // правка со шкалами УХУДШИЛА видимость того, ради чего заведена
      // (нашёл ревьюер дифа).
      // Баланс меньше 2 USD — ярко-красным (наказ Автора 2026-09-01):
      // деньги на исходе должны бить в глаза, а не ждать наведения.
      // kind==='number' в условии ОБЯЗАТЕЛЕН: карточка другого вида с
      // полем left получала бы денежную тревогу при «—» на экране
      // (нашёл codex). Юнит — строго usd, число — через Number.
      const leftN=Number(n.left);
      const low=(kind==='number'&&isFinite(leftN)&&leftN<2&&
        String(l.unit||'').trim().toLowerCase()==='usd');
      pc.textContent=(kind==='number'&&n.some&&lab)?lab:'—';
      if(kind==='number'&&n.some&&lab)
        pc.className='pc num'+(low?' low':'');
      if(low){lowMoney++;b.className='bar none low';
        b.title='⚠ БАЛАНС МЕНЬШЕ 2 USD — канал скоро замолчит по '+
        'деньгам\n'+b.title}
    }
    pc.title=b.title;
    // Подсказка на ВСЕЙ строке: после скрытия имён окон навести на
    // зазор между полосой и процентом значило увидеть общий текст
    // панели вместо имени ЭТОГО окна (нашли codex и deepseek).
    r.title=t;
    r.appendChild(b);r.appendChild(pc);box.appendChild(r);
  });
  const anyRef=L.some(function(l){return String(l.kind)==='refusal'});
  cap.textContent='окон: '+L.length+(hot?' · '+hot+' на исходе':'')+
    (lowMoney?' · ⚠ деньги <2 USD':'')+
    (anyRef?' · ◇ есть отказ':'')+
    (when0?' · замер '+when0:'')+
    (m.limit&&m.limit.stale_reason?' · ⚠ не обновилось':'');
  // Один title: вторая безусловная строка затирала причину устаревания
  // — значок был, объяснение мёртвое (нашли все четверо).
  cap.title=(m.limit&&m.limit.stale_reason
    ?'свежий замер не удался: '+m.limit.stale_reason+
     '\nпоказан ПОСЛЕДНИЙ удачный — его возраст подписан рядом.\n\n':'')+
    'у голоса несколько окон лимита, и каждое показано отдельно.'+
    '\nСреднего между разными окнами не бывает: усреднить 5-часовое с '+
    'недельным значит показать число, которого не сообщал никто.';
}
function renderLim(row,m){
  const L=limsOf(m);
  // ЕДИНЫЙ вид для всех, у кого есть хоть одна шкала: Кими и DeepSeek
  // с одинаковыми балансами рисовались по-разному (у одного строка-
  // таблица, у другого пустая полоса с подписью внизу) — наказ Автора
  // 2026-08-27 «показывать одинаково». Особые ветки ниже остаются
  // только для карточек БЕЗ шкал (none/refusal/unknown).
  if(L.some(function(l){
      return ['number','refusal'].indexOf(String(l.kind||''))>=0}))
    return renderMulti(row,m,L);
  const box=row.querySelector('.limbox'),cap=row.querySelector('.capt');
  const l=L.length?L[0]:null,kind=l&&l.kind?String(l.kind):'';
  box.innerHTML='';cap.innerHTML='';cap.title='';
  // Всё уточняющее — одной сборкой (limTail): источник, оговорки,
  // окно, сброс, тариф, «значение из момента отказа». В подписи для
  // этого места нет, а без него число в панели анонимно.
  const tail=l?limTail(l):'';
  const line=function(t){const b=document.createElement('div');
    b.className='bar none';b.title=t;box.appendChild(b);return b};
  if(!l||!kind||kind==='unknown'){
    // Четыре разных «числа нет», и сливать их в одну подпись нельзя:
    // замер ещё не сделан · сервер описал голос, но без лимита · голоса
    // нет в ответе /voices · самого /voices нет. Только второе говорит
    // что-то о канале, остальные — о нашем окне, и выдать их за
    // свойство квоты значит соврать в поле (правило 8.5).
    let t;
    if(kind==='unknown')t='замер лимита этим окном ещё не сделан'+tail;
    else if(m&&m._got)
      t='лимит не сообщён: сервер не прислал поле limit для этого голоса'+tail;
    else if(VOICES_SRC)t='этого голоса нет в ответе /voices — лимит неизвестен';
    else t='лимиты не показаны: окно не получило /voices — это про окно, а не про квоту';
    line(t);
    if(kind==='unknown')cap.textContent='лимит ещё не измерен';
    cap.title=t;return;
  }
  if(kind==='number'){
    const n=limNums(l),lab=limLabel(l,n),a=agoTxt(agoS(l,m._got));
    const when=a?'замер '+a:'время замера неизвестно';
    if(n.pct==null){
      // Полосу рисовать не из чего, и случая тут два — оба честнее
      // назвать словами. Числа нет вовсе; либо число есть, а потолка
      // нет: у предоплаченного баланса deepseek общего лимита не
      // существует, и шкала выдумала бы его целиком.
      const t=(n.some
        ?'полосы нет: число без потолка — шкалу не из чего строить'
        :'канал объявил число, но сервер его не прислал')+'\n'+when+tail;
      line(t);
      cap.textContent=(lab?lab+' · ':'')+
        (n.some?when:'число объявлено, но не получено');
      cap.title=t;return;
    }
    const b=document.createElement('div');b.className='bar';
    const i=document.createElement('i');i.style.width=n.pct.toFixed(1)+'%';
    if(n.pct>=85)i.className='hot';
    b.appendChild(i);
    b.title='израсходовано '+n.pct.toFixed(1)+'%\n'+when+tail;
    box.appendChild(b);
    cap.textContent=(lab?lab+' · ':'')+when;
    cap.title=b.title;return;
  }
  if(kind==='refusal'){
    // Полосы тут НЕТ намеренно: остатка не существует, известны лишь
    // факт отказа и когда он был. Числа из момента отказа (у kimi они
    // есть) идут в подсказку, а не в шкалу: шкала показывала бы
    // позавчерашний расход как сегодняшний (правило 4).
    const a=agoTxt(agoS(l,m._got)),pad=document.createElement('div');
    pad.className='bar pad';box.appendChild(pad);   // держит высоту строки
    const s=document.createElement('span');s.className='ref';
    s.textContent='◇ отказ по квоте '+(a||'— когда, неизвестно');
    cap.appendChild(s);
    cap.appendChild(document.createTextNode(' · остаток неизвестен'));
    cap.title='число было известно в момент отказа, сейчас — нет'+tail;
    return;
  }
  const t=(kind==='none'
    ?'остаток неизвестен: этот канал его не сообщает'
    :'неизвестный вид лимита: '+kind)+
    (l.caveat?'':'\nотсутствие записи об отказе не значит, что квота цела')+
    tail;
  line(t);
  // У голоса без шкалы может быть тариф и конец оплаченного периода
  // (grok) — обратный таймер к нему, раз уж чисел расхода не бывает.
  const lim0=m.limit||{};
  const pe=lim0.period_end?fmtLeft(lim0.period_end):'';
  cap.textContent=(kind==='none'?'остаток неизвестен':'лимит: '+kind)+
    (lim0.plan?' · '+lim0.plan:'')+(pe?' · период ↻'+pe:'');
  cap.title=t;
}
function verr(name,text,isNote,title){
  const row=VROW[name];if(!row)return;
  const e=row.querySelector('.verr');
  e.textContent=text||'';e.hidden=!text;
  e.className='verr'+(isNote?' note':'');
  e.title=title||'';
  if(!text)delete row.dataset.oldenv;
}
function vplain(row){
  // Строка, о которой /voices ничего не сказал: ручек нет, и подпись
  // объясняет ПОЧЕМУ. «Данных нет» и «менять нельзя» — разные вещи, а
  // пустая ячейка одинаково похожа на обе.
  ['.mdl','.eff'].forEach(function(q){
    const c=row.querySelector(q);
    if(!c||c.contains(document.activeElement))return;
    c.innerHTML='';
    const s=document.createElement('span');
    s.className='fix';s.textContent='—';
    s.title=VOICES_SRC
      ?'этого голоса нет в ответе /voices — модель и усилие неизвестны'
      :'окно не получило /voices — модель и усилие не показаны';
    c.appendChild(s);
  });
}
function vrow(name){
  if(VROW[name])return VROW[name];
  const d=document.createElement('div');
  d.className='vrow';d.dataset.v=name;
  d.innerHTML='<div class="vhead">'+
    '<label class="vpick"><input type="checkbox" checked value="'+esc(name)+'">'+
    '<span class="nm">'+esc(name)+'</span></label>'+
    '<span class="cell mdl"></span><span class="cell eff"></span></div>'+
    '<div class="limbox"></div>'+
    '<div class="cap"><span class="capt"></span>'+
    '<span class="st" id="st-'+esc(name)+'"></span></div>'+
    '<div class="verr" hidden></div>';
  document.getElementById('voices').appendChild(d);
  VROW[name]=d;
  if(!VMETA[name])VMETA[name]={name:name};
  vplain(d);renderLim(d,VMETA[name]);
  return d;
}
function fillCell(row,name,field,cur,list,settable,fixTitle,setTitle,mark,
                  resettable){
  const cell=row.querySelector(field==='model'?'.mdl':'.eff');
  // Не перерисовываем то, в чём сейчас рука: опрос раз в 15 с иначе
  // схлопывал бы открытый список ровно в момент выбора.
  if(cell.contains(document.activeElement))return;
  cell.innerHTML='';
  if(!settable){
    const s=document.createElement('span');
    s.className='fix'+(mark?' set':'');s.textContent=cur||'—';
    // Значение первой строкой подсказки: рамка режет длинное имя
    // многоточием, и «moonshotai/kimi-k3» иначе не прочитать целиком.
    s.title=(cur?cur+'\n':'')+fixTitle
      +(mark?'\n⚠ комната и раунды настроены по-разному':'');
    cell.appendChild(s);return;
  }
  let el;
  if(list&&list.length){
    el=document.createElement('select');
    const all=list.slice();
    // Текущее значение обязано быть в списке, даже если сервер его туда
    // не положил: иначе select молча покажет первый пункт, и панель
    // соврёт, чем голос идёт на самом деле.
    if(cur&&all.indexOf(cur)<0)all.unshift(cur);
    // «(умолчание)» — сброс настройки: пустое значение сервер понимает
    // как «вернуть умолчание канала» (раньше сбросить было нельзя —
    // kimi). Пункт показываем только когда есть что сбрасывать.
    // «(умолчание)» есть ВСЕГДА: после сброса cur='' — без этого пункта
    // селект молча показывал первый реальный, и панель врала (deepseek).
    all.unshift('');
    all.forEach(function(x){
      const o=document.createElement('option');
      o.value=x;o.textContent=x===''?'(умолчание)':x;
      if(x===cur)o.selected=true;el.appendChild(o)});
  }else{
    el=document.createElement('input');el.className='free';
    el.value=cur||'';el.placeholder=field==='model'?'модель':'усилие';
  }
  // Значение первой строкой — как у .fix: ячейка узкая, «deepseek-v4-…»
  // без подсказки не отличить от «deepseek-v4-pro» (нашёл kimi).
  el.title=(cur?cur+'\n':'')+setTitle+
    (mark?'\n⚠ комната и раунды настроены по-разному':'');
  if(mark)el.className=(el.className?el.className+' ':'')+'set';
  el.onchange=function(){
    const v=String(el.value).trim();
    if(v===''&&!resettable){
      // Сбрасывать нечего — POST со сбросом был бы пустым событием-шумом.
      el.value=cur||'';return;
    }
    push(name,field,v,el,cur)};
  cell.appendChild(el);
}
// ── двигаемые границы ────────────────────────────────────────────────
// Три границы: правая панель, высота поля ввода, ширина колонки кнопок.
// Каждая — своя переменная CSS и своя запись в localStorage. Потолки
// обязательны: без них панель утягивается в ноль или на весь экран, и
// вернуть её мышью уже нечем (ручка уезжает за край).
const GRIPS=[
  {id:'grip-side', key:'rt-sidew', varn:'--sidew', axis:'x', def:'344px',
   // 330, а не 220: в строке голоса две ячейки по 6.6rem, два зазора
   // и имя от 4.2rem — на 220px контролы обрезались и появлялась
   // горизонтальная прокрутка (посчитал codex).
   min:330, max:function(){return Math.max(340,innerWidth-360)},
   // Панель справа: тянем влево — ширина растёт.
   calc:function(e){return innerWidth-e.clientX}},
  {id:'grip-acts', key:'rt-actsw', varn:'--actsw', axis:'x', def:'17rem',
   min:120, max:function(){
     const bar=document.getElementById('bar');
     return Math.max(140,bar.clientWidth-180)},
   calc:function(e){
     const bar=document.getElementById('bar').getBoundingClientRect();
     return bar.right-e.clientX-12}},
  {id:'grip-bar', key:'rt-barh', axis:'y',
   // 130, а не 90: #msg сам не ниже 6rem плюс отступы — полоса просто
   // не сожмётся, и нижние 40px хода ручки были обманом (нашёл grok).
   min:130, max:function(){return Math.max(160,innerHeight-220)},
   calc:function(e){return innerHeight-e.clientY}}
];
function sizeGrip(g,px){
  // Применить размер БЕЗ записи: общая часть restore и resize.
  if(narrow()&&g.axis!=='y'){
    // На узком экране правая панель скрыта, а колонка кнопок занимает
    // всю ширину — вертикальные ручки там ничем не управляют, но
    // писали бы --actsw, отравляя десктопную настройку (нашёл grok).
    return;
  }
  px=Math.max(g.min,Math.min(g.max(),px));
  if(g.varn)document.documentElement.style.setProperty(g.varn,px+'px');
  if(g.axis==='y')document.body.style.gridTemplateRows='1fr '+px+'px';
}
function narrow(){
  try{return matchMedia('(max-width:760px)').matches}catch(_){return false}
}
function applyGrip(g,px){
  if(px==null){
    // removeProperty, а не подстановка '1fr auto': инлайн-стиль
    // навсегда перебивал медиа-запрос, и на узком экране полоса
    // застревала в заданной высоте (нашёл kimi).
    if(g.varn)document.documentElement.style.removeProperty(g.varn);
    if(g.axis==='y')document.body.style.removeProperty('grid-template-rows');
    try{localStorage.removeItem(g.key)}catch(_){}
    return;
  }
  // Высота полосы ввода живёт не в переменной, а в самой сетке: строка
  // «auto» подстраивалась бы под содержимое и отменяла заданную высоту.
  sizeGrip(g,px);
  px=Math.max(g.min,Math.min(g.max(),px));
  try{localStorage.setItem(g.key,String(px))}catch(_){}
}
GRIPS.forEach(function(g){
  const el=document.getElementById(g.id);
  if(!el)return;
  // Восстановление НЕ пишет обратно: applyGrip клампит по текущему
  // экрану и сохранил бы ужатое значение — открыл окно узким, и
  // предпочтение «1240» стёрлось бы навсегда (нашёл grok).
  try{const v=parseFloat(localStorage.getItem(g.key));
    if(v>0)sizeGrip(g,v)}catch(_){}
  el.addEventListener('pointerdown',function(ev){
    // Второй pointerdown до отпускания (мультитач, потерянный up)
    // вешал ВТОРОЙ комплект слушателей — они копились (нашёл kimi).
    if(el.classList.contains('on'))return;
    ev.preventDefault();
    el.classList.add('on');document.body.classList.add('dragging');
    document.body.style.cursor=g.axis==='x'?'col-resize':'row-resize';
    // setPointerCapture: указатель, ушедший за пределы ручки (а он
    // уходит сразу — ручка 8px), продолжает слать события ей.
    try{el.setPointerCapture(ev.pointerId)}catch(_){}
    // Слушатели на DOCUMENT, а не на ручке: если setPointerCapture
    // бросил (а он бросает — старый Firefox, чужой pointerId), то
    // pointerup вне восьми пикселей ручки до неё не дошёл бы, .on
    // остался бы навсегда, и guard выше убил бы ручку насмерть
    // (нашёл grok — на этой же правке, что guard и добавила).
    const move=function(e){applyGrip(g,g.calc(e))};
    const up=function(){
      el.classList.remove('on');document.body.classList.remove('dragging');
      document.body.style.cursor='';
      document.removeEventListener('pointermove',move);
      document.removeEventListener('pointerup',up);
      document.removeEventListener('pointercancel',up);
      el.removeEventListener('lostpointercapture',up);
    };
    document.addEventListener('pointermove',move);
    document.addEventListener('pointerup',up);
    document.addEventListener('pointercancel',up);
    el.addEventListener('lostpointercapture',up);
  });
  // Двойной щелчок — вернуть умолчание: утащенную границу иначе
  // возвращать на глаз.
  el.addEventListener('dblclick',function(){applyGrip(g,null)});
});
// Окно сузили — сохранённый размер пересчитываем по новым потолкам, но
// в localStorage НЕ пишем: предпочтение Автора («хочу 1240») переживает
// временное сужение и вернётся, когда места снова хватит (нашёл codex).
addEventListener('resize',function(){
  GRIPS.forEach(function(g){
    let want=NaN;
    try{want=parseFloat(localStorage.getItem(g.key))}catch(_){}
    if(!(want>0))return;
    if(narrow()){
      // Узкий экран: сетка возвращается к своим правилам, инлайн-стиль
      // высоты снимается — иначе он переживал бы медиа-запрос.
      document.body.style.removeProperty('grid-template-rows');
      if(g.varn)document.documentElement.style.removeProperty(g.varn);
      return;
    }
    sizeGrip(g,want);
  });
});

document.getElementById('voices').addEventListener('change',async function(e){
  const t=e.target;
  if(!t||!t.dataset||!t.dataset.execpool)return;
  const name=t.value;
  try{
    const r=await fetch('/voices',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({voice:name,scope:'exec',pool:t.checked})});
    if(!r.ok){t.checked=!t.checked;
      let j={};try{j=await r.json()}catch(_){}
      acterr('пул: '+((j&&j.error)||('ошибка '+r.status)));}
  }catch(err){t.checked=!t.checked;acterr('сервер не ответил: '+err)}
});

function setScope(v){
  SCOPE=v;
  try{localStorage.setItem('rt-scope',v)}catch(_){}
  document.getElementById('sc-room').className='scopetab'+(v==='room'?' on':'');
  document.getElementById('sc-rounds').className='scopetab'+(v==='rounds'?' on':'');
  document.getElementById('sc-exec').className='scopetab'+(v==='exec'?' on':'');
  // Вкладка coder: блок правок ЖИВЁТ только в ней; комнатные и
  // раундовые кнопки уходят — Автор: «внизу куча кнопочек,
  // перегружено» (раунд вкладки-v1: перенос — единогласно).
  try{
    document.getElementById('vhead3').textContent=
      v==='exec'?'Голоса · кресла (🔧 coder)'
      :v==='rounds'?'Голоса · раунды (🎼)':'Голоса · кому уйдёт';
    document.getElementById('msg').placeholder=
      v==='exec'?'Задание исполнителю (кнопка Правка); поле «акт» ниже — для гейта'
      :v==='rounds'?'Вопрос раунда одной строкой (кнопка «Раунд стола»)'
      :'Реплика в комнату (@имя — адресно; Enter — отправить, Alt+Enter — перенос)';
    document.getElementById('coderblk').hidden=(v!=='exec');
    document.getElementById('sendrow').hidden=(v==='exec');
    document.getElementById('roundrow').hidden=(v==='exec');
    document.getElementById('quickrow').hidden=(v==='exec');
  }catch(_){}
  // Перерисовка БЕЗ обновления _got: замера не было, и подделывать его
  // свежесть нельзя (тот же довод, что у прежнего тумблера).
  applyVoices(Object.keys(VMETA).map(function(k){return VMETA[k]}),false);
}
document.getElementById('sc-room').onclick=function(){setScope('room')};
document.getElementById('sc-rounds').onclick=function(){setScope('rounds')};
document.getElementById('sc-exec').onclick=function(){setScope('exec')};
// Восстановленная область должна быть ВИДНА сразу: подсветка вкладок
// живёт в разметке, а SCOPE — в localStorage, и без этой строки они
// расходились до первого клика.
setScope(SCOPE);
function applyVoices(list,fresh){
  // fresh=false — локальная перерисовка (тумблер 💬/🎼): _got трогать
  // нельзя, иначе возраст замера лжёт «только что» без нового опроса
  // (нашёл codex).
  if(fresh===undefined)fresh=true;
  list.forEach(function(raw){
    const m=(typeof raw==='string')?{name:raw}:(raw||{});
    const name=m.name||m.voice||m.id;
    if(!name)return;
    if(fresh)m._got=Date.now();
    VMETA[name]=m;
    const row=vrow(name);
    const efl=Array.isArray(m.efforts)?m.efforts:[];
    const mdl=Array.isArray(m.models)?m.models:[];
    const set=m.set_in_window||{};
    // Подпись области зависит от ВКЛАДКИ: на 🎼 показывать «только
    // live.py» из комнатного scope значило бы отвечать не о том, что
    // человек сейчас правит (нашёл codex).
    const area=SCOPE==='rounds'
      ?'раунды choir.py'+((m.rounds||{}).source?' · '+(m.rounds||{}).source:'')
      :(m.scope||(m.applies_to||[]).join(', '));
    const why=m.locked_why||'';
    // Ручка есть, только если сервер сказал can_set_* И назвал, ЧЕМ её
    // крутить. can_set_effort=true при пустом списке ступеней ручкой не
    // является: POST сверяет значение со списком и отклонит любое —
    // так сейчас у deepseek, которого нет в choir.EFFORT_LADDER.
    // Селектор там был бы управлением, которого не существует.
    // Кресло: ручка только там, где за ней механика (правило 8.5) —
    // dsh не крутится вовсе, claude/kimi — только модель, grok —
    // только усилие; списки объявляет сервер (m.exec.can_*).
    // Галочка строки при 🔧 — это ПУЛ random кресел, не адресат
    // рассылки: своё состояние (m.exec.pool) и свой POST. У голоса без
    // кресла (gemini) галочка гаснет. Дорогой (kimi) в random не
    // входит независимо от галочки — она для него disabled.
    try{
      const pick=row.querySelector('.vpick input');
      if(SCOPE==='exec'){
        if(!row.dataset.pickKeep)row.dataset.pickKeep=pick.checked?'1':'0';
        const vp0=row.querySelector('.vpick');
        if(vp0.dataset.origTitle===undefined)
          vp0.dataset.origTitle=vp0.title||'';
        pick.disabled=!ex||!!(ex&&ex.costly);
        pick.checked=!!(ex&&ex.pool&&!ex.costly);
        pick.dataset.execpool='1';
        row.querySelector('.vpick').title=!ex
          ?'кресла нет: только ревьюер'
          :(ex.costly?'дорогой — в random не входит, зовите явно'
            :'галочка = участие в random-пуле кресел (POST /voices scope=exec)');
      }else if(pick.dataset.execpool){
        delete pick.dataset.execpool;
        pick.disabled=false;
        pick.checked=row.dataset.pickKeep!=='0';
        delete row.dataset.pickKeep;
        const vp=row.querySelector('.vpick');
        vp.title=vp.dataset.origTitle||'';
      }
    }catch(_){}
    const canM=SCOPE==='exec'?!!(m.exec&&m.exec.can_model)
      :m.can_set_model===true;
    const canE=SCOPE==='exec'?!!(m.exec&&m.exec.can_effort&&efl.length>0)
      :(m.can_set_effort===true&&efl.length>0);
    // Тумблер области: 💬 комната / 🎼 раунды. Ячейки показывают пару
    // ВЫБРАННОЙ области; у раундов свои значения (rounds из /voices) и
    // те же списки допустимого.
    const sc=SCOPE;
    const rd=m.rounds||{};
    const ex=m.exec||null;   // кресло исполнителя (вкладка 🔧 coder)
    // Слова вместо прочерка: «у большинства вендоров прочерки» Автор
    // прочёл как поломку, а это честное «рычага нет» (2026-09-02).
    const showM=sc==='room'?(m.model||''):sc==='exec'
      ?((ex&&ex.can_model)?(ex.model||''):(ex?'нет рычага':'нет кресла'))
      :(rd.model||'');
    const showE=sc==='room'?(m.effort||''):sc==='exec'
      ?((ex&&ex.can_effort)?(efl.length?(ex.effort||''):'ступени не объявлены')
        :(ex?'нет рычага':'нет кресла'))
      :(rd.effort||'');
    const srcM=sc==='room'?m.model_source:sc==='exec'
      ?(ex?(ex.model?'задано в окне (кресло)':'умолчание кресла — '+
            (ex.seat||name)):'кресла нет: '+name+' — только ревьюер')
      :(rd.source||'умолчание choir.py');
    const srcE=sc==='room'?m.effort_source:sc==='exec'
      ?(ex?(ex.effort?'задано в окне (кресло)':'умолчание кресла')
          :'кресла нет')
      :(rd.source||'умолчание choir.py');
    // Жёлтая рамка = «комната и раунды настроены ПО-РАЗНОМУ» (наказ
    // Автора 2026-08-31: рамка только там, где есть разница, — тогда
    // понятно, зачем она). Сравниваются ЗАДАННЫЕ значения областей;
    // умолчания областей могут различаться и без рамки — это сказано
    // в подсказке ячейки. Рамка не зависит от выбранной вкладки и не
    // прыгает при переключении.
    const rmS=set.room||{},rdS=set.rounds||{};
    const diffM=(rmS.model||null)!==(rdS.model||null);
    const diffE=(rmS.effort||null)!==(rdS.effort||null);
    const diffNote=function(d,f){return (d?'\n⚠ области различаются: '+
      '💬 '+((rmS[f])||'(умолчание)')+' · 🎼 '+((rdS[f])||'(умолчание)'):'')+
      '\nжёлтая рамка = комната и раунды ЗАДАНЫ по-разному; умолчания '+
      'областей могут различаться и без рамки'};
    fillCell(row,name,'model',showM,mdl,canM,
      clip(why||srcM||'из окна не меняется',300),
      'модель '+(sc==='exec'?'КРЕСЛА':'голоса')+' ('+
      (sc==='room'?'комната':sc==='exec'?'вкладка coder':'раунды/дирижёр')+')'+
      (srcM?'\nоткуда сейчас: '+clip(srcM,200):'')+
      diffNote(diffM,'model')+
      (area?'\nобласть: '+clip(area,300):'')+'\nсмена уходит POST /voices',
      sc==='exec'?false:diffM,
      !!(sc==='room'?rmS.model:sc==='exec'?(set.exec||{}).model:rdS.model));
    fillCell(row,name,'effort',showE,efl,canE,
      clip((m.can_set_effort===true&&!efl.length
        ?'ручка у канала есть, но сервер не назвал ни одной допустимой '+
         'ступени — он отклонит любое значение; '
        :'')+(why||srcE||'рычага усилия у этого канала нет'),300),
      'усилие '+(sc==='exec'?'КРЕСЛА':'голоса')+' ('+
      (sc==='room'?'комната':sc==='exec'?'вкладка coder':'раунды/дирижёр')+')'+
      (srcE?'\nоткуда сейчас: '+clip(srcE,200):'')+
      diffNote(diffE,'effort')+
      (m.efforts_source?'\nсписок ступеней: '+clip(m.efforts_source,120):'')+
      (area?'\nобласть: '+clip(area,300):'')+'\nсмена уходит POST /voices',
      diffE,
      !!(sc==='room'?rmS.effort:rdS.effort));
    renderLim(row,m);
  });
}
async function push(name,field,val,el,prev){
  const m=VMETA[name]||{},body={voice:name,scope:SCOPE};
  // Шлём ТОЛЬКО изменённое поле: сервер хранит вторую половину пары
  // сам, а досылка «текущего» значения из комнаты в область раундов
  // протаскивала бы чужую пару (поймано при сборке тумблера областей).
  body[field]=val;
  verr(name,'');
  let r;
  try{
    r=await fetch('/voices',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  }catch(e){el.value=prev;verr(name,'сервер не ответил: '+e);return}
  let j={};try{j=await r.json()}catch(e){j={}}
  if(!r.ok){
    // Возврат к прежнему значению обязателен: иначе селектор показывает
    // то, чего сервер не принял, — панель врёт, каким голос пойдёт.
    // Ошибка — у строки, а не alert'ом: alert перекрывает ленту и стоит
    // Автору набранной реплики.
    el.value=prev;
    // Сервер отказывает С РАЗБОРОМ (allowed/known/also_ok/why) — и это
    // самое полезное в отказе: без списка допустимых человек будет
    // тыкать наугад. Длинное «почему» уходит в подсказку.
    let t=(j&&j.error)||('ошибка '+r.status);
    if(j&&j.allowed&&j.allowed.length)t+=' — можно: '+j.allowed.join(', ');
    else if(j&&j.allowed)t+=' — допустимых ступеней сервер не знает';
    if(j&&j.known&&j.known.length)t+=' — известные: '+j.known.join(', ');
    verr(name,t,false,
      [(j&&j.why)||'',(j&&j.also_ok)?'или по шаблону: '+j.also_ok:'',
       (j&&j.source)?'список ступеней: '+j.source:'']
      .filter(Boolean).join('\n'));
    return;
  }
  // Что стоит на самом деле — берём из ответа сервера, а не из своего
  // клика: просьбу могли не принять как есть, и показать надо принятое.
  if(j&&j.voice&&j.voice.name)applyVoices([j.voice]);
  else loadVoices();
  // Ход, запущенный ДО смены, идёт со старым окружением — сказать это
  // вслух, иначе «поменял и тут же спросил» прочтётся как ответ новой
  // модели (сервер пишет ту же оговорку в ленту событием voice_config).
  const old=(j&&j.running_with_old_env)||[];
  if(old.length){
    verr(name,'идущий ход запущен ещё со старым окружением',true);
    const row=VROW[name];if(row)row.dataset.oldenv='1';
  }
}
function acterr(text,isNote){
  // Одна строка под кнопками на все отказы отправки. Почему не alert:
  // alert перекрывает ленту, требует второго клика и снимается вслепую
  // тем же Enter, которым отправляют, — то есть отказ можно закрыть, не
  // прочитав. Заметка (не ошибка) идёт приглушённым цветом.
  const e=document.getElementById('acterr');if(!e)return;
  e.textContent=text||'';e.hidden=!text;e.className=isNote?'note':'';
}
function syncQuick(names){
  // Список для быстрого вопроса — из ТЕХ ЖЕ имён, что и галочки слева.
  // Второй, самостоятельный источник имён разошёлся бы с первым молча,
  // и окно предлагало бы спросить голос, которого у комнаты нет.
  if(!qsel||!Array.isArray(names))return;
  const want=[''].concat(names);
  const have=[].map.call(qsel.options,function(o){return o.value});
  if(have.length===want.length&&have.every(function(v,i){return v===want[i]}))return;
  const cur=qsel.value;
  qsel.innerHTML='';
  want.forEach(function(v){
    const o=document.createElement('option');
    o.value=v;o.textContent=v||'случайно';
    if(v===cur)o.selected=true;qsel.appendChild(o)});
  if(want.indexOf(cur)<0)qsel.value='';   // выбранный голос исчез — назад к «случайно»
}
async function loadVoices(){
  let r;
  try{r=await fetch('/voices')}catch(e){return}   // нет — панель живёт на /state
  if(!r.ok)return;
  let j;try{j=await r.json()}catch(e){return}
  const list=Array.isArray(j)?j:(j&&Array.isArray(j.voices)?j.voices:null);
  if(!list||!list.length)return;
  LIM_AGE=(j&&typeof j.limits_age_s==='number')?j.limits_age_s:null;
  VOICES_SRC=true;applyVoices(list);
}
loadVoices();setInterval(loadVoices,15000);

async function state(){
  const s=await(await fetch('/state')).json();
  // «Думает» — по СВОЕМУ акту: у акта с заметкой word_to берутся
  // только реально позванные, у акта без неё (слепой ход, раунд) —
  // его список целиком.
  const busy={};
  s.running.forEach(function(r){
    const set=r.id?SPEAK.get(r.id):null;
    (set?Array.from(set):r.voices).forEach(v=>busy[v]=r.for_s);
  });
  // Записи умерших актов не копятся: карта чистится по живому списку.
  const alive=new Set(s.running.map(r=>r.id).filter(Boolean));
  SPEAK.forEach(function(_,k){if(!alive.has(k))SPEAK.delete(k)});
  LAST_RUN=Array.isArray(s.running)?s.running:[];
  // Поле modes объявляет сервер. Отсутствия поля НЕ достаточно, чтобы
  // сказать «режима нет» — достаточно, чтобы окно не полагалось на него.
  if(Array.isArray(s.modes))MODES=s.modes;
  window.STATE=s;   // страница читает s.edit_voices и прочие поля
  try{renderActs()}catch(_){}
  syncQuick(Array.isArray(s.voices)?s.voices:[]);
  // Строку голоса заводит ЛЮБОЙ из двух источников — /voices (модель,
  // усилие, лимит) или /state (одни имена). Второй запасной намеренно:
  // серверной части /voices может не быть вовсе, и панель обязана
  // остаться рабочей, а не пустой — галочки «кому уйдёт» важнее
  // селекторов.
  s.voices.forEach(v=>{
    const row=vrow(v);
    // О ком /voices сказал (_got), тот перерисовывается своим опросом.
    // Об остальных подпись зависит от того, отвечает ли /voices вообще,
    // — а это меняется на ходу (второй агент дописывает обработчик).
    if(!(VMETA[v]||{})._got){vplain(row);renderLim(row,VMETA[v])}
  });
  s.voices.forEach(v=>{
    // Заметка «идущий ход со старым окружением» живёт ровно пока идёт
    // тот ход: висящая дольше, она через минуту врала бы о положении
    // дел не хуже выдуманной полосы.
    const row=VROW[v];
    if(row&&row.dataset.oldenv&&busy[v]===undefined)verr(v,'');
    const el=document.getElementById('st-'+v);
    if(!el)return;
    if(busy[v]!==undefined){el.textContent='думает '+busy[v]+'с';el.className='st busy'}
    else{el.textContent='';el.className='st'}});
  // «Стоп» показываем, только когда идёт раунд: кнопка, которая всегда
  // на месте, но обычно ничего не делает, учит не доверять кнопкам.
  // Имя берём из поля round, подпись — только запасной путь.
  const rnd=s.running.find(r=>(r.label||'').startsWith('round: ')&&r.auto);
  const sb=document.getElementById('stop');
  if(rnd){const nm=rnd.round||rnd.label.slice(7).split(' ')[0];
    sb.hidden=false;sb.dataset.round=nm;
    sb.textContent='Стоп: '+nm+(rnd.auto?' (авто)':'');}
  else{sb.hidden=true;delete sb.dataset.round;sb.textContent='Стоп';}
  // «Прервать всё» видна ровно пока есть что прерывать — тот же довод,
  // что у «Стоп»: кнопка, которая всегда на месте, но обычно ничего не
  // делает, учит не доверять кнопкам.
  const ab=document.getElementById('abort');
  if(ab)ab.hidden=!LAST_RUN.length;
  const lb=document.getElementById('lotbox');
  if(s.lot){lb.innerHTML=s.lot.conductor
    ?'дирижёр: <b>'+esc(s.lot.conductor)+'</b><br><span class="hint">видно только вам — раскройте, когда вопрос закрыт</span>'
    :'commit '+esc(s.lot.commit)+'…<br>ждём drand-раунд '+s.lot.target;}
  else if(!lb.dataset.err)lb.textContent='не брошен';
}
setInterval(state,2000);state();
// input[type=checkbox] обязательно: в строке голоса теперь живут и
// поля ввода модели/усилия, а `input:checked` по ним не сработает лишь
// по счастливой случайности спецификации — полагаться на неё нельзя,
// цена ошибки в том, ЧЬИ вызовы будут оплачены.
const picked=()=>[...document.querySelectorAll(
  '#voices input[type=checkbox]:checked')].map(i=>i.value);
async function send(blind){
  const text=msg.value.trim(); if(!text)return acterr('пустая реплика',true);
  const project=document.getElementById('project').value.trim();
  acterr('');
  let r;
  // Отказ показываем строкой под кнопками, а не alert'ом — по тому же
  // доводу, что и у строки голоса: alert перекрывает ленту и стоит
  // Автору набранной реплики, если снять его вслепую.
  try{
    r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,blind,voices:picked(),project,brief:briefOn()})});
  }catch(e){return acterr('сервер не ответил: '+e)}
  let j={};try{j=await r.json()}catch(_){}
  if(r.ok)msg.value='';
  else acterr((j&&j.error)||('ошибка '+r.status));
}
// ── БЫСТРЫЙ ВОПРОС: один голос, без протокола ───────────────────────
// Наказ Автора 2026-08-25: «четвертый вариант отправки: быстрый вопрос,
// рядышком подменющечка: модель(рандом по умолчанию)».
//
// Контракт (серверную часть пишет второй агент): POST /act с
// mode:"quick" и voice — именем голоса или null для случайного.
//
// ОСТОРОЖНОСТЬ ПРИ null НЕ ПЕРЕСТРАХОВКА, А ЦЕНА ОШИБКИ. Старый
// обработчик /act поля mode не знает: он смотрит только blind и
// voices, а запрос БЕЗ voices рассылает ВЕЕРОМ на все шесть голосов.
// То есть «мелкий вопрос одному» на несогласованном сервере молча
// стал бы шестью оплаченными вызовами — ровно то, от чего в /act уже
// стоит проверка «не выбран ни один голос». Поэтому:
//   • выбран конкретный голос → шлём и voice, и voices:[voice];
//     на новом сервере это тот же голос, на старом — say ему одному,
//     что по смыслу и есть быстрый вопрос;
//   • выбрано «случайно», а сервер режим quick НЕ объявил (/state.modes)
//     → голос тянет само окно и говорит об этом строкой под кнопками.
//     Это НЕ жребий drand (правило 11 — про ведущего, не про мелочь), и
//     называть его жребием нельзя.
// Как только сервер объявит modes, случайный выбор уходит серверу
// целиком, а окно перестаёт тянуть его само.
async function sendQuick(){
  const text=msg.value.trim(); if(!text)return acterr('пустая реплика',true);
  // Этап 4: галочка превращает быстрый вопрос в выдачу кресла тому же
  // голосу. Голос без рук (не умеет правки) — кресло случайному из
  // умеющих, и подмена называется в подтверждении, а не после.
  if(document.getElementById('qexec').checked){
    const project=document.getElementById('project').value.trim();
    if(!project)return acterr('правке нужен проект: укажите путь к репозиторию');
    // Список умеющих — ОТ СЕРВЕРА (/state.edit_voices): копия в JS
    // разошлась бы молча при расширении пула (та же причина, по которой
    // модели голосов не переписываются в окне). Фолбэк — на случай
    // старого сервера.
    const known=window.STATE&&window.STATE.edit_voices;
    const CAN=known||['codex','claude','grok','deepseek'];
    const want=qsel.value||'';
    const voice=CAN.indexOf(want)>=0?want:null;
    // Без списка от сервера НЕ утверждаем «не умеет» — фолбэк мог
    // отстать от пула, и подпись лгала бы (нашёл deepseek).
    const who=voice||(want?(known?('случайному из умеющих (выбранный «'+
      want+'» правки не умеет)'):'случайному из умеющих (окно не знает '+
      'полного списка — решит сервер)'):'случайно');
    if(!confirm('ОН ЖЕ ИСПОЛНИТЕЛЬ: текст уйдёт заданием на ПРАВКУ — '+
        (voice?voice:who)+'.\nПроект: '+project+
        '\n\nЭто кресло с правом записи в worktree, платный агентный вызов.'))
      return acterr('правка отменена',true);
    acterr('');
    let r;
    try{
      r=await fetch('/edit',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({task:text,voice:voice,project:project})});
    }catch(e){return acterr('сервер не ответил: '+e)}
    let j={};try{j=await r.json()}catch(_){}
    if(!r.ok)return acterr('правка не открыта: '+((j&&j.error)||('ошибка '+r.status)));
    if(!j||!j.act)return acterr('сервер ответил 200 без акта — поле не чищу');
    if(msg.value.trim()===text)msg.value='';
    try{document.getElementById('actid').value=j.act}catch(_){}
    acterr('кресло выдано: акт '+j.act+' ['+(j.voice||'?')+']'+
      (voice?'':' — исполнителя выбрал сервер')+', ветка act/'+j.act,true);
    return;
  }
  const project=document.getElementById('project').value.trim();
  let voice=qsel.value||null,here=false;
  if(!voice&&!(MODES&&MODES.indexOf('quick')>=0)){
    const pool=picked().length?picked()
      :[].map.call(qsel.options,function(o){return o.value}).filter(Boolean);
    if(!pool.length)return acterr('некого спросить: ни одного голоса в списке');
    voice=pool[Math.floor(Math.random()*pool.length)];here=true;
  }
  const body={text:text,mode:'quick',voice:voice,project:project,
              brief:briefOn()};
  if(voice)body.voices=[voice];
  acterr('');
  let r;
  try{
    r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
  }catch(e){return acterr('сервер не ответил: '+e)}
  let j={};try{j=await r.json()}catch(_){}
  if(!r.ok)return acterr('быстрый вопрос не ушёл: '+
    ((j&&j.error)||('ошибка '+r.status)));
  msg.value='';
  acterr(here?'голос выбрало окно: '+voice+
    ' — сервер режим quick пока не объявил':'',true);
}
document.getElementById('say').onclick=()=>send(false);
document.getElementById('blind').onclick=()=>send(true);
document.getElementById('quick').onclick=()=>sendQuick();
// ── ПРЕРВАТЬ ВСЁ ─────────────────────────────────────────────────────
// Подтверждение обязательно и на кнопке, и на Esc: Автор ждал этого
// именно от Esc, а Esc жмут не думая — чтобы убрать фокус, закрыть
// список, отменить выделение. Прерывание же ломает уже ОПЛАЧЕННЫЕ
// вызовы, и восстановить их нечем. Поэтому цена нажатия названа в
// самом вопросе, вместе с перечислением того, что сейчас идёт.
async function abortAll(){
  acterr('');   // прошлая заметка про «нечего прерывать» уже неверна
  if(!LAST_RUN.length)return acterr('прерывать нечего: идущих ходов нет',true);
  const what=LAST_RUN.map(function(r){
    return '· '+(r.label||'ход')+' ('+(r.for_s||0)+'с, '+
      ((r.voices||[]).join(', ')||'голоса не названы')+')'}).join('\n');
  if(!confirm('Прервать ВСЕ идущие ходы?\n\n'+what+
    '\n\nЗаписанное в ленте останется. Ответы слепой фазы идущего раунда '+
    'лежат только в памяти дирижёра и пропадут вместе с оплатой — для них '+
    'мягче кнопка «Стоп». Отменить прерывание нечем.'))return;
  acterr('');
  let r;
  try{
    r=await fetch('/abort',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
  }catch(e){return acterr('сервер не ответил: '+e)}
  let j={};try{j=await r.json()}catch(_){}
  if(!r.ok)return acterr('прервать не вышло: '+
    ((j&&j.error)||('ошибка '+r.status))+
    (r.status===404?' — сервер этого пути ещё не знает':''));
  // Что именно снято, скажет лента и /state: окно не пересказывает
  // ответ сервера своими словами и не рисует «прервано» само.
  acterr('запрос на прерывание ушёл — смотрите ленту',true);
}
document.getElementById('abort').onclick=()=>abortAll();
// Правило имени — копия серверного ROUND_RE, но записанная ЧЕРЕЗ \p{L}:
// в JS \w это только ASCII, и дословный перенос `[\w][\w.-]{0,59}` отверг
// бы «переезд-v1» и «патент-v1» — то есть почти все имена этого стола,
// которые сервер принимает без разговоров (питоновский \w — юникодный).
const RE_ROUND=/^[\p{L}\p{N}_][\p{L}\p{N}_.\-]{0,59}$/u;
document.getElementById('round').onclick=async()=>{
  const question=msg.value.trim();
  if(!question){alert('вопрос раунда — в поле реплики');return}
  // Проверяем ЗДЕСЬ, а не по 400 с сервера: имя спрашивают уже после
  // набранного вопроса, и отказ после prompt() стоил бы Автору второго
  // ввода — а вопрос к тому моменту ещё висит в поле и легко теряется.
  let name='';
  for(;;){
    name=(prompt('Имя раунда (буквы/цифры/точка/дефис, до 60):')||'').trim();
    if(!name)return;
    if(RE_ROUND.test(name))break;
    alert('так нельзя: первый символ — буква или цифра, дальше буквы, '+
          'цифры, точка, дефис, подчёркивание; до 60 символов');
  }
  const auto=document.getElementById('auto').checked;
  const rebuts=+document.getElementById('rebuts').value;
  // Подтверждение только для авто: это единственный режим, где после
  // нажатия человека больше не спрашивают, а вызовы шести голосов
  // платные. По шагам подтверждать нечего — такт сам встанет.
  if(auto&&!confirm('Автопрогон «'+name+'»: pick → expand → ask → rebut ×'+
      rebuts+' → summarize, без остановки. Все шесть голосов, платно. Пускаем?'))return;
  const r=await fetch('/round',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({question,name,auto,rebuts,brief:briefOn()})});
  if(r.ok)msg.value='';
  else alert((await r.json()).error||'ошибка');
};
document.getElementById('stop').onclick=async()=>{
  const name=document.getElementById('stop').dataset.round;
  if(!name)return;
  if(!confirm('Остановить раунд «'+name+'»? Флаг увидит дирижёр между '+
              'шагами: текущий вызов голоса доживёт до конца.'))return;
  const r=await fetch('/stop',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({round:name})});
  if(!r.ok)alert((await r.json()).error||'ошибка');
};
// Перенос строки — Alt+Enter (наказ Автора 2026-08-25). Браузер сам
// его не вставляет: по Alt+Enter в textarea не происходит НИЧЕГО, в
// отличие от Shift+Enter, который перенос делает по умолчанию. Поэтому
// вставляем руками — и через setRangeText, а не склейкой value:
// склейка стирает историю отмены (Ctrl+Z) и уводит каретку в конец,
// то есть посреди набранного абзаца текст молча перемешается.
// Shift+Enter оставлен рабочим намеренно: привычное не отбирают, но в
// подсказках и плейсхолдере называется Alt+Enter.
function nl(){
  const a=msg.selectionStart,b=msg.selectionEnd;
  if(typeof msg.setRangeText==='function')msg.setRangeText('\n',a,b,'end');
  else{msg.value=msg.value.slice(0,a)+'\n'+msg.value.slice(b);
       msg.selectionStart=msg.selectionEnd=a+1}
  msg.scrollTop=msg.scrollHeight;
}
msg.addEventListener('keydown',e=>{
  if(e.key==='Escape'){e.preventDefault();abortAll();return}
  if(e.key!=='Enter')return;
  if(e.altKey){e.preventDefault();nl();return}
  if(e.shiftKey)return;                 // перенос делает сам браузер
  e.preventDefault();send(false)});
document.getElementById('lot').onclick=async()=>{
  const lb=document.getElementById('lotbox');delete lb.dataset.err;
  const r=await fetch('/lot',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({candidates:picked()})});
  const j=await r.json();
  if(r.ok)lb.innerHTML='commit опубликован — имя появится через ~'+j.wait_s+'с';
  else{lb.dataset.err=1;
    lb.innerHTML='<span style="color:var(--err)">'+esc(j.error)+'</span>'}
};
document.getElementById('reveal').onclick=async()=>{
  const r=await fetch('/reveal',{method:'POST'});
  if(r.ok)document.getElementById('lotbox').textContent='раскрыт — см. ленту';
};
</script></body></html>"""


def sweep_orphan_acts() -> int:
    """Закрыть действия, оставшиеся без итога после гибели окна.

    SIGKILL, паника, отключённое питание — и в ленте навсегда висит
    `accepted` без финала, а дети живут в своей сессии и продолжают
    писать реплики: в журнале появляются ответы «после» действия, у
    которого нет конца (нашёл ревьюер дифа). Правило 4 требует записать
    отказ как факт, поэтому на старте помечаем такие акты `unknown` —
    честнее, чем оставить вечное «принято».
    """
    try:
        rows = [json.loads(l) for l in
                FEED.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return 0
    opened, closed = {}, set()
    for d in rows:
        if not isinstance(d, dict) or d.get("kind") != "act_status":
            continue
        aid, st = d.get("act_id"), d.get("status")
        if st == "accepted":
            opened[aid] = d.get("text", "")
        # `detached` — ОКОНЧАТЕЛЬНЫЙ статус ответственности окна: такт
        # пережил его и доигрывает сам, а reap был daemon-нитью умершего
        # процесса и финал уже не запишет. Не считать detached закрытым
        # значит дописывать поверх честного статуса ложный `unknown`
        # (нашёл codex).
        elif st in ("done", "error", "interrupted", "unknown", "detached",
                    # `aborted` — такой же окончательный итог, как
                    # остальные. Без него каждый честно прерванный акт
                    # получал при следующем старте окна второй, ложный
                    # финал «судьба неизвестна» — два итога у одного
                    # act_id (нашли grok и kimi независимо).
                    "aborted"):
            closed.add(aid)
    stale = [a for a in opened if a and a not in closed]
    for aid in stale:
        feed_append("act_status",
                    f"act {aid} судьба неизвестна: окно было перезапущено, "
                    f"итог не записан",
                    act_id=aid, status="unknown")
    return len(stale)


def _last_run() -> dict:
    try:
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember_run(project: str) -> None:
    """Запомнить каталог этого запуска — ради `-c`.

    Молча: не сумели записать — окно всё равно работает, а `-c` в
    следующий раз честно скажет, что помнить нечего.
    """
    try:
        LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
        # Через временный файл и rename: два окна, запущенных разом,
        # иначе оставили бы полуписаный json, и следующий `-c` не смог бы
        # его прочитать (замечание deepseek). rename на одной ФС атомарен.
        tmp = LAST_RUN.with_name(LAST_RUN.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(
            {"project": project, "when": _now()}, ensure_ascii=False),
            encoding="utf-8")
        tmp.replace(LAST_RUN)
    except OSError:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="roundtable",
        description="Окно в комнату стола: лента, реплики, раунды.")
    ap.add_argument("--project", default=None, metavar="ПУТЬ",
                    help=f"каталог, о котором идёт разговор "
                         f"(умолчание — текущий: {PROJECT})")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="продолжить с каталогом прошлого запуска, "
                         "а не с текущим")
    ap.add_argument("--port", type=int, default=None,
                    help=f"порт окна (умолчание {PORT})")
    ap.add_argument("--no-project", action="store_true",
                    help="открыть окно без проекта: поле в панели пустое")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global PROJECT, PORT
    a = parse_args(argv)
    if a.port is not None:
        # `if a.port:` глотал --port 0 молча, а помощь его обещает
        # (0 = «любой свободный», у сокета это законное значение).
        PORT = a.port
    # Порядок намеренный: явный --project сильнее `-c`, `-c` сильнее
    # текущего каталога. Иначе флаг, набранный руками, молча проигрывал бы
    # памяти прошлого запуска — а это ровно тот случай, когда человек
    # уверен, что задал путь, и не понимает, почему стол смотрит не туда.
    if a.no_project:
        PROJECT = ""
    elif a.project:
        PROJECT = str(Path(a.project).expanduser())
    # resolve() ОДИН раз и здесь: /act всё равно делает p.resolve(), и
    # без этого поле показывало `./foo` или путь через симлинк, а голоса
    # читали другой каталог — панель и механика расходились (нашли grok
    # и kimi независимо).
    if PROJECT:
        try:
            PROJECT = str(Path(PROJECT).resolve())
        except OSError:
            pass
    elif a.cont:
        prev = _last_run().get("project")
        if not isinstance(prev, str):
            # `{"project": 123}` в файле роняло окно на Path(PROJECT)
            # ещё до старта сервера (нашёл ревьюер дифа).
            prev = ""
        if prev:
            PROJECT = prev
            print(f"продолжаю с каталогом прошлого запуска: {PROJECT}")
        else:
            print("нечего продолжать: прошлый запуск не записан — "
                  f"беру текущий каталог {PROJECT}", file=sys.stderr)
    # Проверку делаем ТОЛЬКО для умолчания: явный --project — решение
    # человека, и запрещать его нельзя (он может и правда хотеть отдать
    # столу свой каталог). Умолчание же человек не выбирал.
    if PROJECT and not (a.project or a.cont):
        risk = project_risk(PROJECT)
        if risk:
            print(f"⚠ каталог запуска подставлен НЕ БУДЕТ: {risk}.\n"
                  f"  Голоса получают --project на чтение; отдать его "
                  f"можно только явно:\n"
                  f"    roundtable --project {PROJECT}", file=sys.stderr)
            PROJECT = ""
    if PROJECT and not Path(PROJECT).is_dir():
        # Не отказ: окно полезно и без проекта. Но сказать обязаны —
        # молча подставленный несуществующий путь вернулся бы 400-кой на
        # первой же реплике, и виноватой выглядела бы кнопка.
        print(f"⚠ каталога {PROJECT} нет — поле проекта оставлено пустым",
              file=sys.stderr)
        PROJECT = ""
    if PROJECT:
        _remember_run(PROJECT)

    if not FEED.exists():
        print(f"нет ленты {FEED} — сначала python3 live.py ask …",
              file=sys.stderr)
        return 1
    _load_voice_cfg()
    _sync_exec_overrides()               # кресла — из того же кэша
    with CFG_LOCK:
        restored = sum(1 for v in VOICE_CFG.values()
                       if any(v.get(sc) for sc in SCOPES))
    if restored:
        print(f"настройки голосов восстановлены: {restored} "
              f"(последнее установленное — умолчание; файл {CFG_FILE.name})")
    stale = sweep_orphan_acts()
    if stale:
        print(f"закрыто незавершённых действий прошлого запуска: {stale}")
    # SIGTERM/SIGHUP → тот же путь, что Ctrl-C. Без этого весь блок
    # уборки ниже — мёртвый код: окно запускают через `nohup … &` и гасят
    # `kill <pid>`, а дефолтный SIGTERM убивает интерпретатор мимо
    # finally. Дети уведены в свою сессию, значит терминальный SIGHUP до
    # них тоже не дойдёт — сироты остаются ровно в том случае, ради
    # которого правка и писалась (нашёл ревьюер дифа).
    # srv.shutdown() из обработчика нельзя: main-нить внутри
    # serve_forever, будет дедлок.
    def _bye(signum, _frame):
        raise KeyboardInterrupt(f"сигнал {signum}")
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(_sig, _bye)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        # Трейсбек на «порт занят» — грубость: чаще всего это ВТОРОЕ окно
        # или окно, забытое работать в фоне. Человеку нужно не место в
        # socketserver.py, а что делать (поймано на живом запуске Автора).
        if e.errno != 98:
            raise
        print(f"порт {PORT} уже занят — вероятно, окно уже работает.\n"
              f"  открыть:     http://127.0.0.1:{PORT}\n"
              f"  кто держит:  ss -tlnp | grep {PORT}\n"
              f"  закрыть:     pkill -f roundtable.py\n"
              f"  другой порт: roundtable --port {PORT + 1}",
              file=sys.stderr)
        return 1
    print(f"RoundTable: http://127.0.0.1:{PORT}  (лента: {FEED.name}, "
          f"голосов: {len(VOICES)})\n"
          f"  проект: {PROJECT or '— (без проекта)'}")
    # Первый замер лимитов — сразу и в фоне: он читает десятки мегабайт
    # логов и ходит за балансом в сеть, и делать это на первом же
    # открытии страницы значит показать Автору сплошное «unknown».
    # Дальше сбор ленивый — только по запросу /voices и не чаще раза в
    # 30 секунд: если окно никто не открыл, диск никто и не трогает.
    with _LIM_LOCK:
        _LIM["busy"] = True
    threading.Thread(target=_refresh, daemon=True).start()

    def _edit_recover():
        # Акты, которых окно не сторожит: рестарт между интентом и
        # вердиктом иначе оставлял вечный edit_open — главная дыра
        # первой редакции, её нашли все пять ревьюеров. Замок держится
        # → перевзводим наблюдателя; пал → выносим вердикт сейчас.
        try:
            n = edits.recover_edits(
                lambda a, e, w, v: _arm_edit_watch(a, e, w, v))
            if n:
                print(f"восстановлено наблюдателей/вердиктов: {n}")
        except Exception as e:                  # noqa: BLE001
            print(f"edit-recover: {e}", file=sys.stderr)

    def _edit_sweep():
        # Уборка закрытых актов и репост осиротевших маркеров — каждые
        # 60 с. Осиротевший маркер = исполнитель закрылся честно, но
        # окна рядом не было (рестарт): без этого прохода такой close
        # не попадал бы в ленту никогда, и разбор читал бы акт как
        # вылет — ровно та дыра «читателя маркеров нет», которую
        # ревизия ядра называла главной (codex).
        _edit_recover()                         # первый раз — при старте
        while True:
            try:
                with RUN_LOCK:
                    active = {t.get("edit") for t in RUNNING.values()
                              if t.get("edit")}
                edits.sweep(active)
            except Exception as e:              # noqa: BLE001
                print(f"edit-sweep: {e}", file=sys.stderr)
            time.sleep(60)
    threading.Thread(target=_edit_sweep, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        # Снять всё дерево: каждый ход запущен в своей группе
        # (start_new_session), поэтому killpg достаёт и внуков — сами
        # CLI голосов. Без этого закрытое окно оставляло их доживать и
        # жечь квоту (codex, «упустили все», раунд переезд-v1).
        # SIGTERM, не KILL: голос успеет закрыть свою сессию.
        with RUN_LOCK:
            # Забираем записи СЕБЕ: reap-нить, проснувшись, увидит пустоту
            # и промолчит — итог у действия ровно один.
            unfinished = list(RUNNING.items())
            RUNNING.clear()
        # Незавершённые действия обязаны получить ИТОГ в ленте: иначе они
        # навсегда остаются в статусе `accepted`, и через месяц никто не
        # скажет, ответил голос или окно закрыли (нашёл смок 2026-08-25:
        # `WARN act_status неполные`; предсказал codex — «статус лишь в
        # RUNNING»). Пишем ДО killpg: после него daemon-нити reap уже не
        # проснутся, процесс выходит.
        for aid, t in unfinished:
            try:
                # ПЛОСКО, как кладёт spawn и как читает /state. Раньше
                # здесь было t.get("meta").get(...) — ключа "meta" в
                # записи нет вовсе, значит detach автопрогона не работал
                # никогда: такт уходил в interrupted и killpg, стирая
                # оплаченные ответы (нашли grok и kimi в живой ленте).
                auto_round = t.get("auto") and t.get("round")
                if auto_round:
                    feed_append(
                        "act_status",
                        f"act {aid}: окно закрыто, но автопрогон "
                        f"«{auto_round}» продолжается — выставлен "
                        f"флаг-стоп, дирижёр закончит текущий шаг",
                        act_id=aid, status="detached")
                else:
                    # Спросить процесс, прежде чем объявлять ход
                    # несостоявшимся: он мог успеть завершиться, и
                    # реплика голоса уже лежит в ленте. Писать над ней
                    # «прерван» значит врать журналу — через месяц это
                    # прочтётся как «стол не ответил» (нашли grok и
                    # kimi на живом «ОК» от deepseek).
                    rc = t["proc"].poll()
                    if rc == 0:
                        feed_append("act_status",
                                    f"act {aid} done (успел до закрытия "
                                    f"окна): {t['label']}",
                                    act_id=aid, status="done", rc=0)
                    elif rc is not None:
                        feed_append("act_status",
                                    f"act {aid} error (rc={rc}, окно "
                                    f"закрыто): {t['label']}",
                                    act_id=aid, status="error", rc=rc)
                    else:
                        # Ход действительно не завершился. Но частичные
                        # результаты могли попасть в ленту (голос успел
                        # ответить, live.py дописывал карту покрытия) —
                        # статус говорит об оборванной работе, а не об
                        # отсутствии ответов (уточнение codex).
                        feed_append(
                            "act_status",
                            f"act {aid} прерван: окно закрыто во время "
                            f"хода — {t['label']}; частичные результаты "
                            f"могли попасть в ленту выше",
                            act_id=aid, status="interrupted")
            except Exception as e:                  # noqa: BLE001
                print(f"act {aid}: статус не записан: {e}", file=sys.stderr)
        for aid, t in unfinished:
            # Три проверки, и каждая оплачена: живость (poll) — чтобы не
            # целиться в освобождённый pid; сохранённый pgid — чтобы не
            # спрашивать его у ядра задним числом; сверка со своей
            # группой — последний предохранитель от самоубийства окна
            # вместе с оболочкой Автора.
            try:
                if t["proc"].poll() is not None:
                    continue
                if t["pgid"] == os.getpgrp():
                    print(f"act {aid}: своя группа — не трогаю",
                          file=sys.stderr)
                    continue
                # АВТОПРОГОН НЕ УБИВАЕМ. Ответы слепой фазы по правилу 8.5
                # живут только в памяти дирижёра и попадают на диск лишь
                # при закрытии фазы — убить такт на середине значит стереть
                # уже оплаченные вызовы бесследно, без ответов и без
                # долгов. У такта своя сессия (start_new_session), он
                # переживёт окно; вместо сигнала ставим флаг-стоп, и
                # дирижёр закончит текущий шаг, а следующий не начнёт
                # (нашёл ревьюер автопрогона: раньше экспозиция была одна
                # фаза, с галочкой стала весь раунд).
                rnd = t.get("round")
                if t.get("auto") and rnd:
                    stop_file(rnd).write_text("окно закрыто\n",
                                              encoding="utf-8")
                    print(f"act {aid}: автопрогон «{rnd}» продолжается, "
                          f"выставлен флаг-стоп")
                    continue
                os.killpg(t["pgid"], signal.SIGTERM)
                print(f"снята группа хода {aid} (pgid {t['pgid']})")
            except (ProcessLookupError, PermissionError, OSError):
                pass            # уже умер сам — это норма, не ошибка
    return 0


if __name__ == "__main__":
    sys.exit(main())
