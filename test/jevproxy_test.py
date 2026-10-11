#!/usr/bin/env python3
"""Прокси стола к Jev (chamber/jevproxy.py, решение стола jev-v2): ключ
снаружи, пропуск на ход, отказ в слепой фазе, квота, событие `jev` в ленте
с вопросом/входом/ответом, исход «неизвестен» при обрыве. Поставщик —
подменный локальный HTTP-сервер; лента — подменная; ключ — временный файл."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

RT = Path(__file__).resolve().parent.parent
W = Path(tempfile.mkdtemp(prefix="jevp.", dir="/var/tmp"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


UP_PORT, PX_PORT = free_port(), free_port()
os.environ.update(CHOIR_JOURNAL=str(W), ROUNDTABLE_JOURNAL=str(W), CHOIR_ROOM=str(W / "room.jsonl"),
                  CHOIR_JEV_PORT=str(PX_PORT), CHOIR_JEV_PASS_DIR=str(W / "pass"),
                  TYPESAFE_UPSTREAM=f"http://127.0.0.1:{UP_PORT}", TYPESAFE_KEY_FILE=str(W / "key"),
                  CHOIR_JEV_LOG=str(W / "proxy.log"), CHOIR_JEV_QUOTA="2", CHOIR_JEV_MODEL="jev-1.13.0",
                  CHOIR_JEV_BIN=str(W / "jevbin" / "jev"), CHOIR_JEV_STATE_CAP="50")
(W / "jevbin").mkdir(parents=True, exist_ok=True)
# CLI jev для проверки обёртки: копия gist из заметок стола, иначе установленный ~/.local/bin/jev,
# иначе заглушка (тогда проверки обёртки — только на факт запуска)
_src = RT / "journal" / "notes" / "jev" / "jev.py"
if not _src.exists() and shutil.which("jev"):
    _src = Path(shutil.which("jev"))
HAVE_JEV = _src.exists()
if HAVE_JEV:
    shutil.copy(_src, W / "jevbin" / "jev")
else:
    (W / "jevbin" / "jev").write_text("#!/bin/sh\necho stub-jev \"$@\"; echo \"$TYPESAFE_API_KEY\" | grep -q . && echo 'from $TYPESAFE_API_KEY'\n")
os.chmod(W / "jevbin" / "jev", 0o755)
os.environ.pop("CHOIR_EVENT_PROJECT", None)
sys.path.insert(0, str(RT / "chamber"))
import live       # noqa: E402
import jevproxy   # noqa: E402
import atexit
atexit.register(jevproxy.stop)          # сироту-прокси не оставлять и при падении теста

ok = bad = 0


def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond)
    bad += not cond


def feed():
    return [json.loads(l) for l in live.LIVE.read_text(encoding="utf-8").splitlines() if l.strip()] if live.LIVE.exists() else []


# ── подменный поставщик: проверяет Bearer, отвечает ответами по вопросам ──
SEEN = {"auth": [], "bodies": []}
MODE = {"fail": False}


class Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        SEEN["auth"].append(self.headers.get("Authorization")); SEEN["bodies"].append(body)
        if MODE["fail"]:
            self.send_response(500); self.end_headers(); self.wfile.write(b'{"detail":"boom"}'); return
        ans = {}
        for qid, q in body["questions"].items():
            ans[qid] = ({"noul": 0.91} if q["type"] == "noul" else {"score": 1.5, "confidence": 0.8} if q["type"] == "score"
                        else {"choice": "a", "probabilities": {"a": 0.8, "b": 0.2}, "confidence": 0.7})
        out = json.dumps({"model": "jev-1.13.0", "answers": ans, "usage": {"input_tokens": 123, "output_tokens": 4}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("x-typesafe-request-id", "req-xyz"); self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)


up = HTTPServer(("127.0.0.1", UP_PORT), Up)
threading.Thread(target=up.serve_forever, daemon=True).start()

# ── без ключа прокси не поднимается ──
t("без ключа: ensure → False, процесс не запускается", jevproxy.ensure() is False and jevproxy.health() is None)
(W / "key").write_text("sk-test-REAL-KEY\n")
t("ensure с ключом поднимает прокси и видит ключ", jevproxy.ensure() is True and jevproxy.available())

# ── пропуск и обёртка ──
cdir = W / "call"; cdir.mkdir()
tok = jevproxy.issue_pass("grok", "r1", "rebut1", blind=False, project=str(W / "proj"), call_dir=cdir)
wrapper = (cdir / "bin" / "jev").read_text()
t("issue_pass: запись пропуска, файл jev-pass, обёртка bin/jev с базой прокси и пином",
  (W / "pass" / f"{tok}.json").exists() and (cdir / "jev-pass").read_text().strip() == tok
  and f"127.0.0.1:{PX_PORT}" in wrapper and "jev-1.13.0" in wrapper and os.access(cdir / "bin" / "jev", os.X_OK))
t("настоящий ключ в обёртке и пропуске не встречается", "sk-test-REAL-KEY" not in wrapper and "sk-test" not in (cdir / "jev-pass").read_text())


def call(token, questions=None, state="текст вопроса"):
    body = json.dumps({"state": state, "questions": questions or {"q": {"type": "noul", "instructions": "да?"}}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PX_PORT}/v1/systemone", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ── вызов через прокси: ключ подставлен, событие в ленте со всем ──
code, resp = call(tok)
ev = feed()[-1]
t("вызов прошёл: ответ поставщика вернулся как есть", code == 200 and resp["answers"]["q"]["noul"] == 0.91)
t("поставщик получил настоящий ключ и пин модели, а не пропуск",
  SEEN["auth"][-1] == "Bearer sk-test-REAL-KEY" and SEEN["bodies"][-1]["model"] == "jev-1.13.0")
t("событие jev в ленте: author=голос, статус ok, вопросы, вход, ответы, модель, request_id, токены, проект",
  ev["kind"] == "jev" and ev["author"] == "grok" and ev["status"] == "ok" and ev["questions"]["q"]["type"] == "noul"
  and ev["state"] == "текст вопроса" and ev["answers"]["q"]["noul"] == 0.91 and ev["model"] == "jev-1.13.0"
  and ev["request_id"] == "req-xyz" and ev["input_tokens"] == 123 and ev["project"] == str(W / "proj")
  and ev["round"] == "r1" and ev["phase"] == "rebut1" and ev["pass"] == tok[:8] and "sk-test" not in json.dumps(ev))
t("текст события читается: число вопросов и P", "1 вопр." in ev["text"] and "P=0.91" in ev["text"])

# ── длинный вход: голова + sha + размер ──
code, _ = call(tok, state="x" * 200)
ev = feed()[-1]
t("вход длиннее потолка — голова, sha256, размер, truncated", ev["state"]["truncated"] is True and len(ev["state"]["head"]) == 50 and ev["state"]["chars"] == 200)

# ── квота 2: третий вызов — отказ, событие refused_quota ──
code, resp = call(tok)
ev = feed()[-1]
t("квота исчерпана: 403 (без повторов CLI), событие refused_quota с вопросом", code == 403 and ev["status"] == "refused_quota" and "questions" in ev
  and "квота" in resp["detail"]["message"])

# ── слепая фаза: отказ БЕЗ текста вопроса ──
tb = jevproxy.issue_pass("claude", "r1", "blind", blind=True, project=None, call_dir=None)
code, resp = call(tb, state="секретная позиция")
ev = feed()[-1]
t("слепая фаза: 403, событие refused_blind без вопроса и входа",
  code == 403 and ev["status"] == "refused_blind" and "questions" not in ev and "state" not in ev
  and "секретная" not in json.dumps(ev) and ev["author"] == "claude")
call(tb); code, _ = call(tb)
t("отказы слепой фазы тоже тратят квоту: третий — refused_quota (не более QUOTA событий на пропуск)",
  feed()[-1]["status"] == "refused_quota")

# ── чужой/кривой пропуск ──
code, _ = call("deadbeef" * 4)
ev = feed()[-1]
t("неизвестный пропуск: 401, событие refused_pass от «?» БЕЗ тела запроса", code == 401 and ev["status"] == "refused_pass" and ev["author"] == "?"
  and "questions" not in ev and "state" not in ev)
code, _ = call("../../etc/passwd")
t("пропуск с путём — отказ без чтения файлов", code == 401)

# ── поставщик упал: 5xx — upstream_error; сеть оборвана — outcome_unknown ──
tok2 = jevproxy.issue_pass("kimi", "r1", "rebut1", blind=False, project=None, call_dir=None)
MODE["fail"] = True
code, _ = call(tok2)
ev = feed()[-1]
t("5xx поставщика: голосу 400 (CLI не повторяет), событие upstream_error с деталью, квота возвращена",
  code == 400 and ev["status"] == "upstream_error" and "HTTP 500" in ev["detail"]
  and json.loads((W / "pass" / f"{tok2}.json").read_text())["used"] == 0)
MODE["fail"] = False
up.shutdown(); up.server_close()
code, _ = call(tok2)
ev = feed()[-1]
t("поставщик недоступен: 400, событие outcome_unknown, квота НЕ возвращена (вызов мог быть оплачен)",
  code == 400 and ev["status"] == "outcome_unknown" and json.loads((W / "pass" / f"{tok2}.json").read_text())["used"] == 1)

# ── пин модели обеспечен прокси: просьба --model jev-latest перекрывается ──
up3 = HTTPServer(("127.0.0.1", UP_PORT), Up); threading.Thread(target=up3.serve_forever, daemon=True).start()
tokm = jevproxy.issue_pass("kimi", "r1", "rebut1", blind=False, project=None, call_dir=None)
body = json.dumps({"state": "s", "model": "jev-latest", "questions": {"q": {"type": "noul", "instructions": "?"}}}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{PX_PORT}/v1/systemone", data=body, method="POST",
                             headers={"Authorization": f"Bearer {tokm}", "Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=10).read()
t("пин: голос просил jev-latest — поставщику ушёл jev-1.13.0", SEEN["bodies"][-1]["model"] == "jev-1.13.0")
# длинный questions тоже режется потолком
big_q = {"q": {"type": "noul", "instructions": "x" * 300}}
req = urllib.request.Request(f"http://127.0.0.1:{PX_PORT}/v1/systemone", data=json.dumps({"state": "s", "questions": big_q}).encode(), method="POST",
                             headers={"Authorization": f"Bearer {tokm}", "Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=10).read()
except urllib.error.HTTPError:
    pass
t("длинные questions в событии — голова + sha, не мегабайты", feed()[-1].get("questions", {}).get("truncated") is True)
# отзыв пропуска: после revoke — 401, файлы обёртки убраны
jevproxy.revoke_pass(tokm, None)
code, _ = call(tokm)
t("revoke_pass: отозванный пропуск → 401", code == 401 and not (W / "pass" / f"{tokm}.json").exists())
tokr = jevproxy.issue_pass("grok", "r1", "rebut1", blind=False, project=None, call_dir=cdir)
jevproxy.revoke_pass(tokr, cdir)
t("revoke_pass с каталогом: jev-pass и bin/ убраны", not (cdir / "jev-pass").exists() and not (cdir / "bin").exists())
up3.shutdown(); up3.server_close()

# ── удержание: пропуск с hold — событие не в ленте, а в HOLD_DIR; flush публикует; stale — уборкой ──
up4 = HTTPServer(("127.0.0.1", UP_PORT), Up); threading.Thread(target=up4.serve_forever, daemon=True).start()
n0 = len(feed())
th = jevproxy.issue_pass("deepseek", "r1", "rebut2", blind=False, project=str(W / "proj"), call_dir=None, hold="r1|rebut2")
code, resp = call(th, state="позиция посреди витка")
call(th, state="второй вопрос")
hd = jevproxy.HOLD_DIR / jevproxy._sha("r1|rebut2")[:16]
t("удержание: вызовы прошли (200), в ленте событий нет, в каталоге удержания — два файла",
  code == 200 and len(feed()) == n0 and hd.is_dir() and len(list(hd.glob("*.json"))) == 2)
t("каталог удержания — под каталогом пропусков (скрыт от клетки вместе с кэшем стола)",
  str(hd).startswith(str(W / "pass")))
th_b = jevproxy.issue_pass("kimi", "r1", "blind", blind=True, project=None, call_dir=None, hold="r1|blind")
call(th_b)
t("отказ слепой фазы при hold — сразу в ленту (текста нет, удерживать нечего)", feed()[-1]["status"] == "refused_blind")
k = jevproxy.flush("r1|rebut2")
tail = feed()[-2:]
t("flush: два события опубликованы в порядке записи, с held=true, автор и содержимое целы",
  k == 2 and [e["state"] for e in tail] == ["позиция посреди витка", "второй вопрос"] and all(e["held"] is True and e["author"] == "deepseek" for e in tail)
  and tail[0]["project"] == str(W / "proj") and not hd.exists())
t("повторный flush — ноль", jevproxy.flush("r1|rebut2") == 0)
th2 = jevproxy.issue_pass("grok", "r9", "rebut1", blind=False, project=None, call_dir=None, hold="r9|rebut1")
call(th2, state="застрявшее")
hd2 = jevproxy.HOLD_DIR / jevproxy._sha("r9|rebut1")[:16]
t("flush_stale: свежее удержание не трогается", jevproxy.flush_stale() == 0 and hd2.is_dir())
for f in [hd2, *hd2.glob("*.json")]:
    os.utime(f, (time.time() - 8000, time.time() - 8000))
t("flush_stale: удержание без записей дольше двух часов опубликовано с held_stale", jevproxy.flush_stale() == 1 and feed()[-1]["held_stale"] is True and feed()[-1]["state"] == "застрявшее")
up4.shutdown(); up4.server_close()

# ── /v1/models: с пропуском отвечает прокси, без — 401 ──
req = urllib.request.Request(f"http://127.0.0.1:{PX_PORT}/v1/models", headers={"Authorization": f"Bearer {tok2}"})
with urllib.request.urlopen(req, timeout=5) as r:
    models = json.loads(r.read())
t("/v1/models с пропуском — список с пином, к поставщику не ходит", models["proxy"] is True and models["data"][0]["id"] == "jev-1.13.0")

# ── настоящий CLI jev через обёртку: ключ из пропуска, база — прокси ──
env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
env["HOME"] = str(W / "home"); (W / "home").mkdir(exist_ok=True)
env["PATH"] = f"{cdir / 'bin'}:{env.get('PATH', '')}"
# свежий пропуск в тот же каталог вызова (прежний выбран квотой), поставщик снова жив
up2 = HTTPServer(("127.0.0.1", UP_PORT), Up); threading.Thread(target=up2.serve_forever, daemon=True).start()
tok3 = jevproxy.issue_pass("grok", "r1", "rebut1", blind=False, project=None, call_dir=cdir)
if HAVE_JEV:
    p = subprocess.run(["jev", "doctor"], capture_output=True, text=True, env=env, timeout=60)
    t("CLI jev через обёртку: ключ — пропуск из файла (не настоящий), база — прокси, пробный вызов прошёл",
      "from $TYPESAFE_API_KEY" in p.stdout and "sk-test" not in p.stdout and "ok    POST /v1/systemone" in p.stdout
      and feed()[-1]["kind"] == "jev" and feed()[-1]["status"] == "ok" and feed()[-1]["pass"] == tok3[:8])
    if "ok    POST /v1/systemone" not in p.stdout:
        print("   doctor stdout:", p.stdout[-400:].replace("\n", " ⏎ "), "stderr:", p.stderr[-300:].replace("\n", " ⏎ "))
    p = subprocess.run(["jev", "yes", "Is `text` a question?", "--field", "text=почему?"], capture_output=True, text=True, env=env, timeout=60)
    t("CLI jev yes через прокси: ответ вернулся, событие с вопросом и входом", p.returncode == 0 and "yes" in p.stdout
      and feed()[-1]["status"] == "ok" and "noul" in json.dumps(feed()[-1]["questions"]).lower())   # questions режется потолком стенда (50)
    if not (p.returncode == 0 and "yes" in p.stdout):
        print("   jev yes rc:", p.returncode, "stdout:", p.stdout[-300:].replace("\n", " ⏎ "), "stderr:", p.stderr[-300:].replace("\n", " ⏎ "), "last event:", feed()[-1].get("status"), feed()[-1].get("detail"))
else:
    p = subprocess.run(["jev", "doctor"], capture_output=True, text=True, env=env, timeout=60)
    t("обёртка (без CLI jev на стенде): ключ из пропуска попал в среду настоящего jev", "from $TYPESAFE_API_KEY" in p.stdout)
    print("SKIP  CLI jev через прокси: ни заметки gist, ни ~/.local/bin/jev на стенде нет")
up2.shutdown(); up2.server_close()

# ── пакет: строка всем одинаковая, в слепой — «закрыт», без прокси — пусто ──
t("notice: открытая фаза называет квоту и пин; слепая — «закрыт»; без прокси — пусто",
  "jev-1.13.0" in jevproxy.notice(False, enabled=True) and "закрыт" in jevproxy.notice(True, enabled=True)
  and jevproxy.notice(False, enabled=False) == "")

# ── уборка пропусков ──
old = W / "pass" / ("ab" * 16 + ".json"); old.write_text("{}"); os.utime(old, (1, 1))
t("sweep: пропуск старше суток убран, свежие целы", jevproxy.sweep_passes() >= 1 and not old.exists() and (W / "pass" / f"{tok}.json").exists())

t("stop(): прокси, поднятый ensure(), останавливается по pid-файлу", jevproxy.stop() is True)
time.sleep(0.3)
t("после stop() прокси не отвечает", jevproxy.health() is None)
shutil.rmtree(W, ignore_errors=True)
print(f"\njevproxy: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
