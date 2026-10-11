"""Стенограмма акта — общий писатель для комнаты (live.py) и раундов (choir.py).

Автор хочет видеть ход работы голоса, как в терминале Claude Code: вопрос,
кто отвечает и какой командой, мысли модели и вызовы инструментов по мере
прихода, ответ, итог (правило 17; наказ 2026-09-06, доработка 2026-09-14:
«дирижёр отдаёт только сухую статистику»). Одна стенограмма на акт окна —
<RT_ACT_DIR>/<RT_ACT_ID>.raw.log; её хвост отдаёт GET /act_log?kind=raw,
и вкладки окна читают её вместо печати процесса, как только она есть.

Кто и когда пишет — решает вызывающий: в слепой фазе (правило 8.5) поток
голоса копится в памяти и ложится сюда разом по закрытии; здесь только
путь, запись и строка."""
from __future__ import annotations

import os
import re
import shlex
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()
_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")


def path() -> Path | None:
    """Файл стенограммы или None (запуск не из окна). Имя акта — из
    окружения: принимается только имя, не путь."""
    d, a = os.environ.get("RT_ACT_DIR"), os.environ.get("RT_ACT_ID")
    if not d or not a or not _ID_RE.fullmatch(a):
        return None
    return Path(d) / f"{a}.raw.log"


def write(data: bytes) -> None:
    p = path()
    if p is None or not data:
        return
    with _LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("ab") as f:
                f.write(data)
        except OSError as e:          # диск кончился — ответ важнее файла
            print(f"стенограмма не пишется: {e}", file=sys.stderr)


def line(msg: str) -> None:
    """Строка дирижёра (в лог акта она печатается отдельно)."""
    write((msg.rstrip("\n") + "\n").encode("utf-8", "replace"))


def head(name: str, cmd: list[str], ptext: str, *, channel: str = "",
         cont: bool | None = None) -> str:
    """Шапка хода — как строка приглашения в терминале: кто, какой
    командой (текст промпта заменён меткой — он лежит в файле голоса),
    новая нить или продолжение, включены ли мысли."""
    shown = [f"<промпт, {len(ptext)} симв.>" if (ptext and x == ptext) else x
             for x in cmd]
    tail = (f" · линия {channel}" if channel else "")
    if cont is not None:
        tail += " · продолжение нити" if cont else " · новая нить"
    tail += " · мысли " + ("вкл" if os.environ.get("CHOIR_THOUGHTS") == "1" else "выкл")
    return f"\n→ {name}{tail}\n$ {shlex.join(shown)}\n"
