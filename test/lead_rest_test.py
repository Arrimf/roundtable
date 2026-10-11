#!/usr/bin/env python3
"""Отдых от сводов: голос из LEAD_REST не попадает в жребий ведущего, но
запись жребия называет его; CHOIR_LEAD_REST="" снимает; если снять некого —
код 2. Журнал — во временном каталоге. Запуск: python3 test/lead_rest_test.py"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

J = Path(tempfile.mkdtemp(prefix="lead-rest-"))
os.environ.update(ROUNDTABLE_JOURNAL=str(J), CHOIR_JOURNAL=str(J),
                  CHOIR_ROOM=str(J / "room.jsonl"), CHOIR_RT_NO_BWRAP="1")
os.environ.pop("CHOIR_LEAD_REST", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chamber"))
import choir                                    # noqa: E402

N_OK = 0


def check(name, cond):
    global N_OK
    if not cond:
        print("ПРОВАЛ:", name)
        sys.exit(1)
    N_OK += 1


choir.beacon = lambda: {"round": 7, "signature": "cd" * 48}
choir.warn_decision_debts = lambda: None
seed = J / "seed.md"
seed.write_text("вопрос", encoding="utf-8")


def lots():
    out = []
    for l in (J / "room.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(l)
        if e.get("phase") == "pick":
            out.append(e)
    return out


def pick(rnd, voices, exclude=None):
    return choir.cmd_pick(argparse.Namespace(round=rnd, seed=str(seed), voices=voices,
                                             exclude=exclude, anchor=None, project=None))


check("LEAD_REST по умолчанию содержит gemini", "gemini" in choir.lead_rest())
rc = pick("r1", "claude,gemini,grok")
lot = lots()[-1]
check("жребий без gemini: код 0, кандидаты claude и grok", rc == 0 and lot["candidates"] == ["claude", "grok"])
check("запись жребия несёт rest и rest_why", lot["rest"] == ["gemini"] and "отдых" in lot["rest_why"]["gemini"])
check("ведущий — не отдыхающий", lot["text"] in ("claude", "grok"))

rc = pick("r2", "claude,grok")
check("без отдыхающих среди названных — поля rest нет", rc == 0 and "rest" not in lots()[-1])

rc = pick("r3", "gemini")
check("некого выбрать — код 2", rc == 2)

rc = pick("r4", "claude,gemini", exclude="claude")
check("автор якоря исключён, gemini отдыхает — код 2", rc == 2)

os.environ["CHOIR_LEAD_REST"] = ""
rc = pick("r5", "claude,gemini")
check("CHOIR_LEAD_REST=\"\" снимает отдых: gemini среди кандидатов",
      rc == 0 and lots()[-1]["candidates"] == ["claude", "gemini"] and "rest" not in lots()[-1])

os.environ["CHOIR_LEAD_REST"] = "grok,kimi"
rc = pick("r6", "claude,gemini,grok,kimi")
lot = lots()[-1]
check("CHOIR_LEAD_REST задаёт свой состав отдыхающих", rc == 0 and lot["rest"] == ["grok", "kimi"]
      and lot["candidates"] == ["claude", "gemini"])

# запись жребия — источник для запасного жребия свода и summarize --by
os.environ.pop("CHOIR_LEAD_REST", None)
rc = pick("r7", "claude,gemini,grok")
rnd = choir.read_round("r7")
check("lot_rest читает поле rest из записи жребия", choir.lot_rest(rnd) == ["gemini"])
check("lot_rest пуст у раунда без отдыхающих", choir.lot_rest(choir.read_round("r2")) == [])
# summarize --by отдыхающего — код 2 без явной переменной
rc = choir.cmd_summarize(argparse.Namespace(round="r7", by="gemini", project=None, out=None, effort=None))
check("summarize --by gemini в раунде с отдыхом — код 2", rc == 2)
os.environ["CHOIR_LEAD_REST"] = "gemini"
try:
    choir.lead_rest()
    os.environ["CHOIR_LEAD_REST"] = "gemeni"
    try:
        choir.lead_rest(); check("опечатка в CHOIR_LEAD_REST — SystemExit", False)
    except SystemExit as e:
        check("опечатка в CHOIR_LEAD_REST — SystemExit с именем", "gemeni" in str(e))
finally:
    os.environ.pop("CHOIR_LEAD_REST", None)
# --exclude и отдых вместе в проходящем случае
rc = pick("r8", "claude,gemini,grok", exclude="claude")
lot = lots()[-1]
check("--exclude claude + отдых gemini → кандидат один grok, оба поля в записи",
      rc == 0 and lot["candidates"] == ["grok"] and lot["excluded"] == ["claude"] and lot["rest"] == ["gemini"])

print(f"{N_OK} проверок ок")
