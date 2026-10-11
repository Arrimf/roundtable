"""Разбор потока событий Грока (`grok --output-format streaming-json`).

Общий для раундов (choir.py), комнаты (live.py) и кресла секретаря
(secretary_turn.py): модуль без зависимостей, чтобы choir.py не тянул
live.py на импорте.
"""
from __future__ import annotations

import json
import re

_GROK_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


def grok_stream_answer(out: str) -> str:
    """Ответ Грока из `--output-format streaming-json` (NDJSON событий ACP).

    ПОЧЕМУ ПОТОК (2026-10-08, «Грок часто пропускает»): в формате plain
    Грок печатает в stdout только ТЕКСТ АССИСТЕНТА (включая реплики перед
    инструментами — их насос 10-07 и принял за «мысли без терминатора»);
    рассуждения и вызовы инструментов туда не идут. Сторож тишины (300 с)
    снимал его посреди работы: десять снятий `stalled` за 25.09–08.10, а
    журналы сессий самого Грока (~/.grok/sessions/.../events.jsonl) за
    три сегодняшних снятия показывают события каждые доли секунды до
    самого снятия. В потоке событий мысли (`thought`), текст (`text`) и
    инструменты (`tool_call*`) идут построчно, и думающий Грок виден живым.

    Ответ — склейка дельт `text`; куски, разделённые вызовами
    инструментов, — через пустую строку (plain клеил их вплотную:
    «коду.Сверяю»). Мысли в ответ не входят (правило 17: они видны в
    стенограмме). Восстановление из сессии (choir._extract) режет так же."""
    segs: list[str] = []
    cur: list[str] = []
    for line in _GROK_ANSI.sub("", out or "").replace("\r", "\n").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "text" and isinstance(d.get("data"), str):
            cur.append(d["data"])
        elif t in ("tool_call", "tool_call_update"):
            if "".join(cur).strip():
                segs.append("".join(cur).strip())
            cur = []
    if "".join(cur).strip():
        segs.append("".join(cur).strip())
    return "\n\n".join(segs).strip()




def grok_stream_diag(out: str) -> str:
    """Сообщения САМОГО CLI из потока: строки не-JSON (ошибки входа,
    квоты, занятости — под pty stderr слит в stdout) и события `error`.
    Именно это, а не весь stdout, годится для поиска квоты и для причины
    отказа: в stdout теперь лежат мысли и вывод инструментов (прочитанный
    файл со словами «usage limit» давал бы ложную «квоту»; ревизия)."""
    rows: list[str] = []
    for line in _GROK_ANSI.sub("", out or "").replace("\r", "\n").splitlines():
        t = line.strip()
        if not t or t in ("Finishing session…", "Finishing session..."):
            continue
        if t.startswith("{"):
            try:
                d = json.loads(t)
            except ValueError:
                continue                       # обрезок JSON — не сообщение CLI
            if d.get("type") == "error":
                rows.append(str(d.get("message") or d.get("error") or d.get("data") or t)[:500])
            continue
        rows.append(t[:500])
    return "\n".join(rows)[-1500:]


SLIM_TEXT = 1500


def slim_line(line: str) -> str:
    """Строка потока для СТЕНОГРАММЫ: у `tool_call_update` вывод инструмента
    дублируется байтовым массивом (`rawOutput`) — строка в десятки КБ,
    больше порции чтения окна (64 КиБ режут JSON посреди, и вкладка
    показывала сырьё), секреты в байтах не видит чистка выдачи. Массив
    выбрасывается, текст вывода режется до SLIM_TEXT с пометкой; прочие
    строки — как есть. В ответ и разбор это не влияет: они читают stdout."""
    t = line.strip()
    if not t.startswith("{") or '"tool_call' not in t[:60]:   # быстрый отсев, разделители JSON любые
        return line
    try:
        d = json.loads(t)
    except ValueError:
        return line
    if d.get("type") not in ("tool_call", "tool_call_update"):
        return line
    d.pop("rawOutput", None)
    for c in d.get("content") or []:
        inner = c.get("content") if isinstance(c, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str) and len(inner["text"]) > SLIM_TEXT:
            n = len(inner["text"])
            inner["text"] = inner["text"][:SLIM_TEXT] + f"… [обрезано для стенограммы: {n} симв.]"
    if isinstance(d.get("rawInput"), (dict, list)):
        raw = json.dumps(d["rawInput"], ensure_ascii=False)
        if len(raw) > SLIM_TEXT:
            d["rawInput"] = {"обрезано": raw[:SLIM_TEXT] + f"… [{len(raw)} симв.]"}
    end = line[len(line.rstrip("\r\n")):]      # терминатор строки сохраняется как был
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")) + end


def slim_sink(sink):
    """Обёртка приёмника стенограммы (принимает байты построчно)."""
    if sink is None:
        return None

    def _w(data: bytes):
        # приёмник получает блок ЦЕЛЫХ строк (комната — несколько сразу,
        # раунд — по одной): каждая строка худеет отдельно
        try:
            txt = data.decode("utf-8")
        except UnicodeDecodeError:
            return sink(data)
        return sink("".join(slim_line(ln) for ln in txt.splitlines(keepends=True)).encode("utf-8"))
    return _w
