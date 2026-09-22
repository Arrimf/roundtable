#!/usr/bin/env python3
"""Как отдать голосу длинный промпт: не аргументом argv.

Один аргумент execve не длиннее MAX_ARG_STRLEN (32 страницы = 131072
байт): свод раунда prodolzhit-lyuboe-v1 (2026-09-22) ушёл голосу claude
аргументом и упал «Argument list too long: bwrap». Общий модуль для
дирижёра (choir.py) и комнаты (live.py) — иначе две копии расходятся
(kimi).

По голосам: claude -p и codex exec без аргумента читают промпт из stdin;
kimi stdin не читает («argument missing») — ему короткая
строка «задание в файле …» с путём к своему файлу промпта (в раунде —
каталог вызова в карантине, ro в клетке; в комнате — свой каталог
голоса, rw). grok и HTTP-адаптеры промпт и так берут файлом. Порог —
ниже потолка одного аргумента с запасом на UTF-8 в argv. Что голос
РЕАЛЬНО получил (stdin-текст или обёртку) — sha считает вызывающий по
`seen_text`, не по исходному промпту (grok: sha выдавал себя за «что
голос видел»)."""
from __future__ import annotations

import os

ARG_MAX_PROMPT = 120_000        # байт UTF-8; потолок одного аргумента 131071

STDIN_VOICES = {"claude", "codex"}


def file_note(pfile) -> str:
    return (f"Задание целиком — в файле {pfile}: прочитайте его и выполните, "
            f"что там написано. Отвечайте по нему.")


def deliver(argv: list[str], prompt: str, pfile) -> tuple[list[str], str | None, str, str]:
    """(argv, stdin_text, via, seen_text). via: '' — промпт короткий или
    его нет в argv (файлом), 'stdin', 'file'. seen_text — что голос
    реально получил."""
    if not prompt or prompt not in argv or len(prompt.encode("utf-8")) <= ARG_MAX_PROMPT:
        return argv, None, "", prompt
    i = argv.index(prompt)
    name = os.path.basename(argv[0]) if argv else ""
    if name in STDIN_VOICES:
        # codex exec без [PROMPT] читает инструкции из stdin (его --help);
        # «-» ушёл бы промптом-строкой
        return argv[:i] + argv[i + 1:], prompt, "stdin", prompt
    note = file_note(pfile)
    return argv[:i] + [note] + argv[i + 1:], None, "file", note


def feed_stdin(proc, text: str, binary: bool) -> None:
    """Писать stdin отдельной нитью: промпт больше буфера трубы, а CLI
    может начать отвечать раньше, чем дочитает — иначе взаимная
    блокировка. EOF — всегда (finally), иначе CLI ждал бы до сторожа
    тишины (kimi)."""
    import threading

    def _run():
        try:
            proc.stdin.write(text.encode("utf-8") if binary else text)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
    threading.Thread(target=_run, daemon=True).start()
