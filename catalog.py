"""Каталог моделей и ступеней усилия — ОДИН на все вкладки окна.

Зачем отдельный модуль. До 2026-09-02 списки моделей были буквами в
VOICE_CTL окна, и у Клода там не было Fable 5.1 просто потому, что её
не было в день, когда список писали. Наказ Автора: «появилась новая
модель — давай предусмотрим кнопочку обновить список доступных». Список
поэтому не пишется руками, а РАЗВЕДЫВАЕТСЯ у самих каналов, и у каждого
названо, откуда он взят и когда:

  claude    api.anthropic.com/v1/models по OAuth-токену самого Claude
            Code (~/.claude/.credentials.json) — проверено 2026-09-02:
            отдаёт claude-fable-5-1, claude-opus-5, … Алиасы CLI
            (fable/opus/sonnet/haiku) идут первыми — ими живут live.py
            и choir.py;
  codex     ~/.codex/models_cache.json — кэш пишет сам CLI с сервера
            (slug, supported_reasoning_levels, default_reasoning_level,
            visibility). Мы его только читаем: заставить codex обновить
            кэш можно лишь платным вызовом;
  grok      ~/.grok/models_cache.json — то же: reasoning_efforts и
            reasoning_effort (умолчание) по каждой модели. Прямой GET
            к cli-chat-proxy токеном auth.json отвечает 401 — проверено,
            поэтому только кэш;
  kimi      Moonshot /v1/models ключом линии из ~/.kimi-code/config.toml
            ПЛЮС алиасы [models] того же конфига — CLI принимает только
            алиасы, сервер знает только свои имена; оба списка честно
            подписаны. Второй провайдер (moonshotai2) — это вторая ЛИНИЯ
            того же кошелька, а не вторая модель;
  deepseek  `deepseek-http --models` (что видит ключ);
  gemini    `gemini-http --models` (что видит ключ), без image/tts/live —
            столу нужен текст.

Усилия — по моделям, где канал их различает (codex, grok): union по
всем моделям пропускал пару grok-4.5+xhigh, которую CLI роняет с
'unknown effort level'. У кого рычага нет (kimi), лестница пустая — и
это честное «нет», а не забытое поле.

Кэш ~/.cache/choir/rt-models.json переживает перезапуск окна; без него
работает запасной список CURATED с подписью «разведки не было». Разведка
не роняет окно: каждый источник под своим таймаутом, отказ источника —
строка в отчёте, а не исключение наружу.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(os.environ.get("CHOIR_RT_MODELS")
             or Path.home() / ".cache" / "choir" / "rt-models.json")
NET_TIMEOUT = 12.0
CLI_TIMEOUT = 45.0
VOICES = ("claude", "codex", "grok", "kimi", "deepseek", "gemini")

# Тот же класс символов, что MODEL_RE окна: имя уходит аргументом CLI, а
# у Грока — строкой в `script -qec`. Чужое имя из разведки не должно
# обойти проверку, которую проходит имя, набранное руками.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
# Ступень усилия уходит в `-c model_reasoning_effort=<x>` Кодекса БЕЗ
# кавычек (с кавычками CLI виснет) — то есть в TOML-значение. Класс
# символов ступени поэтому узкий: кавычка или перевод строки из
# правленного руками кэша были бы инъекцией в конфиг (нашёл grok).
EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
EFFORT_ORDER = ("none", "low", "medium", "high", "xhigh", "max", "ultra")
# Похожее на ключ или токен — из чужого текста вырезается ДО того, как
# он станет причиной отказа: причина уходит в отчёт, отчёт — в ленту
# (нашли codex, grok, kimi независимо).
_SECRET_RE = re.compile(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{6,}|AIza[0-9A-Za-z_-]{10,}|"
                        r"[A-Za-z0-9_-]{40,})")

# ЗАПАС, не истина: список на случай, когда разведка недоступна (нет
# ключа, нет кэша CLI). Подпись источника у такого списка говорит
# «запасной» — и кнопка ⟳ в окне зовёт разведку заново.
CURATED: dict[str, list[str]] = {
    "claude": ["fable", "opus", "sonnet", "haiku"],
    "codex": ["gpt-5.6-sol"],
    "grok": ["grok-4.6", "grok-4.5"],
    "kimi": ["kimi-k3"],
    "deepseek": ["deepseek-v4-pro", "deepseek-v4-flash"],
    "gemini": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.1-pro"],
}
CURATED_EFFORTS: dict[str, list[str]] = {
    "claude": ["low", "medium", "high", "xhigh", "max"],
    "codex": ["low", "medium", "high", "xhigh"],
    "grok": ["low", "medium", "high"],
    "kimi": [],
    "deepseek": ["low", "medium", "high", "max", "none"],
    "gemini": ["low", "medium", "high"],
}
# Алиасы CLI Клода: ими написаны умолчания live.py/choir.py, и без них
# селектор не мог бы показать «opus (умолчание)» пунктом списка.
CLAUDE_ALIASES = ["fable", "opus", "sonnet", "haiku"]

CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"
CODEX_CACHE = Path.home() / ".codex" / "models_cache.json"
GROK_CACHE = Path.home() / ".grok" / "models_cache.json"
KIMI_CFG = Path.home() / ".kimi-code" / "config.toml"
ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models?limit=100"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Джемини отдаёт полсотни имён; столу нужны текстовые модели. Фильтр
# по суффиксам — намеренно грубый и виден в подписи источника.
GEMINI_SKIP = re.compile(r"image|tts|live|transcribe|embedding|customtools|"
                         r"translate|audio|veo|imagen|robotics|computer-use")

_LOCK = threading.Lock()
# Разведка — ОДНА за раз: стартовая нить и кнопка ⟳ иначе писали один
# tmp-файл кэша и затирали друг друга (нашли codex, grok, claude).
_REFRESH_LOCK = threading.Lock()
CATALOG: dict[str, dict] = {}


def scrub(text: str) -> str:
    return _SECRET_RE.sub("‹вырезано›", str(text or ""))


class Unavailable(Exception):
    """Источник недоступен — причина в тексте, окно её покажет."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sort_efforts(effs) -> list[str]:
    seen: list[str] = []
    for e in effs or []:
        e = str(e).strip()
        if e and EFFORT_RE.match(e) and e not in seen:
            seen.append(e)
    return sorted(seen, key=lambda x: EFFORT_ORDER.index(x)
                  if x in EFFORT_ORDER else 99)


