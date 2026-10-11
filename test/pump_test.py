#!/usr/bin/env python3
"""Сторож тишины считает ПОРЦИИ байтов, а не законченные строки (2026-10-07).
Повод: Grok 1.0.46 печатает ход мыслей без терминатора вообще, построчный
насос ждал его молча, и живой голос снимался на 300-й секунде со статусом
stalled (8 записей с текстом без единого \\n). Терминаторы при этом
нормализуются в \\n, как раньше делала текстовая обёртка (universal
newlines), иначе через pty в ответ попадали бы \\r\\n. Без моделей:
run_watched на bash-командах с короткими пределами."""
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(prefix="pump."))
os.environ.update(CHOIR_JOURNAL=str(T), CHOIR_ROOM=str(T / "room.jsonl"), CHOIR_RT_NO_BWRAP="1",
                  CHOIR_DROP_DIR=str(T / "drop"))
sys.path.insert(0, str(RT / "chamber"))
import choir   # noqa: E402

ok = bad = 0


def check(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


# байты без терминатора — признак жизни: 4 порции по 1 с при пределе тишины 3 с
r = choir.run_watched(["bash", "-c", "for i in 1 2 3 4; do printf 'думаю%s' $i; sleep 1; done; echo; echo ответ"],
                      cwd=str(T), hard_limit=30, idle_limit=3, voice="grok")
check("печать без перевода строки держит голос живым (не stalled)", r["status"] == "ok" and r["silence_s"] < 3)
check("текст без \\n не потерян и собран в строку", "думаю1думаю2думаю3думаю4\n" in r["stdout"] and "ответ" in r["stdout"])

# терминаторы нормализуются как universal newlines: \r, \r\n → \n; stdout как прежде
r = choir.run_watched(["bash", "-c", "printf 'a\\rb\\rc\\r\\nd\\n'"], cwd=str(T), hard_limit=30, idle_limit=5)
check("\\r и \\r\\n → \\n в stdout (как раньше через обёртку)", r["status"] == "ok" and r["stdout"] == "a\nb\nc\nd\n")
r = choir.run_watched(["script", "-qec", "printf 'a\\nb\\n'", "/dev/null"], cwd=str(T), hard_limit=30, idle_limit=5)
check("через pty (script): в stdout нет \\r", r["status"] == "ok" and "\r" not in r["stdout"] and r["stdout"].replace("\r", "") .endswith("a\nb\n"))

# \r\n, разрезанный границей порции, — одна строка, без пустой
got = []
r = choir.run_watched(["bash", "-c", "printf 'x\\r'; sleep 0.5; printf '\\ny\\n'"], cwd=str(T), hard_limit=30, idle_limit=5,
                      sink=lambda b: got.append(b))
lines = b"".join(got).split(b"\n")
check("\\r на краю порции удерживается: x и y — две строки, пустой нет",
      r["status"] == "ok" and r["stdout"] == "x\ny\n" and b"" not in [l for l in lines[:-1]] and lines[:2] == [b"x", b"y"])

# настоящая тишина по-прежнему снимается
r = choir.run_watched(["bash", "-c", "printf 'старт'; sleep 6; echo поздно"], cwd=str(T), hard_limit=30, idle_limit=2, voice="grok")
check("тишина дольше предела — stalled, с тем, что успел напечатать", r["status"] == "stalled" and "старт" in r["stdout"] and r["silence_s"] >= 2)

# stderr тоже считается, многобайтовые символы на границе порций целы, хвост декодера на EOF — замена
r = choir.run_watched(["bash", "-c", "python3 -c \"import sys,time\nfor i in range(3):\n    sys.stderr.write('ё'*3000); sys.stderr.flush(); time.sleep(0.7)\nprint('готово')\""],
                      cwd=str(T), hard_limit=30, idle_limit=3)
check("stderr порциями — признак жизни; UTF-8 на границе порций цел",
      r["status"] == "ok" and r["stderr"].count("ё") == 9000 and "�" not in r["stderr"] and "готово" in r["stdout"])
r = choir.run_watched(["bash", "-c", "printf 'ab\\xd1'"], cwd=str(T), hard_limit=30, idle_limit=5)
check("оборванный UTF-8 на EOF — символ замены, не потеря", r["stdout"] == "ab�")

# хвост без терминатора доходит до стенограммы строкой
got = []
r = choir.run_watched(["bash", "-c", "printf 'хвост без перевода'"], cwd=str(T), hard_limit=30, idle_limit=5,
                      sink=lambda b: got.append(b))
check("хвост без \\n — в стенограмму строкой", r["status"] == "ok" and any("хвост без перевода".encode() in b for b in got)
      and r["stdout"] == "хвост без перевода")

# тень ранних отказов видит строку, оборванную \r (прогресс в pty)
r = choir.run_watched(["bash", "-c", "printf 'HTTP 503 service unavailable\\r' >&2; sleep 0.3; printf 'дальше\\n' >&2; echo fine"],
                      cwd=str(T), hard_limit=30, idle_limit=5, voice="gemini", phase="rebut1")
check("stderr-строка, оборванная \\r, доходит до тени ранних отказов (со следующей порцией)", r["status"] == "ok" and bool(r.get("early_warns")))

print(f"\npump: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
