"""Аренды исполнителя: кто держит право правки и как окно узнаёт о вылете.

Механика из СПЕКА-исполнитель-v1.md (v2, отревьюирована столом
2026-08-31). Три кита, каждый оплачен находкой ревизии:

1. Замок держит ПРОЦЕСС ИСПОЛНИТЕЛЯ весь act — не окно (иначе ложная
   жизнь: окно живёт, исполнитель умер) и не по-ходово (иначе ложная
   смерть между ходами) — дыру нашёл grok.
2. Окно узнаёт о падении БЛОКИРУЮЩИМ flock: наблюдатель висит на том же
   файле и просыпается ровно в момент освобождения — без опроса и
   без «flock никого не будит» (codex).
3. Штатность отличает НЕ замок, а событие закрытия: исполнитель до
   выхода кладёт close маркером (<act>.<epoch>.close.json) и в ленту; замок
   упал без close = вылет. Обычный wait() и смерть — один syscall,
   замком их не различить (grok).

Файл замка живёт до уборки закрытого акта: удаление и пересоздание
дают два inode — две «живые» аренды (codex). Создать его может любой,
кто пришёл первым (acquire, проба is_held, наблюдатель) — важно не
кто создал, а что имя и inode не расходятся: за этим следит
_same_file во всех трёх местах.

Эпоха монотонна и живёт в своём файле под flock; всё с эпохой меньше
текущей гейт отвергает — оживший после ложной смерти пишет в пустоту.
Честно: под одним uid и файл эпохи переписываем — это ОБНАРУЖЕНИЕ,
не запрет (вердикт раунда право-записи-v1).
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path

LEASE_DIR = Path(os.environ.get("CHOIR_LEASE_DIR")
                 or Path.home() / ".cache" / "choir" / "leases")
EPOCH_FILE = LEASE_DIR / "epoch"
# Метка «ряд эпох начат»: отличает первый запуск от потери файла эпох.
BORN_FILE = LEASE_DIR / "epoch.born"


def lease_path(act_id: str) -> Path:
    if not act_id or "/" in act_id or act_id.startswith("."):
        raise ValueError(f"кривой act_id: {act_id!r}")
    return LEASE_DIR / f"lease-{act_id}.lock"


def act_files(act_id: str, suffix: str, prefix: str = "") -> list:
    """Файлы акта по точному префиксу имени, БЕЗ glob.

    glob(f"{act}.*.close.json") молча промахивался мимо акта с
    квадратной скобкой в имени (`x[a]`) — маркер не находился, улику
    сносили как ненужную; и наоборот, `a8` цеплял чужой `a8x`
    (нашёл grok). Точное сравнение строк не имеет метасимволов."""
    if not LEASE_DIR.exists():
        return []
    head = f"{prefix}{act_id}."
    return [f for f in LEASE_DIR.iterdir()
            if f.name.startswith(head) and f.name.endswith(suffix)]


def meta_path(act_id: str) -> Path:
    """Файл с pid/pgid хода — рядом с замком, но отдельно от него."""
    return lease_path(act_id).with_name(f"meta-{act_id}.json")


def _write_sync(path: Path, body: str) -> None:
    """Записать и ДОВЕСТИ ДО ДИСКА — файл и имя. Метка born без fsync
    обходила бы всю защиту от отката эпох молча (нашёл grok)."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class EpochCorrupt(RuntimeError):
    """Файл эпох нечитаем — чеканка остановлена, нужна рука человека."""


