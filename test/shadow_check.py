#!/usr/bin/env python3
"""Сторож затенения: локальная переменная функции повторяет имя импорта
модуля, а внутри функции зовётся <имя>.<атрибут> — это упадёт только на
живом вызове (2026-09-16: `names` — список голосов — затенил модуль имён
файлов в choir.cmd_run, раунд стол-v3-изоляция упал на затравке; pyflakes
такого не ловит). Код выхода 1 — есть находки; печатает файл:строка."""
import ast
import sys
from pathlib import Path


def check(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    imports: dict[str, str] = {}
    for n in tree.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                imports[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                imports[a.asname or a.name] = n.module
    out: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
        if fn.args.vararg:
            local.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            local.add(fn.args.kwarg.arg)
        globs: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Global):
                globs |= set(n.names)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
            if isinstance(n, (ast.For, ast.comprehension)):
                for t in ast.walk(n.target):
                    if isinstance(t, ast.Name):
                        local.add(t.id)
        shadow = (local & set(imports)) - globs
        if not shadow:
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id in shadow):
                out.append(f"{path}:{n.lineno}: в {fn.name}() локальная "
                           f"«{n.value.id}» затеняет импорт {imports[n.value.id]}, "
                           f"а зовётся .{n.attr}")
    return out


def main(argv: list[str]) -> int:
    found: list[str] = []
    for f in argv:
        found += check(Path(f))
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
