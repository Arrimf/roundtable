#!/usr/bin/env python3
"""Память нити из ленты для голоса без сессии (deepseek-http): устав и
цель каждый ход, вся нить с origin, свои реплики как «Вы», потолок с
названной обрезкой, выключатель по окружению. Живых голосов не зовёт."""
import json
import os
import sys
import tempfile
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="memory."))
os.environ.update(ROUNDTABLE_JOURNAL=str(T / "journal"), CHOIR_RT_NO_BWRAP="1",
                  CHOIR_MEMORY_CAP="4000")
os.environ.pop("CHOIR_DEEPSEEK_MEMORY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chamber"))
import live                                     # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond)
    FAIL += not cond


(T / "journal").mkdir(parents=True, exist_ok=True)
live.LIVE.parent.mkdir(parents=True, exist_ok=True)
check("deepseek — голос без сессии; память включена по умолчанию",
      live.VOICES["deepseek"].get("no_session") and live.memory_on("deepseek")
      and not live.memory_on("claude"))
os.environ["CHOIR_DEEPSEEK_MEMORY"] = "0"
check("CHOIR_DEEPSEEK_MEMORY=0 выключает", not live.memory_on("deepseek"))
os.environ["CHOIR_DEEPSEEK_MEMORY"] = "1"

# лента: разговор в три реплики, среди них своя
live.post("arr", "say", "вопрос номер один")
live.post("deepseek", "say", "мой прошлый ответ")
live.post("claude", "say", "ответ клода")
evs = live.read_events(since=0)
st = {"cursor": evs[-1]["id"], "turns": 2, "origin": 0}
p_mem = live.build_prompt("deepseek", st, evs, False, memory=True)
p_delta = live.build_prompt("deepseek", st, [e for e in evs if e["author"] != "deepseek"], False)
check("память: устав каждый ход (нить его не помнит)", live.CHARTER[:40] in p_mem and live.CHARTER[:40] not in p_delta)
check("память: история нити с заголовком, свои реплики — «Вы»",
      "ИСТОРИЯ ЭТОЙ НИТИ" in p_mem and "— Вы: мой прошлый ответ" in p_mem and "Голос claude: ответ клода" in p_mem)
check("дельта: своих реплик нет, заголовок прежний", "Вы: мой прошлый" not in p_delta and "Что сказали с вашего прошлого хода" in p_delta)

# потолок: длинные реплики режутся с начала, обрезка названа
for i in range(6):
    live.post("claude", "say", f"реплика {i} " + "x" * 900)
evs = live.read_events(since=0)
p_cap = live.build_prompt("deepseek", st, evs, False, memory=True)
check("потолок: старейшие реплики срезаны, обрезка названа числом",
      "история обрезана по потолку 4000" in p_cap and "вопрос номер один" not in p_cap
      and "реплика 5" in p_cap)

# deliver с фальшивым turn: промпт памяти уходит голосу, событие несёт memory=feed, origin в состоянии
seen = {}
def fake_turn(name, prompt):
    seen[name] = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
    return {"author": name, "kind": "say", "text": "ок", "elapsed_s": 0.1}
live.turn = fake_turn
live.available = lambda n: True
live.deliver(["deepseek", "claude"])
check("deliver: deepseek получил историю нити, claude — дельту",
      "ИСТОРИЯ ЭТОЙ НИТИ" in seen["deepseek"] and "ИСТОРИЯ ЭТОЙ НИТИ" not in seen["claude"])
last = [json.loads(l) for l in live.LIVE.open(encoding="utf-8")][-2:]
by = {e["author"]: e for e in last}
check("событие deepseek несёт memory=feed, у claude поля нет",
      by["deepseek"].get("memory") == "feed" and "memory" not in by["claude"])
stp = live._state_path("deepseek")
st_d = json.loads(stp.read_text(encoding="utf-8"))
check("origin записан в состояние нити и не сдвигается курсором",
      "origin" in st_d and st_d["origin"] <= st_d["cursor"])
live.post("arr", "say", "второй вопрос")
live.deliver(["deepseek"])
check("второй ход: обе реплики Автора видны (старейшая — если не срезана потолком, и тогда обрезка названа)",
      "второй вопрос" in seen["deepseek"] and (
          "вопрос номер один" in seen["deepseek"] or "история обрезана по потолку" in seen["deepseek"]))
check("второй ход: свой прошлый ответ помечен «Вы»", "— Вы: ок" in seen["deepseek"])
# новая нить при длинной ленте: origin — хвост COLD_TAIL, не 0, и это названо в промпте
live._state_path("deepseek").unlink()
for i in range(live.COLD_TAIL + 5):
    live.post("claude", "say", f"шум {i}")
live.deliver(["deepseek"])
st0 = live.load_state("deepseek")
check("origin новой нити при длинной ленте — хвост, не 0; сохранён в состоянии",
      int(st0.get("origin") or 0) > 0 and st0["origin"] <= st0["cursor"])
check("промпт называет, с какого события ведётся нить, и старого шума в нём нет",
      "нить ведётся с события №" in seen["deepseek"] and "шум 0" not in seen["deepseek"] and "шум 44" in seen["deepseek"])
# всё срезано потолком — обрезка всё равно названа
live.MEMORY_CAP = 100
p_all = live.build_prompt("deepseek", st0, live.read_events(since=0), False, memory=True, origin=3)
check("потолок срезал всё — заголовок и обрезка всё равно названы", "история обрезана по потолку 100" in p_all and "ИСТОРИЯ ЭТОЙ НИТИ" in p_all)
live.MEMORY_CAP = 4000
# смена цели: действующая — только в шапке, прошлая — в истории
live.post("roundtable", "goal", "цель А", goal="цель А"); live.post("roundtable", "goal", "цель Б", goal="цель Б")
p_goal = live.build_prompt("deepseek", st0, live.read_events(since=0), False, memory=True)
check("цель: действующая «цель Б» в шапке, в истории — только прошлая «цель А»",
      p_goal.count("цель Б") == 1 and "сменил цель: цель А" in p_goal)
live.post("roundtable", "goal", "", goal="")
p_off = live.build_prompt("deepseek", st0, live.read_events(since=0), False, memory=True)
check("снятая цель: шапки ЦЕЛЬ нет, в истории «(цель снята)» есть — прошлая не выглядит действующей",
      "ЦЕЛЬ (целеполагатель" not in p_off and "(цель снята)" in p_off)
check("_origin_of: записанный ноль — «с начала», не пересчёт хвоста",
      live._origin_of({"origin": 0, "cursor": 99}) == 0 and live._origin_of({"cursor": 7}) == 7)

print(f"\nmemory: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
