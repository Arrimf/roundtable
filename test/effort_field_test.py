#!/usr/bin/env python3
"""Нулевой шаг раунда jev-v1 (решение Автора 2026-09-23): запись ответа
несёт поле `effort` — усилие, фактически переданное голосу, — и обрезка
чужих ответов в пакете витка выключена по умолчанию (решение Автора:
«давай уберём лимиты»), а по CHOIR_REBUT_QUOTE_LIMIT возвращается и
называется в тексте. Без платных вызовов: проверяются функции."""
import importlib
import os
import sys
import tempfile
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="rt-effort."))
os.environ.update(CHOIR_JOURNAL=str(T / "journal"), CHOIR_ROOM=str(T / "journal" / "room.jsonl"),
                  ROUNDTABLE_SANDBOX=str(T / "sandbox"))
for k in list(os.environ):
    if k.startswith("CHOIR_ROUND_") or k in ("CHOIR_REBUT_QUOTE_LIMIT", "CHOIR_CLAUDE_EFFORT",
                                             "CHOIR_GROK_EFFORT", "CHOIR_CODEX_EFFORT", "CHOIR_GEMINI_EFFORT"):
        os.environ.pop(k)
(T / "journal").mkdir(parents=True)
RT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RT / "chamber"))
import choir   # noqa: E402
import live    # noqa: E402

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"PASS  {name}")
    else:
        FAIL += 1; print(f"FAIL  {name}")


# ── раунды: effort_passed совпадает с тем, что уходит в argv (cmd_of) ──
check(choir.effort_passed("claude") == choir.CLAUDE_EFFORT == "max", "раунды: claude — CLAUDE_EFFORT (max, наказ 2026-08-24)")
check(choir.effort_passed("codex") == choir.eff("codex") == "high", "раунды: codex без переменной — eff(): ступень ниже потолка")
check(choir.effort_passed("grok") == choir.eff("grok") == "medium", "раунды: grok без переменной — eff()")
check(choir.effort_passed("kimi") is None and choir.effort_passed("kimi-alt") is None, "раунды: kimi — рычага нет → None (не пустая строка)")
check(choir.effort_passed("deepseek") == "max" and choir.effort_passed("gemini") == "high", "раунды: deepseek/gemini — умолчания их флагов")
os.environ["CHOIR_ROUND_CODEX_EFFORT"] = "low"
check(choir.effort_passed("codex") == "low", "раунды: CHOIR_ROUND_CODEX_EFFORT перекрывает eff() — как в argv")
os.environ.pop("CHOIR_ROUND_CODEX_EFFORT")
choir.EFFORT_OVERRIDE = "max"
check(choir.effort_passed("codex") == "xhigh" and choir.effort_passed("grok") == "high",
      "раунды: --effort max садится на потолок каждого CLI — и в записи то же")
choir.EFFORT_OVERRIDE = None

# ── ask_one без вызова CLI: у missing поля effort НЕТ (голосу ничего не передали) ──
orig_av, orig_ver = choir.available, choir.cli_version
choir.available = lambda name: False
choir.cli_version = lambda name: "test"
try:
    rec = choir.ask_one("grok", "вопрос", "t-effort", "blind", None, "blind", use_role=False)
    check("effort" not in rec and rec["status"] == "missing", "ask_one: missing — без поля effort (правило 8.5: не передавали — не пишем)")
    # oversize: пакет больше потолка канала — не отправлен, записан, квота не тронута
    choir.available = lambda name: True
    big = "x" * (choir.PACKET_CAP["gemini"] + 1)
    rec = choir.ask_one("gemini", big, "t-effort", "rebut1", None, "after_round", use_role=False)
    check(rec["status"] == "oversize" and rec["packet_cap"] == choir.PACKET_CAP["gemini"]
          and rec["packet_chars"] > rec["packet_cap"] and "effort" not in rec and "не отправлен" in rec["detail"],
          "ask_one: пакет больше потолка gemini → oversize с размером и потолком, без вызова")
    small = choir.ask_one("gemini", "коротко", "t-effort", "rebut1", None, "after_round", use_role=False)
    check(small["status"] != "oversize", "ask_one: короткий пакет gemini потолком не задет")
finally:
    choir.available, choir.cli_version = orig_av, orig_ver
check(choir.PACKET_CAP.get("codex", 0) == 0, "потолок пакета — только у каналов с известным пределом (gemini)")

# ── комната ──
check(live.effort_passed("claude") == live.CLAUDE_EFFORT and live.effort_passed("grok") == live.GROK_EFFORT
      and live.effort_passed("gemini") == live.GEMINI_EFFORT, "комната: claude/grok/gemini — константы argv")
check(live.effort_passed("codex") is None and live.effort_passed("kimi") is None, "комната: codex без флага, kimi — None")
os.environ.pop("DEEPSEEK_EFFORT", None)
check(live.effort_passed("deepseek") == "max", "комната: deepseek — умолчание адаптера max")
os.environ["DEEPSEEK_EFFORT"] = "low"
check(live.effort_passed("deepseek") == "low", "комната: deepseek — DEEPSEEK_EFFORT из среды, как читает адаптер")
os.environ.pop("DEEPSEEK_EFFORT")

# ── обрезка чужих ответов в витке: по умолчанию нет; по переменной — есть и названа ──
check(choir.REBUT_QUOTE_LIMIT == 0, "REBUT_QUOTE_LIMIT без переменной = 0 (без обрезки)")
check(choir._env_int("CHOIR_X_NOPE", 7) == 7, "_env_int: нет переменной → умолчание")
os.environ["CHOIR_X_BAD"] = "6000a"
check(choir._env_int("CHOIR_X_BAD", 0) == 0, "_env_int: кривое значение → умолчание с предупреждением, не трасса")
os.environ["CHOIR_X_BAD"] = "-5"
check(choir._env_int("CHOIR_X_BAD", 3) == 3, "_env_int: отрицательное → умолчание")
os.environ.pop("CHOIR_X_BAD")
os.environ["CHOIR_REBUT_QUOTE_LIMIT"] = "100"
importlib.reload(choir)
check(choir.REBUT_QUOTE_LIMIT == 100, "CHOIR_REBUT_QUOTE_LIMIT возвращает обрезку")
src = Path(choir.__file__).read_text(encoding="utf-8")
check("if REBUT_QUOTE_LIMIT <= 0 or len(t) <= REBUT_QUOTE_LIMIT:" in src and "[обрезано на {REBUT_QUOTE_LIMIT} симв.]" in src,
      "_quote: при 0 — целиком, при лимите — обрезка названа в тексте пакета")
os.environ.pop("CHOIR_REBUT_QUOTE_LIMIT")

print(f"\neffort_field: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
