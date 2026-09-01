"""Ворота последовательного доступа к голосу: очередь вместо падения.

Зачем. У организации Кими **concurrency = 1**: два одновременных запроса
дают `429 ... max organization concurrency: 1`. В `choir.py` и `live.py`
для этого стоял `threading.Lock` — он держит очередь ВНУТРИ одного
процесса дирижёра и совершенно бессилен между процессами. Поймано
2026-08-20 на своде раунда `патент-v1`: `rebut` уже отработал, но его
соединение сервер ещё считал живым, следующий `summarize` запустился
отдельным процессом и упал за 17 секунд.

Что здесь. Замок на файле (`flock`), общий для всех процессов этой
машины, плюс ожидание вместо отказа: голос не «занят, приходите позже»,
а «встаньте в очередь». Плюс повтор на тот 429, который всё-таки
прорвался — сервер сам пишет «try again after 1 seconds», и спорить
с ним незачем.

Почему не «флаг занятости» в файле. Флаг переживает падение процесса:
дирижёра убили — флаг остался, и голос заперт навсегда. `flock` снимается
ядром при завершении процесса, каким бы оно ни было, поэтому мёртвый
держатель освобождает очередь сам.

ВОРОТА НАЗЫВАЮТСЯ ОРГАНИЗАЦИЕЙ, А НЕ ГОЛОСОМ (2026-08-26). Раньше имя
ворот совпадало с именем голоса (`kimi.lock`), и это работало ровно
пока у голоса был один ключ. С двумя ключами в разных организациях
(см. `channels.py`) общие ворота заперли бы обе линии в одну очередь —
выигрыш исчез бы молча, а в журнале это выглядело бы как «Кими опять
долго думает». Теперь имя ворот выводится из провайдера
(`channels.gate_name`), а `first_free_gate` берёт первые свободные и
говорит, какие именно взял. Следствие, о котором стоит помнить при
обновлении: процесс со СТАРЫМ кодом держит `kimi.lock`, а новый —
`moonshotai.lock`, и друг друга они не видят; после правки перезапустить
надо и раунд, и живую комнату.
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

GATE_DIR = Path.home() / ".cache" / "choir" / "gates"

# Сколько ждать своей очереди. Больше самого длинного хода голоса: смысл
# ворот в том, чтобы дождаться, а не в том, чтобы вежливо сдаться.
WAIT_LIMIT = 3600
POLL = 1.0

# Повтор на 429 concurrency: сервер просит «через секунду», даём с запасом
# и растущей паузой. Это НЕ ретрай на исчерпанную квоту (TPD) — тот
# бессмысленен и лишь глубже закапывает лимит (см. `_kimi_quota`).
RETRY_PAUSES = (3, 8, 20)


def _busy_note(path: Path) -> str:
    """Кто держит ворота — для внятного сообщения ждущему."""
    try:
        return path.read_text(encoding="utf-8").strip()[:120]
    except OSError:
        return "неизвестно кто"


def _holders(names, paths) -> str:
    """Кто держит КАЖДЫЕ из ворот: с двумя каналами «занято» без имени
    линии не говорит ничего — непонятно, ждём организацию А или обе."""
    return " · ".join(f"{n}: {_busy_note(p)}" for n, p in zip(names, paths))


@contextmanager
def mark_quota(gate: str) -> None:
    """Пометить линию исчерпанной на СЕГОДНЯ (UTC-дата в файле).

    Ворота знают только «занято/свободно», а линия с выбранной дневной
    квотой свободна ВСЕГДА — по ней никто не работает. Без этой пометки
    выбор «первая свободная» предпочитал бы мёртвую линию вечно: вызов
    уходил бы на неё, Кими на 429 не падает, а молча ретраит — и висел
    бы до таймаута (1800 с), списывая ~4300 токенов за каждый отбитый
    запрос с той самой квоты, которую добивает (нашёл ревьюер, сценарий
    A). Обратное тоже важно: когда мёртв основной, отметка выталкивает
    вызовы на живой запасной — иначе второй ключ давал бы параллельность,
    но не давал отказоустойчивости, хотя читатель ждёт именно её.
    """
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    (GATE_DIR / f"{gate}.quota").write_text(
        time.strftime("%Y-%m-%d", time.gmtime()), encoding="utf-8")


def quota_dead(gate: str) -> bool:
    """Линия помечена исчерпанной сегодня? Метка вчерашняя — квота
    сброшена, линия снова в игре (TPD суточный)."""
    try:
        stamp = (GATE_DIR / f"{gate}.quota").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stamp == time.strftime("%Y-%m-%d", time.gmtime())


def alive_gates(names: list[str]) -> list[str]:
    """Отсеять помеченные квотой — но НИКОГДА не отдавать пустой список:
    если мертвы все, честнее подождать живую очередь и получить честный
    отказ, чем не позвать голос вовсе (карта покрытия обязана различать
    «не ответил» и «не звали»)."""
    alive = [n for n in names if not quota_dead(n)]
    return alive or list(names)


@contextmanager
def first_free_gate(names, *, wait_limit: int = WAIT_LIMIT, on_wait=None):
    """Занять ПЕРВЫЕ свободные ворота из перечисленных; вернуть их имя.

    Зачем не «проверить и взять». У Кими с 2026-08-26 две линии в разных
    организациях (см. channels.py), и выбор канала звучит так: основной,
    пока он свободен, иначе запасной вместо очереди. Наивная реализация —
    спросить «занято ли?» и потом занять — оставляет щель между вопросом
    и ответом: за неё ворота успевают занять, и вызов встаёт в ту самую
    очередь, ради обхода которой всё затевалось. Здесь щели нет: занятие
    атомарно (`flock` LOCK_NB), и наружу отдаётся имя тех ворот, которые
    РЕАЛЬНО взяты, — по нему вызывающий и выбирает канал. Свойство
    обеспечено механикой, а не намерением (правило 8.5).

    Заняты все — ждём любых освободившихся, а не первых по списку: ждать
    именно основной канал, когда запасной вот-вот освободится, значит
    самим себе устроить очередь.

    Порядок значим: перечисляйте от основного к запасному.

    on_wait(seconds, holder) зовётся раз в секунду ожидания — молчащая
    очередь неотличима от зависшего вызова, а спутать их значит снова
    записать чужую занятость как медлительность участника.

    ВАЖНО про потоки: `flock` берётся на open file description, а не на
    процесс, — два потока одного процесса, открывшие файл каждый своим
    `os.open`, разводятся так же честно, как два процесса (проверено
    2026-08-26: второй поток получает EWOULDBLOCK). Поэтому отдельный
    `threading.Lock` поверх этих ворот не нужен: он бы только запер оба
    канала в одну внутрипроцессную очередь.
    """
    if isinstance(names, str):
        names = [names]
    names = list(names)
    if not names:
        raise ValueError("first_free_gate: нечего занимать")
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    paths = [GATE_DIR / f"{n}.lock" for n in names]
    fhs = [os.open(p, os.O_RDWR | os.O_CREAT, 0o644) for p in paths]
    t0 = time.monotonic()
    waited_reported = -1
    held: int | None = None
    try:
        while held is None:
            for i, fh in enumerate(fhs):
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = i
                    break
                except OSError:
                    continue
            if held is not None:
                break
            waited = int(time.monotonic() - t0)
            if waited >= wait_limit:
                raise TimeoutError(
                    f"ворота {', '.join(names)} заняты дольше {wait_limit} с; "
                    f"держит: {_holders(names, paths)}")
            if on_wait and waited != waited_reported:
                waited_reported = waited
                on_wait(waited, _holders(names, paths))
            time.sleep(POLL)

        fh = fhs[held]
        os.ftruncate(fh, 0)
        os.write(fh, f"pid {os.getpid()} с {time.strftime('%H:%M:%S')}"
                     .encode("utf-8"))
        os.fsync(fh)
        yield names[held]
    finally:
        if held is not None:
            try:
                fcntl.flock(fhs[held], fcntl.LOCK_UN)
            except OSError:
                pass
        for fh in fhs:
            try:
                os.close(fh)
            except OSError:
                pass


@contextmanager
def serial_gate(name: str, *, wait_limit: int = WAIT_LIMIT,
                on_wait=None):
    """Пропустить в ворота `name` только одного — остальные ждут.

    Частный случай `first_free_gate` с одними воротами: у голоса без
    запасной линии выбирать не из чего. Оставлено отдельным именем,
    потому что так его зовут ручные вызовы и старый код.
    """
    with first_free_gate([name], wait_limit=wait_limit, on_wait=on_wait):
        yield


def concurrency_429(blob: str) -> bool:
    """Тот ли это 429, который лечится повтором.

    Различать обязательно: `max organization concurrency` значит «сейчас
    занято, подождите», а `TPD rate limit` — «на сегодня всё». Повторять
    второй нельзя: каждый отбитый запрос всё равно списывает токены
    (замер 2026-08-19: +12 827 за три повтора), то есть ретрай отодвигает
    восстановление квоты.
    """
    if "429" not in blob and "rate_limit" not in blob:
        return False
    low = blob.lower()
    if "tpd" in low or "tokens per day" in low:
        return False
    return "concurrency" in low


def with_retry(run, *, blob_of, pauses=RETRY_PAUSES, on_retry=None):
    """Выполнить `run()`, повторив, если сервер сказал «занято, позже».

    `blob_of(result)` достаёт из результата текст вывода — модуль не знает,
    как устроен результат вызова, и знать не должен.
    """
    last = run()
    for pause in pauses:
        if not concurrency_429(blob_of(last)):
            return last
        if on_retry:
            on_retry(pause)
        time.sleep(pause)
        last = run()
    return last
