#!/usr/bin/env python3
"""Сторож POST /access (`_bwrap_alive`) и карта «сейчас» в карточке раунда
(`_coverage_now`) — юнит, без окна и без голосов. Ревизия 22.09: положительная
ветка сторожа не была покрыта; чужой bwrap (собственная песочница Грока)
блокировал запись доступа с ложной причиной."""
import json, os, subprocess, sys, tempfile
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="accguard."))
RT = Path(__file__).resolve().parent.parent
(T / "journal").mkdir(); (T / "chamber").mkdir(); (T / "acts").mkdir(); (T / "leases").mkdir()
for f in (RT / "chamber").glob("*.py"):
    (T / "chamber" / f.name).write_bytes(f.read_bytes())
(T / "journal" / "live.jsonl").write_text("")
os.environ.update(ROUNDTABLE_CHAMBER=str(T / "chamber"), ROUNDTABLE_JOURNAL=str(T / "journal"),
                  CHOIR_RT_ACTS=str(T / "acts"), CHOIR_LEASE_DIR=str(T / "leases"),
                  CHOIR_WT_DIR=str(T / "wts"), CHOIR_RT_NO_DISCOVERY="1",
                  CHOIR_RT_MODELS=str(T / "models.json"), CHOIR_RT_VOICES=str(T / "voices.json"),
                  CHOIR_CANARY_HOME=str(T / "chome"))
os.environ.pop("CHOIR_RT_NO_BWRAP", None)
sys.path.insert(0, str(RT)); sys.path.insert(0, str(T / "chamber"))
import roundtable as rt                                       # noqa: E402
import jail                                                   # noqa: E402

ok = bad = 0
def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name); ok += bool(cond); bad += not cond

class R:
    def __init__(self, rc, out=""): self.returncode, self.stdout = rc, out

calls = []
def fake_run(argv, **kw):
    calls.append(argv)
    return fake_run.result
rt.subprocess.run = fake_run
OWN_GROK = "3575753 bwrap --cap-drop ALL --bind / / --bind /home/u /home/u --bind /home/u/.grok /home/u/.grok\n"
OURS = "4100 bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /home/u --unshare-pid --unshare-ipc --die-with-parent\n"
GROK_WITH_PROMPT = ("3607463 bwrap --cap-drop ALL --bind / / --bind /home/u /home/u -- grok -p "
                    "РЕВИЗИЯ: в jail.prefix стоит --ro-bind / / и --unshare-pid, проверьте --die-with-parent\n")

fake_run.result = R(1, "")
t("нет bwrap (pgrep rc=1) → False", rt._bwrap_alive() is False)
fake_run.result = R(0, OWN_GROK)
t("чужой bwrap (песочница Грока: --cap-drop ALL --bind / /) → False", rt._bwrap_alive() is False)
fake_run.result = R(0, OWN_GROK + OURS)
t("наша клетка среди чужих (сигнатура --ro-bind / / и --unshare-pid) → True", rt._bwrap_alive() is True)
fake_run.result = R(0, GROK_WITH_PROMPT)
t("чужой bwrap с НАШЕЙ сигнатурой в промпте после « -- » → False (второй круг ревизии)", rt._bwrap_alive() is False)
fake_run.result = R(0, OURS + " -- claude -p текст про --ro-bind / /\n")
t("наша клетка с промптом после « -- » → True", rt._bwrap_alive() is True)
t("pgrep зовётся по имени процесса с аргументами, не -f", calls[-1][:1] == ["pgrep"] and "-a" in calls[-1] and "-x" in calls[-1] and "-f" not in calls[-1])
def raise_run(argv, **kw): raise OSError("pgrep нет")
rt.subprocess.run = raise_run
t("pgrep недоступен → False (сторож честно молчит)", rt._bwrap_alive() is False)
rt.subprocess.run = fake_run; fake_run.result = R(0, OURS)
os.environ["CHOIR_RT_NO_BWRAP"] = "1"; n0 = len(calls)
t("CHOIR_RT_NO_BWRAP=1 (jail.disabled) → False без вызова pgrep", rt._bwrap_alive() is False and len(calls) == n0)
del os.environ["CHOIR_RT_NO_BWRAP"]
pref = jail.prefix("claude", rw=[str(T)], cwd=str(T))
line = " ".join(pref)
t("настоящий jail.prefix распознаётся как наша клетка (с pid и командой после --)",
  rt._is_our_jail("4242 " + " ".join([*pref, "--", "claude", "-p", "x"])))
# карта «сейчас»
lot = [{"id": "l1", "ts": "2026-09-22T10:00:00+00:00", "round": "d0", "phase": "pick", "voice": "choir", "role": "lot", "text": "kimi"}]
t("только жребий — coverage_now = None, не обрывок", rt._coverage_now(lot, "d0") is None)
recs = lot + [{"id": "s1", "ts": "2026-09-22T10:00:01+00:00", "round": "d0", "phase": "blind", "voice": "arr", "role": "seed", "text": "q", "called": ["claude", "grok"]},
              {"id": "a1", "ts": "2026-09-22T10:00:02+00:00", "round": "d0", "phase": "blind", "voice": "claude", "role": "answer", "status": "ok", "text": "x"}]
h = rt._coverage_now(recs, "d0")
t("затравка и один ответ — строка со счётом, без «Свод писал»", h.startswith("> Карта покрытия (по журналу): слепая фаза — звали 2, ответили 1, упали: grok (missing)") and "Свод писал" not in h)
print(f"\n{ok} PASS, {bad} FAIL")
import shutil; shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if bad else 0)
