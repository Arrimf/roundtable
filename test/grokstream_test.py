#!/usr/bin/env python3
"""Ответ Грока из потока событий (chamber/grokstream.py) и флаг потока в
командах раундов, комнаты и кресла секретаря. Повод — «Грок часто
пропускает» (2026-10-08): в формате plain мысли и инструменты не шли в
stdout, и сторож тишины снимал думающего Грока."""
import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
T = Path(tempfile.mkdtemp(prefix="grokstream."))
os.environ.update(CHOIR_JOURNAL=str(T), ROUNDTABLE_JOURNAL=str(T), CHOIR_ROOM=str(T / "room.jsonl"), CHOIR_RT_NO_BWRAP="1")
sys.path.insert(0, str(RT / "chamber"))
import grokstream   # noqa: E402

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond); FAIL += not cond


def ev(**d):
    return json.dumps(d, ensure_ascii=False)


stream = "\r\n".join([
    "\x1b[?25l" + ev(type="available_commands", tools=["read_file"]),
    ev(type="thought", data="Посчитаю "), ev(type="thought", data="файлы"),
    ev(type="text", data="Считаю"), ev(type="text", data=" файлы."),
    ev(type="tool_call", toolCallId="c1", toolName="run_terminal_command", rawInput={"command": "ls"}),
    ev(type="tool_call_update", toolCallId="c1", status="completed"),
    ev(type="thought", data="три файла"),
    ev(type="text", data="ЧИСЛО ФАЙЛОВ: "), ev(type="text", data="3"),
    ev(type="usage", usage={"input_tokens": 1}),
    ev(type="end", stopReason="end_turn", sessionId="s1"),
    "\x1b]0;grok\x07Finishing session…"])
a = grokstream.grok_stream_answer(stream)
check(a == "Считаю файлы.\n\nЧИСЛО ФАЙЛОВ: 3", f"текст — склейка дельт, куски между инструментами через пустую строку, мыслей нет: {a!r}")
check(grokstream.grok_stream_answer("") == "" and grokstream.grok_stream_answer("plain text без json") == "", "пусто и не-JSON — пустой ответ (статус empty, не выдумка)")
check(grokstream.grok_stream_answer(ev(type="text", data="a\nb")) == "a\nb", "перевод строки внутри дельты сохраняется")

# сообщения CLI для квоты и причины: не мысли и не вывод инструментов
import early   # noqa: E402
tool_out = "\n".join([
    ev(type="thought", data="думаю про 429 rate limit"),
    ev(type="tool_call", toolCallId="c1", toolName="read_file", rawInput={"pattern": "usage limit"}),
    ev(type="tool_call_update", toolCallId="c1", content=[{"type": "content", "content": {"type": "text", "text": "HTTP 429: usage limit reached " * 100}}],
       rawOutput={"output": [72, 84, 84, 80] * 3000}),
    ev(type="end", stopReason="end_turn")])
check(early.quota_in(tool_out) is not None and early.quota_in(grokstream.grok_stream_diag(tool_out)) is None,
      "квота: в сыром stdout «429 … limit» из мыслей и вывода инструмента ловится (ложно), в сообщениях CLI — нет")
err_out = "\n".join([ev(type="error", message="429 Too Many Requests: rate limit exceeded"), "Error: not logged in", "Finishing session…"])
dg = grokstream.grok_stream_diag(err_out)
check("429 Too Many Requests" in dg and "not logged in" in dg and "Finishing" not in dg and early.quota_in(dg) is not None,
      "diag: событие error и не-JSON строки CLI — причина отказа и квота видны")
big = tool_out.splitlines()[2]
sl = grokstream.slim_line(big + "\n")
check(len(big) > 20000 and len(sl) < 3000 and "rawOutput" not in sl and "обрезано для стенограммы" in sl and sl.endswith("\n"),
      f"slim_line: вывод инструмента без байтового массива и с обрезкой ({len(big)} → {len(sl)} байт), терминатор сохранён")
check(grokstream.slim_line(ev(type="text", data="x") + "\n") == ev(type="text", data="x") + "\n" and grokstream.slim_line("❯ не json\n") == "❯ не json\n",
      "slim_line: прочие строки — как есть")
got = []
sink = grokstream.slim_sink(got.append)
sink((tool_out + "\n").encode("utf-8"))
check(len(got) == 1 and b"rawOutput" not in got[0] and got[0].count(b"\n") == 4, "slim_sink: блок из нескольких строк худеет построчно")

import choir   # noqa: E402
import live    # noqa: E402
cmd_r = choir.VOICES["grok"]["cmd"]("p", T / "p.txt", None)
cmd_l = live._grok_cmd(T / "p.txt", False, None)
check("--output-format streaming-json" in cmd_r[2] and choir.VOICES["grok"].get("extract") is not None, "раунд: grok идёт потоком событий, ответ — extract")
check("--output-format streaming-json" in cmd_l[2] and live.VOICES["grok"].get("extract") is not None, "комната: grok идёт потоком событий, ответ — extract")
check(choir.VOICES["grok"]["extract"](stream) == a and live.VOICES["grok"]["extract"](stream) == a, "раунд и комната разбирают одинаково")
import secretary_turn   # noqa: E402
cmd_s, _, _ = secretary_turn._argv({"agent": "grok", "bin": "grok", "model": "", "effort": "", "no_session": False}, first=True,
                                   sid="00000000-0000-4000-8000-000000000000", prompt="x", project=str(T),
                                   pfile=T / "p.txt", afile=T / "a.txt", agent_dir=T)
check("--output-format streaming-json" in cmd_s[2] and secretary_turn._extract({"agent": "grok"}, stream, T / "a.txt") == a,
      "кресло секретаря: grok потоком, ответ тем же разбором")

# канарейка: у grok реплика перед инструментом — отдельный абзац, вердикт после неё
check(choir.canary_verdict("ok", "Проверю журналы.\n\nЧИСТО", voice="grok") == "clean"
      and choir.canary_verdict("ok", "Проверю журналы.\n\nЧИСТО", voice="codex") == "unclear"
      and choir.canary_verdict("ok", "Проверю.\n\nЧИСТО, но нашёл", voice="grok") == "unclear"
      and choir.canary_verdict("ok", "Проверю.\n\nНАЙДЕНО: /x", voice="grok") == "found",
      "канарейка: grok — вердикт в первом абзаце после реплики перед инструментом; строгость строки та же; у других — как было")
check(choir.VOICES["grok"].get("diag") is not None and live.VOICES["grok"].get("diag") is not None and choir.VOICES["grok"].get("slim") and live.VOICES["grok"].get("slim"),
      "раунд и комната: у grok есть diag (квота/причина) и тонкая стенограмма")

import shutil
shutil.rmtree(T, ignore_errors=True)
print(f"\ngrokstream: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
