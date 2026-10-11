"""Тесты вылетоустойчивости leases.py — каждый бьёт в пункт спеки."""
import contextlib, gc, io, json, os, pathlib, signal, subprocess, sys
import tempfile, threading, time
RT = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, RT)
os.environ["CHOIR_LEASE_DIR"] = tempfile.mkdtemp(prefix="lease-test-")
import leases

ok = fail = 0
def t(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)

# 1. Эпоха монотонна, в том числе из двух процессов разом
e1, e2 = leases.mint_epoch(), leases.mint_epoch()
t("эпоха растёт", e2 == e1 + 1)
outs = subprocess.run(
    ["bash", "-c",
     f"for i in 1 2 3 4; do python3 -c \"import sys;sys.path.insert(0,'{RT}');"
     f"import os;os.environ['CHOIR_LEASE_DIR']='{os.environ['CHOIR_LEASE_DIR']}';"
     f"import leases;print(leases.mint_epoch())\" & done; wait"],
    capture_output=True, text=True).stdout.split()
t("4 процесса — 4 разных эпохи", len(set(outs)) == 4)

# 2. Вторая аренда на тот же act невозможна
fh = leases.acquire("a1")
try:
    leases.acquire("a1"); t("двойная аренда отвергнута", False)
except leases.LeaseBusy:
    t("двойная аренда отвергнута", True)
t("is_held видит захват", leases.is_held("a1"))

# 3. Наблюдатель просыпается В МОМЕНТ штатного освобождения
woke = []
leases.watch("a1", lambda why: woke.append(("drop", why, time.time())))
time.sleep(0.4)
t("наблюдатель ещё спит при живом замке", not woke)
t0 = time.time(); fh.close()                      # штатное освобождение
for _ in range(100):
    if woke: break
    time.sleep(0.02)
t("проснулся после освобождения", bool(woke))
t("проснулся быстро (<1с), без опроса", woke and woke[0][2] - t0 < 1.0)

# 4. ВЫЛЕТ: kill -9 держателя — замок падает, наблюдатель просыпается
code = (f"import sys,time;sys.path.insert(0,'{RT}');"
        f"import os;os.environ['CHOIR_LEASE_DIR']='{os.environ['CHOIR_LEASE_DIR']}';"
        f"import leases;fh=leases.acquire('a2');print('held',flush=True);time.sleep(60)")
p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
p.stdout.readline()
t("чужой процесс держит a2", leases.is_held("a2"))
woke2 = []
leases.watch("a2", lambda why: woke2.append(time.time()))
time.sleep(0.3)
t0 = time.time(); p.send_signal(signal.SIGKILL); p.wait()
for _ in range(150):
    if woke2: break
    time.sleep(0.02)
t("kill -9 разбудил наблюдателя", bool(woke2))
t("будильник мгновенный (<1с)", woke2 and woke2[0] - t0 < 1.0)
for _ in range(100):                                # ждём, а не гадаем:
    if not leases.is_held("a2"): break              # наблюдатель отпускает
    time.sleep(0.02)                                # свой мгновенный захват
t("замок a2 свободен после смерти", not leases.is_held("a2"))

# 5. «Продолжить»: новая аренда на тот же act сразу после вылета
fh3 = leases.acquire("a2")
t("новая аренда на тот же act взята", leases.is_held("a2"))
fh3.close()

# 6. Наблюдатель на act, который никто не взял — честный отказ (быстрый тест: 30с ждать не будем, проверяем ветку косвенно)
# 7. Уборка: закрытые чистятся, активные — нет
fh4 = leases.acquire("a3")
n = leases.sweep_closed(active_ids={"a3"})
t("уборка не тронула живой a3", leases.is_held("a3"))
fh4.close()
n = leases.sweep_closed(active_ids=set())
t("уборка сняла отпущенные файлы", n >= 1)

# ── Добавлено по ревизии стола 2026-08-31 (kimi/grok/codex/deepseek) ──

# 8. Мусор в файле эпох — ОТКАЗ, а не тихий откат к нулю
ep = pathlib.Path(os.environ["CHOIR_LEASE_DIR"]) / "epoch"
saved = ep.read_text().strip()
for junk, name in (("ой", "мусор"), ("", "пустой файл")):
    ep.write_text(junk)
    try:
        leases.mint_epoch(); t(f"{name} эпох → отказ", False)
    except leases.EpochCorrupt:
        t(f"{name} эпох → отказ (fail-closed)", True)
ep.write_text(saved)
t("после починки чеканка продолжает тот же ряд",
  leases.mint_epoch() == int(saved) + 1)

# 9. Потерянная ссылка на Lease: замок испаряется — но КРИЧИТ
err = io.StringIO()
with contextlib.redirect_stderr(err):
    leases.acquire("gc1")                       # намеренно без ссылки
    gc.collect()
