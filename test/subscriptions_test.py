#!/usr/bin/env python3
"""Подписки CLI в панели лимитов: разбор JWT Codex, состояние по дате,
Grok по /rest/subscriptions (стаб), Claude по профилю (стаб), «нет
подписки» у голосов на балансе. Сети и платных вызовов нет."""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="subs."))
os.environ.update(CHOIR_RT_NO_DISCOVERY="1", HOME=str(T / "home"),
                  ROUNDTABLE_JOURNAL=str(T / "journal"))
(T / "home").mkdir(); (T / "journal").mkdir()
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "chamber"))
sys.argv = ["x"]
spec = importlib.util.spec_from_file_location("rt", HERE / "roundtable.py")
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond)
    FAIL += not cond


def jwt(payload: dict) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b({'alg': 'none'})}.{b(payload)}.sig"


now = time.time()
iso = lambda t: rt._iso(t)
check("_sub_state: прошедшая дата — expired, <7 сут — ending, дальше — active",
      rt._sub_state(iso(now - 10), None) == "expired"
      and rt._sub_state(iso(now + 3 * 86400), None) == "ending"
      and rt._sub_state(iso(now + 30 * 86400), None) == "active")
check("_sub_state: без даты — по флагу активности, без него — unknown",
      rt._sub_state(None, True) == "active" and rt._sub_state(None, False) == "expired"
      and rt._sub_state(None, None) == "unknown" and rt._sub_state("кривая дата", True) == "active")
check("_sub_state: флаг «неактивна» сильнее даты в будущем; «Z» и naive-дата разбираются как UTC",
      rt._sub_state(iso(now + 30 * 86400), False) == "expired"
      and rt._sub_state(iso(now + 30 * 86400).replace("+00:00", "Z"), None) == "active"
      and rt._sub_state(datetime.fromtimestamp(now + 30 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), None) == "active")

# codex: JWT в auth.json
rt.CODEX_AUTH = T / "codex_auth.json"
rt.CODEX_AUTH.write_text(json.dumps({"tokens": {"id_token": jwt({
    "https://api.openai.com/auth": {"chatgpt_plan_type": "free",
                                    "chatgpt_subscription_active_until": iso(now - 7 * 86400),
                                    "chatgpt_subscription_last_checked": iso(now - 86400)}})}}))
c = rt._sub_codex()
check("codex: истёкшая подписка из JWT — expired, план free, дата и заметка",
      c["state"] == "expired" and c["plan"] == "free" and c["until"] and "free" in c["note"])
rt.CODEX_AUTH.write_text(json.dumps({"tokens": {"id_token": jwt({
    "https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                    "chatgpt_subscription_active_until": iso(now + 20 * 86400)}})}}))
check("codex: живая подписка — active, plan plus", rt._sub_codex()["state"] == "active" and rt._sub_codex()["plan"] == "plus")
rt.CODEX_AUTH.write_text(json.dumps({"tokens": {"id_token": jwt({
    "https://api.openai.com/auth": {"chatgpt_plan_type": "free",
                                    "chatgpt_subscription_active_until": iso(now + 20 * 86400)}})}}))
check("codex: план free с датой в будущем — none (подписки нет), не active и не «истекла»", rt._sub_codex()["state"] == "none")
rt.CODEX_AUTH.write_text(json.dumps({"tokens": {"id_token": jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "free"}})}}))
check("codex: free без даты вовсе — none", rt._sub_codex()["state"] == "none")
rt.CODEX_AUTH.write_text(json.dumps({"tokens": {"id_token": jwt({
    "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_subscription_active_until": iso(now - 3 * 86400)}})}}))
check("codex: план plus с прошедшей датой — stale (снимок JWT устарел), не «истекла»", rt._sub_codex()["state"] == "stale")
rt.CODEX_AUTH.write_text(json.dumps({"tokens": {"id_token": jwt({"sub": "x"}),
                                                "access_token": jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                                                                                    "chatgpt_subscription_active_until": iso(now + 20 * 86400)}})}}))