def _clean_models(names) -> list[str]:
    out: list[str] = []
    for n in names or []:
        n = str(n).strip()
        if n and MODEL_RE.match(n) and n not in out:
            out.append(n)
    return out


def _run(argv: list[str], timeout: float = CLI_TIMEOUT) -> str:
    """stdout адаптера; отказ — Unavailable с хвостом stderr."""
    if not shutil.which(argv[0]) and not Path(argv[0]).exists():
        raise Unavailable(f"{argv[0]} не найден в PATH")
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Unavailable(f"{argv[0]}: таймаут {int(timeout)} с")
    except OSError as e:
        raise Unavailable(f"{argv[0]}: {e}")
    if r.returncode != 0:
        tail = ANSI_RE.sub("", (r.stderr or r.stdout or "")).strip()[-200:]
        raise Unavailable(f"{argv[0]}: код {r.returncode} {scrub(tail)}")
    return ANSI_RE.sub("", r.stdout or "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редиректы ЗАПРЕЩЕНЫ: запрос несёт Authorization с OAuth-токеном
    Claude Code или ключом Moonshot, и стандартный обработчик urllib
    унёс бы заголовок на чужой хост (нашёл codex)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unavailable(f"HTTP {code}: редирект отклонён (токен не "
                          f"должен уйти на другой хост)")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get_json(url: str, headers: dict, timeout: float = NET_TIMEOUT):
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Unavailable:
        raise
    except urllib.error.HTTPError as e:
        # Тело ответа НЕ читаем в причину: оно чужое, уходит в отчёт и
        # ленту, а эндпоинт может отражать заголовки запроса.
        raise Unavailable(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise Unavailable(f"сеть: {scrub(type(e).__name__ + ': ' + str(e))}"[:160])


# ── парсеры: чистые функции, их и тестируем ─────────────────────────
def parse_codex_cache(doc) -> dict:
    """models_cache.json Кодекса → модели (visibility=list, по priority),
    ступени и умолчание усилия по каждой."""
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, list) or not models:
        raise Unavailable("models_cache.json: нет списка models")
    rows = []
    for m in models:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("id")
        if not slug or m.get("visibility") == "hide":
            continue
        effs = []
        for lv in m.get("supported_reasoning_levels") or []:
            v = lv.get("effort") if isinstance(lv, dict) else lv
            if v:
                effs.append(str(v))
        prio = m.get("priority")
        rows.append((prio if isinstance(prio, int) else 999, str(slug),
                     _sort_efforts(effs), m.get("default_reasoning_level")))
    rows.sort(key=lambda r: (r[0], r[1]))
    out = {"models": _clean_models(r[1] for r in rows),
           "efforts_by_model": {r[1]: r[2] for r in rows if r[2]},
           "default_effort_by_model": {r[1]: str(r[3]) for r in rows
                                       if r[3]}}
    if not out["models"]:
        raise Unavailable("models_cache.json: ни одной видимой модели")
    return out


def parse_grok_cache(doc) -> dict:
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, dict) or not models:
        raise Unavailable("models_cache.json: нет словаря models")
    names, ebm, dbm = [], {}, {}
    for mid, m in models.items():
        info = (m.get("info") if isinstance(m, dict) else None) or {}
        if info.get("hidden"):
            continue
        names.append(str(mid))
        effs = []
        for e in info.get("reasoning_efforts") or []:
            v = e.get("value") if isinstance(e, dict) else e
            if v:
                effs.append(str(v))
        if effs:
            ebm[str(mid)] = _sort_efforts(effs)
        if info.get("reasoning_effort"):
            dbm[str(mid)] = str(info["reasoning_effort"])
    # Порядок кэша = порядок сервера: первая — та, что CLI берёт без
    # -m (сверено по summary.json сессии стола 2026-09-02: grok-4.6).
    out = {"models": _clean_models(names), "efforts_by_model": ebm,
           "default_effort_by_model": dbm}
    if not out["models"]:
        raise Unavailable("models_cache.json: ни одной видимой модели")
    return out


