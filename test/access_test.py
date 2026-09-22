#!/usr/bin/env python3
"""ACCESS.txt проекта (chamber/access.py): разбор, отказы поимённо,
строка пакета, отпечаток, bind'ы клетки. Без bwrap и без голосов —
jail.prefix чистая функция. Дом — подменный: проверки предка дома и
состояния CLI не должны зависеть от настоящего ~ машины."""
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RT / "chamber"))

W = Path(tempfile.mkdtemp(prefix="accesstest.", dir="/var/tmp"))
HOME = W / "home"
(HOME / ".claude").mkdir(parents=True)
(HOME / ".codex").mkdir()
(HOME / ".claude" / "projects").mkdir()
os.environ["HOME"] = str(HOME)
os.environ.pop("CHOIR_RT_NO_BWRAP", None)

import access                                             # noqa: E402
import jail                                               # noqa: E402

ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


# Проект — НЕ рядом с домом: иначе предок проекта был бы предком дома,
# и отказ «rw на предка проекта» маскировался бы отказом «предок дома».
proj = W / "work" / "proj"; (proj / "sub").mkdir(parents=True)
data = W / "data"; data.mkdir()
other = W / "other"; other.mkdir()
one = W / "one.txt"; one.write_text("x")
hide = W / "hide"; (hide / "q").mkdir(parents=True)

# ── 1. Нет проекта / нет файла ──
a = access.load(None)
t("без проекта — пустой доступ, без файла, без отпечатка",
  a.empty and a.file is None and a.sha is None and access.fact(a) == {}
  and access.notice(a) == "" and access.mark(a) == "")
a = access.load(proj)
t("проект без ACCESS.txt — пусто, файл не назван, mark пуст",
  a.empty and a.file is None and access.mark(a) == "" and a.project == str(proj))

# ── 2. Разбор и отказы поимённо ──
(proj / access.FILE).write_text(
    "# комментарий\n"
    f"r {data}\n"
    f"rw  {other}\r\n"                     # CRLF и два пробела
    f"r {data}\n"                          # повтор — один bind
    f"r {one}\n"                           # файл — допустим
    "r relative/path\n"
    f"r {W / 'nope'}\n"
    f"x {data}\n"
    "r\n"
    f"r {HOME}\n"
    f"r {HOME.parent}\n"
    f"r {HOME / '.claude'}\n"
    f"r {HOME / '.claude' / 'projects'}\n"
    f"r {hide / 'q'}\n"
    f"r {proj / 'sub'}\n"
    f"rw {proj}\n"
    f"rw {proj.parent}\n"
    f"rw {RT}\n"
    f"rw {RT / 'journal'}\n"
    f"r {RT}\n",
    encoding="utf-8")
a = access.load(proj, hide=[str(hide)])
t("ro: data, файл, стол на чтение (RT — настоящий стол, рядом roundtable.py); rw: other; повтор схлопнут",
  a.ro == [str(data), str(one), str(RT)] and a.rw == [str(other)])
t("файл назван, отпечаток есть, fact несёт access_sha",
  a.file == str(proj / access.FILE) and a.sha and len(a.sha) == 16
  and access.fact(a) == {"access_sha": a.sha})
rej = "\n".join(a.rejects)
t("отвергнуто: относительный, несуществующий, кривой режим, без пути",
  "не абсолютный" in rej and "не существует" in rej
  and "режим «x»" in rej and "нет пути" in rej)
t("отвергнуто: дом и предок дома",
  rej.count("дом голоса или его предок") == 2)
t("отвергнуто: состояние CLI — как скрытые записи дома (сам каталог и внутри него)",
  rej.count("скрытая запись дома") == 2)
t("отвергнуто: внутри скрытого каталога стола",
  "внутри скрытого каталога стола" in rej)
t("отвергнуто: внутри проекта; rw на проект и его предка",
  rej.count("открыт и так") == 1 and rej.count("кресло пишет только в worktree") == 2)
t("отвергнуто: rw на стол и на его журнал",
  rej.count("rw на стол") == 2)
t("каждая отвергнутая строка несёт номер и текст",
  all(r.startswith("строка ") and "«" in r for r in a.rejects))
t("число отказов = число плохих строк", len(a.rejects) == 14)

# ── 3. Что уходит в клетку и в пакет ──
t("вне кресла rw читается, записи нет",
  access.ro_paths(a) == [str(data), str(one), str(RT), str(other)]
  and access.rw_paths(a) == [])
t("в кресле rw пишется, ro — только r",
  access.ro_paths(a, chair=True) == [str(data), str(one), str(RT)]
  and access.rw_paths(a, chair=True) == [str(other)])
n = access.notice(a)
t("строка пакета называет чтение и запись креслу, отвергнутого в ней нет",
  n.startswith("ДОСТУП ПО ACCESS.txt ПРОЕКТА") and str(data) in n
  and f"на запись: {other}" in n and "отвергнут" not in n.lower()
  and "nope" not in n)
m = access.mark(a)
t("стенограмма: записи и отвергнутое с номерами строк",
  m.startswith("\n📂 ACCESS.txt: r ") and f"rw {other}" in m
  and "ОТВЕРГНУТО: строка 6" in m)

# ── 4. Bind'ы клетки: prefix чистая функция ──
pref = jail.prefix("claude", ro=access.ro_paths(a), rw=access.rw_paths(a),
                   cwd=str(proj))
