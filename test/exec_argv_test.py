#!/usr/bin/env python3
"""argv кресел: явные умолчания и рычаги (наказ Автора 2026-09-02).

Проверяет ТОЛЬКО сборку командной строки — без запуска CLI: то, что
окно показывает как «умолчание», обязано совпадать с тем, что уходит
исполнителю, иначе поле врёт (правило 8.5)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RT))
TMP = tempfile.mkdtemp(prefix="argv.")
os.environ["CHOIR_DSH_PATCH_DIR"] = str(Path(TMP) / "dshp")
import edits                                          # noqa: E402

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


def argv(voice):
    return edits.EDIT_VOICES[voice]("задача", "/base/.git")


def flag(av, f):
    return av[av.index(f) + 1] if f in av else None


# ── умолчания без настройки ──────────────────────────────────────────
edits.EXEC_OVERRIDES = {}
a = argv("claude")
check(flag(a, "--model") == edits.EXEC_DEFAULTS["claude"]["model"]
      and flag(a, "--effort") == edits.EXEC_DEFAULTS["claude"]["effort"],
      "claude: --model/--effort уходят ВСЕГДА, равны EXEC_DEFAULTS")
check(a[-2:] == ["--", "задача"], "claude: задание за `--`")
a = argv("codex")
check("-m" not in a and not any(t.startswith("model") for t in a),
      "codex: без настройки флагов модели/усилия нет — хранит config.toml")
a = argv("grok")
check("--model" not in a[2] and "--effort" not in a[2],
      "grok: без настройки — серверный дефолт, флагов нет")
a = argv("kimi")
check(flag(a, "-m") == edits.exec_default("kimi", "model")
      and "/" in flag(a, "-m"),
      "kimi: -m всегда, имя с провайдером (из конфига при каждом вызове)")
a = argv("deepseek")
patch = Path(flag(a, "--patch"))
check(patch.exists() and 'model: "deepseek-v4-flash"' in patch.read_text(),
      "deepseek: патч dsh уходит всегда, умолчание flash в нём")
check("provider: deepseek-official" in patch.read_text(), "deepseek: провайдер в патче")

# ── рычаги совпадают с EXEC_LEVERS ───────────────────────────────────
edits.EXEC_OVERRIDES = {
    "claude": {"model": "fable", "effort": "max"},
    "codex": {"model": "gpt-5.5", "effort": "xhigh"},
    "grok": {"model": "grok-4.5", "effort": "high"},
    "kimi": {"model": "kimi-k2.6"},
    "deepseek": {"model": "deepseek-v4-pro"},
}
a = argv("claude")
check(flag(a, "--model") == "fable" and flag(a, "--effort") == "max",
      "claude: оба рычага доезжают")
a = argv("codex")
check("model=gpt-5.5" in a and "model_reasoning_effort=xhigh" in a,
      "codex: -c model / -c model_reasoning_effort без кавычек")
a = argv("grok")
check("--model grok-4.5" in a[2] and "--effort high" in a[2],
      "grok: --model (новый рычаг) и --effort в строке script")
a = argv("kimi")
check(flag(a, "-m") == "moonshotai/kimi-k2.6",
      "kimi: провайдер из default_model конфига, не литерал")
a = argv("deepseek")
check('model: "deepseek-v4-pro"' in Path(flag(a, "--patch")).read_text(),
      "deepseek: любая модель — свой патч-файл")
# Каждый рычаг из EXEC_LEVERS — отдельно и с ОТРИЦАТЕЛЬНЫМ случаем:
# прежняя проверка «модель где-то в argv» была тавтологией (нашёл
# субагент-ревьюер). Значение усилия ищется по ТЕКСТУ argv/патча.
WANT = {"claude": {"model": "fable", "effort": "max"},
        "codex": {"model": "gpt-5.5", "effort": "xhigh"},
        "grok": {"model": "grok-4.5", "effort": "high"},
        "kimi": {"model": "kimi-k2.6", "effort": "high"},
        "deepseek": {"model": "deepseek-v4-pro", "effort": "high"}}
for v, levers in edits.EXEC_LEVERS.items():
    edits.EXEC_OVERRIDES = {v: dict(WANT[v])}       # усилие задано ВСЕМ
    a = argv(v)
    text = " ".join(a)
    if v == "deepseek":
        text += " " + Path(flag(a, "--patch")).read_text()
    has_model = WANT[v]["model"] in text
    has_effort = ("effort=" + WANT[v]["effort"] in text
                  or "--effort " + WANT[v]["effort"] in text
                  or "--effort" in a and flag(a, "--effort") == WANT[v]["effort"])
    check(has_model == ("model" in levers), f"{v}: рычаг модели ⇔ модель в argv")
    check(has_effort == ("effort" in levers),
          f"{v}: рычаг усилия ⇔ усилие в argv (у kimi/dsh его быть НЕ должно)")
edits.EXEC_OVERRIDES = {}
# Разрешённые окном умолчания (grok из кэша CLI) уходят флагами.
edits.EXEC_RESOLVED = {"grok": {"model": "grok-4.6", "effort": "high"}}
a = argv("grok")
check("--model grok-4.6" in a[2] and "--effort high" in a[2],
      "grok: EXEC_RESOLVED → флаги; панель и argv совпадают")
edits.EXEC_OVERRIDES = {"grok": {"model": "grok-4.5"}}
a = argv("grok")
check("--model grok-4.5" in a[2] and "--effort high" in a[2],
      "grok: настройка окна сильнее разрешённого умолчания")
edits.EXEC_RESOLVED = {}
edits.EXEC_OVERRIDES = {}

# ── защита имён ──────────────────────────────────────────────────────
try:
    edits.dsh_patch("bad name; rm -rf /")
    check(False, "dsh_patch: имя вне класса символов отвергнуто")
except ValueError:
    check(True, "dsh_patch: имя вне класса символов отвергнуто")
edits.EXEC_OVERRIDES = {"grok": {"model": "x'; echo pwned"}}
a = argv("grok")
check("pwned" in a[2] and "'\"'\"'" in a[2], "grok: кавычка в имени экранирована shlex")
edits.EXEC_OVERRIDES = {}
p1 = edits.dsh_patch("deepseek-v4-pro")
m1 = p1.stat().st_mtime_ns
p2 = edits.dsh_patch("deepseek-v4-pro")
check(p1 == p2 and p2.stat().st_mtime_ns == m1, "dsh_patch: тот же файл не перезаписывается")
check(edits.dsh_patch("a/b") != edits.dsh_patch("a_b"),
      "dsh_patch: a/b и a_b — РАЗНЫЕ файлы (хеш в имени)")
check(edits.MODEL_RE is __import__("catalog").MODEL_RE,
      "MODEL_RE — один объект на кресло и каталог")

print(f"\n{N - FAILED}/{N} PASS" + (f", {FAILED} FAIL" if FAILED else ""))
sys.exit(1 if FAILED else 0)