def parse_kimi_config(cfg: dict) -> dict:
    """Алиасы [models] без провайдера, порядок файла, без дублей;
    default_model — тоже без провайдера."""
    names = []
    for alias in (cfg.get("models") or {}):
        short = str(alias).split("/")[-1]
        if short not in names:
            names.append(short)
    dm = str(cfg.get("default_model") or "").split("/")[-1]
    return {"models": _clean_models(names), "default_model": dm or None}


def parse_kimi_api(doc) -> list[str]:
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise Unavailable("/v1/models: нет data")
    return _clean_models(sorted(str(m.get("id")) for m in data
                                if isinstance(m, dict) and m.get("id")))


def parse_anthropic_api(doc) -> list[str]:
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise Unavailable("/v1/models: нет data")
    return _clean_models(str(m.get("id")) for m in data
                         if isinstance(m, dict) and m.get("id"))


def parse_lines(text: str, prefix: str) -> list[str]:
    """`--models` адаптеров: по имени в строке, шапки и отступы не в
    счёт. prefix отсекает служебные строки («ключ #0: 52 моделей»)."""
    out = []
    for line in (text or "").splitlines():
        tok = line.strip()
        if tok.startswith(prefix):
            out.append(tok.split()[0])
    return _clean_models(out)


def parse_gemini_lines(text: str) -> list[str]:
    return [m for m in parse_lines(text, "gemini-") if not GEMINI_SKIP.search(m)]


def parse_effort_help(text: str, flag: str) -> list[str]:
    """Ступени из --help: `--effort <level> … (low, medium, …)` у claude,
    `--thinking {low,medium,high}` у gemini-http."""
    text = ANSI_RE.sub("", text or "")
    m = re.search(re.escape(flag) + r"[^\n]*\n?[^\n]*?[({]([a-z, ]+)[)}]", text)
    if not m:
        return []
    # Только известные ступени: скобка вроде «(see docs)» рядом с флагом
    # иначе подменила бы рабочий список мусором (нашёл kimi).
    return _sort_efforts(s.strip() for s in m.group(1).split(",")
                         if s.strip() in EFFORT_ORDER)


# ── разведка по голосам ──────────────────────────────────────────────
def _claude_token() -> str:
    try:
        d = json.loads(CLAUDE_CREDS.read_text(encoding="utf-8"))
        tok = (d.get("claudeAiOauth") or {}).get("accessToken")
    except (OSError, ValueError, AttributeError):
        tok = None
    if not tok:
        raise Unavailable(f"нет OAuth-токена в {CLAUDE_CREDS}")
    return str(tok)