def bound(kind, p):
    return any(pref[i] == kind and pref[i + 1] == p and pref[i + 2] == p
               for i in range(len(pref) - 2))
t("комната/раунд: data и other — ro-bind, --bind other нет",
  bound("--ro-bind", str(data)) and bound("--ro-bind", str(other))
  and not bound("--bind", str(other)))
pref = jail.prefix("claude", ro=access.ro_paths(a, chair=True),
                   rw=access.rw_paths(a, chair=True), cwd=str(proj))
t("кресло: other — --bind (запись), data — ro-bind",
  bound("--bind", str(other)) and bound("--ro-bind", str(data)))

# ── 4b. Ревизия 20.09: точки дома, виртуальные деревья, /tmp, ссылки, конфликт режимов, каталоги стола ──
(HOME / ".ssh").mkdir(); (HOME / ".gnupg").mkdir()
link = W / "link"; os.symlink(str(data), str(link))
# wt — под W/work, не под W: W — предок подменного дома, и любая строка на W
# отвергалась бы как «предок дома», маскируя проверяемый отказ.
wt = W / "wtroot" / "wt"; (wt / "act1").mkdir(parents=True)   # не под work/: там проект, и предок проекта отвергся бы раньше
os.environ["CHOIR_WT_DIR"] = str(wt)
(proj / access.FILE).write_text(
    f"r {HOME / '.ssh'}\n"
    f"rw {HOME / '.gnupg'}\n"
    f"r {HOME / 'Cursor_W_like'}\n"          # не точка — допустимо (создадим)
    "r /proc\n"
    "r /run/user\n"
    "rw /dev/shm\n"
    "r /tmp\n"
    "rw /var/tmp\n"
    f"r {W / 'work'}\n"                    # подкаталог /var/tmp — допустимо
    f"r {link}\n"
    f"r {data}\n"
    f"rw {data}\n"                           # конфликт режимов — поздняя строка отвергается
    f"rw {wt / 'act1'}\n"                    # worktree чужого акта (CHOIR_WT_DIR) — отказ
    f"r {wt}\n"                              # и на чтение — отказ
    f"rw {W / 'wtroot'}\n"                 # предок каталога стола (wt) — отказ
    f"rw {proj / 'sub'}\n",                  # rw внутрь проекта — своя причина
    encoding="utf-8")
(HOME / "Cursor_W_like").mkdir()
a = access.load(proj)
rej = "\n".join(a.rejects)
t("точки дома отвергнуты (r ~/.ssh, rw ~/.gnupg), обычный каталог дома допущен",
  rej.count("скрытая запись дома") == 2 and str(HOME / "Cursor_W_like") in a.ro)
t("виртуальные деревья отвергнуты: /proc, /run/user, /dev/shm",
  rej.count("виртуальное дерево") == 3)
t("корни /tmp и /var/tmp целиком отвергнуты, подкаталог /var/tmp допущен",
  rej.count("общий каталог целиком") == 2 and str(W / "work") in a.ro)
t("путь через ссылку отвергнут с указанием цели",
  "ведёт через ссылку" in rej and str(data) in rej.split("ведёт через ссылку")[1][:200])
t("конфликт режимов: поздняя строка отвергнута, ранняя действует",
  "уже задан строкой" in rej and str(data) in a.ro and str(data) not in a.rw)
t("worktree актов (CHOIR_WT_DIR): отказ и на rw, и на r; rw предку стола — отказ",
  rej.count("каталог стола") == 2 and "предка каталога стола" in rej)
t("rw внутрь проекта — названо записью в главный checkout",
  "запись в главный checkout" in rej)
t("число отказов = число плохих строк (13)", len(a.rejects) == 13)
del os.environ["CHOIR_WT_DIR"]
snap = access.snapshot(a); b = access.from_snapshot(snap)
t("снимок туда-обратно: ro/rw/sha сохраняются", b.ro == a.ro and b.rw == a.rw and b.sha == a.sha)
t("notice для кресла называет и чтение, и запись; без rw — только чтение",
  "на ЗАПИСЬ" not in access.notice(a, chair=True) and str(W / "work") in access.notice(a, chair=True))
c = access.Access(ro=["/x"], rw=["/y"], sha="ab")
t("notice для кресла с rw: слово «на ЗАПИСЬ» и путь", "на ЗАПИСЬ" in access.notice(c, chair=True) and "/y" in access.notice(c, chair=True))

# ── 5. Симлинк вместо файла, пустой файл ──
(proj / access.FILE).unlink()
os.symlink(str(one), str(proj / access.FILE))
a = access.load(proj)
t("ACCESS.txt-ссылка не читается, причина названа",
  a.empty and a.file is None and any("ссылка" in r for r in a.rejects))
(proj / access.FILE).unlink()
(proj / access.FILE).write_text(access.TEMPLATE, encoding="utf-8")
a = access.load(proj)
t("шаблон: файл есть, записей нет, mark говорит «записей нет», fact пуст",
  a.file and a.empty and "записей нет" in access.mark(a) and access.fact(a) == {})

print(f"\n{ok} PASS, {bad} FAIL")
import shutil; shutil.rmtree(W, ignore_errors=True)
sys.exit(1 if bad else 0)
