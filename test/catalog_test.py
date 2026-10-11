#!/usr/bin/env python3
"""Каталог моделей: парсеры чистые, refresh не роняет окно, кэш валидируется.

Без сети и без CLI: DISCOVER подменяется, источники — литералы в
форме, в которой их отдают живые каналы (сняты 2026-09-02)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RT))
TMP = tempfile.mkdtemp(prefix="cat.")
os.environ["CHOIR_RT_MODELS"] = str(Path(TMP) / "rt-models.json")
import catalog                                       # noqa: E402

N = 0
FAILED = 0


def check(cond, what):
    global N, FAILED
    N += 1
    if cond:
        print(f"  ok   {what}")
    else:
        FAILED += 1
        print(f"  FAIL {what}")


# ── парсеры ──────────────────────────────────────────────────────────
codex_doc = {"fetched_at": "2026-09-02T12:52:00Z", "models": [
    {"slug": "gpt-5.6-sol", "priority": 1, "visibility": "list",
     "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": e} for e in
                                    ("low", "medium", "high", "xhigh",
                                     "max", "ultra")]},
    {"slug": "gpt-reserve", "priority": 3, "visibility": "hide",
     "supported_reasoning_levels": [{"effort": "low"}]},
    {"slug": "gpt-5.4", "priority": 16, "visibility": "list",
     "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": e} for e in
                                    ("xhigh", "low", "high", "medium")]},
    "мусор", {"slug": None},
]}
c = catalog.parse_codex_cache(codex_doc)
check(c["models"] == ["gpt-5.6-sol", "gpt-5.4"],
      "codex: видимые модели по priority, hide отброшен")
check(c["efforts_by_model"]["gpt-5.4"] == ["low", "medium", "high", "xhigh"],
      "codex: лестница модели упорядочена по силе")
check(c["default_effort_by_model"] == {"gpt-5.6-sol": "low", "gpt-5.4": "medium"},
      "codex: умолчание усилия по модели")
try:
    catalog.parse_codex_cache({"models": []})
    check(False, "codex: пустой кэш → Unavailable")
except catalog.Unavailable:
    check(True, "codex: пустой кэш → Unavailable")

grok_doc = {"models": {
    "grok-4.6": {"info": {"reasoning_effort": "high", "hidden": False,
                          "reasoning_efforts": [{"value": v} for v in
                                                ("xhigh", "high", "medium",
                                                 "low")]}},
    "grok-4.5": {"info": {"reasoning_effort": "high",
                          "reasoning_efforts": [{"value": v} for v in
                                                ("high", "medium", "low")]}},
    "grok-secret": {"info": {"hidden": True}},
}}
g = catalog.parse_grok_cache(grok_doc)
check(g["models"] == ["grok-4.6", "grok-4.5"],
      "grok: порядок кэша сохранён, hidden отброшен")
check(g["efforts_by_model"]["grok-4.5"] == ["low", "medium", "high"]
      and "xhigh" in g["efforts_by_model"]["grok-4.6"],
      "grok: xhigh только у grok-4.6")
check(g["default_effort_by_model"]["grok-4.6"] == "high",
      "grok: умолчание усилия из reasoning_effort")

kimi_cfg = {"default_model": "moonshotai/kimi-k3",
            "models": {"moonshotai/kimi-k2.5": {}, "moonshotai/kimi-k3": {},
                       "moonshotai2/kimi-k3": {}}}
k = catalog.parse_kimi_config(kimi_cfg)
check(k["models"] == ["kimi-k2.5", "kimi-k3"],
      "kimi: алиасы без провайдера, вторая линия не дублирует модель")
check(k["default_model"] == "kimi-k3", "kimi: default_model без провайдера")
check(catalog.parse_kimi_api({"data": [{"id": "kimi-k3"}, {"id": "kimi-k2.6"},
                                       {"nope": 1}]})
      == ["kimi-k2.6", "kimi-k3"], "kimi: /v1/models разобран и отсортирован")

check(catalog.parse_anthropic_api({"data": [{"id": "claude-fable-5-1"},
                                            {"id": "bad name"}]})
      == ["claude-fable-5-1"], "anthropic: имя вне класса символов отброшено")

ds_out = "deepseek-v4-flash\ndeepseek-v4-pro\n\n"
check(catalog.parse_lines(ds_out, "deepseek") == ["deepseek-v4-flash",
                                                   "deepseek-v4-pro"],
      "deepseek: --models построчно")
gem_out = ("ключ #0: 52 моделей\n   gemini-3.7-flash\n   gemini-3-pro-image\n"
           "   gemini-3.5-transcribe\n   gemini-3.8-flash\n   gemini-3.1-flash-live-preview\n")
check(catalog.parse_gemini_lines(gem_out) == ["gemini-3.7-flash", "gemini-3.8-flash"],
      "gemini: шапка, image/transcribe/live отброшены")

claude_help = ("  --effort <level>                      Effort level for the current session\n"
               "                                        (low, medium, high, xhigh, max)\n")
check(catalog.parse_effort_help(claude_help, "--effort")
      == ["low", "medium", "high", "xhigh", "max"], "claude --help: ступени")
gem_help = "  \x1b[1;36m--thinking\x1b[0m \x1b[1;33m{low,medium,high}\x1b[0m\n"
check(catalog.parse_effort_help(gem_help, "--thinking") == ["low", "medium", "high"],
      "gemini-http --help: ступени сквозь ANSI")
check(catalog.parse_effort_help("nothing here", "--effort") == [],
      "нет флага → пустой список, не исключение")

# ── refresh: отказ источника не стирает список и не роняет окно ─────
calls = {"n": 0}


def ok_claude():
    return {"models": ["fable", "claude-fable-5-1"], "efforts": ["low", "max"],
            "source": "тест"}


def boom():
    raise catalog.Unavailable("нет ключа")


def crash():
    raise RuntimeError("сюрприз парсера")


catalog.DISCOVER = {"claude": ok_claude, "codex": boom, "grok": crash}
rep = catalog.refresh(["claude", "codex", "grok"], timeout=5)
check(rep["claude"]["ok"] and rep["claude"]["models"] == 2, "refresh: удачный источник записан")
check(not rep["codex"]["ok"] and "нет ключа" in rep["codex"]["error"],
      "refresh: Unavailable → строка отчёта")
check(not rep["grok"]["ok"] and "RuntimeError" in rep["grok"]["error"],
      "refresh: чужое исключение → строка отчёта, не 500")
check(catalog.models("codex") == catalog.CURATED["codex"],
      "refresh: без разведки — запасной список")
check("запасной" in catalog.source("codex"), "источник запаса подписан")
check(catalog.efforts("claude") == ["low", "max"], "efforts из entry")
check(catalog.models("claude")[0] == "fable", "порядок моделей сохранён")
# второй отказ после удачи: прежний список остаётся, ошибка рядом
catalog.DISCOVER = {"claude": boom}
rep2 = catalog.refresh(["claude"], timeout=5)
check(rep2["claude"]["kept"] and catalog.models("claude") == ["fable", "claude-fable-5-1"],
      "refresh: отказ после удачи НЕ стирает прежний список")
check("не удалась" in catalog.source("claude"), "…и ошибка видна в источнике")

# ── кэш на диске: валидируется, кривое → запас ──────────────────────
saved = json.loads(Path(os.environ["CHOIR_RT_MODELS"]).read_text())
check(saved["claude"]["models"] == ["fable", "claude-fable-5-1"], "кэш записан")
Path(os.environ["CHOIR_RT_MODELS"]).write_text(json.dumps(
    {"claude": {"models": ["ok-name", "bad name"], "efforts": []},
     "grok": {"models": ["grok-4.6"], "efforts": ["low"]}}))
catalog.CATALOG.clear()
catalog.load()
check(catalog.models("claude") == catalog.CURATED["claude"],
      "кэш с кривым именем → запас (не Popen с мусором)")
check(catalog.models("grok") == ["grok-4.6"], "валидная запись кэша принята")
Path(os.environ["CHOIR_RT_MODELS"]).write_text("{не json")
catalog.CATALOG.clear()
catalog.load()
check(catalog.models("deepseek") == catalog.CURATED["deepseek"],
      "битый json кэша → запас, без падения")

# ── efforts по модели и умолчания ────────────────────────────────────
catalog.CATALOG["grok"] = catalog._entry_from("grok", dict(g, source="т"))
check(catalog.efforts("grok", "grok-4.5") == ["low", "medium", "high"]
      and catalog.efforts("grok", "grok-4.6")[-1] == "xhigh"
      and catalog.efforts("grok", "grok-9") == ["low", "medium", "high", "xhigh"],
      "efforts(model): своя лестница, неизвестная модель → общая")
check(catalog.default_model("grok") == "grok-4.6"
      and catalog.default_effort("grok", "grok-4.6") == "high",
      "grok: умолчания из кэша (первая модель, её reasoning_effort)")
catalog.CATALOG["kimi"] = catalog._entry_from("kimi", dict(k, efforts=[], source="т"))
check(catalog.default_model("kimi") == "kimi-k3" and catalog.efforts("kimi") == [],
      "kimi: default_model конфига, усилий нет")
check(catalog.default_model("claude") is None,
      "claude: умолчание объявляет не каталог, а вызывающий код")

# ── находки ревизии 2026-09-02 ──────────────────────────────────────
check(catalog.parse_effort_help("  --effort <x>  (see docs)\n", "--effort") == [],
      "parse_effort_help: скобка с мусором не подменяет лестницу")
check(catalog.parse_lines("deepseek models:\ndeepseek-v4-pro\n", "deepseek-")
      == ["deepseek-v4-pro"], "parse_lines: заголовок с префиксом без дефиса отброшен")
c2 = catalog.parse_codex_cache({"models": [
    {"slug": "gpt-a", "priority": None, "visibility": "list",
     "supported_reasoning_levels": [{"effort": "low"}]},
    {"slug": "gpt-b", "priority": 1, "visibility": "list",
     "supported_reasoning_levels": [{"effort": "low"}]}]})
check(c2["models"] == ["gpt-b", "gpt-a"], "parse_codex_cache: priority null → в конец, не TypeError")
check(catalog._sort_efforts(["high", "hi\"gh", "x=1\nfoo", "xhigh"]) == ["high", "xhigh"],
      "ступени вне класса символов (кавычка, перевод строки) отброшены")
check("‹вырезано›" in catalog.scrub("Authorization: Bearer sk-abcdefghijklmnop")
      and "sk-abcdef" not in catalog.scrub("x sk-abcdefghijklmnop y"),
      "scrub: токен и ключ вырезаны")
e = catalog._normalize_entry("codex", {"models": ["gpt-5.5"], "efforts": ["low"],
                                       "efforts_by_model": "мусор"})
check(e is None, "кэш: строка вместо словаря лестниц → запись негодна")
e = catalog._normalize_entry("codex", {"models": ["gpt-5.5"], "efforts": ["low", 'hi"gh'],
                                       "efforts_by_model": {"gpt-5.5": ["low", "x y"]},
                                       "default_effort_by_model": {"gpt-5.5": "low"},
                                       "fetched_at": "2026-09-02T00:00:00+00:00"})
check(e is not None and e["efforts"] == ["low"] and e["efforts_by_model"] == {"gpt-5.5": ["low"]}
      and e["fetched_at"] == "2026-09-02T00:00:00+00:00",
      "кэш: кривые ступени отфильтрованы, остальное сохранено")
# kimi: без алиаса — не в селектор; без алиасов вовсе — отказ
catalog._kimi_cfg = lambda: {"default_model": "moonshotai/kimi-k3",
                             "models": {"moonshotai/kimi-k3": {}},
                             "providers": {"moonshotai": {"api_key": "k", "base_url": "https://x/v1"}}}
catalog._get_json = lambda url, headers, timeout=1: {"data": [{"id": "kimi-k3"}, {"id": "kimi-k9-new"}]}
k2 = catalog._discover_kimi()
check(k2["models"] == ["kimi-k3"] and k2["unaliased"] == ["kimi-k9-new"],
      "kimi: серверное имя без алиаса НЕ в селекторе, а в unaliased")
catalog._kimi_cfg = lambda: {"providers": {}, "models": {}}
try:
    catalog._discover_kimi()
    check(False, "kimi: без алиасов → Unavailable, а не пустой список")
except catalog.Unavailable:
    check(True, "kimi: без алиасов → Unavailable, а не пустой список")
# отказ на запасной записи меняет причину
catalog.CATALOG["gemini"] = catalog._fallback("gemini", "старая причина")
catalog.DISCOVER = {"gemini": boom}
catalog.refresh(["gemini"], timeout=5)
check("нет ключа" in catalog.source("gemini") and "старая причина" not in catalog.source("gemini"),
      "refresh: отказ на запасной записи заменяет причину")
# пустой успешный ответ не стирает разведанный список
catalog.CATALOG["deepseek"] = catalog._entry_from("deepseek", {"models": ["deepseek-v4-pro"],
                                                               "efforts": ["max"], "source": "т"})
catalog.DISCOVER = {"deepseek": lambda: {"models": [], "efforts": [], "source": "пусто"}}
r3 = catalog.refresh(["deepseek"], timeout=5)
check(not r3["deepseek"]["ok"] and catalog.models("deepseek") == ["deepseek-v4-pro"],
      "refresh: пустой ответ источника — отказ, список сохранён")
# дедлайн ожидания не держит вызов
import time as _t
catalog.DISCOVER = {"grok": lambda: (_t.sleep(3), {"models": ["grok-4.6"], "efforts": []})[1]}
t0 = _t.monotonic()
r4 = catalog.refresh(["grok"], timeout=0.5)
check(_t.monotonic() - t0 < 2.5 and not r4["grok"]["ok"] and "таймаут" in r4["grok"]["error"],
      "refresh: дедлайн ожидания отпускает вызов, зависший источник — «таймаут»")
_t.sleep(3)   # дать нити дозреть, чтобы не портить следующие проверки

print(f"\n{N - FAILED}/{N} PASS" + (f", {FAILED} FAIL" if FAILED else ""))
sys.exit(1 if FAILED else 0)
