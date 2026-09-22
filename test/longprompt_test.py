#!/usr/bin/env python3
"""Длинный промпт — не аргументом: claude через stdin, kimi — «прочитай
файл»; короткий — как был. Раунд и комната. Живых голосов не зовёт."""
import os, sys, tempfile, json
from pathlib import Path
T = Path(tempfile.mkdtemp(prefix="lp."))
os.environ.update(ROUNDTABLE_JOURNAL=str(T / "journal"), CHOIR_RT_NO_BWRAP="1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chamber"))
import choir, live, promptio                        # noqa: E402
PASS = FAIL = 0
def check(n, c):
    global PASS, FAIL
    print(("PASS  " if c else "FAIL  ") + n); PASS += bool(c); FAIL += not c
big = "и" * 70000                                   # 140 КБ UTF-8 — за MAX_ARG_STRLEN
a, st, via, seen = promptio.deliver(["claude", "-p", "--model", "m", big], big, T / "p")
check("claude — промпт снят из argv, ушёл в stdin, факт stdin, seen = сам промпт", via == "stdin" and big not in a and st == big and seen == big)
a, st, via, seen = promptio.deliver(["/home/u/.local/bin/claude", "-p", big], big, T / "p")
check("claude по полному пути — тоже stdin (basename)", via == "stdin")
a, st, via, seen = promptio.deliver(["codex", "exec", "-s", "read-only", "-o", "/x", big], big, T / "p")
check("codex — stdin, без аргумента-промпта и без «-»", via == "stdin" and a == ["codex", "exec", "-s", "read-only", "-o", "/x"] and st == big)
a, st, via, seen = promptio.deliver(["/k/kimi", "-m", "x", "-p", big], big, T / "p")
check("kimi — короткая строка «задание в файле <карантин>», stdin нет, seen = обёртка", via == "file" and st is None and str(T / "p") in a[-1] and big not in a and seen == a[-1])
a, st, via, seen = promptio.deliver(["codex", "exec", "-o", "/x", "short"], "short", T / "p")
check("короткий — без изменений", via == "" and a[-1] == "short" and seen == "short")
a, st, via, seen = promptio.deliver(["gemini-http", "--prompt-file", "/x"], big, T / "p")
check("промпта нет в argv (файлом) — без изменений", via == "" and st is None)
mid = "x" * 100000
a, st, via, seen = promptio.deliver(["claude", "-p", mid], mid, T / "p")
check("100 КБ — ещё аргументом (порог 120 КБ, не 60)", via == "")
# run_watched со stdin: кот читает stdin целиком (больше буфера трубы) и отдаёт длину
r = choir.run_watched(["bash", "-c", "wc -c"], cwd=str(T), hard_limit=30, idle_limit=30, voice="claude", stdin_text=big)
check("run_watched: 140 КБ в stdin доставлены целиком, без взаимной блокировки", r["status"] == "ok" and r["stdout"].strip() == str(len(big.encode())))
# комната
# EOF гарантирован и при ошибке записи: заглушка stdin, бросающая на write
class _Bad:
    def __init__(self): self.closed = False
    def write(self, *_): raise ValueError("encode")
    def close(self): self.closed = True
class _P: stdin = _Bad()
promptio.feed_stdin(_P, "x", binary=False)
import time; time.sleep(0.2)
check("feed_stdin: при ошибке записи stdin всё равно закрыт (EOF голосу)", _P.stdin.closed)
cp = live._run_capture(["bash", "-c", "wc -c"], str(T), 30, None, voice="claude", stdin_text=big)
check("live._run_capture (без sink): stdin доставлен", cp.stdout.strip() == str(len(big.encode())))
cp = live._run_capture(["bash", "-c", "wc -c"], str(T), 30, lambda b: None, voice="claude", stdin_text=big)
check("live._run_capture (с sink): stdin доставлен", cp.stdout.strip() == str(len(big.encode())))
print(f"\nlongprompt: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
