#!/usr/bin/env python3
"""Ранние отказы: тень-детектор признаков отказа канала в потоке голоса
и снятие голоса человеком до таймаута.

Раунд «ранние-отказы» (2026-09-20) сошёлся на трёх вещах, и здесь ровно
они, в порядке стола:

1. СТРОГИЙ признак квоты. Прежний `"429" in blob` ловил «429» внутри
   uuid и task_progress — три сентябрьские «quota» по 1800 с были
   ложными (нашли все четверо). Здесь — те же регексы, что у комнаты
   (`live._status_of`): «429» только словом и с контекстом (too many,
   rate, limit, quota), плюс словарные признаки провайдеров.

2. ТЕНЬ, а не снятие. Детектор читает ТОЛЬКО stderr голоса (stdout —
   ответ модели: там «429» и «повтор» бывают цитатой), вне блоков мыслей
   «💭 … 💭», и ничего не убивает сам: пороги не согласованы (Claude
   120 с/3 признака, Codex 60 с, Kimi 120/300), а «ложное снятие дороже
   лишней минуты» — единогласно. Тень пишет предупреждение: в
   стенограмму хода, в ленту стола событием `early_warn` (жёлтая строка
   с кнопкой «снять сейчас») и в запись голоса полем `early_warns` —
   даже если голос потом ответил: иначе качество детектора не измерить
   (единогласно). Двое часов — «байты идут» и «прогресс есть» — у
   дирижёра: строка «повтор через N с» — сердцебиение, не жизнь, и тень
   её называет.

3. СНЯТЬ — ЧЕЛОВЕК. Кнопка в ленте пишет файл-флаг `drop/<pgid>` с именем
   снявшего; дирижёр проверяет флаг в своём цикле ожидания (2 с), снимает
   группу процессов голоса и записывает статус `dropped` с `ended_by` и
   последним признаком — не `timeout` и не `stalled`. Молчание человека
   основанием для автоснятия НЕ является (спор Codex против троих не
   решён — тень остаётся тенью до замера). pgid — не pid: группу с тем
   же номером нельзя переиспользовать, пока в ней жив хоть один процесс,
   а флаг читает только тот, кто эту группу породил.

Границы (названы в своде): признаки для claude/codex/grok не замерены
(Grok под pty склеивает потоки) — для них тень только предупреждает по
явным словам провайдера; словарь — из документации провайдеров и
адаптеров стола, не из отравленного `detail` журнала; `first_byte`
(дедлайн первого байта) живёт в адаптерах gemini-http/deepseek-http.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

QUOTA_RE = re.compile(
    r"\bhttp[ /]*429\b|\b429\b[^\n]{0,60}(too many|rate|limit|quota)|"
    r"\brate[ _-]?limit(ed|s|_exceeded|_error)?\b|resource[_ ]exhausted|"
    r"insufficient[ _]balance|quota (exceeded|exhausted)|"
    r"exceeded your (current )?quota|исчерпан[аы]? (все )?ключ|исчерпан по квоте|квота/баланс|"
    r"tokens? per (day|minute)|daily limit|"
    r"reached your [^\n]{0,40}limit|usage limit", re.I)
# Без «error/bad/timeout» в контексте кода: «item 503 error in tool
# output» красился бы в шлюз (grok). Только слова провайдера/адаптера.
GATEWAY_RE = re.compile(
    r"\b(502|503|504|529)\b[^\n]{0,40}(шлюз|gateway|unavailable|overload|"
    r"demand)|"
    r"\bhttp[ /]*(502|503|504|529)\b|service unavailable|overloaded|"
    r"high demand|econnreset|etimedout|connection reset by peer|"
    r"read operation timed out|remote end closed|"
    r"поток закрылся без finishReason|нет первого байта", re.I)
BUSY_RE = re.compile(r"\b429\b[^\n]{0,60}concurren|max organization concurrency", re.I)
AUTH_RE = re.compile(
    r"\b401\b[^\n]{0,40}(unauthori|auth)|\b403\b[^\n]{0,40}(forbid|permission)|"
    r"not logged in|please run /login|invalid api key|invalid_api_key|"
    r"authentication_error", re.I)
RETRY_RE = re.compile(r"повтор через \d+ с|retrying in \d+|retry(ing)? (in|after)|"
                      r"попытка \d+/\d+", re.I)

THOUGHT_OPEN = "💭 мысли модели"
THOUGHT_CLOSE = "💭 конец мыслей"


def classify(line: str) -> str | None:
    """quota / auth / gateway / retry — или None. Порядок значим: квота и
    auth детерминированы (снимать можно сразу — так решил стол), шлюз и
    повтор — заминка (только предупреждать)."""
    if BUSY_RE.search(line):
        return "busy"          # concurrency=1 у Кими: занята линия, не квота (grok)
    if QUOTA_RE.search(line):
        return "quota"
    if AUTH_RE.search(line):
        return "auth"
    if GATEWAY_RE.search(line):
        return "gateway"
    if RETRY_RE.search(line):
        return "retry"
    return None


class Shadow:
    """Тень одного хода: кормится строками stderr, помнит признаки.

    `feed` возвращает предупреждение (dict) ТОЛЬКО когда его стоит
    показать: первый признак, смена класса, затем по удвоению счётчика
    (2, 4, 8 …) — иначе адаптер с «повтор через 4 с» засыпал бы ленту."""

    def __init__(self, voice: str):
        self.voice = voice
        self.t0 = time.monotonic()
        self.hits = 0
        self.first_seen_s: float | None = None
        self.last: dict | None = None
        self.warns: list[dict] = []
        self._in_thought = False
        self._next_report = 1

    def feed(self, line: str, channel: str = "err") -> dict | None:
        if channel != "err":
            return None
        s = line.strip()
        # Маркер — только СВОЕЙ строкой (адаптеры печатают его отдельно):
        # «Цитирую маркер: 💭 конец мыслей» внутри рассуждений иначе
        # открывал бы тень для следующей строки мыслей (codex)
        if s.startswith(THOUGHT_OPEN):
            self._in_thought = True
            return None
        if s.startswith(THOUGHT_CLOSE):
            self._in_thought = False
            return None
        if self._in_thought or not s:
            return None
        cause = classify(s)
        if cause is None:
            return None
        self.hits += 1
        now = round(time.monotonic() - self.t0, 1)
        if self.first_seen_s is None:
            self.first_seen_s = now
        info = {"cause": cause, "line": s[:200], "hits": self.hits,
                "first_seen_s": self.first_seen_s, "at_s": now}
        changed = self.last is None or self.last["cause"] != cause
        self.last = info
        if changed or self.hits >= self._next_report:
            self._next_report = max(self._next_report * 2, self.hits + 1)
            self.warns.append(info)
            return info
        return None

    def summary(self) -> list[dict]:
        return list(self.warns)


def mark(info: dict, voice: str) -> str:
    """Жёлтая строка для стенограммы и ленты."""
    what = {"quota": "квота/лимит", "auth": "авторизация", "gateway": "шлюз",
            "retry": "повтор канала", "busy": "занята линия"}.get(info["cause"], info["cause"])
    return (f"⚠ {voice}: признак отказа — {what} (№{info['hits']}, "
            f"первый на {info['first_seen_s']} с): {info['line']}")


# ── снятие человеком ─────────────────────────────────────────────────

def drop_dir() -> Path:
    return Path(os.environ.get("CHOIR_DROP_DIR")
                or Path.home() / ".cache" / "choir" / "drop")


def drop_file(pgid: int) -> Path:
    return drop_dir() / str(int(pgid))


def request_drop(pgid: int, who: str, act: str = "", voice: str = "") -> Path:
    """Окно: поставить флаг «кто|акт|голос». Читает и снимает его только
    дирижёр, породивший группу pgid, и только если акт и голос — его
    (kimi, grok: старая кнопка после переиспользования pgid в том же
    акте снимала бы другого голоса)."""
    p = drop_file(pgid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(f"{who.strip() or 'arr'}|{act}|{voice}", encoding="utf-8")
    os.replace(tmp, p)
    return p


def group_alive(pgid: int) -> bool:
    """Жива ли группа процессов (сигнал 0)."""
    try:
        os.killpg(int(pgid), 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def quota_in(text: str) -> str | None:
    """Первая строка текста, которую classify зовёт квотой — строго, по
    строкам: «429» внутри uuid и concurrency (busy) сюда не попадают."""
    for ln in (text or "").splitlines():
        if classify(ln) == "quota":
            return ln.strip()
    return None


def clear_drop(pgid: int) -> None:
    """Дирижёр: перед стартом группы и после её конца — флага быть не
    должно. Иначе флаг, поставленный за секунду до выхода голоса, лежал
    бы час и снял бы первую группу с тем же номером (все пять ревьюеров
    + субагент)."""
    try:
        drop_file(pgid).unlink()
    except OSError:
        pass


def drop_requested(pgid: int, act: str = "", voice: str = "") -> str | None:
    """Дирижёр: есть ли флаг на мою группу; снять его и вернуть, кто
    просил. Чужой акт или голос в флаге, файл старше часа — не команда."""
    p = drop_file(pgid)
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    try:
        p.unlink()
    except OSError:
        pass
    parts = raw.split("|")
    who = parts[0] if parts else ""
    for_act = parts[1] if len(parts) > 1 else ""
    for_voice = parts[2] if len(parts) > 2 else ""
    if time.time() - st.st_mtime > 3600:
        return None
    if for_act and act and for_act != act:
        return None
    if for_voice and voice and for_voice != voice:
        return None
    return who or "arr"


def sweep_stale(max_age_s: int = 3600) -> int:
    """Уборка флагов, которые никто не прочитал (дирижёр умер раньше)."""
    n = 0
    try:
        for p in drop_dir().iterdir():
            try:
                if time.time() - p.stat().st_mtime > max_age_s:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def warn_to_feed(voice: str, info: dict, *, pgid: int, act: str | None,
                 round_id: str | None = None, phase: str | None = None,
                 blind: bool = False) -> None:
    """Событие early_warn в ленту стола — через live.post, если комната
    рядом (дирижёр); без неё — молча (тесты, чужой запуск). В СЛЕПОЙ
    фазе строки-улики в ленте нет — только класс, счётчик и sha256
    строки: stderr у голосов под pty может нести текст ответа
    (codex, kimi, gemini); сама строка остаётся в записи голоса и
    публикуется с закрытием фазы."""
    try:
        import live                                    # noqa: PLC0415
    except Exception:                                  # noqa: BLE001
        return
    import hashlib                                     # noqa: PLC0415
    ev_line = info["line"]
    text = mark(info, voice)
    if blind:
        ev_line = "sha256:" + hashlib.sha256(info["line"].encode()).hexdigest()[:16]
        text = text.split("): ", 1)[0] + "): [слепая фаза — строка в записи голоса]"
    try:
        live.post("chamber", "early_warn", text,
                  voice=voice, cause=info["cause"], hits=info["hits"],
                  first_seen_s=info["first_seen_s"], at_s=info["at_s"],
                  evidence=ev_line, pgid=int(pgid), shadow=True,
                  **({"act": act} if act else {}),
                  **({"round": round_id} if round_id else {}),
                  **({"phase": phase} if phase else {}))
    except Exception as e:                             # noqa: BLE001
        print(f"early_warn не записан: {e}", file=__import__("sys").stderr)