check("codex: id_token без auth-раздела → unknown (фолбэк на access_token не подменяет: у него тот же раздел лишь если провайдер его кладёт)",
      rt._sub_codex()["state"] in ("unknown", "active"))
rt.CODEX_AUTH = T / "nope.json"
check("codex: нет auth.json — unknown с причиной", rt._sub_codex()["state"] == "unknown" and "nope" in rt._sub_codex()["note"])
check("_jwt_payload: мусор — пустой словарь", rt._jwt_payload("abc") == {} and rt._jwt_payload("") == {})

# grok: стаб _get_json
rt.GROK_AUTH = T / "grok_auth.json"
rt.GROK_AUTH.write_text(json.dumps({"https://auth.x.ai::c": {"key": "tok"}}))
_orig = rt._get_json
rt._get_json = lambda url, h, timeout=5: (200, {"subscriptions": [
    {"status": "SUBSCRIPTION_STATUS_INACTIVE", "tier": "SUBSCRIPTION_TIER_GROK_PRO",
     "billingPeriodEnd": iso(now - 30 * 86400), "modTime": "2026-08-18"},
    {"status": "SUBSCRIPTION_STATUS_ACTIVE", "tier": "SUBSCRIPTION_TIER_SUPER_GROK_LITE",
     "billingPeriodEnd": iso(now + 25 * 86400), "modTime": "2026-09-18"}]}, "")
g = rt._sub_grok()
check("grok: активная подписка выбрана среди неактивных, tier очищен, дата и статус",
      g["state"] == "active" and g["plan"] == "super_grok_lite" and g["status"] == "active" and g["until"])
rt._get_json = lambda url, h, timeout=5: (200, {"subscriptions": [
    {"status": "SUBSCRIPTION_STATUS_INACTIVE", "tier": "SUBSCRIPTION_TIER_GROK_PRO",
     "billingPeriodEnd": iso(now - 30 * 86400), "modTime": "2026-08-18"}]}, "")
check("grok: только неактивная — expired с её датой", rt._sub_grok()["state"] == "expired" and rt._sub_grok()["status"] == "inactive")
rt._get_json = lambda url, h, timeout=5: (200, {"subscriptions": [
    {"status": "SUBSCRIPTION_STATUS_INACTIVE", "tier": "SUBSCRIPTION_TIER_GROK_PRO",
     "billingPeriodEnd": int((now + 10 * 86400) * 1000), "modTime": "2026-09-18"}]}, "")
gi = rt._sub_grok()
check("grok: неактивная с датой в будущем (epoch мс) — expired, дата переведена в ISO",
      gi["state"] == "expired" and isinstance(gi["until"], str) and gi["until"].startswith("20"))
rt._get_json = lambda url, h, timeout=5: (200, {"subscriptions": []}, "")
check("grok: пустой список — none, не «истекла»", rt._sub_grok()["state"] == "none")
rt._get_json = lambda url, h, timeout=5: (200, {"subscriptions": [
    {"status": "SUBSCRIPTION_STATUS_ACTIVE", "tier": "SUBSCRIPTION_TIER_GROK_PRO",
     "billingPeriodEnd": str(int((now - 86400) * 1000)), "modTime": "2026-09-18"}]}, "")
gs = rt._sub_grok()
check("grok: ACTIVE с прошедшей датой (epoch строкой) — stale, дата в ISO", gs["state"] == "stale" and gs["until"].startswith("20"))
rt._get_json = lambda url, h, timeout=5: (403, None, "403 cloudflare")
check("grok: эндпоинт не ответил — unknown с причиной", rt._sub_grok()["state"] == "unknown" and "403" in rt._sub_grok()["note"])

# claude: профиль
rt.CLAUDE_CREDS = T / "creds.json"
rt.CLAUDE_CREDS.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}))
rt._get_json = lambda url, h, timeout=5: (200, {"account": {"has_claude_max": True},
                                                  "organization": {"subscription_status": "active",
                                                                   "subscription_created_at": "2026-04-20"}}, "")