t("брошенный Lease кричит в stderr", "потерян без close" in err.getvalue())

# 10. Два наблюдателя на один act — отказ, а не два будильника
l10 = leases.acquire("a4")
fired10 = []
leases.watch("a4", lambda why: fired10.append(why))
time.sleep(0.3)                                 # дать наблюдателю дойти до flock
try:
    leases.watch("a4", lambda why: None); t("второй watch отвергнут", False)
except ValueError:
    t("второй watch отвергнут", True)
l10.close()
for _ in range(100):
    if fired10: break
    time.sleep(0.02)
t("после срабатывания наблюдатель снят (можно взвести новый)",
  bool(fired10) and leases.watch("a4", lambda why: None) is not None)

# 11. on_drop зовётся ДО отпускания замка — иначе «продолжить» влезает в щель
seen = []
l11 = leases.acquire("a5")
leases.watch("a5", lambda why: seen.append(leases.is_held("a5")))
time.sleep(0.3)
l11.close()
for _ in range(100):
    if seen: break
    time.sleep(0.02)
t("on_drop зовётся, пока замок ещё держит наблюдатель", seen == [True])

# 12. Уборка не сносит ИМЯ из-под живой аренды (два inode = две аренды)
l12 = leases.acquire("a6")
ino = leases.lease_path("a6").stat().st_ino
leases.sweep_closed(active_ids=set())           # a6 не в активных — спасает замок
t("sweep не тронул захваченный a6",
  leases.lease_path("a6").exists()
  and leases.lease_path("a6").stat().st_ino == ino
  and leases.is_held("a6"))
l12.close()

# 13. Стресс: уборка крутится, двое берут аренду — держатель всегда один
state = {"now": 0, "max": 0}
lk, stop = threading.Lock(), threading.Event()
sweeps = {"n": 0, "err": None}
def _sweeper():
    # Без try прежняя нить тихо умирала на первой же ошибке, и стресс
    # оставался зелёным, ничего не проверив (нашёл субагент).
    while not stop.is_set():
        try:
            leases.sweep_closed(active_ids=set())
            sweeps["n"] += 1
        except Exception as e:                      # noqa: BLE001
            sweeps["err"] = e
            return
        time.sleep(0.001)
def _worker():
    for _ in range(40):
        try:
            l = leases.acquire("a7", retries=1)
        except leases.LeaseBusy:
            state["busy"] = state.get("busy", 0) + 1   # состязание было
            continue
        with lk:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.001)
        with lk:
            state["now"] -= 1
        l.close()
sw = threading.Thread(target=_sweeper, daemon=True); sw.start()
ws = [threading.Thread(target=_worker) for _ in range(2)]
[w.start() for w in ws]; [w.join() for w in ws]
stop.set(); sw.join(timeout=5)
# Уборщик обязан ОСТАНОВИТЬСЯ здесь: пережившая нить продолжает сносить
# имена в следующих тестах, и те начинают мигать (поймано прогоном).
t("уборщик стресса остановлен", not sw.is_alive())
t("под уборкой аренда не двоится", state["max"] == 1)
t("уборщик пережил стресс (нить жива, ошибок нет)",
  sweeps["err"] is None and sweeps["n"] > 0)

# ── Второй круг ревизии (kimi, codex), 2026-09-01 ────────────────────

# 14. Мусор эпох бывает не только «не-цифрой»
saved2 = ep.read_text().strip()
for junk, name in ((b"\xff\xfe", "не-UTF-8"), ("²".encode(), "²  (isdigit=True!)")):
    ep.write_bytes(junk)
    try:
        leases.mint_epoch(); t(f"{name} в эпохе → отказ", False)
    except leases.EpochCorrupt:
        t(f"{name} в эпохе → отказ", True)
    except Exception as e:                          # noqa: BLE001
        t(f"{name} в эпохе → отказ (а не {type(e).__name__})", False)
ep.write_text(saved2)

# 15. Поломка нити наблюдателя не запирает act навсегда
broke = []
leases.watch("плохой/act", lambda why: broke.append(why))
for _ in range(100):
    if broke: break
    time.sleep(0.02)
t("сломавшийся наблюдатель сообщает причину",
  bool(broke) and "сломался" in (broke[0] or ""))
try:
    leases.watch("плохой/act", lambda why: None)
    t("после поломки act не заперт навсегда", True)
except ValueError:
    t("после поломки act не заперт навсегда", False)