def mint_epoch() -> int:
    """Следующая эпоха. Монотонность — flock на отдельном lock-файле;
    сама запись — во временный файл + rename, атомарно.

    Fail-closed на мусор ОБЯЗАТЕЛЕН (нашли deepseek и grok): прежняя
    редакция на нечитаемом файле начинала с нуля — и все протухшие
    эпохи разом становились «свежими», то есть ломался единственный
    механизм против split-brain. Прежняя запись truncate→write ещё и
    оставляла ПУСТОЙ файл при смерти между ними — та же беда другим
    путём; rename это окно закрывает.
    """
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    lock = LEASE_DIR / "epoch.lock"
    with lock.open("a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        if EPOCH_FILE.exists():
            # Мусор бывает разный, и fail-closed обязан ловить ВЕСЬ:
            # не-UTF-8 роняло UnicodeDecodeError, а «²».isdigit() ещё и
            # True — int() падал ValueError мимо нашего EpochCorrupt
            # (нашёл codex). Наружу должно выходить одно исключение, по
            # которому зовущий понимает: чеканка остановлена.
            try:
                raw = EPOCH_FILE.read_text(encoding="utf-8").strip()
                cur = int(raw)
                if cur < 0:
                    raise ValueError("отрицательная эпоха")
                if not BORN_FILE.exists():
                    _write_sync(BORN_FILE, "эпохи начаты\n")   # бэкфилл:
                    # у старых установок born нет, и потеря epoch снова
                    # начинала ряд с единицы (нашёл grok)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                raise EpochCorrupt(
                    f"{EPOCH_FILE}: не читается как число ({e}). Откат к "
                    f"нулю оживил бы протухшие эпохи; почините руками"
                ) from e
        elif BORN_FILE.exists() and not list(LEASE_DIR.glob("epoch.*.tmp")):
            # Файл эпох УЖЕ жил, а теперь его нет — это не первый
            # запуск, а потеря (нашёл deepseek): начни мы с нуля, все
            # протухшие эпохи разом снова стали бы валидными — тот же
            # split-brain, что закрыт для мусора, только другим входом.
            raise EpochCorrupt(
                f"{EPOCH_FILE} исчез, хотя чеканка уже велась "
                f"({BORN_FILE}). Ряд эпох нельзя начинать заново — "
                f"впишите в {EPOCH_FILE} число заведомо большее всех "
                f"выданных. Каталог целиком сносить НЕ надо: там лежат "
                f"маркеры и стенографы незакрытых актов")
        else:
            cur = 0                      # первый запуск — честный старт
        nxt = cur + 1
        tmp = EPOCH_FILE.with_name(f"epoch.{os.getpid()}.tmp")
        tmp.write_text(str(nxt))
        with tmp.open() as tf:
            os.fsync(tf.fileno())
        tmp.replace(EPOCH_FILE)
        # fsync КАТАЛОГА, а не только файла: без него rename живёт в
        # кэше, и внезапное выключение откатывает эпоху на предыдущую —
        # то есть оживляет протухшую, ровно то, против чего вся
        # монотонность и заведена (нашёл kimi).
        dfd = os.open(str(LEASE_DIR), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        # Метка «ряд начат» — ТОЛЬКО ПОСЛЕ успешной записи эпохи.
        # Обратный порядок превращал первый же сбой (полный диск, RO-fs,
        # kill между двумя записями) в вечный кирпич: born есть, epoch
        # нет — и mint_epoch навсегда отвечает EpochCorrupt на чистой
        # системе (воспроизвёл субагент-ревьюер, дважды).
        if not BORN_FILE.exists():
            try:
                _write_sync(BORN_FILE, "эпохи начаты\n")
            except OSError as e:
                raise EpochCorrupt(f"{BORN_FILE} не записана: {e}") from e
        return nxt


class Lease:
    """Держатель аренды. ПРИВЯЖИ ИЛИ ПОТЕРЯЕШЬ: сборщик мусора CPython
    закрывает файл брошенного объекта — замок «испаряется» без смерти
    процесса, и наблюдатель честно объявляет вылет живому (нашли kimi,
    codex, grok и я в один голос). __del__ кричит об этом в stderr —
    потерянная ссылка не пройдёт молча."""

    def __init__(self, act_id: str, fh):
        self.act_id = act_id
        self._fh = fh
        self._closed = False
        self._meta: dict = {}
        # Ссылку на stderr берём СЕЙЧАС: на завершении интерпретатора
        # `import sys` внутри __del__ падает «import of sys halted», и
        # предупреждение — вместе с ним и close() — не случалось ровно
        # в самом частом случае потери ссылки: процесс просто кончился
        # (поймал субагент-ревьюер, воспроизведя выход из python).
        self._err = __import__("sys").stderr

    def write_meta(self, **kw) -> None:
        """Данные о ходе (pid и pgid CLI) — окну для «отозвать».

        Пишется ОТДЕЛЬНЫМ файлом и атомарно (tmp→replace), а не в сам
        lock-файл: там было truncate→write, и окно, читающее в этот
        момент, получало обрезанный json — то есть «pgid нет», то есть
        «отозвать» некого (нашёл codex). Переименовать поверх самого
        замка нельзя: замок живёт на inode, подмена имени осиротила бы
        его. Ошибку записи не глотаем молча — она означает, что окно
        останется без рычага, и об этом надо знать."""
        m = meta_path(self.act_id)
        # СЛИВАЕМ, а не перезаписываем: вторая запись («CLI кончился»)
        # не несёт cli_starttime, и поле, ради которого чинили killpg в
        # чужую группу, исчезало ровно в тот момент, когда становилось
        # нужным (нашёл grok; тест это прятал — starttime смотрели на
        # одном акте, cli_done на другом).
        self._meta.setdefault("since", time.time())
        self._meta.update(act=self.act_id, pid=os.getpid(), **kw)
        body = json.dumps(self._meta, ensure_ascii=False)
        # Тот, кто будет «отзывать» ход по pgid, ОБЯЗАН сверить
        # starttime: pid'ы переиспользуются, и сигнал в чужую группу по
        # протухшему номеру мы в этом доме уже ловили (CLAUDE.md,
        # 2026-08-26). Поле кладёт вызывающий (см. proc_starttime).
        try:
            tmp = m.with_name(m.name + f".{os.getpid()}.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())    # эпоха и маркер уже синхронны —
            tmp.replace(m)              # pgid не должен быть слабее (grok)
            dfd = os.open(str(m.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            import sys as _s
            print(f"⚠ meta аренды {self.act_id} не записана ({e}): окну "
                  f"нечем «отозвать» этот ход", file=_s.stderr)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fh.close()

    def __del__(self):
        if not getattr(self, "_closed", True):
            try:
                print(f"⚠ Lease({self.act_id}) потерян без close(): замок "
                      f"пал по прихоти сборщика мусора, наблюдатель увидит "
                      f"вылет. Держите ссылку.", file=self._err)
            except Exception:                       # noqa: BLE001
                pass                                # stderr уже закрыт
            try:
                self._fh.close()
            except OSError:
                pass


def proc_starttime(pid: int) -> int | None:
    """Момент старта процесса из /proc/<pid>/stat (поле 22, в тиках).

    Пара (pid, starttime) уникальна и переживает переиспользование
    номеров: без неё «отозвать» бьёт в чужую группу процессов."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _held_by_other(fh) -> bool:
    """Держит ли замок кто-то ДРУГОЙ — проба на уже открытом fd.

    flock даётся описанию открытого файла, а не процессу: своя вторая
    проба конфликтует с чужим захватом честно, даже внутри процесса.

    ⚠ Звать МОЖНО ТОЛЬКО на fd, который сам замка не держит: на своём
    же захваченном fd LOCK_EX|LOCK_NB пройдёт как повторный захват, а
    LOCK_UN ниже его ОТПУСТИТ — проба разоружит собственную аренду
    (заметил субагент-ревьюер: имя приглашает позвать иначе)."""
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return False


def _same_file(fh, p: Path) -> bool:
    """Тот ли файл на имени p, который открыт в fh (inode+устройство)."""
    try:
        a, b = os.fstat(fh.fileno()), p.stat()
    except OSError:
        return False
    return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)


def acquire(act_id: str, *, retries: int = 3) -> "Lease":
    """Взять аренду (сторона исполнителя). Возвращает Lease — держи
    ссылку до конца акта, закрывай ПОСЛЕ записи close-события.
    Занято — LeaseBusy: две аренды на один act невозможны.

    Короткие повторы против МЕРЦАНИЯ: is_held и просыпающийся
    наблюдатель берут замок на микросекунды, и настоящий acquire мог
    поймать ложное «занято» (нашли grok и deepseek независимо).
    """
    p = lease_path(act_id)
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    retries = max(1, int(retries))      # retries=0 роняло UnboundLocalError
    for attempt in range(retries):
        fh = p.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            if attempt + 1 == retries:
                raise LeaseBusy(act_id)
            time.sleep(0.01 * (attempt + 1))
            continue
        # ВЗЯЛИ ЗАМОК — ПРОВЕРЬ, ЧТО ОН ЖИВОЙ. Между open и flock уборка
        # могла снять ИМЯ: тогда мы сторожим осиротевший inode, а
        # следующий acquire создаст по тому же имени новый файл и тоже
        # «преуспеет» — две аренды на один акт. Стресс-тест ловил это
        # сразу: max одновременных держателей = 2 (защита стояла только
        # в sweep, а дыра была в acquire).
        if _same_file(fh, p):
            break
        fh.close()
        if attempt + 1 == retries:
            raise LeaseBusy(act_id)
        time.sleep(0.01 * (attempt + 1))
    lease = Lease(act_id, fh)
    lease.write_meta()
    return lease


class LeaseBusy(RuntimeError):
    pass


def is_held(act_id: str) -> bool:
    """Держит ли КТО-ТО аренду прямо сейчас. Неблокирующая проба."""
    p = lease_path(act_id)
    if not p.exists():
        return False
    with p.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


_WATCHED: set = set()
_WATCH_LOCK = threading.Lock()


def watch(act_id: str, on_drop, *, poll_grace: float = 0.5,
          grace: float = 30.0) -> threading.Thread:
    """Наблюдатель окна: нить, спящая в блокирующем flock.

    КОНТРАКТ (потребовали все четверо ревьюеров, каждый со своей
    стороны): наблюдатель сообщает ТОЛЬКО «замок пал». Штатность или
    вылет решает ВЫЗЫВАЮЩИЙ — по наличию события close ЭТОЙ ЭПОХИ в
    ленте (или маркера <act>.<epoch>.close.json): close есть —
    штатное завершение, нет — вылет и карантин.

    on_drop зовётся ДО отпускания нашего мгновенного захвата: иначе
    «продолжить» успевал взять новую аренду, пока событие падения ещё
    не обработано (нашёл deepseek). Второй watch на тот же act —
    отказ ValueError, а не два конкурирующих будильника (codex).
    После срабатывания наблюдатель СНЯТ: новую аренду сторожит только
    новый watch, взводимый окном вместе с ней.

    ЧТО КОЛБЭКУ НЕЛЬЗЯ: брать аренду или взводить новый watch на этот
    же act ПРЯМО В НЁМ. Наблюдатель в этот момент ещё держит LOCK_EX и
    ещё числится в _WATCHED, так что acquire вернёт LeaseBusy, а watch
    — ValueError, и «продолжить» молча не случится (поймал
    субагент-ревьюер: обещание в этой же докстроке было невыполнимо).
    Колбэк должен быть коротким и ставить работу в очередь окна.

    Стартовая грация — опрос по poll_grace до grace секунд: да, это опрос, но
    ровно на рукопожатии запуска (замок ещё не взят исполнителем);
    сторожение падения — по-прежнему блокирующий flock без опроса.
    """
    with _WATCH_LOCK:
        if act_id in _WATCHED:
            raise ValueError(f"наблюдатель за {act_id} уже взведён")
        _WATCHED.add(act_id)
    fired = threading.Event()

    def fire(why):
        if not fired.is_set():          # идемпотентность (grok)
            fired.set()
            try:
                on_drop(why)
            except Exception:           # noqa: BLE001
                # Вся обработка вылета живёт в колбэке окна. Съеденное
                # здесь исключение означало: вылет не обработан, и об
                # этом не узнал никто (поймал субагент-ревьюер).
                import sys as _s
                import traceback
                print(f"⚠ on_drop({act_id}) упал — падение аренды "
                      f"осталось НЕобработанным:", file=_s.stderr)
                traceback.print_exc()

    def run():
        try:
            _watch_body(act_id, fire, poll_grace, grace)
        except Exception as e:          # noqa: BLE001
            # Поломка самой нити — тоже исход, и молчать о ней нельзя:
            # окно ждало бы сигнала вечно. Плюс снятие из _WATCHED
            # обязано случиться при ЛЮБОМ выходе, иначе act навсегда
            # «уже под наблюдением» и второй watch не взвести (нашёл
            # codex — обе половины: и щель, и вечный ValueError).
            fire(f"наблюдатель сломался: {e}")
        finally:
            with _WATCH_LOCK:
                _WATCHED.discard(act_id)
    t = threading.Thread(target=run, daemon=True,
                         name=f"lease-watch-{act_id}")
    t.start()
    return t


def _watch_body(act_id: str, fire, poll_grace: float,
                grace: float = 30.0) -> None:
    """Тело наблюдателя: дождаться захвата, потом падения.

    Снятие из _WATCHED делает вызывающий — и ТОЛЬКО после того, как
    замок отпущен: сними раньше, и параллельный новый watch успеет
    взять флаг, увидеть замок УХОДЯЩЕГО наблюдателя за аренду и
    объявить ложное падение сразу за нашим callback (codex).

    ДОКАЗАТЕЛЬСТВО ЗАХВАТА — не мгновенная проба. Проба врёт в обе
    стороны, и обе поймали третьим кругом:
      • короткий ход (CLI упал сразу) успевает взять и отпустить замок
        ВНУТРИ одного интервала опроса — проба не видит ничего, и через
        grace секунд окно получает «аренда не взята» по штатно
        закрытому акту (нашёл kimi);
      • мимолётный захват уборки или чужой пробы выглядит держателем —
        наблюдатель тут же кричит «замок пал» по ходу, который ещё не
        начинался (нашёл grok, 1 ложный на 200 прогонов).
    Поэтому захват засчитывается по СЛЕДУ: свежая meta (её пишет
    acquire сразу после захвата) или реальное ожидание на блокирующем
    flock. Мимолётная проба не даёт ни того, ни другого.
    """
    p = lease_path(act_id)
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    m = meta_path(act_id)

    def _mtime():
        try:
            return m.stat().st_mtime_ns
        except OSError:
            return None

    m0 = _mtime()                       # снимок ДО: meta прошлой попытки
    def took_it():                      # ...не должна считаться следом
        cur = _mtime()
        return cur is not None and (m0 is None or cur > m0)

    fh = p.open("a+")
    try:
        deadline = time.time() + grace
        while time.time() < deadline:
            # Перецепка, пока замок ничей: уборка снимает имя (акта ещё
            # нет в active_ids), исполнитель заводит НОВЫЙ inode — и
            # наблюдатель сторожил бы осиротевший файл, которого никто
            # не возьмёт (нашли субагент и grok; дыра рождена вчерашней
            # починкой «один fd на весь срок» — тот самый класс, ради
            # которого и делается следующий круг).
            if not _same_file(fh, p):
                fh.close()
                fh = p.open("a+")
                continue
            if _held_by_other(fh):
                t0 = time.monotonic()
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)   # спим до падения
                waited = time.monotonic() - t0
                try:
                    if took_it() or waited > 0.05:
                        fire(None)      # сообщить ДО отпускания
                        return
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                continue                # это была проба, а не аренда
            if took_it():
                fire(None)              # взяли и отпустили между опросами
                return
            time.sleep(max(0.0, min(poll_grace, deadline - time.time())))
        fire(f"аренда так и не была взята за {grace:g} с")
    finally:
        fh.close()

def _meta_done(act_id: str) -> bool | None:
    """Закрылся ли ход штатно по meta: True/False/None (meta нет)."""
    try:
        return json.loads(meta_path(act_id).read_text()).get("cli_done") is True
    except (OSError, ValueError):
        return None


def forget(act_id: str) -> int:
    """Снести ВСЕ следы акта — зовёт окно, когда разбор вылета закончен
    и карантин разрешён. Без этого улики вылета лежат до старости: см.
    политику ниже, они специально переживают уборку."""
    n = 0
    for f in ([lease_path(act_id), meta_path(act_id)]
              + act_files(act_id, ".close.json")
              + act_files(act_id, ".log", prefix="edit-")):
        try:
            if f.exists():
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def sweep_closed(active_ids, *, tmp_age_s: float = 86400,
                 crash_age_s: float = 30 * 86400) -> int:
    """Убрать хвосты ЗАКРЫВШИХСЯ актов. Возвращает число снятых ЗАМКОВ
    (meta, стенографы и tmp считаются отдельно и молча).

    Только не держимые и не активные: файл живого акта трогать нельзя
    (два inode — две аренды).

    ПОЛИТИКА УЛИК — главный урок трёх кругов ревизии:
      • маркер <act>.<epoch>.close.json уборка НЕ трогает никогда. Это
        единственная запись о честном закрытии, когда лента упала; его
        снимает тот, кто прочитал (исполнитель при удачной записи или
        окно, допостив). Первая редакция сносила маркер вместе с
        замком — и честно закрытый ход задним числом становился вылетом
        без следов (нашёл субагент-ревьюер, правило 4);
      • стенограф и meta ВЫЛЕТА тоже переживают уборку. Вторая редакция
        берегла лог только при живом маркере — то есть ровно там, где
        он не нужен, и стирала там, где он единственный (тот же
        ревьюер, следующий круг: «защита включена наоборот»). Признак
        штатности — cli_done в meta, его пишет обёртка после wait();
      • всё это живёт до forget(act) от окна или до crash_age_s —
        безбрежно копиться уликам тоже нельзя.
    """
    if isinstance(active_ids, str):
        # Строка вместо множества давала подстрочный поиск: половина
        # актов молча числилась активной и не убиралась никогда. Тихую
        # порчу меняем на громкую ошибку (нашёл субагент-ревьюер).
        raise TypeError("active_ids — множество имён, а не строка")
    active = set(active_ids or ())
    n = 0
    if not LEASE_DIR.exists():
        return 0
    now = time.time()

    def _evidence(act: str) -> bool:
        """Есть ли по акту непрочитанные улики: маркер или незакрытый ход."""
        if act_files(act, ".close.json"):
            return True
        done = _meta_done(act)
        return done is False            # meta есть, cli_done нет → вылет

    for p in sorted(LEASE_DIR.glob("lease-*.lock")):
        act = p.name[len("lease-"):-len(".lock")]
        if act in active:
            continue
        # Посторонний файл в каталоге не должен валить уборку целиком:
        # lease-.hidden.lock роняло lease_path ValueError'ом мимо
        # except OSError, и НИ ОДИН акт дальше не убирался (субагент).
        try:
            mp = meta_path(act)         # он же проверяет имя акта
        except ValueError:
            continue
        # Удаляем, ДЕРЖА замок на этом же fd: иначе между «свободен» и
        # unlink кто-то берёт аренду на старом inode, мы сносим ИМЯ, а
        # следующий acquire создаёт второй файл — две «живые» аренды
        # одного акта (нашёл codex, до стресс-теста дело не дошло бы).
        try:
            with p.open("a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue            # занят — живой, не трогаем
                if not _same_file(fh, p):
                    continue            # имя уже указывает на другой файл
                keep = _evidence(act)
                p.unlink()
                # Сверяем ЕЩЁ РАЗ: пока мы держали замок на старом
                # inode, кто-то мог начать новый ход и записать свежую
                # meta — снеся её, мы отняли бы у окна pgid только что
                # начатого хода (нашёл kimi).
                if not keep and not p.exists():
                    mp.unlink(missing_ok=True)
                    for lg in act_files(act, ".log", prefix="edit-"):
                        lg.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass

    # Стенографы разбираются ОТДЕЛЬНЫМ проходом: замок акта снимается
    # раньше, чем прочитан его маркер, и привязанная только к замку
    # уборка логов не наступала бы никогда.
    for lg in LEASE_DIR.glob("edit-*.log"):
        stem = lg.name[len("edit-"):-len(".log")]
        act = stem.rsplit(".", 1)[0]
        if act in active:
            continue
        try:
            if lease_path(act).exists():
                continue                # акт ещё числится живым
        except ValueError:
            continue                    # чужой файл — не наше дело
        try:
            old = now - lg.stat().st_mtime > crash_age_s
            if _evidence(act) and not old:
                continue                # улика: держим до forget() окна
            lg.unlink(missing_ok=True)
            if old:
                meta_path(act).unlink(missing_ok=True)
        except OSError:
            pass

    # Осиротевшие tmp'шки (смерть между write и replace) — по возрасту:
    # молодые могут быть чужой живой записью прямо сейчас.
    for t in LEASE_DIR.glob("*.tmp"):
        try:
            if now - t.stat().st_mtime > tmp_age_s:
                t.unlink(missing_ok=True)
        except OSError:
            pass
    return n
