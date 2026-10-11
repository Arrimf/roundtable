#!/usr/bin/env python3
"""Прокси стола к Jev (TypeSafe): ключ снаружи клетки, каждый вызов — событие ленты.

Решение стола (раунд jev-v2, 2026-10-06; Автор: «отличная механика»):
голоса получают Jev НЕ ключом в клетку, а через прокси на 127.0.0.1,
который держит ключ сам, пишет каждый вызов в `journal/live.jsonl`
событием `jev` (вопросы, вход, ответ, модель, время), в слепой фазе
отвечает отказом (решение Автора: «в слепой фазе Jev не звать») и
считает квоту на ход. Так любой вызов Jev виден столу, как реплика
(правило 10: ничего молчаливого; правило 8.5: свойство — механикой).

Как это выглядит для голоса. В каталог вызова (карантин раунда или
каталог голоса в комнате) кладётся ПРОПУСК `jev-pass` — случайный
одноразовый токен, привязанный к голосу, раунду, фазе и проекту, — и
обёртка `bin/jev`, которая выставляет `TYPESAFE_BASE_URL` на прокси,
`TYPESAFE_API_KEY` на пропуск и зовёт настоящий `jev` (CLI из gist,
~/.local/bin/jev). `bin/` ставится первым в PATH голоса. Настоящий ключ
в клетку не попадает; `_API_KEY` в среде клетка снимает (jail.unset_env),
поэтому пропуск едет файлом, а не переменной. Пропуск — не секрет: он
одноразовый на ход и весь его след — в ленте.

Прокси — отдельный процесс на 127.0.0.1:8789 (CHOIR_JEV_PORT), его
поднимает `ensure()` из окна или дирижёра, если он не отвечает; без
ключа TypeSafe (`~/.config/typesafe/api_key`) не поднимается — тогда
голосам Jev недоступен, и в пакете про него нет ни слова: чего нет,
то не обещается (fail-closed).

Событие `jev` в ленте: author = голос; round, phase, project, pass
(короткий id), questions (как есть), state (текст целиком до
STATE_CAP символов, иначе голова + sha256 + размер), answers, model
(из ответа), request_id, latency_ms, input_tokens, status =
ok | refused_blind | refused_quota | refused_pass | upstream_error |
outcome_unknown (ответ ушёл, результат не получен — codex/claude).
Отказ в слепой фазе пишется БЕЗ текста вопроса: лента видна голосам
(каталог проекта ro), а вопрос — часть ещё не раскрытой позиции. В
ОТКРЫТОЙ фазе события удерживаются (`hold` пропуска → HOLD_DIR, скрыт
от клетки) и публикуются дирижёром по закрытии фазы (`flush`) — иначе
соседи читали бы вопрос и ответ голоса посреди витка, поимённо.
Удержания старше часа публикует уборка прокси с пометкой held_stale.

Границы (названы столом): Codex в своей песочнице сети не имеет —
ему Jev недоступен, и пакет это говорит всем одинаково; квота — на
ход (CHOIR_JEV_QUOTA, по умолчанию 20, claude); пин модели по умолчанию
jev-1.13.0 (CHOIR_JEV_MODEL). С 2026-10-08 Jev зовёт и дирижёр — только
для пометок (chamber/jevtable.py: канарейка пересказа, второй глаз на
свод, тень усилия) и тем же путём: пропуск `choir`, событие в ленте.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live  # noqa: E402

PORT = int(os.environ.get("CHOIR_JEV_PORT") or 8789)   # 8779 занят стендом voices_http_test
BASE = f"http://127.0.0.1:{PORT}"
PASS_DIR = Path(os.environ.get("CHOIR_JEV_PASS_DIR") or Path.home() / ".cache" / "choir" / "jev-pass")
UPSTREAM = (os.environ.get("TYPESAFE_UPSTREAM") or "https://api.typesafe.ai").rstrip("/")
KEY_FILE = Path(os.environ.get("TYPESAFE_KEY_FILE") or Path.home() / ".config" / "typesafe" / "api_key")
LOG_FILE = Path(os.environ.get("CHOIR_JEV_LOG") or Path.home() / ".cache" / "choir" / "jevproxy.log")
QUOTA = int(os.environ.get("CHOIR_JEV_QUOTA") or 20)
PIN = os.environ.get("CHOIR_JEV_MODEL") or "jev-1.13.0"
STATE_CAP = int(os.environ.get("CHOIR_JEV_STATE_CAP") or 8000)
JEV_BIN = os.environ.get("CHOIR_JEV_BIN") or str(Path.home() / ".local" / "bin" / "jev")
NO_NET = ("codex",)            # своя песочница без сети — Jev недоступен


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def key_present() -> bool:
    try:
        return bool(KEY_FILE.read_text().strip())
    except OSError:
        return False


def _read_key() -> str:
    return KEY_FILE.read_text().strip()


# ── пропуска ────────────────────────────────────────────────────────

WRAPPER = """#!/bin/sh
# Jev через прокси стола (chamber/jevproxy.py): ключ снаружи клетки,
# каждый вызов — событие ленты. Пропуск одноразовый, на этот ход.
d="$(cd "$(dirname "$0")/.." && pwd)"
export TYPESAFE_BASE_URL="{base}"
export TYPESAFE_API_KEY="$(cat "$d/jev-pass")"
export TYPESAFE_DEFAULT_MODEL="{pin}"
exec "{jev}" "$@"
"""


HOLD_DIR = PASS_DIR / "hold"           # удержанные события открытой фазы: публикует дирижёр по закрытии


def issue_pass(voice: str, round_id: str, phase: str, *, blind: bool,
               project: str | None, call_dir: Path | None, quota: int = QUOTA,
               hold: str | None = None) -> str:
    """Выписать пропуск голосу на один ход: запись в PASS_DIR и, если дан
    каталог вызова, файл `jev-pass` и обёртка `bin/jev` в нём. Возвращает
    токен (он же имя записи). В слепой фазе пропуск тоже выписывается —
    прокси ответит отказом и запишет его: отказ — тоже факт (правило 4)."""
    token = secrets.token_hex(16)
    PASS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"token": token, "voice": voice, "round": round_id or None, "phase": phase,
           "blind": bool(blind), "project": project, "quota": int(quota), "used": 0,
           "created": time.time(), "hold": hold}
    tmp = PASS_DIR / f".{token}.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, PASS_DIR / f"{token}.json")
    if call_dir is not None:
        call_dir = Path(call_dir)
        pf = call_dir / "jev-pass"
        pf.write_text(token + "\n", encoding="utf-8")
        os.chmod(pf, 0o600)
        b = call_dir / "bin"
        b.mkdir(exist_ok=True)
        w = b / "jev"
        w.write_text(WRAPPER.format(base=BASE, pin=PIN, jev=JEV_BIN), encoding="utf-8")
        os.chmod(w, 0o755)
    return token


def revoke_pass(token: str | None, call_dir: Path | None = None) -> None:
    """Отозвать пропуск по концу хода: запись в PASS_DIR и файлы в каталоге
    вызова. Без отзыва токен жил бы сутки — голос с резюмируемой нитью
    помнил бы `cat jev-pass` и звал Jev в слепой фазе следующего раунда
    под старым пропуском (ревизия: субагент). Одноразовость — механикой."""
    p = _pass_path(token or "")
    if p is not None:
        try:
            p.unlink()
        except OSError:
            pass
    if call_dir is not None:
        call_dir = Path(call_dir)
        for f in (call_dir / "jev-pass", call_dir / "bin" / "jev"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            (call_dir / "bin").rmdir()
        except OSError:
            pass


def _pass_path(token: str) -> Path | None:
    if not token or not all(c in "0123456789abcdef" for c in token) or len(token) != 32:
        return None
    return PASS_DIR / f"{token}.json"


_PASS_LOCK = threading.Lock()


def _take(token: str) -> tuple[dict | None, str]:
    """Прочитать пропуск и списать один вызов. (запись, причина отказа)."""
    p = _pass_path(token)
    if p is None or not p.exists():
        return None, "refused_pass"
    with _PASS_LOCK:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "refused_pass"
        if rec.get("used", 0) >= rec.get("quota", QUOTA):
            return rec, "refused_quota"
        rec["used"] = rec.get("used", 0) + 1
        # запись «на месте» (r+), не создавая файл заново: пропуск, отозванный
        # дирижёром между чтением и записью, не должен воскреснуть (grok)
        if not _rewrite(p, rec):
            return None, "refused_pass"
        if rec.get("blind"):
            return rec, "refused_blind"       # отказ тоже стоит единицу квоты: не более QUOTA событий (grok)
    return rec, ""


def _rewrite(p: Path, rec: dict) -> bool:
    """Перезаписать запись пропуска на месте; False — файла уже нет (отозван)."""
    try:
        with p.open("r+", encoding="utf-8") as f:
            f.seek(0); f.truncate()
            f.write(json.dumps(rec, ensure_ascii=False))
        return True
    except OSError:
        return False


def _refund(token: str) -> None:
    """Вернуть единицу квоты: поставщик ОТВЕТИЛ ошибкой — вызов не состоялся.
    На обрыве (outcome_unknown) не возвращается: запрос мог быть оплачен (grok)."""
    p = _pass_path(token)
    if p is None or not p.exists():
        return
    with _PASS_LOCK:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec["used"] = max(0, rec.get("used", 0) - 1)
            _rewrite(p, rec)
        except (OSError, ValueError):
            pass


def sweep_passes(max_age: float = 86400) -> int:
    """Убрать пропуска старше суток (ход давно кончился)."""
    n = 0
    cut = time.time() - max_age
    try:
        for f in PASS_DIR.glob("*.json"):
            try:
                if f.stat().st_mtime < cut:
                    f.unlink(); n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


# ── событие ленты ───────────────────────────────────────────────────

def _state_field(state) -> dict | str:
    """Вход целиком до STATE_CAP символов (решение Автора: тексты стола
    наружу можно, секретов нет); длиннее — голова, sha и размер."""
    s = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    if len(s) <= STATE_CAP:
        return state
    return {"head": s[:STATE_CAP], "sha256": _sha(s), "chars": len(s), "truncated": True}


def _brief(answers: dict | None) -> str:
    if not answers:
        return ""
    bits = []
    if not isinstance(answers, dict):
        return str(answers)[:120]
    for qid, a in list(answers.items())[:6]:
        if not isinstance(a, dict):
            continue
        try:
            if "noul" in a:
                bits.append(f"{qid}: P={float(a['noul']):.2f}")
            elif "choice" in a:
                bits.append(f"{qid}: {a['choice']}")
            elif "score" in a:
                bits.append(f"{qid}: {float(a['score']):.2f}")
        except (TypeError, ValueError):             # null/строка от поставщика — не ронять событие (grok)
            bits.append(f"{qid}: {str(a)[:40]}")
    return "; ".join(bits)


def log_event(rec: dict | None, *, status: str, questions=None, state=None, answers=None,
              model: str | None = None, request_id: str = "", latency_ms: int | None = None,
              input_tokens: int | None = None, detail: str = "") -> dict:
    voice = (rec or {}).get("voice") or "?"
    project = (rec or {}).get("project")
    n = len(questions) if isinstance(questions, dict) else 0
    if status == "ok":
        text = f"Jev: {n} вопр. → {_brief(answers)}" + (f" · {model}" if model else "")
    elif status == "refused_blind":
        text = "Jev: отказ — слепая фаза, вызовы закрыты (решение стола jev-v2)"
    elif status == "refused_quota":
        text = f"Jev: отказ — квота хода исчерпана ({(rec or {}).get('quota')})"
    elif status == "refused_pass":
        text = "Jev: отказ — пропуск не опознан"
    else:
        text = f"Jev: {status}" + (f" — {detail}" if detail else "")
    fields = {"status": status, "round": (rec or {}).get("round"), "phase": (rec or {}).get("phase"),
              "pass": ((rec or {}).get("token") or "")[:8] or None}
    # без пропуска и в слепой фазе тело запроса в ленту не идёт: иначе
    # любой процесс на 127.0.0.1 дописывал бы в журнал что угодно (ревизия)
    if status not in ("refused_blind", "refused_pass") and questions is not None:
        fields["questions"] = _state_field(questions)      # тот же потолок, что у входа
        fields["state"] = _state_field(state)
    if answers is not None:
        fields["answers"] = answers
    if model:
        fields["model"] = model
    if request_id:
        fields["request_id"] = request_id
    if latency_ms is not None:
        fields["latency_ms"] = latency_ms
    if input_tokens is not None:
        fields["input_tokens"] = input_tokens
    if detail and status not in ("refused_blind",):
        fields["detail"] = detail
    fields = {k: v for k, v in fields.items() if v is not None}
    hold = (rec or {}).get("hold")
    if hold and status not in ("refused_blind", "refused_pass"):
        # УДЕРЖАНИЕ (ревизия jevproxy: субагент, grok). Лента в клетке видна
        # голосам ro; событие открытой фазы с вопросом и ответом одного
        # голоса соседи прочли бы посреди витка — раньше своего ответа и
        # поимённо, в обход анонимных меток (правила 8.5, 9). Поэтому в
        # открытой фазе событие ложится не в ленту, а в каталог удержания
        # (скрыт от клетки вместе с кэшем стола), и публикуется дирижёром
        # по закрытии фазы (`flush`) — как слепые ответы. Отказы без
        # текста идут сразу: они не несут позиции.
        d = HOLD_DIR / _sha(hold)[:16]
        d.mkdir(parents=True, exist_ok=True)
        ev = {"author": voice, "kind": "jev", "text": text, "project": project,
              "held_at": live._now(), **fields}          # то же время, что ts ленты (UTC ISO)
        # имя — наносекунды под замком: два голоса одного витка пишут
        # одновременно, и счётчик по glob давал бы им один номер (ревизия)
        with _PASS_LOCK:
            name = f"{time.time_ns():020d}-{secrets.token_hex(3)}"
            tmp = d / f".{name}.tmp"
            tmp.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, d / f"{name}.json")
        return ev
    return live.post(voice, "jev", text, project=project, **fields)


def flush(hold: str, *, stale: bool = False) -> int:
    """Опубликовать удержанные события фазы `hold` в порядке записи.
    Зовёт дирижёр по закрытии фазы (не HTTP: голос в клетке не должен
    уметь вскрыть удержание раньше срока). Возвращает число событий."""
    d = HOLD_DIR / _sha(hold)[:16]
    if not d.is_dir():
        return 0
    return _flush_dir(d, stale=stale)


def flush_stale(max_age: float = 7200) -> int:
    """Удержания, в которые дольше двух часов ничего не писалось: дирижёр
    умер, не закрыв фазу — опубликовать с пометкой, чтобы оплаченные
    вызовы не пропали из журнала. Порог — больше жёсткого потолка голоса
    (1800 с): живая фаза с молчащим Jev под него не попадает (ревизия).
    Сюда же попадает поздняя запись голоса, снятого с запросом в полёте
    уже после закрытия фазы, — она честно выйдет с пометкой."""
    n = 0
    try:
        for d in HOLD_DIR.iterdir():
            if not d.is_dir():
                continue
            try:
                last = max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.glob("*.json")])
            except OSError:
                continue
            if time.time() - last > max_age:
                n += _flush_dir(d, stale=True)
    except OSError:
        pass
    return n


def _flush_dir(d: Path, *, stale: bool) -> int:
    """Опубликовать файлы каталога удержания по порядку имён (наносекунды).
    Файл снимается с диска ДО публикации (rename в .posting): два
    публикатора (дирижёр и уборка прокси) иначе дали бы дубль; не
    удалось опубликовать — файл возвращается на место."""
    n = 0
    for f in sorted(d.glob("*.json")):
        claim = f.with_suffix(".posting")
        try:
            os.rename(f, claim)               # атомарно: второй публикатор файла уже не увидит
        except OSError:
            continue
        try:
            ev = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            claim.unlink(missing_ok=True)
            continue
        author = ev.pop("author", "?"); text = ev.pop("text", "")
        ev.pop("kind", None); project = ev.pop("project", None)
        ev["held"] = True
        if stale:
            ev["held_stale"] = True
        try:
            live.post(author, "jev", text, project=project, **ev)
        except Exception as e:                  # noqa: BLE001  лента не записалась — вернуть файл
            try:
                os.rename(claim, f)
            except OSError:
                pass
            print(f"jevproxy: удержанное событие не опубликовано: {e}", file=sys.stderr)
            continue
        claim.unlink(missing_ok=True)
        n += 1
    try:
        d.rmdir()
    except OSError:
        pass
    return n


# ── сервер ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "jevproxy/1.0"

    def log_message(self, fmt, *args):            # noqa: A003
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + (fmt % args) + "\n")
        except OSError:
            pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self) -> str:
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.lower().startswith("bearer ") else ""

    def do_GET(self):                              # noqa: N802
        if self.path == "/health":
            # отпечатки конфигурации: дирижёр с другим журналом/каталогом
            # пропусков (тестовое окно) не должен считать этот прокси своим
            return self._json(200, {"ok": True, "upstream": UPSTREAM, "key": key_present(),
                                    "pin": PIN, "quota": QUOTA, "fingerprint": fingerprint()})
        if self.path == "/v1/models":
            # `jev doctor` и `jev --version` ходят сюда: отвечаем за прокси,
            # к поставщику не идём — пропуск на это не тратится
            p = _pass_path(self._token())
            if p is None or not p.exists():
                return self._json(401, {"detail": {"error_type": "authentication_error",
                                                   "message": "пропуск стола не опознан"}})
            return self._json(200, {"data": [{"id": PIN}, {"id": "jev-latest"}], "proxy": True})
        return self._json(404, {"detail": "нет такого пути"})

    def do_POST(self):                             # noqa: N802
        if self.path != "/v1/systemone":
            return self._json(404, {"detail": "нет такого пути"})
        try:
            self._post()
        except Exception as e:                     # noqa: BLE001
            # исключение в потоке иначе теряло бы оплаченный ответ и событие
            try:
                log_event(None, status="proxy_error", detail=f"{type(e).__name__}: {e}"[:200])
            except Exception:                      # noqa: BLE001
                pass
            try:
                self._json(500, {"detail": {"error_type": "proxy_error", "message": str(e)[:200]}})
            except Exception:                      # noqa: BLE001
                pass

    def _post(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, OSError):
            return self._json(400, {"detail": "тело не разбирается"})
        questions = body.get("questions") if isinstance(body, dict) else None
        state = body.get("state") if isinstance(body, dict) else None
        tok = self._token()
        rec, why = _take(tok)
        if why:
            log_event(rec, status=why, questions=questions, state=state)
            # 401/403 CLI jev не повторяет; 429 он повторял бы 6 раз с паузами
            # и плодил бы шесть событий отказа — поэтому квота тоже 403
            code = {"refused_pass": 401, "refused_blind": 403, "refused_quota": 403}[why]
            msg = {"refused_pass": "пропуск стола не опознан",
                   "refused_blind": "слепая фаза: Jev закрыт (решение стола jev-v2)",
                   "refused_quota": f"квота хода исчерпана ({(rec or {}).get('quota')})"}[why]
            return self._json(code, {"detail": {"error_type": "table_refusal", "message": msg}})
        if not key_present():
            log_event(rec, status="upstream_error", questions=questions, state=state,
                      detail="у прокси нет ключа TypeSafe")
            return self._json(503, {"detail": {"error_type": "no_key", "message": "у прокси нет ключа"}})
        body = dict(body)
        body["model"] = PIN               # пин обеспечен прокси, не просьбой: CLI всегда шлёт свой
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(UPSTREAM + "/v1/systemone", data=data, method="POST",
                                     headers={"Authorization": f"Bearer {_read_key()}",
                                              "Content-Type": "application/json",
                                              "User-Agent": "roundtable-jevproxy/1.0"})
        t0 = time.time()
        # Таймаут короче клиентского (jev CLI ждёт 30 с и ПОВТОРЯЕТ): иначе
        # второй запрос шёл бы при живом первом, а ответ первого писался бы
        # в ленту уже в закрытый сокет (ревизия). Ошибки поставщика отдаём
        # кодом 400: 429/5xx/502 CLI повторял бы до 6 раз — шесть событий и
        # шесть единиц квоты за один сбой; квота за несостоявшийся вызов
        # возвращается.
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
                rid = r.headers.get("x-typesafe-request-id", "")
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:600]
            _refund(tok)
            log_event(rec, status="upstream_error", questions=questions, state=state,
                      latency_ms=int((time.time() - t0) * 1000), detail=f"HTTP {e.code}: {txt[:200]}")
            return self._json(400, {"detail": {"error_type": "upstream_error",
                                               "message": f"поставщик ответил HTTP {e.code}: {txt[:200]}"}})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # запрос мог дойти, а ответ — нет: исход неизвестен, так и пишем;
            # квота не возвращается — вызов мог быть оплачен
            log_event(rec, status="outcome_unknown", questions=questions, state=state,
                      latency_ms=int((time.time() - t0) * 1000), detail=str(e)[:200])
            return self._json(400, {"detail": {"error_type": "upstream_unreachable", "message": str(e)[:200]}})
        try:
            resp = json.loads(raw.decode("utf-8"))
        except ValueError:
            resp = {}
        usage = resp.get("usage") or {}
        log_event(rec, status="ok", questions=questions, state=state, answers=resp.get("answers"),
                  model=resp.get("model"), request_id=rid,
                  latency_ms=int((time.time() - t0) * 1000), input_tokens=usage.get("input_tokens"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if rid:
            self.send_header("x-typesafe-request-id", rid)
        self.end_headers()
        self.wfile.write(raw)


def fingerprint() -> str:
    """Чьи это журнал и каталог пропусков: прокси чужого стенда не наш."""
    return _sha(f"{live.LIVE.resolve()}|{PASS_DIR.resolve()}|{PIN}|{QUOTA}|{UPSTREAM}")[:16]


PID_FILE = PASS_DIR.parent / "jevproxy.pid"


def serve(port: int = PORT) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    sweep_passes()
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
    except OSError:
        pass

    def _sweeper():                 # раз в час: старые пропуска и удержания без дирижёра
        while True:
            time.sleep(3600)
            sweep_passes()
            try:
                flush_stale()
            except Exception as e:                  # noqa: BLE001  уборка не должна ронять нить
                print(f"jevproxy: уборка удержаний: {e}", file=sys.stderr)
    threading.Thread(target=_sweeper, daemon=True).start()
    srv.serve_forever()


def stop() -> bool:
    """Остановить прокси, поднятый ensure() (по pid-файлу)."""
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 15)
        PID_FILE.unlink(missing_ok=True)
        return True
    except (OSError, ValueError):
        return False


# ── доступность и запуск ────────────────────────────────────────────

def health(timeout: float = 0.5) -> dict | None:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def available() -> bool:
    """Прокси отвечает, ключ есть, отпечаток конфигурации наш, бинарь jev
    исполняем (иначе обёртка упала бы «not found» при пакете «доступен»)."""
    h = health()
    return bool(h and h.get("ok") and h.get("key") and h.get("fingerprint") == fingerprint()
                and os.access(JEV_BIN, os.X_OK))


def ensure(spawn: bool = True) -> bool:
    """Прокси отвечает и ключ есть — True. Не отвечает — поднять отдельным
    процессом (если ключ есть), подождать до 3 с. Без ключа — False и
    без попыток: голосам Jev недоступен, пакет это скажет."""
    if available():
        return True
    if not spawn or not key_present() or not os.access(JEV_BIN, os.X_OK):
        return False
    if _port_busy():
        # порт занят: либо наш прокси ещё поднимается (второй дирижёр в ту
        # же секунду — ревизия: deepseek), либо чужой/другого стенда.
        # Подождать health до 3 с, потом — не наш.
        for _ in range(30):
            time.sleep(0.1)
            if available():
                return True
        return False
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as lf:
        subprocess.Popen([sys.executable, str(HERE / "jevproxy.py"), "serve"],
                         stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(HERE))
    for _ in range(30):
        time.sleep(0.1)
        if available():
            return True
    return False


def _port_busy() -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def notice(blind: bool, *, enabled: bool) -> str:
    """Строка пакета — ВСЕМ одинаковая (слепой пакет бит-в-бит, правило 13).
    Пусто — когда прокси нет: тогда и говорить нечего."""
    if not enabled:
        return ""
    if blind:
        return ("JEV: в слепой фазе закрыт (решение стола jev-v2) — вызов `jev` вернёт отказ, "
                "и отказ будет записан в ленту. В открытой фазе доступен.\n\n")
    return ("JEV ДОСТУПЕН: команда `jev` в PATH (yes / pick / rate / ask / rank / batch; "
            "`jev guide` — как формулировать). Это новый член стола: отвечает не текстом, а "
            "вероятностью, выбором из списка или оценкой по шкале. Каждый вызов — событие "
            f"ленты стола (вопрос, вход, ответ): открытая механика, не тайная. Квота {QUOTA} "
            f"вызовов на этот ход, модель {PIN}. Codex в своей песочнице без сети пропуска не "
            "получает — у него `jev` ответит «no API key»; так и скажите, не гадайте ответ.\n\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Прокси стола к Jev")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="слушать 127.0.0.1:PORT")
    sub.add_parser("ensure", help="поднять, если не отвечает")
    sub.add_parser("status", help="здоровье прокси")
    sub.add_parser("stop", help="остановить прокси, поднятый ensure()")
    sub.add_parser("sweep", help="убрать старые пропуска")
    fl = sub.add_parser("flush", help="опубликовать удержанные события фазы")
    fl.add_argument("hold")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        print(f"jevproxy: 127.0.0.1:{PORT} → {UPSTREAM}; ключ {'есть' if key_present() else 'НЕТ'}", flush=True)
        serve()
    elif a.cmd == "ensure":
        ok = ensure()
        print("прокси отвечает" if ok else "прокси не поднят (нет ключа или порт занят)")
        return 0 if ok else 1
    elif a.cmd == "status":
        h = health()
        print(json.dumps(h, ensure_ascii=False) if h else "не отвечает")
        return 0 if h else 1
    elif a.cmd == "stop":
        print("остановлен" if stop() else "не найден")
    elif a.cmd == "sweep":
        print(f"убрано пропусков: {sweep_passes()}; опубликовано застрявших удержаний: {flush_stale()}")
    elif a.cmd == "flush":
        print(f"опубликовано событий: {flush(a.hold)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