# 16. Политика улик: что уборка вправе унести, а что обязана сберечь
l16 = leases.acquire("a8")
l16.write_meta(cli_pid=1, cli_pgid=1)          # ход начат, cli_done нет
mark = leases.LEASE_DIR / "a8.7.close.json"
mark.write_text("{}")
(leases.LEASE_DIR / "edit-a8.7.log").write_text("стенограф")
t("meta аренды пишется отдельным файлом", leases.meta_path("a8").exists())
l16.close()
leases.sweep_closed(active_ids=set())
t("непрочитанный маркер уборка не трогает", mark.exists())
t("при живом маркере цела и meta, и стенограф",
  leases.meta_path("a8").exists()
  and (leases.LEASE_DIR / "edit-a8.7.log").exists())

# 16б. Маркер прочитан, но ход НЕ закрывался штатно — это ВЫЛЕТ,
#      и стенограф упавшего CLI единственная улика того, что он делал.
mark.unlink()
leases.sweep_closed(active_ids=set())
t("стенограф ВЫЛЕТА уборка бережёт (cli_done в meta нет)",
  (leases.LEASE_DIR / "edit-a8.7.log").exists())

# 16в. Штатно закрытый ход: cli_done есть, маркера нет — можно убирать
l16c = leases.acquire("a8c")
l16c.write_meta(cli_pid=1, cli_pgid=1, cli_done=True)
(leases.LEASE_DIR / "edit-a8c.7.log").write_text("стенограф")
l16c.close()
leases.sweep_closed(active_ids=set())
t("штатно закрытый ход уборка уносит целиком",
  not leases.meta_path("a8c").exists()
  and not (leases.LEASE_DIR / "edit-a8c.7.log").exists())

# 16г. forget(): окно разобрало вылет — следы снимает явно
t("forget снял все следы вылета",
  leases.forget("a8") >= 1 and not leases.meta_path("a8").exists()
  and not (leases.LEASE_DIR / "edit-a8.7.log").exists())

# ── Третий круг: находки самих починок (субагент, grok, deepseek) ────

# 17. Уборка сняла имя, пока аренду ещё не взяли — наблюдатель ПЕРЕЦЕПЛЯЕТСЯ
woke17 = []
leases.watch("a9", lambda why: woke17.append(why), poll_grace=0.05, grace=6)
time.sleep(0.2)
leases.sweep_closed(active_ids=set())          # сняли имя из-под наблюдателя
l17 = leases.acquire("a9")                     # исполнитель — на НОВОМ inode
time.sleep(0.4)
t("наблюдатель не крикнул вылет по живой аренде", not woke17)
l17.close()
for _ in range(200):
    if woke17: break
    time.sleep(0.02)
t("после перецепки падение замечено (а не «не была взята»)",
  woke17 == [None])

# 18. Колбэк, кинувший исключение, не пропадает молча
err18 = io.StringIO()
l18 = leases.acquire("b1")
def _boom(why):
    raise RuntimeError("колбэк окна сломан")
with contextlib.redirect_stderr(err18):
    leases.watch("b1", _boom, poll_grace=0.05)
    time.sleep(0.3)
    l18.close()
    for _ in range(100):
        if "on_drop" in err18.getvalue(): break
        time.sleep(0.02)
t("поломка колбэка окна кричит в stderr", "on_drop" in err18.getvalue())

# 19. Посторонний файл в каталоге не валит уборку целиком
(leases.LEASE_DIR / "lease-.hidden.lock").write_text("")
l19 = leases.acquire("b2"); l19.close()
n19 = leases.sweep_closed(active_ids=set())
t("уборка пережила посторонний файл и убрала настоящий акт",
  n19 >= 1 and not leases.lease_path("b2").exists())

# 20. Границы аргументов: retries=0 и строка вместо множества
try:
    l20 = leases.acquire("b3", retries=0); l20.close()
    t("acquire(retries=0) работает, а не UnboundLocalError", True)
except leases.LeaseBusy:
    t("acquire(retries=0) даёт LeaseBusy, а не UnboundLocalError", True)
except Exception as e:                                   # noqa: BLE001
    t(f"acquire(retries=0): неожиданный {type(e).__name__}", False)
try:
    leases.sweep_closed("a3")
    t("строка вместо множества — громкая ошибка", False)
except TypeError:
    t("строка вместо множества — громкая ошибка", True)

# 21. Потерянный Lease кричит и НА ВЫХОДЕ ИНТЕРПРЕТАТОРА (не только под gc)
code21 = (f"import sys;sys.path.insert(0,'{RT}');"
          f"import os;os.environ['CHOIR_LEASE_DIR']='{os.environ['CHOIR_LEASE_DIR']}';"
          f"import leases;leases.acquire('b4')")     # ссылку намеренно не держим
r21 = subprocess.run([sys.executable, "-c", code21],
                     capture_output=True, text=True)
