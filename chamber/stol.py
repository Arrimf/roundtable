#!/usr/bin/env python3
"""stol — один вход во все форматы стола.

Зачем. В папке уже четыре инструмента (`choir`, `live`, `dossier`,
`draw_ui`) и две независимые оси: РЕЖИМ взаимодействия (раунд, лента,
конкурс, персональные задания) и ПОДАЧА фактов (голос читает диск сам /
получает досье / отвечает вслепую). Они перемножаются, и каждый раз
приходится вспоминать, что чем звать. Диспетчер держит карту.

Чего он НЕ делает: не сводит ответы, не выносит вердиктов и не заводит
собственного журнала. Это тонкая обёртка — вся работа и вся запись
остаются в тех же choir.py / live.py, что и раньше. Профили лежат в
`tables.json` как данные: новый режим добавляется правкой json, без
правки кода.

    python stol.py карта                      — какие режимы есть и когда что
    python stol.py совет --раунд x --тема q.md
    python stol.py лента "текст реплики" --раунд x
    python stol.py совет --раунд x --тема q.md --сухой   — показать и не звать
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES = HERE / "tables.json"
TOOLS = {"choir": HERE / "choir.py", "live": HERE / "live.py",
         "dossier": HERE / "dossier.py"}   # draw_ui.py — личный, не публикуется


def load() -> dict:
    d = json.loads(PROFILES.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def карта() -> int:
    """Таблица режимов. Главная колонка — «когда», а не «что»: имя
    режима ничего не подсказывает, а повод для выбора подсказывает."""
    prof = load()
    print()
    for name, p in prof.items():
        mark = "  (служебный)" if p.get("служебный") else ""
        print(f"\033[1m{name}\033[0m{mark} — {p['описание']}")
        print(f"    когда:  {p['когда']}")
        print(f"    цена:   {p['цена']}")
        print()
    print("подача фактов — ось отдельная от режима:")
    print("  голос читает диск сам   codex, grok, kimi, claude")
    print("  голосу нужен пакет      gemini (прямой вызов API, файлов не видит)")
    print("  → режим «досье» готовит пакет с якорями и канарейкой\n")
    return 0


def подставить(шаг: list[str], зам: dict[str, str]) -> list[str]:
    """Подстановка + выброс необязательных флагов с пустым значением.

    Какие флаги инструмент вообще принимает — знает профиль, а не код:
    `rebut` не имеет `--voices`, и попытка дописать его всем шагам
    подряд роняет второй шаг уже после того, как первый отработал и
    заплатил за вызовы (поймано на раунде ветки-v1).
    """
    out: list[str] = []
    for x in шаг:
        for k, v in зам.items():
            x = x.replace("{" + k + "}", v)
        if x == "" and out and out[-1].startswith("--"):
            out.pop()                       # флаг без значения — не флаг
            continue
        out.append(x)
    return out


def запуск(a: argparse.Namespace) -> int:
    prof = load()
    if a.режим not in prof:
        print(f"нет такого режима: {a.режим}\n", file=sys.stderr)
        return карта() or 2
    p = prof[a.режим]

    зам = {"раунд": a.раунд or "", "тема": a.тема or "",
           "текст": a.текст or "", "проект": a.проект or "",
           "выход": a.выход or "", "голоса": a.голоса or ""}
    НЕОБЯЗАТЕЛЬНЫЕ = {"голоса"}          # пусто → флаг просто исчезает
    нужно = {k for шаг in p["шаги"] for x in шаг
             for k in зам if "{" + k + "}" in x} - НЕОБЯЗАТЕЛЬНЫЕ
    пусто = sorted(k for k in нужно if not зам[k])
    if пусто:
        print(f"режиму «{a.режим}» не хватает: {', '.join('--' + k for k in пусто)}",
              file=sys.stderr)
        return 2

    for i, шаг in enumerate(p["шаги"], 1):
        cmd = [sys.executable, str(TOOLS[шаг[0]])] + подставить(шаг[1:], зам)
        print(f"\n\033[1m[{a.режим} {i}/{len(p['шаги'])}]\033[0m "
              + " ".join(cmd[1:]))
        if a.сухой:
            continue
        r = subprocess.run(cmd, cwd=str(HERE))
        if r.returncode != 0:
            print(f"\nшаг {i} вернул {r.returncode} — дальше не идём "
                  f"(следующий шаг опирается на записанное предыдущим)",
                  file=sys.stderr)
            return r.returncode
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="один вход во все форматы стола",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="python stol.py карта — что бывает и когда что применять")
    ap.add_argument("режим", help="имя профиля из tables.json, либо «карта»")
    ap.add_argument("текст", nargs="?", help="для ленточных режимов")
    ap.add_argument("--раунд")
    ap.add_argument("--тема", help="файл затравки")
    ap.add_argument("--проект")
    ap.add_argument("--выход")
    ap.add_argument("--голоса", help="через запятую")
    ap.add_argument("--сухой", action="store_true",
                    help="показать команды и не звать никого")
    a = ap.parse_args()
    if a.режим in ("карта", "list", "--карта"):
        return карта()
    return запуск(a)


if __name__ == "__main__":
    sys.exit(main())