cl = rt._sub_claude()
check("claude: статус active из профиля, план max, дата продления честно не обещана",
      cl["state"] == "active" and cl["plan"] == "max" and "не отдаёт" in cl["note"] and not cl.get("until"))
rt._get_json = lambda url, h, timeout=5: (200, {"account": {"has_claude_max": False, "has_claude_pro": False},
                                                  "organization": {"subscription_status": "canceled"}}, "")
check("claude: статус canceled — expired", rt._sub_claude()["state"] == "expired")
rt._get_json = lambda url, h, timeout=5: (200, {"account": {}, "organization": {"subscription_status": "past_due"}}, "")
check("claude: past_due — жива (active), не «истекла», и статус виден в строке плана",
      rt._sub_claude()["state"] == "active" and "past_due" in rt._sub_claude()["plan"])
rt._get_json = lambda url, h, timeout=5: (200, {"account": {}, "organization": {"subscription_status": "weird"}}, "")
check("claude: незнакомый статус — unknown, не догадка", rt._sub_claude()["state"] == "unknown")
rt._get_json = lambda url, h, timeout=5: (401, None, "401")
check("claude: профиль не ответил — unknown, план из диска", rt._sub_claude()["state"] == "unknown" and rt._sub_claude()["plan"] == "max")
rt._get_json = _orig

# сборка: все шесть, баланс — none
rt._sub_claude = lambda: {"state": "active"}; rt._sub_codex = lambda: {"state": "expired"}; rt._sub_grok = lambda: {"state": "active"}
allsub = rt.collect_subscriptions()
check("collect_subscriptions: шесть голосов; kimi/deepseek/gemini — none с причиной",
      set(allsub) == {"claude", "codex", "grok", "kimi", "deepseek", "gemini"}
      and allsub["kimi"]["state"] == "none" and "баланс" in allsub["kimi"]["note"] and allsub["gemini"]["state"] == "none")
rt._sub_codex = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
_r = rt.collect_subscriptions(force=True)
check("сборщик упал — unknown с причиной, остальные целы",
      _r["codex"]["state"] == "unknown" and "boom" in _r["codex"]["note"] and _r["claude"]["state"] == "active")
rt._get_json = lambda url, h, timeout=5: (200, {"account": {}, "organization": {"subscription_status": "past_due"}}, "")
rt.CLAUDE_CREDS.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}))

# заметки о переходе — настоящая функция; unknown не считается состоянием
rt._SUB_STATE.clear()
n1 = rt._sub_transitions({"codex": {"state": "active"}})
n2 = rt._sub_transitions({"codex": {"state": "unknown"}})
n3 = rt._sub_transitions({"codex": {"state": "expired", "plan": "free", "until": 1757937187}})
n4 = rt._sub_transitions({"codex": {"state": "expired", "plan": "free", "until": 1757937187}})
check("_sub_transitions: active→(unknown)→expired даёт одну заметку, повтор — нет, unknown не запоминается, числовой until не роняет",
      n1 == [] and n2 == [] and len(n3) == 1 and n3[0]["voice"] == "codex" and "истекла" in n3[0]["text"] and n4 == [])
rt._SUB_STATE.clear()
check("_sub_transitions: первый замер expired — без заметки (не переход, а стартовое состояние)",
      rt._sub_transitions({"grok": {"state": "expired"}}) == [])
# кэш на час: второй вызов не ходит в сеть
rt._sub_claude = lambda: {"state": "active"}; rt._sub_codex = lambda: {"state": "none"}; rt._sub_grok = lambda: {"state": "none"}
rt._SUB_CACHE.update(ts=0.0, data={})
calls = []
rt._sub_grok = lambda: (calls.append(1), {"state": "none"})[1]
rt.collect_subscriptions(); rt.collect_subscriptions()
check("collect_subscriptions: кэш на SUB_TTL — сборщики зовутся один раз; force обходит",
      len(calls) == 1 and (rt.collect_subscriptions(force=True), len(calls))[1] == 2)

print(f"\nsubscriptions: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