def _discover_claude() -> dict:
    ids = parse_anthropic_api(_get_json(ANTHROPIC_MODELS, {
        "Authorization": f"Bearer {_claude_token()}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "roundtable-catalog"}))
    effs: list[str] = []
    try:
        effs = parse_effort_help(_run(["claude", "--help"], 20), "--effort")
    except Unavailable:
        pass
    return {"models": CLAUDE_ALIASES + [i for i in ids
                                       if i not in CLAUDE_ALIASES],
            "efforts": effs or None,
            "source": f"api.anthropic.com/v1/models по OAuth Claude Code: "
                      f"{len(ids)} имён; алиасы CLI первыми"
                      + ("" if effs else "; ступени — запасной список "
                                         "(claude --help не разобрался)")}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Unavailable(f"{path} нет — CLI ещё не писал кэш")
    except (OSError, ValueError) as e:
        raise Unavailable(f"{path}: {e}")


def _age(iso: str | None) -> str:
    if not iso:
        return "дата в кэше не указана"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return f"кэш от {dt.isoformat(timespec='minutes')} ({h:.0f} ч назад)"
    except ValueError:
        return f"кэш от {iso}"


def _discover_codex() -> dict:
    doc = _read_json(CODEX_CACHE)
    out = parse_codex_cache(doc)
    out["source"] = (f"~/.codex/models_cache.json (пишет сам CLI с сервера; "
                     f"{_age(doc.get('fetched_at'))})")
    return out


def _discover_grok() -> dict:
    doc = _read_json(GROK_CACHE)
    out = parse_grok_cache(doc)
    out["source"] = (f"~/.grok/models_cache.json (пишет сам CLI с сервера; "
                     f"{_age(doc.get('fetched_at'))})")
    return out


def _kimi_cfg() -> dict:
    import tomllib
    try:
        with KIMI_CFG.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise Unavailable(f"{KIMI_CFG} нет")
    except (OSError, ValueError) as e:
        raise Unavailable(f"{KIMI_CFG}: {e}")


def _discover_kimi() -> dict:
    cfg = _kimi_cfg()
    local = parse_kimi_config(cfg)
    provs = cfg.get("providers") or {}
    key, base = None, "https://api.moonshot.ai/v1"
    for _, p in sorted(provs.items()):
        if isinstance(p, dict) and isinstance(p.get("api_key"), str):
            key = p["api_key"]
            base = str(p.get("base_url") or base).rstrip("/")
            break
    remote: list[str] = []
    note = ""
    if key:
        try:
            remote = parse_kimi_api(_get_json(
                f"{base}/models", {"Authorization": f"Bearer {key}"}))
        except Unavailable as e:
            note = f"; сервер не ответил ({e})"
    else:
        note = "; ключа линии в конфиге нет — только алиасы"
    # В селектор — ТОЛЬКО алиасы CLI: именно их принимает `kimi -m`.
    # Серверные имена без алиаса CLI не запустит; они остаются полем
    # unaliased и строкой в подписи источника — предлагать их пунктом
    # значило бы ронять голос на платном ходу (нашли все четверо).
    names = list(local["models"])
    extra = [m for m in remote if m not in names]
    if not names:
        raise Unavailable(f"в {KIMI_CFG.name} нет алиасов [models]"
                          + (f"; сервер знает: {', '.join(extra)}" if extra
                             else ""))
    return {"models": names, "efforts": [],
            "default_model": local.get("default_model"),
            "unaliased": extra,
            "source": f"алиасы [models] {KIMI_CFG.name}: {len(names)}; "
                      f"Moonshot /v1/models: {len(remote)}"
                      + (f" (без алиаса в CLI: {', '.join(extra)})"
                         if extra else "") + note}


def _discover_deepseek() -> dict:
    names = parse_lines(_run(["deepseek-http", "--models"]), "deepseek-")
    if not names:
        raise Unavailable("deepseek-http --models: пусто")
    effs: list[str] = []
    try:
        effs = parse_effort_help(_run(["deepseek-http", "--help"], 20),
                                 "--effort")
    except Unavailable:
        pass
    return {"models": names, "efforts": effs or None,
            "source": f"deepseek-http --models (что видит ключ): "
                      f"{len(names)}"}


def _discover_gemini() -> dict:
    names = parse_gemini_lines(_run(["gemini-http", "--models"]))
    if not names:
        raise Unavailable("gemini-http --models: пусто")
    effs: list[str] = []
    try:
        effs = parse_effort_help(_run(["gemini-http", "--help"], 20),
                                 "--thinking")
    except Unavailable:
        pass
    return {"models": names, "efforts": effs or None,
            "source": f"gemini-http --models (что видит ключ), без "
                      f"image/tts/live: {len(names)}"}


DISCOVER = {"claude": _discover_claude, "codex": _discover_codex,
            "grok": _discover_grok, "kimi": _discover_kimi,
            "deepseek": _discover_deepseek, "gemini": _discover_gemini}


# ── каталог: слияние, кэш, доступ ────────────────────────────────────
def _fallback(name: str, why: str) -> dict:
    return {"models": list(CURATED.get(name, [])),
            "efforts": list(CURATED_EFFORTS.get(name, [])),
            "efforts_by_model": None, "default_effort_by_model": None,
            "source": f"запасной список в catalog.py ({why})",
            "fetched_at": None, "error": why}


def _entry_from(name: str, found: dict) -> dict:
    ebm = found.get("efforts_by_model") or None
    effs = found.get("efforts")
    if effs is None:
        effs = (_sort_efforts(v for vs in ebm.values() for v in vs)
                if ebm else list(CURATED_EFFORTS.get(name, [])))
    return {"models": _clean_models(found.get("models")),
            "efforts": _sort_efforts(effs),
            "efforts_by_model": ebm,
            "default_effort_by_model": found.get("default_effort_by_model")
            or None,
            "default_model": found.get("default_model"),
            "unaliased": found.get("unaliased") or [],
            "source": found.get("source", ""),
            "fetched_at": _now(), "error": None}


def _normalize_entry(name: str, e) -> dict | None:
    """Запись кэша → запись каталога той же формы, что даёт разведка;
    None — запись негодна. Правленный руками файл проходит через ТОТ ЖЕ
    класс символов, что живая разведка: строка вместо словаря лестниц
    иначе роняла /voices TypeError'ом (нашли codex, claude)."""
    if not isinstance(e, dict):
        return None
    models = e.get("models")
    if not isinstance(models, list) or not models or not all(
            isinstance(m, str) and MODEL_RE.match(m) for m in models):
        return None
    ebm = e.get("efforts_by_model")
    if ebm is not None:
        if not isinstance(ebm, dict):
            return None
        ebm = {str(k): _sort_efforts(v) for k, v in ebm.items()
               if isinstance(v, list) and MODEL_RE.match(str(k))}
    dbm = e.get("default_effort_by_model")
    if dbm is not None:
        if not isinstance(dbm, dict):
            return None
        dbm = {str(k): str(v) for k, v in dbm.items()
               if isinstance(v, str) and EFFORT_RE.match(v)}
    dm = e.get("default_model")
    if dm is not None and not (isinstance(dm, str) and MODEL_RE.match(dm)):
        dm = None
    effs = e.get("efforts")
    if not isinstance(effs, list):
        return None
    una = e.get("unaliased")
    return {"models": _clean_models(models),
            "efforts": _sort_efforts(effs),
            "efforts_by_model": ebm or None,
            "default_effort_by_model": dbm or None,
            "default_model": dm,
            "unaliased": [str(x) for x in una] if isinstance(una, list) else [],
            "source": str(e.get("source") or ""),
            "fetched_at": (str(e["fetched_at"]) if isinstance(
                e.get("fetched_at"), str) else None),
            "error": (str(e["error"]) if isinstance(e.get("error"), str)
                      else None)}


def load() -> None:
    """Кэш с диска, иначе запас. Кривой кэш (руками правили) — запас с
    объяснением в stderr, а не падение окна на импорте."""
    disk: dict = {}
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            disk = raw
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"⚠ {CACHE.name} не прочитался ({e}) — списки моделей из "
              f"запаса, нажмите ⟳", file=sys.stderr)
    with _LOCK:
        for v in VOICES:
            e = _normalize_entry(v, disk.get(v))
            CATALOG[v] = (e if e is not None
                          else _fallback(v, "разведки ещё не было — "
                                            "кнопка ⟳ в окне"))


def _save() -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_name(CACHE.name + f".{os.getpid()}."
                              f"{threading.get_ident()}.tmp")
        with _LOCK:
            body = json.dumps(CATALOG, ensure_ascii=False, indent=1)
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(CACHE)
    except OSError as e:
        print(f"каталог моделей не сохранился: {e}", file=sys.stderr)


def _note_failure(n: str, why: str) -> bool:
    """Отказ источника: прежний РАЗВЕДАННЫЙ список остаётся (с причиной
    рядом), запасной — заменяется запасным с НОВОЙ причиной: иначе после
    неудачного ⟳ подсказка говорила «разведки ещё не было» (нашли claude
    и субагент). Возвращает, сохранён ли разведанный список."""
    why = scrub(why)[:200]
    with _LOCK:
        prev = CATALOG.get(n)
        kept = bool(prev and prev.get("fetched_at"))
        if kept:
            prev = dict(prev)
            prev["error"] = why
            CATALOG[n] = prev
        else:
            CATALOG[n] = _fallback(n, why)
    return kept


def refresh(names=None, timeout: float = CLI_TIMEOUT + 10) -> dict:
    """Разведка по голосам ПАРАЛЛЕЛЬНО; отчёт по каждому: сколько нашли и
    откуда, либо почему нет. Отказ источника НЕ стирает разведанный
    список — вчерашний кэш честнее пустого селектора.

    timeout — ДЕДЛАЙН ОЖИДАНИЯ, а не убийство: зависший источник не
    держит ответ (executor закрывается без wait, нить дозревает в
    фоне и уже никуда не пишет), а его строка в отчёте — «таймаут».
    Прежний `with ThreadPoolExecutor` ждал всех — timeout был
    декорацией (нашли codex, kimi, claude, субагент)."""
    names = list(dict.fromkeys(n for n in (names or VOICES) if n in DISCOVER))
    report: dict[str, dict] = {}
    with _REFRESH_LOCK:
        t0 = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=max(1, len(names)))
        futs = {n: ex.submit(DISCOVER[n]) for n in names}
        for n, fut in futs.items():
            left = max(0.5, timeout - (time.monotonic() - t0))
            try:
                found = fut.result(timeout=left)
                entry_ = _entry_from(n, found)
                if not entry_["models"]:
                    raise Unavailable("источник ответил пустым списком")
                with _LOCK:
                    CATALOG[n] = entry_
                report[n] = {"ok": True, "models": len(entry_["models"]),
                             "efforts": len(entry_["efforts"]),
                             "source": entry_["source"]}
            except Unavailable as e:
                kept = _note_failure(n, str(e))
                report[n] = {"ok": False, "error": scrub(str(e))[:200],
                             "kept": kept}
            except Exception as e:                  # noqa: BLE001
                # Таймаут дедлайна или сюрприз парсера — тоже строка
                # отчёта, а не 500 наружу: остальные уже разведаны.
                why = (f"таймаут ожидания {int(timeout)} с"
                       if isinstance(e, (TimeoutError, _FutTimeout))
                       or not str(e)
                       else f"{type(e).__name__}: {e}")[:200]
                kept = _note_failure(n, why)
                report[n] = {"ok": False, "error": scrub(why), "kept": kept}
        ex.shutdown(wait=False, cancel_futures=True)
        _save()
    return report

def entry(name: str) -> dict:
    with _LOCK:
        e = CATALOG.get(name)
        if e is None:
            e = _fallback(name, "голос каталогу неизвестен")
        return dict(e)


def models(name: str) -> list[str]:
    return list(entry(name).get("models") or [])


def efforts(name: str, model: str | None = None) -> list[str]:
    """Лестница для голоса; с моделью — её собственная, если канал
    различает модели (codex, grok). Модель вне кэша → общая лестница:
    запрещать имя, которого кэш не видел, нельзя (список устареет
    раньше провайдера), но и пару проверить нечем."""
    e = entry(name)
    ebm = e.get("efforts_by_model") or {}
    if model and model in ebm:
        return list(ebm[model])
    return list(e.get("efforts") or [])


def default_effort(name: str, model: str | None) -> str | None:
    dbm = entry(name).get("default_effort_by_model") or {}
    return dbm.get(model or "") or None


def default_model(name: str) -> str | None:
    """Умолчание, объявленное самим каналом: у kimi — default_model
    конфига, у grok — первая модель кэша (порядок сервера; CLI берёт её
    без -m). У остальных умолчание объявляет НЕ каталог, а код,
    который зовёт голос (live.py, choir.py, edits.py)."""
    e = entry(name)
    if e.get("default_model"):
        return str(e["default_model"])
    if name == "grok" and e.get("models") and e.get("fetched_at"):
        return str(e["models"][0])
    return None


def unaliased(name: str) -> list[str]:
    """Имена, которые сервер знает, а CLI без алиаса не запустит."""
    return list(entry(name).get("unaliased") or [])


def source(name: str) -> str:
    e = entry(name)
    s = e.get("source") or ""
    if e.get("error") and e.get("fetched_at"):
        s += f"; последняя разведка не удалась: {e['error']}"
    return s


load()