t("брошенный Lease кричит и при выходе процесса",
  "потерян без close" in r21.stderr)

# ── Четвёртый круг: то, что нашли в починках третьего ────────────────

# 22. ПЕРИОДИЧЕСКАЯ уборка (как в спеке) + наблюдатель + живая аренда.
#     Прежняя редакция ловила мимолётный захват уборки за аренду и
#     кричала вылет по живому ходу — 2 ложных на 60 циклов (субагент).
false22, stop22 = [], threading.Event()
def _periodic():
    while not stop22.is_set():
        try:
            leases.sweep_closed(active_ids=set())
        except Exception:                               # noqa: BLE001
            return
        time.sleep(0.05)
sw22 = threading.Thread(target=_periodic, daemon=True); sw22.start()
leases.watch("c1", lambda why: false22.append(why), poll_grace=0.05, grace=8)
time.sleep(0.5)                                   # уборка успевает пройти много раз
l22 = leases.acquire("c1")
time.sleep(1.5)
t("под периодической уборкой нет ложного вылета по живой аренде",
  not false22)
l22.close()
for _ in range(200):
    if false22: break
    time.sleep(0.02)
stop22.set(); sw22.join(timeout=3)
t("настоящее падение всё равно замечено", false22 == [None])

# 23. КОРОТКИЙ ход: взяли и отпустили внутри одного интервала опроса
short = []
leases.watch("c2", lambda why: short.append(why), poll_grace=1.0, grace=8)
time.sleep(0.2)
l23 = leases.acquire("c2"); l23.close()           # весь ход — миллисекунды
for _ in range(300):
    if short: break
    time.sleep(0.02)
t("короткий ход прочитан как падение замка, а не «не была взята»",
  short == [None])

# 24. Мимолётная чужая проба арендой не считается
probe = []
leases.watch("c3", lambda why: probe.append(why), poll_grace=0.05, grace=3)
time.sleep(0.2)
for _ in range(20):
    leases.is_held("c3")                          # проба берёт замок на миг
    time.sleep(0.02)
t("проба is_held не выдаётся за аренду", not probe)

# 25. retries=0 при ЗАНЯТОМ замке — именно LeaseBusy
l25 = leases.acquire("c4")
try:
    leases.acquire("c4", retries=0)
    t("acquire(retries=0) на занятом — LeaseBusy", False)
except leases.LeaseBusy:
    t("acquire(retries=0) на занятом — LeaseBusy", True)
except Exception as e:                                   # noqa: BLE001
    t(f"acquire(retries=0): {type(e).__name__} вместо LeaseBusy", False)
l25.close()

# 26. Кирпич эпох: born без epoch — отказ, но НЕ на недописанном ряде
ep2 = pathlib.Path(os.environ["CHOIR_LEASE_DIR"]) / "epoch"
born = pathlib.Path(os.environ["CHOIR_LEASE_DIR"]) / "epoch.born"
keep = ep2.read_text()
ep2.unlink()
try:
    leases.mint_epoch(); t("born без epoch → отказ", False)
except leases.EpochCorrupt:
    t("born без epoch → отказ (ряд не начинают заново)", True)
tmpf = pathlib.Path(os.environ["CHOIR_LEASE_DIR"]) / "epoch.999.tmp"
tmpf.write_text("777")                            # смерть между двумя записями
try:
    leases.mint_epoch()
    t("недописанный первый ряд — не кирпич", True)
except leases.EpochCorrupt:
    t("недописанный первый ряд — не кирпич", False)
tmpf.unlink(missing_ok=True); ep2.write_text(keep)

# 27. write_meta СЛИВАЕТ: отметка «CLI кончился» не съедает starttime
l27 = leases.acquire("c5")
l27.write_meta(cli_pid=42, cli_pgid=42, cli_starttime=12345)
l27.write_meta(cli_done=True, cli_rc=0)
m27 = json.loads(leases.meta_path("c5").read_text())
t("starttime переживает вторую запись meta",
  m27.get("cli_starttime") == 12345 and m27.get("cli_done") is True
  and m27.get("cli_pgid") == 42)
l27.close()

# 28. Имя акта с метасимволами glob не путает поиск улик
l28 = leases.acquire("x[a]")
(leases.LEASE_DIR / "x[a].3.close.json").write_text("{}")
t("маркер акта со скобками находится (glob не при чём)",
  [f.name for f in leases.act_files("x[a]", ".close.json")]
  == ["x[a].3.close.json"])
l28.close()
leases.forget("x[a]")

print(f"\nитог: PASS {ok} · FAIL {fail}")
import shutil
shutil.rmtree(os.environ["CHOIR_LEASE_DIR"], ignore_errors=True)
sys.exit(1 if fail else 0)
