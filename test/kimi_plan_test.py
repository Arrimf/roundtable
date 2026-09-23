#!/usr/bin/env python3
"""Кими на подписке Kimi Code (2026-09-23): линия подписки первой,
ключи резервом по галочке; метка квоты с TTL; алиас подписки в комнате;
шкалы плана и подписка из ответа kimi web (стаб, без процесса);
оценка продления у Claude. Живых голосов и kimi web не зовёт."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="kimiplan."))
os.environ.update(ROUNDTABLE_JOURNAL=str(T / "journal"), CHOIR_RT_NO_BWRAP="1",
                  CHOIR_GATE_DIR=str(T / "gates"), CHOIR_RT_NO_DISCOVERY="1")
os.environ.pop("CHOIR_KIMI_RESERVE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chamber"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import channels                                 # noqa: E402
import serial_gate as sg                        # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond)
    FAIL += not cond


def cfg(text: str) -> Path:
    f = T / f"config-{len(os.listdir(T))}.toml"
    f.write_text(text, encoding="utf-8")
    return f


PLAN = '''default_model = "moonshotai2/kimi-k3"
[providers.moonshotai2]
base_url = "https://api.moonshot.ai/v1"
type = "openai"
api_key = "sk-x"
[providers."managed:kimi-code"]
type = "kimi"
api_key = ""
base_url = "https://api.kimi.ai/coding/v1"
[models."moonshotai2/kimi-k3"]
provider = "moonshotai2"
model = "kimi-k3"
[models."kimi-code/k3"]
provider = "managed:kimi-code"
model = "k3"
[models."kimi-code/k3-256k"]
provider = "managed:kimi-code"
model = "k3-256k"
'''
KEYS_ONLY = '''default_model = "moonshotai/kimi-k3"
[providers.moonshotai]
type = "openai"
api_key = "a"
[providers.moonshotai2]
type = "openai"
api_key = "b"
[models."moonshotai/kimi-k3"]
provider = "moonshotai"
model = "kimi-k3"
[models."moonshotai2/kimi-k3"]
provider = "moonshotai2"
model = "kimi-k3"
'''

# ── каналы ──────────────────────────────────────────────────────────
ch = channels.kimi_channels(cfg(PLAN))
check("подписка + ключ: линия code первая (role=plan, ворота managed_kimi-code), alt резервом",
      [c["name"] for c in ch] == ["code", "alt"] and ch[0]["role"] == "plan"
      and ch[0]["gate"] == "managed_kimi-code" and ch[0]["model"] == "kimi-code/k3"
      and ch[1]["role"] == "api")
os.environ["CHOIR_KIMI_RESERVE"] = "0"
ch0 = channels.kimi_channels(cfg(PLAN))
check("резерв выключен: только линия подписки", [c["name"] for c in ch0] == ["code"])
os.environ["CHOIR_KIMI_RESERVE"] = "1"
ch2 = channels.kimi_channels(cfg(KEYS_ONLY))
check("без подписки: как раньше — main и alt, резерв-галочка ни на что не влияет",
      [c["name"] for c in ch2] == ["main", "alt"] and channels.plan_line(ch2) is None)
os.environ["CHOIR_KIMI_RESERVE"] = "0"
check("без подписки и с выключенным резервом ключевые линии остаются (резерв — понятие при подписке)",
      [c["name"] for c in channels.kimi_channels(cfg(KEYS_ONLY))] == ["main", "alt"])
os.environ["CHOIR_KIMI_RESERVE"] = "1"
check("конфига нет: одна ключевая линия по умолчанию, не «code»",
      [c["name"] for c in channels.kimi_channels(T / "nope.toml")] == ["main"])
check("plan_line находит линию подписки", channels.plan_line(ch)["name"] == "code")
check("reserve_on: 0/false/off — выкл, иначе вкл",
      not channels.reserve_on({"CHOIR_KIMI_RESERVE": "0"}) and not channels.reserve_on({"CHOIR_KIMI_RESERVE": "off"})
      and channels.reserve_on({}) and channels.reserve_on({"CHOIR_KIMI_RESERVE": "1"}))

# ── метка квоты: сутки у ключа, TTL у подписки ─────────────────────
sg.mark_quota("g-api")
check("ключевая линия: метка до конца суток UTC", sg.quota_dead("g-api"))
sg.mark_quota("g-plan", ttl=1)
check("подписка: метка с TTL — жива, пока не истёк", sg.quota_dead("g-plan"))
time.sleep(1.1)
check("TTL истёк — линия снова в игре", not sg.quota_dead("g-plan"))
sg.clear_quota("g-api")
check("clear_quota снимает метку", not sg.quota_dead("g-api"))
check("mark_quota — обычная функция, не contextmanager (до 2026-09-23 метка не ставилась вовсе)",
      not hasattr(sg.mark_quota, "__wrapped__") and (T / "gates" / "g-plan.quota").read_text().startswith("until:"))
check("alive_gates: помеченная линия исключена, все мертвы — все возвращаются",
      (sg.mark_quota("x", ttl=60) or sg.alive_gates(["x", "y"]) == ["y"])
      and (sg.mark_quota("y", ttl=60) or sg.alive_gates(["x", "y"]) == ["x", "y"]))

# ── алиас линии подписки в комнате ─────────────────────────────────
import live                                     # noqa: E402
live.KIMI_CONFIG = cfg(PLAN)
plan = {"name": "code", "model": "kimi-code/k3", "role": "plan", "provider": "managed:kimi-code"}
api = {"name": "alt", "model": "moonshotai2/kimi-k3", "role": "api", "provider": "moonshotai2"}
live.KIMI_MODEL = ""
check("без переопределения — модель линии", live._kimi_line_model(plan) == "kimi-code/k3")
live.KIMI_MODEL = "k3-256k"
check("переопределение окна на подписке: объявленный алиас принимается",
      live._kimi_line_model(plan) == "kimi-code/k3-256k")
live.KIMI_MODEL = "kimi-k3"
check("переопределение окна, которого у подписки нет — своя модель линии (не падать «неизвестной моделью»)",
      live._kimi_line_model(plan) == "kimi-code/k3")
check("ключевая линия: как раньше — провайдер линии + модель окна",
      live._kimi_line_model(api) == "moonshotai2/kimi-k3")
live.KIMI_MODEL = ""

# ── окно: шкалы плана и подписка из ответа kimi web (без процесса) ──
import roundtable as rt                         # noqa: E402
_REAL_SNAPSHOT = rt.kimi_plan_snapshot          # ниже подменяется стабами
USAGE = {"kind": "ok", "quota": {"usages": {
    "limit5h": {"usedRatio": 0.42, "resetAt": "2026-09-23T17:45:50Z"},
    "monthTotal": {"usedRatio": 0.07, "resetAt": "2026-10-24T00:00:00Z"},
    "monthCode": {"usedRatio": 0.05, "resetAt": "2026-10-24T00:00:00Z"}},
    "extraUsage": {"balanceCents": 2500, "totalCents": 2500, "monthlyChargeLimitEnabled": False,
                   "monthlyChargeLimitCents": 0, "monthlyUsedCents": 130, "currency": "CNY"}}}
g, note = rt.kimi_plan_gauges(USAGE, "2026-09-23T13:00:00+00:00")
names = [x["name"] for x in g]
check("шкалы: 5 часов (окно 300 мин, сброс), месяц (всё), месяц (код), доп. расход",
      names == ["5 часов", "месяц (всё)", "месяц (код)", "доп. расход"] and g[0]["known"] == 42.0
      and g[0]["window_minutes"] == 300 and g[0]["resets_at"] == "2026-09-23T17:45:50Z"
      and g[0]["unit"] == "percent" and g[3]["known"] == 25.0 and g[3]["unit"] == "cny"
      and "1.30" in g[3]["note"] and note == "")
g2, note2 = rt.kimi_plan_gauges({"kind": "error", "message": "re-login required"}, "t")
check("ошибка account service — шкал нет, причина словами", g2 == [] and "re-login" in note2)
g3, note3 = rt.kimi_plan_gauges({"error": "kimi web не поднялся"}, "t")
check("kimi web не поднялся — шкал нет, причина словами", g3 == [] and "не поднялся" in note3)
g4, _ = rt.kimi_plan_gauges({"kind": "ok", "quota": {"usages": {"limit5h": {"usedRatio": 0}}, "extraUsage": None}}, "t")
check("extraUsage null — без кошелька; usedRatio 0 — шкала есть", [x["name"] for x in g4] == ["5 часов"])

# подписка: стаб снимка
def snap(auth, usage=None, ui=None):
    return {"auth": auth, "usage": usage, "userinfo": ui, "at": "t"}
rt.kimi_plan_snapshot = lambda force=False: snap(
    {"managed_provider": {"name": "managed:kimi-code", "status": "authenticated"}},
    USAGE, {"kind": "ok", "userInfo": {"userLevelName": "Plus", "userLevel": 20}})
s = rt._sub_kimi()
check("подписка: authenticated → active, план Plus, дата — сброс месячной квоты, оговорка в note",
      s["state"] == "active" and s["plan"] == "Plus" and s["until"] == "2026-10-24T00:00:00Z"
      and "сброс месячной квоты" in s["until_kind"] and "не отдаёт" in s["note"])
rt.kimi_plan_snapshot = lambda force=False: snap({"managed_provider": {"name": "managed:kimi-code", "status": "expired"}})
check("вход expired → expired", rt._sub_kimi()["state"] == "expired")
rt.kimi_plan_snapshot = lambda force=False: snap({"managed_provider": None})
check("провайдера подписки нет → none с подсказкой kimi login", rt._sub_kimi()["state"] == "none"
      and "kimi login" in rt._sub_kimi()["note"])
rt.kimi_plan_snapshot = lambda force=False: snap({"error": "kimi web не поднялся за 20 с"})
check("kimi web не ответил → unknown, не «нет подписки»", rt._sub_kimi()["state"] == "unknown")
rt.kimi_plan_snapshot = lambda force=False: snap(
    {"managed_provider": {"status": "authenticated"}}, USAGE, {"kind": "ok", "userInfo": {"userLevelName": "Plus"}})
lim = rt._lim_kimi({})
check("_lim_kimi: шкалы плана в карточке, план в extra",
      lim.get("plan") == "Plus" and [x["name"] for x in lim.get("limits", [])][:1] == ["5 часов"])
check("_kimi_key пропускает управляемого провайдера и пустой ключ",
      (lambda: (setattr(rt, "KIMI_CFG", cfg(PLAN)) or rt._kimi_key()) == "sk-x")())
rt.KIMI_CFG = cfg('[providers."managed:kimi-code"]\ntype = "kimi"\napi_key = "zzz"\n')
check("только управляемый провайдер — ключа резерва нет", rt._kimi_key() is None)

# ── Claude: оценка продления ────────────────────────────────────────
from datetime import datetime, timezone
now19 = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc).timestamp()
a = rt._monthly_anniversary("2026-04-20T13:09:57Z", now=now19)
check("годовщина: следующее 20-е после 19.09 — 20.09", a.startswith("2026-09-20"))
now21 = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp()
check("после 20.09 — 20.10", rt._monthly_anniversary("2026-04-20T13:09:57Z", now=now21).startswith("2026-10-20"))
check("короткий месяц: 31 января → 28 февраля", rt._monthly_anniversary("2026-01-31T00:00:00Z", now=1770000000).startswith("2026-02-28"))
check("кривая дата — None", rt._monthly_anniversary("не дата") is None and rt._monthly_anniversary(None) is None)

# ── кресло: алиас подписки, если объявлен ──────────────────────────
import edits                                    # noqa: E402
home0 = Path.home
try:
    fake = T / "home"; (fake / ".kimi-code").mkdir(parents=True)
    (fake / ".kimi-code" / "config.toml").write_text(PLAN, encoding="utf-8")
    Path.home = classmethod(lambda cls: fake)
    check("кресло: с подпиской — kimi-code/k3, хотя default_model конфига — ключевой",
          edits._kimi_default_model() == "kimi-code/k3")
    (fake / ".kimi-code" / "config.toml").write_text(KEYS_ONLY, encoding="utf-8")
    check("кресло без подписки: default_model конфига", edits._kimi_default_model() == "moonshotai/kimi-k3")
finally:
    Path.home = home0

# ── ревизия 23.09: resolve_alias, ворота по алиасу, мягкое состояние, каталог ──
warned = []
pc = cfg(PLAN)
check("resolve_alias: объявленный алиас окна на подписке", channels.resolve_alias(plan, "k3-256k", pc) == "kimi-code/k3-256k")
check("resolve_alias: необъявленный на резерве — своя модель, предупреждение один раз",
      channels.resolve_alias(api, "k3", pc, warn=warned.append) == "moonshotai2/kimi-k3"
      and channels.resolve_alias(api, "k3", pc, warn=warned.append) == "moonshotai2/kimi-k3" and len(warned) == 1)
check("resolve_alias: провайдер в значении — берётся хвост", channels.resolve_alias(plan, "x/k3", pc) == "kimi-code/k3")
check("resolve_alias: конфиг не прочитан — подписка своя модель, ключ склейка (как раньше)",
      channels.resolve_alias(plan, "k3", T / "nope.toml") == "kimi-code/k3"
      and channels.resolve_alias(api, "kimi-k9", T / "nope.toml") == "moonshotai2/kimi-k9")
check("gates_for_model: подписка → только её ворота; ключ → ключевые; чужой алиас → все",
      channels.gates_for_model(ch, "kimi-code/k3") == ["managed_kimi-code"]
      and channels.gates_for_model(ch, "moonshotai2/kimi-k3") == ["moonshotai2"]
      and channels.gates_for_model(ch, "zzz/q") == ["managed_kimi-code", "moonshotai2"])
check("_sub_state soft: справочная дата в прошлом/через 3 дня не даёт ending/expired при active",
      rt._sub_state("2020-01-01T00:00:00Z", True, soft=True) == "active"
      and rt._sub_state("2020-01-01T00:00:00Z", False, soft=True) == "expired"
      and rt._sub_state(None, None, soft=True) == "unknown")
from datetime import timedelta
soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
rt.kimi_plan_snapshot = lambda force=False: snap(
    {"managed_provider": {"status": "authenticated"}},
    {"kind": "ok", "quota": {"usages": {"monthTotal": {"usedRatio": 0.5, "resetAt": soon}}, "extraUsage": None}},
    {"kind": "ok", "userInfo": {"userLevelName": "Plus"}})
check("kimi: сброс месячной квоты через 3 дня — по-прежнему active, не «⚠ до»", rt._sub_kimi()["state"] == "active")
rt._get_json = lambda url, h, timeout=5: (200, {"account": {"has_claude_max": True},
                                                  "organization": {"subscription_status": "active",
                                                                   "subscription_created_at": soon}}, "")
rt.CLAUDE_CREDS = T / "creds.json"; rt.CLAUDE_CREDS.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}))
check("claude: оценка через 3 дня — active (оценка не рождает ending)", rt._sub_claude()["state"] == "active")
# kimi_plan_snapshot без входа — kimi web не поднимается
import roundtable as rt2
rt2.kimi_plan_snapshot = _REAL_SNAPSHOT
rt2._kimi_plan_declared = lambda: False
rt2._KIMI_PLAN_CACHE.update(ts=0.0, data=None)
called = []
rt2.kimi_web_query = lambda *a, **k: called.append(1) or {}
sn = rt2.kimi_plan_snapshot(force=True)
check("без входа: снимок без запуска kimi web, подписка none",
      not called and sn["auth"]["managed_provider"] is None and rt2._sub_kimi()["state"] == "none")
# каталог: первый провайдер с ключом — не managed
import catalog
catalog._kimi_cfg = lambda: {"default_model": "moonshotai2/kimi-k3",
                             "providers": {"managed:kimi-code": {"type": "kimi", "api_key": "", "base_url": "https://api.kimi.ai/coding/v1"},
                                           "moonshotai2": {"type": "openai", "api_key": "sk-r", "base_url": "https://api.moonshot.ai/v1"}},
                             "models": {"kimi-code/k3": {}, "moonshotai2/kimi-k3": {}}}
seen_url = []
catalog._get_json = lambda url, *a, **k: seen_url.append(url) or {"data": [{"id": "kimi-k3"}]}
d = catalog._discover_kimi()
check("каталог: ключ резерва moonshotai2, не заглушка подписки; /models — у Moonshot",
      seen_url and "api.moonshot.ai" in seen_url[0] and "k3" in d.get("models", []))
check("mark_quota пишет атомарно — временного файла не остаётся",
      (sg.mark_quota("atom", ttl=5) or not list((T / "gates").glob("atom.quota.*.tmp"))) and sg.quota_dead("atom"))

print(f"\nkimi_plan: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
