#!/usr/bin/env python3
"""Круги по ключам в адаптере gemini-http (~/.local/bin/gemini-http, вне
репозитория; publish.sh копирует его в public/adapters/). Сеть подменена:
post() отвечает по сценарию, часы и sleep — счётчик. Без адаптера на
машине — пропуск с кодом 0. Запуск: python3 test/gemini_http_passes_test.py"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
from pathlib import Path

ADAPTER = Path.home() / ".local" / "bin" / "gemini-http"
N_OK = 0


def check(cond, what):
    global N_OK
    if not cond:
        print("ПРОВАЛ:", what)
        sys.exit(1)
    N_OK += 1


def load():
    os.environ["GEMINI_PASS_BUDGET_S"] = "240"
    loader = importlib.machinery.SourceFileLoader("gh_under_test", str(ADAPTER))
    spec = importlib.util.spec_from_loader("gh_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.load_keys = lambda: ["k1", "k2", "k3"]
    mod.start_index = lambda n: 0
    mod.remember_index = lambda i: None
    mod.write_history = lambda *a, **k: None
    mod.STREAM = False
    mod.logs = []
    mod.log = lambda m: mod.logs.append(m)
    mod.clock = [0.0]
    mod.time.monotonic = lambda: mod.clock[0]
    mod.time.sleep = lambda s: mod.clock.__setitem__(0, mod.clock[0] + s)
    return mod


def scripted(mod, script):
    calls = []

    def post(url, payload, timeout):
        calls.append(url.split("key=")[-1])
        mod.clock[0] += 1.0
        code = script[min(len(calls) - 1, len(script) - 1)]
        if code == 200:
            return 200, {"candidates": [{"content": {"parts": [{"text": "принято"}]},
                                         "finishReason": "STOP"}],
                         "usageMetadata": {"totalTokenCount": 5}}
        if code == 429:
            return 429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}}
        return code, {"error": {"status": "UNAVAILABLE", "message": "high demand"}}
    mod.post = post
    return calls


def run(script, timeout=600, budget=None, more_models=False):
    mod = load()
    if budget is not None:
        mod.PASS_BUDGET_S = budget
    calls = scripted(mod, script)
    with contextlib.redirect_stdout(io.StringIO()):      # ответ адаптер печатает сам
        rc = mod.ask("вопрос", "gemini-3.6-flash", timeout, None, None,
                     thinking="low", more_models=more_models)
    rounds = [l for l in mod.logs if l.startswith("круг")]
    return rc, len(calls), rounds, mod.clock[0], mod


def main() -> int:
    if not ADAPTER.exists():
        print("адаптера gemini-http нет — пропуск")
        return 0
    rc, n, rounds, t, mod = run([503])
    check(rc == (1, "gateway"), "весь шлюз 503 → код 1, причина gateway")
    check(len(rounds) >= 2 and n == 9 + 3 * len(rounds), "при 503 круги; после первого — по одной попытке на ключ")
    check(t <= mod.PASS_BUDGET_S + 9 * 4 + 90, "круги укладываются в бюджет плюс последний проход")

    rc, n, rounds, t, _ = run([429])
    check(rc == (3, "quota") and rounds == [] and n == 3, "весь 429 → код 3 quota сразу, без кругов")

    rc, n, rounds, t, _ = run([503], budget=0)
    check(rc == (1, "gateway") and rounds == [] and n == 9, "бюджет 0 → один проход, 3 ключа × 3 повтора")

    rc, n, rounds, t, _ = run([503] * 9 + [200])
    check(rc[0] == 0 and len(rounds) == 1 and n == 10, "второй круг отвечает → успех после одной паузы")

    rc, n, rounds, t, _ = run([503], timeout=60)
    check(t <= 65 and rounds == [], "дедлайн 60 с сильнее бюджета кругов")

    rc, n, rounds, t, _ = run([503] * 9 + [429])
    check(rc == (3, "quota") and len(rounds) == 1 and n == 12,
          "503 в первом круге, 429 во втором → квота по последнему кругу, третьего круга нет")

    rc, n, rounds, t, _ = run([503], more_models=True)
    check(rc == (1, "gateway") and n == 4 and rounds == [],
          "с запасной моделью уходим на первом 5xx второго ключа (3 + 1 запроса), кругов нет")

    rc, n, rounds, t, mod = run([503])
    check(any("кругов" in l for l in mod.logs if l.startswith("ВСЕ")), "итоговая строка называет число кругов")

    print(f"{N_OK} проверок ок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
