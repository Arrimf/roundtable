"""Merge-гейт: ревизия дифа столом и приёмка правки в main (этап 3).

СПЕКА-исполнитель-v1 (v2), пп. 6–8:

  РЕВИЗИЯ  → диф base..head уходит ВСЕМ голосам, кроме исполнителя,
             одним и тем же пакетом (правило 1). Каждый ответ — событие
             edit_review в ленте, ДОСЛОВНО (правило 3), с вердиктом
             первой строкой и sha, НА КОТОРЫЙ он дан. Отказ канала —
             статус, не мнение (правило 4).
  ПРИЁМКА  → merge() под файловым flock: аренда закрыта (или adopt
             Автора) ∙ base_sha не сдвинут ∙ одобрений ≥2 и ни одного
             ОТКАЗА — всё НА ТЕКУЩИЙ head ветки. Доправка после
             ревизии меняет head — и старые одобрения гаснут сами.
  СЛЕД     → merge-коммит несёт трейлеры Reviewed-by, Base-sha и
             Patch-sha; третий элемент тройки — result — это sha
             САМОГО коммита (внутрь себя он лечь не может: sha зависит
             от текста). Вся тройка вместе — в событии edit_merge.
             Автор коммита — ГОЛОС-исполнитель, не Автор стола
             (правило 6). Событие — ПОСЛЕ move ref, проверки — ДО.

ИСТИНА — REF, НЕ РАБОЧАЯ КОПИЯ (спека, граница codex): merge-коммит
строится во временном detached-worktree, main двигается атомарным
`update-ref` со сверкой старого значения (CAS — гонка со сдвигом main
проигрывает честно, а не молча). Рабочая копия Автора обновляется
ПОСЛЕ и только если она чиста и стоит на той же ветке; грязная не
трогается никогда — событие называет, обновилась она или нет.

ЧТО ДОБРАНО ПОЗЖЕ ПЕРВОЙ ВЕРСИИ (2026-09-01, все четыре «чего нет»):
— rebase_act(): сдвиг main гейт чинит сам — в worktree акта, под
  замком, identity гейта (авторы сохраняются); прежние одобрения
  гаснут, событие edit_rebase главнее интента (_effective_base);
— scope: заявка files в интенте, сверка дифа fnmatch'ем (fail-closed
  при неудавшейся сверке); не заявлен — так и пишется;
— пломба ветки: seal_probe/seal_update под замком гейта, сдвиг ВНЕ
  гейта — одно нейтральное seal_note на сдвиг (ручные коммиты легальны);
— повторная ревизия шлёт ДЕЛЬТУ доправки первой (полный диф — справкой).
ЧЕГО НЕТ по-прежнему: дельты в review_batch (батч кормит полným дифом —
на пачке дельта каждого акта своя, и потолок пачки держит объём);
дельта после force-push прежнего head — диф неродственных sha, честная
деградация.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edits                                             # noqa: E402
import leases                                            # noqa: E402

QUORUM = 2          # минимум одобрений (спека п.6)
REVIEW_TIMEOUT = 600

# ── Ревьюеры вер1 ────────────────────────────────────────────────────
# Первая КОДИФИКАЦИЯ ручной практики ревизий вер1 (до сих пор она жила
# в CLAUDE.md и мышечной памяти дирижёра). Каждый вызов — read-only;
# промпт у всех ОДИН файл (правило 1: одинаковый вопрос и контекст).
# Грок — через pty (script): без терминала его песочница молча не
# применяется, и «рецензент начинает писать в репозиторий» (CLAUDE.md).
# gemini и deepseek файлов не видят — диф у них только из пакета, и это
# свойство канала, а не изъян ревизии (правило 14.5 наоборот: CLI-голоса
# и есть контролёры на первоисточнике).
# Метка eyes (правило 14.5): что голосу ВЫДАЛИ — «files» значит канал
# агентный и диск доступен, «dossier» значит физически виден только
# пакет. Метка не утверждает, что голос прочитал (машине это
# неизвестно) — ровно как в каноне.
REVIEWER_EYES = {"codex": "files", "claude": "files", "grok": "files",
                 "kimi": "dossier",    # режим экономии: работает по пакету
                 "deepseek": "files",  # контролёр dsh
                 "gemini": "dossier"}
REVIEWER_SEATS = {"deepseek": "dsh"}   # чем физически исполняется голос

REVIEWERS = {
    # Кодекс — ЗАКРУЧЕН ДО MEDIUM в ревизиях гейта (наказ Автора
    # 2026-09-01: «стал очень прожорлив»). Замер за 24ч: 296 запросов,
    # 64.6М входных токенов, 64% выхода — рассуждения на high. Раундов
    # стола это не касается (там своя лестница усилий из наказа
    # 2026-08-19). Вторая экономия — не гулять по репозиторию, когда
    # пакет полон: агентные шаги и есть свежий вход.
    "codex": lambda pf: ["codex", "exec", "-s", "read-only",
                         # БЕЗ кавычек в значении: -c 'k="v"' вешает
                         # codex до таймаута (проверено живьём, дважды),
                         # -c k=v работает.
                         "-c", "model_reasoning_effort=medium",
                         f"Прочитай файл {pf} и выполни, что там "
                         f"написано. Диф в пакете полный: по репозиторию "
                         f"ходи только если видишь в пакете противоречие "
                         f"или он ссылается на то, чего не показывает."],
    "claude": lambda pf: ["claude", "-p", "--permission-mode", "plan",
                          "--model",
                          os.environ.get("CHOIR_CLAUDE_MODEL", "fable"),
                          "--", f"Прочитай файл {pf} и выполни, что там "
                                f"написано. Только чтение."],
    "grok": lambda pf: ["script", "-qec",
                        f"grok -p {shlex.quote(f'Прочитай файл {pf} и выполни, что там написано.')} "
                        f"--sandbox read-only --no-plan", "/dev/null"],
    # Кими — РЕЖИМ ЭКОНОМИИ (наказ Автора 2026-09-01: «мнение Кими
    # важно, только придумаем, как его экономнее использовать»). Его
    # цена — агентность: одна ревизия = 15–20 внутренних запросов с
    # перечитыванием контекста (замер за ночь: 165 запросов, 6.3М
    # токенов кэш-чтения, ~5 USD). Поэтому здесь он работает ПО ПАКЕТУ:
    # диф полный, по файлам не ходит — 1–3 запроса вместо 20. Правило
    # 14.5 не страдает: контролёры на первоисточнике в этом же круге —
    # codex, grok и claude, они диск читают сами.
    "kimi": lambda pf: [str(Path.home() / ".kimi-code" / "bin" / "kimi"),
                        "-m", "moonshotai/kimi-k3", "-p",
                        f"Только читай сам файл {pf} и отвечай ПО НЕМУ: "
                        f"диф в нём полный, по другим файлам НЕ ходи "
                        f"(контролёры первоисточника в этом круге — "
                        f"другие голоса). Коротко, без рассуждений "
                        f"вслух. Выполни, что написано в {pf}."],
    # DEEPSEEK В ГЕЙТЕ — КОНТРОЛЁР НА ПЕРВОИСТОЧНИКЕ (наказ Автора
    # 2026-09-01: «прикрути dsh контролёром»). Правило 14.5 требует
    # обоих классов зрения: агентный dsh сам ходит в живую ветку
    # (замер: 7 с, 2–3 внутренних запроса — кими-болезни нет), а
    # ЧИСТЫМ ЧИТАТЕЛЕМ ПАКЕТА остаётся gemini — класс не потерян.
    # За столом deepseek по-прежнему говорит по HTTP; здесь другое
    # кресло, и событие несёт seat="dsh".
    # Контролёр — на PRO: умолчание dsh оказалось flash (вскрылось
    # 2026-09-01 вопросом Автора), а сверять факты слабейшей моделью
    # канала — экономия не там.
    "deepseek": lambda pf: ["dsh", "--profile", "headless",
                            "--patch",
                            str(Path(__file__).resolve().parent
                                / "dsh-model-pro.yaml"),
                            f"Ты — КОНТРОЛЁР НА ПЕРВОИСТОЧНИКЕ (правило "
                            f"14.5). Прочитай файл {pf}; вердикт выноси "
                            f"по живому репозиторию, но СТРОГО ПО SHA ИЗ "
                            f"ПАКЕТА: git diff <база>..<голова> в "
                            f"указанном project (не по вершине ветки — "
                            f"она могла legально уехать, и это не "
                            f"подлог; и не git show <голова>, который "
                            f"даёт один последний коммит). Расхождение "
                            f"пакета с фактом ПО ЭТИМ SHA — ОТКАЗ."],
    "gemini": lambda pf: ["gemini-http", "--prompt-file", str(pf),
                          "--model", _gemini_models(),
                          "--thinking", "high", "--timeout",
                          str(REVIEW_TIMEOUT - 60)],
}

_VERDICT_RE = re.compile(r"ВЕРДИКТ\s*:\s*(ОДОБРЯЮ|ОТКАЗ)", re.IGNORECASE)


def _parse_verdict(text: str) -> str:
    """Вердикт из ответа. ОБА варианта в тексте → unclear.

    Голоса-агенты пишут преамбулы («Сначала прочитаю…»), так что «только
    первая строка» ломала живых; а поиск первого совпадения ловил ЦИТАТУ
    инструкции с обоими вариантами и засчитывал одобрение (нашли codex и
    gemini). Однозначность вместо позиции: один исход в тексте — он и
    вердикт; оба — непонятно, и это честнее догадки.
    """
    found = {m.group(1).upper() for m in _VERDICT_RE.finditer(text[:4000])}
    kinds = {("approve" if "ОДОБР" in f else "refuted") for f in found}
    return kinds.pop() if len(kinds) == 1 else "unclear"


def _gemini_models() -> str:
    """Каскад моделей Джемини — ОБЩИЙ с комнатой (live._gemini_models).

    Первый живой прогон гейта звал адаптер без каскада, и Джемини лёг
    на тех же 503, от которых каскад заведён в тот же день — третья
    копия правила разошлась бы так же молча."""
    sys.path.insert(0, str(edits.CHOIR))
    try:
        import live                                      # noqa: PLC0415
        return live._gemini_models()
    except Exception:                                    # noqa: BLE001
        return "gemini-3.7-flash,gemini-3.6-flash"


def _git(repo: Path, *args, timeout: int = 120):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PATH"] = f"{Path.home() / '.local' / 'bin'}:{env.get('PATH', '')}"
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"git {' '.join(args)}: {e}"
    if r.returncode != 0:
        return None, f"git {' '.join(args)}: rc={r.returncode} " \
                     f"{r.stderr.strip()[:300]}"
    return r.stdout, ""


class GateRefused(RuntimeError):
    """Гейт не пропустил: причина в тексте, ref не двигался."""


# ── Сбор состояния акта из ленты ─────────────────────────────────────

def act_state(act: str) -> dict:
    """Всё, что лента знает об акте: интент, закрытие, adopt, ревизии.

    Лента читается целиком — это истина, а не кэш; акт без edit_open
    для гейта не существует, какой бы worktree ни лежал на диске.
    """
    st = {"open": None, "close": None, "crash": None, "adopt": None,
          "rebase": None, "reviews": []}
    for e in edits._feed_events(None):   # свежие первыми
        if e.get("act") != act:
            continue
        k = e.get("kind")
        # Фильтр автора — на ВСЕХ видах, не только close: строку
        # edit_review/edit_adopt мог вписать любой голос прямо в
        # разговоре, и две поддельные строки составили бы кворум
        # (нашли grok и codex; close уже фильтровался, остальные — нет).
        # Под одним uid это обнаружение, не запрет — но случайная
        # реплика голоса с полем kind не должна двигать main.
        if e.get("author") not in edits._CLOSE_AUTHORS:
            continue
        if k == "edit_open" and st["open"] is None:
            st["open"] = e
        elif k == "edit_close" and st["close"] is None:
            st["close"] = e
        elif k == "edit_crash" and st["crash"] is None:
            st["crash"] = e
        elif k == "edit_adopt" and st["adopt"] is None:
            st["adopt"] = e
        elif k == "edit_rebase" and st["rebase"] is None:
            st["rebase"] = e
        elif k == "edit_review":
            st["reviews"].append(e)
        elif k == "edit_merge":
            st.setdefault("merge", e)
    return st


def branch_head(project: Path, act: str) -> str:
    out, err = _git(project, "rev-parse", f"refs/heads/act/{act}")
    if out is None:
        raise GateRefused(f"ветка act/{act} не найдена: {err}")
    return out.strip()


def act_diff(project: Path, act: str, base_sha: str):
    """(текст дифа base..head, head_sha, patch_sha). Диф — ПО ВЕТКЕ:
    запись мимо git он не видит, и это названная граница (спека п.5)."""
    head = branch_head(project, act)
    out, err = _git(project, "diff", f"{base_sha}..{head}")
    if out is None:
        raise GateRefused(f"диф не взят: {err}")
    patch_sha = hashlib.sha256(out.encode()).hexdigest()
    return out, head, patch_sha


# ── Ревизия стола ────────────────────────────────────────────────────

def last_reviewed_sha(st: dict, head: str) -> str | None:
    """Самый свежий sha, который стол уже смотрел (≠ текущему head)."""
    for r in st["reviews"]:              # свежие первыми
        sha = r.get("sha")
        if sha and sha != head:
            return sha
    return None


def _review_prompt(act: str, st: dict, diff: str, head: str,
                   delta: str | None = None,
                   prev_sha: str | None = None,
                   base_sha: str | None = None) -> str:
    op = st["open"]
    base_sha = base_sha or _effective_base(st)
    delta_block = ""
    if delta is not None and prev_sha:
        delta_block = f"""

ЭТО ДОПРАВКА (спека п.6: повторная ревизия — ТОЛЬКО ДЕЛЬТЫ). Стол уже
смотрел {prev_sha[:12]}; ниже СНАЧАЛА дельта {prev_sha[:12]}..{head[:12]}
— читайте её; полный диф оставлен в конце для справки, перечитывать его
целиком не нужно. Вердикт выносится НОВОМУ sha {head[:12]}.

── ДЕЛЬТА ДОПРАВКИ ─────────────────────────────────────────────────
{delta}"""
    return f"""РЕВИЗИЯ ПРАВКИ ЗА СТОЛОМ (merge-гейт, этап 3). Вы — ревьюер.{delta_block}

Акт {act}; исполнитель {op.get('voice')}; ветка act/{act} проекта
{op.get('project')}; база {base_sha[:12]} (после rebase — эффективная,
не из интента), голова {head[:12]}.

ЗАДАНИЕ исполнителю было:
{op.get('text', '')}

Ниже полный диф base..head. Голоса с доступом к диску могут открыть живые
файлы проекта и ветку; диф в пакете может не показывать запись мимо git.

УСТРОЙСТВО, чтобы не спутать (первый живой прогон спутал): правка живёт
В ВЕТКЕ act/{act}, а РАБОЧАЯ КОПИЯ проекта до приёмки стоит НА БАЗЕ — файл
на диске проекта НЕ совпадает с правкой, и это норма, а не «запись мимо
git». Сверяйте содержимое ветки: git -C {op.get('project')} show
{head[:12]}:<путь>; поведение — на checkout ВЕТКИ или на блобе из show.

ПЕРВОЙ СТРОКОЙ ответа — ровно одно из двух:
ВЕРДИКТ: ОДОБРЯЮ
ВЕРДИКТ: ОТКАЗ
Дальше — до 200 слов: что проверили и почему такой вердикт; при отказе —
что именно чинить. Найденный дефект БЕЗ отказа не бывает: нашли — ОТКАЗ.

── diff ({head[:12]}) ──────────────────────────────────────────────
{diff}"""


def _run_reviewers(picked: dict, pf: Path, timeout: int, *, cwd: str):
    """Параллельные вызовы ревьюеров; (имя, результат) по мере готовности.

    Кими — ЧЕРЕЗ ВОРОТА, не напрямую: у его организации concurrency=1,
    и прямой вызов параллельно чужому раунду давал 429 со списанием
    токенов за отбитый запрос — грабля, оплаченная тремя сутками
    «медленного Кими» (CLAUDE.md; первый набросок гейта наступал на неё
    заново). rc!=0 с непустым stdout — error, не мнение: обрывок
    упавшего канала прежде мог стать действующим одобрением (codex).
    """
    def _call(name, mk):
        argv = mk(pf)
        t0 = time.monotonic()
        if name == "kimi":
            sys.path.insert(0, str(edits.CHOIR))
            from serial_gate import first_free_gate    # noqa: PLC0415
            from channels import kimi_channels         # noqa: PLC0415
            gates = sorted({c["gate"] for c in kimi_channels()}) or ["kimi"]
            with first_free_gate(gates):
                return _plain(name, argv, t0)
        return _plain(name, argv, t0)

    def _plain(name, argv, t0):
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=timeout,
                               cwd=cwd)
            out = (r.stdout or "").strip()
            el = round(time.monotonic() - t0, 1)
            if r.returncode != 0:
                return name, {"status": "error", "text": out,
                              "detail": (r.stderr or "")[-300:]
                              or f"rc={r.returncode}",
                              "elapsed_s": el}
            if not out:
                return name, {"status": "empty", "text": "",
                              "elapsed_s": el}
            return name, {"status": "ok", "text": out, "elapsed_s": el}
        except subprocess.TimeoutExpired:
            return name, {"status": "timeout", "text": "",
                          "detail": f"не уложился в {timeout} с",
                          "elapsed_s": round(time.monotonic() - t0, 1)}
        except OSError as e:
            return name, {"status": "error", "text": "", "detail": str(e),
                          "elapsed_s": round(time.monotonic() - t0, 1)}

    with ThreadPoolExecutor(max_workers=len(picked) or 1) as ex:
        futs = [ex.submit(_call, n, f) for n, f in picked.items()]
        for fu in as_completed(futs):
            yield fu.result()


def review(act: str, *, reviewers=None, timeout: int = REVIEW_TIMEOUT,
           post=None) -> list[dict]:
    """Разослать диф всем, кроме исполнителя; записать ответы в ленту.

    Возвращает записанные события edit_review. Отказ канала — статус
    (timeout/error/empty), не вердикт; придумывать мнение за упавшего
    нельзя ни при каких обстоятельствах (правило 4).
    """
    post = post or edits.post
    st = act_state(act)
    if not st["open"]:
        raise GateRefused(f"акт {act} не открывался (edit_open нет)")
    if st.get("merge"):
        raise GateRefused("акт уже принят — ревизия не нужна, вызовы "
                          "стоили бы денег зря")
    if leases.is_held(act):
        raise GateRefused("исполнитель ещё работает — ревизия дифа "
                          "движущейся мишени жгла бы вызовы зря")
    if not st["close"] and not st["adopt"]:
        raise GateRefused("ревизия — после закрытия акта (или adopt "
                          "Автора для вылетевшего)")
    project = Path(st["open"].get("project", ""))
    base_sha = _effective_base(st)
    diff, head, patch_sha = act_diff(project, act, base_sha)
    if not diff.strip():
        raise GateRefused("диф пуст — ревьюировать нечего")

    executor_voice = st["open"].get("voice")
    picked = {n: f for n, f in (reviewers or REVIEWERS).items()
              if n != executor_voice}

    # Свой файл на каждый прогон: общий по act+head второй запуск
    # перетирал и unlink'ал, пока первый ещё читали CLI голосов
    # (нашли grok, gemini, deepseek).
    pf = Path(tempfile.gettempdir()) / (
        f"edit-review-{act}-{head[:8]}-{os.getpid()}-"
        f"{time.monotonic_ns() & 0xffffff:x}.md")
    prev = last_reviewed_sha(st, head)
    delta = None
    if prev:
        delta, _err = _git(project, "diff", f"{prev}..{head}")
        # дельта не взялась (например, prev унесло gc) — честно шлём
        # полный диф, как раньше; это деградация, не ложь
    pf.write_text(_review_prompt(act, st, diff, head,
                                 delta=delta, prev_sha=prev,
                                 base_sha=base_sha),
                  encoding="utf-8")

    events = []
    try:
        results = _run_reviewers(picked, pf, timeout, cwd=str(project))
        for name, res in results:
            verdict = _parse_verdict(res.get("text", ""))
            if res["status"] != "ok":
                verdict = "absent"       # отказ канала — не мнение
            full = res.get("text", "")
            if len(full) > 20000:
                # Правило 3 разрешает ужать воду, но не молча: метка
                # обрезки обязательна (прежний потолок 4000 резал
                # обоснования отказов без следа — deepseek, grok).
                full = full[:20000] + "\n[…обрезано гейтом, было "
                full += f"{len(res['text'])} символов]"
            ev = post("edit_review",
                      f"ревизия {act} [{name}]: "
                      + (res["text"][:400] if res["status"] == "ok"
                         else f"({res['status']}) {res.get('detail', '')}"),
                      act=act, voice=name, sha=head, patch_sha=patch_sha,
                      verdict=verdict, status=res["status"],
                      elapsed_s=res.get("elapsed_s"),
                      eyes=REVIEWER_EYES.get(name, "files"),
                      seat=REVIEWER_SEATS.get(name, name),
                      full_text=full)
            events.append(ev)
    finally:
        pf.unlink(missing_ok=True)
    return events


# ── Приёмка ──────────────────────────────────────────────────────────

def checks(act: str) -> dict:
    """Проверки гейта БЕЗ действий — карточке окна и merge() перед ref."""
    st = act_state(act)
    out: dict = {"act": act, "ok": False, "reasons": []}
    if not st["open"]:
        out["reasons"].append("нет edit_open — акт неизвестен ленте")
        return out
    project = Path(st["open"].get("project", ""))
    base_sha = _effective_base(st)
    out["project"], out["base_sha"] = str(project), base_sha
    out["voice"] = st["open"].get("voice")
    if st.get("merge"):
        out["reasons"].append(f"уже принят: {st['merge'].get('result_sha')}")
        return out
    if not st["close"]:
        if st["crash"] and st["adopt"]:
            out["adopted"] = True        # спека п.9: adopt снимает
            #                              условие закрытой аренды
        elif st["crash"]:
            out["reasons"].append("акт вылетел — нужен adopt Автора")
        else:
            out["reasons"].append("акт не закрыт (edit_close нет)")
    if leases.is_held(act):
        out["reasons"].append("аренда ещё держится — исполнитель работает")
    try:
        _, head, patch_sha = act_diff(project, act, base_sha)
    except GateRefused as e:
        out["reasons"].append(str(e))
        return out
    out["head"], out["patch_sha"] = head, patch_sha
    # СКОУП (спека п.7): заявлены файлы — диф обязан в них уложиться.
    # fnmatch: заявка может быть маской (dir/*.py). Не заявлен — гейт
    # честно пишет «не заявлен», сверки нет (правило 8.5 наоборот).
    declared = st["open"].get("files")
    if declared:
        import fnmatch
        touched = sorted(_diff_paths(project, base_sha, head))
        stray = [p2 for p2 in touched
                 if not any(fnmatch.fnmatch(p2, pat) for pat in declared)]
        out["scope"] = (f"соблюдён ({len(touched)} файл(ов) в "
                        f"{len(declared)} заявленных)")
        if stray:
            out["scope"] = "нарушен: " + ", ".join(stray[:5])
            out["reasons"].append(
                "диф вышел за заявленный скоуп: " + ", ".join(stray[:5])
                + " — либо доправьте, либо откройте акт с честным скоупом")
    else:
        out["scope"] = "не заявлен"
    # Ветка приёмки — ИЗ ИНТЕНТА, по имени ref. HEAD checkout не
    # годится: detached или переключение Автора двинули бы не ту ветку
    # (нашли codex, grok, deepseek). Старый интент без ветки — отказ.
    branch = st["open"].get("branch")
    if not branch:
        out["reasons"].append("интент не назвал ветку базы (акт открыт "
                              "до этой версии гейта) — примите руками")
        return out
    out["branch"] = branch
    main_now, err = _git(project, "rev-parse", f"refs/heads/{branch}")
    if main_now is None:
        out["reasons"].append(f"ветка {branch} не прочитана: {err}")
        return out
    out["main_now"] = main_now.strip()
    try:
        moved = seal_probe(project, branch)
        if moved:
            out["seal"] = moved
    except Exception as e:               # noqa: BLE001
        print(f"seal_probe: {e}", file=sys.stderr)
    if out["main_now"] != base_sha:
        out["stale_base"] = True
        out["reasons"].append(
            f"main уехал ({base_sha[:12]} → {out['main_now'][:12]}): "
            f"нужен rebase ветки — гейт сделает его сам командой "
            f"rebase/кнопкой «Принять» (спека п.7), затем ревизия дельты")
    # ПОСЛЕДНИЙ вердикт голоса на этом sha побеждает: «REFUTED
    # блокирует до разбора» (спека п.6) — разбор случился, голос
    # передумал, и вечный отказ держал бы гейт после снятого возражения
    # (поймано первым живым прогоном: отказ был артефактом постановки).
    # reviews в act_state лежат свежие ПЕРВЫМИ — setdefault берёт
    # самое свежее мнение каждого голоса.
    last: dict = {}
    for r in st["reviews"]:
        if r.get("voice") == out["voice"]:
            continue                     # исполнитель себя не одобряет
        if r.get("sha") == head and r.get("verdict") in ("approve",
                                                         "refuted"):
            last.setdefault(r.get("voice"), r.get("verdict"))
    out["approvals"] = sorted(v for v, d in last.items() if d == "approve")
    out["refuted_by"] = sorted(v for v, d in last.items() if d == "refuted")
    refuted = out["refuted_by"]
    if refuted:
        out["reasons"].append("есть ОТКАЗ на этот sha: "
                              + ", ".join(out["refuted_by"])
                              + " — доправьте и ревизуйте дельту")
    if len(out["approvals"]) < QUORUM:
        out["reasons"].append(
            f"одобрений на {head[:12]}: {len(out['approvals'])} "
            f"(нужно ≥{QUORUM}); ревизии на старые sha не считаются")
    out["ok"] = not out["reasons"]
    return out


def _diff_paths(project: Path, a: str, b: str) -> set:
    """Пути файлов дифа a..b: их «изменённость» после move ref —
    ожидаемый артефакт самого merge, не рука Автора."""
    out, _ = _git(project, "diff", "--name-only", f"{a}..{b}")
    return {p for p in (out or "").split() if p}


def rebase_act(act: str, *, post=None) -> dict:
    """Перенести ветку акта на уехавший main (спека п.7: «сдвиг main →
    rebase → ревизия дельты»). МЕХАНИКА, не вердикт: гейт не решает,
    хороша ли правка, — он выравнивает базу и честно объявляет, что
    старые одобрения сгорели (head сменился — они гаснут сами).

    Конфликт — rebase --abort и честный отказ: разруливать смысловые
    столкновения — работа исполнителя или Автора, не гейта.
    После успеха base_sha акта в ленте УСТАРЕВАЕТ — событие edit_rebase
    несёт новый base и обе головы; checks() читает его поверх интента.
    """
    post = post or edits.post
    st = act_state(act)
    if not st["open"]:
        raise GateRefused(f"акт {act} неизвестен ленте")
    if st.get("merge"):
        raise GateRefused("акт уже принят")
    if leases.is_held(act):
        raise GateRefused("аренда держится — сперва пусть исполнитель "
                          "закончит")
    if not st["close"] and not st["adopt"]:
        raise GateRefused("rebase — после закрытия акта (или adopt)")
    project = Path(st["open"].get("project", ""))
    branch = st["open"].get("branch")
    if not branch:
        raise GateRefused("интент не назвал ветку базы")
    wt = Path(st["open"].get("worktree", ""))
    if not wt.is_dir():
        raise GateRefused(f"worktree акта не найден ({wt}) — rebase "
                          f"негде делать; перенесите руками")
    with _gate_lock(project) as lk:
        # Под замком гейта: два «Принять» в гонке иначе гнали два
        # rebase в одном дереве (нашли grok и codex).
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        # Дерево обязано СТОЯТЬ на ветке акта: после detached/switch
        # rebase переносил бы чужой HEAD и писал фиктивное событие
        # (нашёл codex).
        on_branch, err = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        if on_branch is None or on_branch.strip() != f"act/{act}":
            raise GateRefused(f"worktree стоит не на act/{act} "
                              f"({(on_branch or '').strip() or err}) — "
                              f"верните ветку и повторите")
        # Хвост прошлого вылета: недоигранный rebase чистится ЯВНО и
        # называется, а не маскируется под «конфликт» (нашёл deepseek).
        gd, _e = _git(wt, "rev-parse", "--git-path", "rebase-merge")
        if gd and (wt / gd.strip()).exists():
            _git(wt, "rebase", "--abort")
            print("прошлый rebase был не доигран — снят --abort",
                  file=sys.stderr)
        st_wt, _e = _git(wt, "status", "--porcelain", "-uno")
        if st_wt is None or st_wt.strip():
            # git отказывает грязному дереву ДО переноса, и прежний
            # текст «конфликтует» дезинформировал (нашёл gemini).
            raise GateRefused("в worktree незакоммиченное — rebase не "
                              "начат; закоммитьте или уберите своё")
        old_head = branch_head(project, act)
        new_base, err = _git(project, "rev-parse", f"refs/heads/{branch}")
        if new_base is None:
            raise GateRefused(f"ветка {branch} не прочитана: {err}")
        new_base = new_base.strip()
        cur_base = _effective_base(st)
        if new_base == cur_base:
            raise GateRefused("база не уезжала — rebase не нужен")
        # identity гейта: rebase переписывает коммиты, без user.name
        # git отказывает на машинах без глобального конфига. АВТОРЫ
        # коммитов сохраняются — подписывается только committer.
        out, err = _git(wt, "-c", "user.name=roundtable-gate",
                        "-c", "user.email=gate@roundtable.local",
                        "rebase", new_base, timeout=300)
        if out is None:
            _git(wt, "rebase", "--abort")
            raise GateRefused(f"rebase не прошёл — гейт откатил "
                              f"(--abort); подробности: {err}")
        new_head = branch_head(project, act)
    fields = dict(act=act, epoch=st["open"].get("epoch"),
                  base_sha=new_base, old_base_sha=cur_base,
                  old_head=old_head, head=new_head, branch=branch)
    text = (f"правка {act}: ветка перенесена на {branch} "
            f"({cur_base[:12]} → {new_base[:12]}); прежние одобрения "
            f"сгорели вместе со старой головой — нужна ревизия "
            f"дельты {old_head[:12]}..{new_head[:12]}")
    try:
        return post("edit_rebase", text, **fields)
    except Exception as e:               # noqa: BLE001
        # Ветка УЖЕ переписана — молчать нельзя: лента считала бы базу
        # старой вечно (нашёл codex). Тот же приём, что у close/merge:
        # маркер, который донесёт sweep окна.
        m = edits.marker_path(act, st["open"].get("epoch") or 0, "rebase")
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(json.dumps(dict(fields, text=text),
                                ensure_ascii=False), encoding="utf-8")
        print(f"лента недоступна ({e}); rebase записан маркером {m.name}",
              file=sys.stderr)
        return dict(fields, kind="edit_rebase", text=text, marker=str(m))


def _effective_base(st: dict) -> str:
    """База акта с учётом edit_rebase: свежий rebase главнее интента.
    Правило 9 в миниатюре: интент неизменяем, уточнение — новой записью,
    и читается она поверх исходной."""
    if st.get("rebase"):
        return st["rebase"].get("base_sha") or ""
    return st["open"].get("base_sha", "")


# ── Пломба ветки (спека п.5) ─────────────────────────────────────────
# Обнаружение, НЕ запрет (вердикт право-записи-v1): под одним uid ref
# переписываем кем угодно. Пломба — файл с последним sha, который видел
# гейт; расхождение при следующей проверке называется В ЛЕНТУ одним
# событием на сдвиг. Ручные коммиты Автора и Клода мимо гейта ЛЕГАЛЬНЫ
# (канонный путь review-gate живёт рядом) — поэтому событие seal_note
# нейтральное: «двинут вне гейта», не «взлом».

def _seal_path(project: Path, branch: str) -> Path:
    h = hashlib.sha256(f"{project}\n{branch}".encode()).hexdigest()[:16]
    return leases.LEASE_DIR / f"seal-{h}.json"


def seal_update(project: Path, branch: str, sha: str, why: str) -> None:
    sp = _seal_path(project, branch)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(sp.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"project": str(project), "branch": branch,
                               "sha": sha, "why": why,
                               "ts": time.time()}, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(sp)


def seal_probe(project: Path, branch: str, *, post=None) -> str | None:
    """Сверить пломбу с фактом; сдвиг мимо гейта — событие в ленту.

    Возвращает текст расхождения или None. Первый вызов на ветке пломбы
    не имеет — он её СТАВИТ молча: до гейта историю двигали легально, и
    кричать о прошлом нечего.
    """
    post = post or edits.post
    cur, err = _git(project, "rev-parse", f"refs/heads/{branch}")
    if cur is None:
        return f"ветка не прочитана: {err}"
    cur = cur.strip()
    sp = _seal_path(project, branch)
    try:
        seal = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seal_update(project, branch, cur, "первая пломба (наблюдение)")
        return None
    if seal.get("sha") == cur:
        return None
    note = (f"пломба {branch} @ {project}: ref двинут ВНЕ гейта "
            f"{seal.get('sha', '')[:12]} → {cur[:12]} (легально для "
            f"ручных коммитов; для актов стола — повод разобраться)")
    try:
        post("seal_note", note, project=str(project), branch=branch,
             old_sha=seal.get("sha"), new_sha=cur)
    except Exception as e:               # noqa: BLE001
        print(f"seal_note не записан: {e}", file=sys.stderr)
    seal_update(project, branch, cur, "сдвиг вне гейта (зафиксирован)")
    return note


def _gate_lock(project: Path):
    leases.LEASE_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return (leases.LEASE_DIR / f"merge-{h}.lock").open("a+")


def merge(act: str, *, blind_open: bool = False, blind_check=None,
          post=None) -> dict:
    """Принять правку в main. Проверки ДО, событие ПОСЛЕ move ref.

    Возвращает событие edit_merge. Любой непорядок — GateRefused,
    ref не двигался.
    """
    post = post or edits.post
    if blind_open or (blind_check and blind_check()):
        # Спека: гейт не принимает, пока открыта слепая фаза — merge
        # менял бы диск под ногами голосов, отвечающих вслепую.
        raise GateRefused("идёт слепая фаза — merge заморожен до её конца")
    st = act_state(act)
    c = checks(act)
    if not c["ok"]:
        raise GateRefused("гейт не пропустил: " + "; ".join(c["reasons"]))
    project = Path(c["project"])
    base_sha, head, patch_sha = c["base_sha"], c["head"], c["patch_sha"]
    voice = c["voice"]
    # task — СЫРОЕ задание (поле события); text события уже несёт
    # префикс «правка … [голос]:», и subject задваивался.
    task = (st["open"].get("task") or st["open"].get("text")
            or "").split("\n")[0]

    with _gate_lock(project) as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        # Повторная сверка ПОД замком: между checks() и flock main мог
        # сдвинуться (второй merge) — CAS ниже поймал бы и это, но
        # честный отказ лучше загадочного.
        c2 = checks(act)
        if not c2["ok"]:
            raise GateRefused("гейт не пропустил (под замком): "
                              + "; ".join(c2["reasons"]))
        if blind_check and blind_check():
            raise GateRefused("идёт слепая фаза — merge заморожен "
                              "(проверено под замком гейта)")
        # ПИН головы: все git-операции ниже — по sha ИЗ ПРОВЕРКИ, не по
        # имени ветки акта. Коммит, добавленный между checks и merge,
        # иначе въезжал БЕЗ ревизии, а patch_sha в истории оставался от
        # старого дифа (нашли codex и grok).
        head, patch_sha = c2["head"], c2["patch_sha"]
        base_sha = c2["base_sha"]
        branch_now = c2["branch"]
        # Чистота рабочей копии снимается ДО move ref: после update-ref
        # git сравнивает дерево уже с НОВЫМ HEAD, и чистая копия всегда
        # выглядит «M по всем файлам правки» — проверка после ref
        # лгала бы «есть незакоммиченное» на пустом месте (поймано
        # прогоном: тест был ложно-зелёным на подстроке «обновлена»).
        # -uno: untracked НЕ грязь для этого решения — reset --hard их
        # не трогает, потерь нет. Живой прогон: __pycache__ от ревизии
        # голоса заморозил бы обновление копии навсегда.
        st0, _ = _git(project, "status", "--porcelain", "-uno")
        clean_before = st0 is not None and not st0.strip()

        trailers = "".join(f"Reviewed-by: {v}\n" for v in c2["approvals"])
        msg = (f"act {act}: {task[:60]}\n\n"
               f"{st['open'].get('text', '')[:1000]}\n\n"
               + ("Adopted-by: arr (из карантина)\n" if c2.get("adopted")
                  else "")
               + trailers
               + f"Act: {act} epoch {st['open'].get('epoch')}\n"
               f"Base-sha: {base_sha}\nPatch-sha: {patch_sha}\n")

        # Merge-коммит строится в detached-worktree: main занят рабочей
        # копией Автора, второй checkout той же ветки git не даст, а
        # трогать рабочую копию до move ref нельзя — истина ref.
        with tempfile.TemporaryDirectory(prefix="gate-") as td:
            out, err = _git(project, "worktree", "add", "--detach",
                            td + "/m", base_sha)
            if out is None:
                raise GateRefused(f"временное дерево: {err}")
            try:
                # merge ПО SHA (пин), автора ставит user.name/email —
                # amend был лишним звеном: хуки и подпись он дёргал
                # второй раз, а author и так берётся из -c (grok).
                out, err = _git(Path(td) / "m", "-c",
                                f"user.name={voice}",
                                "-c", f"user.email={voice}@roundtable.local",
                                "merge", "--no-ff", head, "-m", msg)
                if out is None:
                    raise GateRefused(f"merge не собрался: {err}")
                result, err = _git(Path(td) / "m", "rev-parse", "HEAD")
                if result is None:
                    raise GateRefused(f"result не прочитан: {err}")
                result = result.strip()
                # ДВИЖЕНИЕ ИСТИНЫ: атомарный CAS — если main уже не
                # base_sha, ref не двигается и гонка проиграна вслух.
                out, err = _git(project, "update-ref",
                                f"refs/heads/{branch_now}", result, base_sha)
                if out is None:
                    raise GateRefused(f"update-ref (CAS): {err}")
            finally:
                _git(project, "worktree", "remove", "--force", td + "/m")

        # Рабочая копия — ПОСЛЕ ref и только если чиста; чистота
        # перепроверяется ВПЛОТНУЮ к reset: между давним снимком и
        # reset Автор мог начать правку, и «грязную не трогаем»
        # превращалось в стирание его работы (нашли все четверо).
        # Проверка теперь versus НОВЫЙ HEAD (=result): трекаемая
        # правка Автора видна в любом случае; файлы, «изменённые»
        # только тем, что ref уехал вперёд, отфильтровать нельзя — но
        # clean_before уже сказал, что ДО ref их не было, а появиться
        # им, кроме рук Автора, неоткуда: сравниваем оба снимка.
        st1, _ = _git(project, "status", "--porcelain", "-uno")
        # Сравниваем ПУТИ, не строки porcelain: буквы статуса после
        # move ref другие (staged M против worktree M), и строковое
        # сравнение видело «руку Автора» в собственном артефакте гейта
        # (поймано прогоном теста).
        moved = _diff_paths(project, base_sha, result)
        dirty_paths = {ln[3:] for ln in (st1 or "").splitlines() if ln}
        if not clean_before:
            wc = ("грязная: НЕ обновлена — закоммитьте/уберите своё и "
                  "выполните git reset --hard " + result[:12])
        elif dirty_paths - moved:
            wc = ("НЕ обновлена: рабочая копия изменилась во время "
                  "приёмки (" + ", ".join(sorted(dirty_paths - moved)[:3])
                  + ") — разберитесь и выполните git reset --hard "
                  + result[:12])
        else:
            ok_r, err = _git(project, "reset", "--hard", result)
            wc = ("обновлена (reset --hard)" if ok_r is not None
                  else f"НЕ обновлена: {err}")
        # Пломба — ЕЩЁ ПОД замком гейта: снаружи probe успевал в щель
        # между update-ref и записью пломбы и называл СВОЙ ЖЕ merge
        # «сдвигом вне гейта» (нашли все четверо).
        seal_update(project, branch_now, result,
                    f"merge акта {act} гейтом")

    fields = dict(act=act, epoch=st["open"].get("epoch"), voice=voice,
                  base_sha=base_sha, patch_sha=patch_sha, result_sha=result,
                  reviewed_by=c2["approvals"], branch=branch_now,
                  scope=c2.get("scope", "не заявлен"),
                  adopted=bool(c2.get("adopted")), worktree_copy=wc)
    text = (f"правка {act} [{voice}] принята в {branch_now}: "
            f"{task[:80]}\nreviewed-by: "
            + ", ".join(c2["approvals"]) + f"; рабочая копия {wc}")
    try:
        return post("edit_merge", text, **fields)
    except Exception as e:               # noqa: BLE001
        # Ref УЖЕ сдвинут — молчать нельзя: маркер рядом с арендами,
        # sweep окна донесёт его в ленту, когда она оживёт (нашёл
        # codex: прежде API падал 500-кой ПОСЛЕ фактического merge, и
        # вечная лента оставалась без события).
        m = edits.marker_path(act, st["open"].get("epoch") or 0, "merge")
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(json.dumps(dict(fields, text=text),
                                ensure_ascii=False), encoding="utf-8")
        print(f"лента недоступна ({e}); merge записан маркером {m.name}",
              file=sys.stderr)
        return dict(fields, kind="edit_merge", text=text,
                    marker=str(m))


def adopt(act: str, *, by: str = "arr", post=None) -> dict:
    """Событие Автора: принять ВЫЛЕТЕВШИЙ акт к рассмотрению (спека
    п.9). Снимает единственное условие — закрытую аренду; ревизия,
    кворум и база остаются в силе."""
    post = post or edits.post
    st = act_state(act)
    if not st["open"]:
        raise GateRefused(f"акт {act} неизвестен ленте")
    if st["close"]:
        raise GateRefused("акт закрыт штатно — adopt не нужен")
    if not st["crash"]:
        raise GateRefused("акт не вылетал (edit_crash нет) — adopt не к чему")
    if leases.is_held(act):
        raise GateRefused("аренда держится — исполнитель ещё работает")
    return post("edit_adopt",
                f"правка {act}: Автор принял вылетевший акт к рассмотрению "
                f"(adopt) — условия ревизии и базы остаются в силе",
                act=act, epoch=st["open"].get("epoch"), by=by)


# ── CLI: окно запускает долгую ревизию через spawn, статусы бесплатно ─
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="merge-гейт стола (этап 3)")
    ap.add_argument("cmd", choices=("review", "review-batch", "merge",
                                    "adopt", "checks", "rebase"))
    ap.add_argument("act", nargs="?", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "review-batch":
            evs = review_batch(a.act.split(",") if a.act else None)
            for e in evs:
                print(f"{e.get('voice'):>9} × {e.get('act')}: "
                      f"{e.get('verdict')} ({e.get('status')})")
            return 0
        if not a.act:
            ap.error("нужен act")
        if a.cmd == "review":
            evs = review(a.act)
            for e in evs:
                print(f"{e.get('voice'):>9}: {e.get('verdict')} "
                      f"({e.get('status')}, {e.get('elapsed_s')} с)")
            return 0
        if a.cmd == "merge":
            print("ВНИМАНИЕ: CLI не видит слепых фаз окна — штатный путь "
                  "приёмки лежит через POST /edit_merge", file=sys.stderr)
            ev = merge(a.act)
            print(f"принято: {ev['result_sha'][:12]} "
                  f"(reviewed-by: {', '.join(ev['reviewed_by'])}); "
                  f"копия {ev['worktree_copy']}")
            return 0
        if a.cmd == "rebase":
            ev = rebase_act(a.act)
            print(f"перенесено: база {ev['base_sha'][:12]}, голова "
                  f"{ev['head'][:12]} — нужна ревизия дельты")
            return 0
        if a.cmd == "adopt":
            adopt(a.act)
            print("adopt записан")
            return 0
        c = checks(a.act)
        print(json.dumps(c, ensure_ascii=False, indent=1))
        return 0 if c["ok"] else 1
    except GateRefused as e:
        print(f"ГЕЙТ: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())


# ── Этап 5: батч-ревизия ─────────────────────────────────────────────
# Наказ Автора (2026-09-01): «поштучно — проверено, доказано, работает;
# преимущество батчей — экономия? предлагаю тумблер». Умолчание везде
# ПОШТУЧНО; батч — явный выбор ради экономии вызовов: N актов = один
# веер вместо N. Цена батча честно названа в подсказке окна: внимание
# ревьюера делится на все дифы сразу, и большие пачки он читает хуже —
# отсюда жёсткие потолки.
BATCH_MAX_ACTS = 4
BATCH_MAX_DIFF = 60_000       # суммарно символов дифа на пачку

_BATCH_VERDICT_RE = re.compile(
    r"ВЕРДИКТ\s+([0-9a-f]{8,32})\s*:\s*(ОДОБРЯЮ|ОТКАЗ)", re.IGNORECASE)


_ACTS_CACHE: dict = {"key": None, "val": None}


def acts_summary(limit: int = 12) -> list[dict]:
    """Лёгкий обзор актов для окна: стадия + счёт ревизий на текущий
    head. ОДИН проход по ленте + rev-parse на закрытый акт; результат
    кэшируется по (размер, mtime) ленты — /state зовёт это каждые 15 с,
    и полный скан на каждый тик вешал бы опрос с ростом журнала (нашли
    все пятеро; kimi ставил на этом ОТКАЗ; прежняя редакция вдобавок
    гоняла act_state() на каждый акт — квадрат).

    stage: opening/working → closed/crashed/adopted → merged. Кнопка
    окна существует только на легальной стадии (идея голоса claude)."""
    live_path = edits.CHOIR / "live.jsonl"
    try:
        st_f = live_path.stat()
        key = (st_f.st_size, st_f.st_mtime_ns, limit)
    except OSError:
        key = None
    if key is not None and _ACTS_CACHE["key"] == key:
        return _ACTS_CACHE["val"]

    opens: dict = {}
    closed, crashed, adopted, merged, rebased = set(), set(), {}, {}, {}
    reviews: dict = {}
    order: list = []
    for e in edits._feed_events(None):   # свежие первыми, один проход
        if e.get("author") not in edits._CLOSE_AUTHORS:
            continue
        k, act = e.get("kind"), e.get("act")
        if not act:
            continue
        if k == "edit_open" and act not in opens:
            opens[act] = e
            order.append(act)
        elif k == "edit_close":
            closed.add(act)
        elif k == "edit_crash":
            crashed.add(act)
        elif k == "edit_adopt" and act not in adopted:
            adopted[act] = e
        elif k == "edit_merge" and act not in merged:
            merged[act] = e
        elif k == "edit_review":
            reviews.setdefault(act, []).append(e)

    out = []
    for act in order[:limit]:
        e = opens[act]
        row = {"act": act, "voice": e.get("voice"),
               "seat": e.get("seat"), "task": (e.get("task") or "")[:80],
               "project": e.get("project")}
        if act in merged:
            row["stage"] = "merged"
            row["result"] = (merged[act].get("result_sha") or "")[:12]
        elif act in crashed and act in adopted:
            row["stage"] = "adopted"
        elif act in crashed:
            row["stage"] = "crashed"
        elif act in closed:
            row["stage"] = "closed"
        else:
            row["stage"] = ("working" if leases.is_held(act)
                            else "opening")
        if row["stage"] in ("closed", "adopted") and e.get("project"):
            try:
                head = branch_head(Path(e["project"]), act)
                last = {}
                for r in reviews.get(act, []):   # свежие первыми
                    if (r.get("sha") == head
                            and r.get("voice") != row["voice"]
                            and r.get("verdict") in ("approve", "refuted")):
                        last.setdefault(r.get("voice"), r.get("verdict"))
                row["approvals"] = sorted(v for v, d in last.items()
                                          if d == "approve")
                row["refused"] = sorted(v for v, d in last.items()
                                        if d == "refuted")
                row["quorum"] = QUORUM
            except GateRefused:
                row["stage"] = "lost"    # ветка act/… исчезла из репо
        out.append(row)
    if key is not None:
        _ACTS_CACHE["key"], _ACTS_CACHE["val"] = key, out
    return out


def pending_acts() -> list[str]:
    """Акты, ждущие ревизии: закрытые (или adopt) и не принятые."""
    out = []
    seen = set()
    for e in edits._feed_events(None):
        if e.get("author") not in edits._CLOSE_AUTHORS:
            continue
        act = e.get("act")
        if not act or act in seen or e.get("kind") != "edit_open":
            continue
        seen.add(act)
        st = act_state(act)
        if st.get("merge") or leases.is_held(act):
            continue
        if st["close"] or st["adopt"]:
            out.append(act)
    return out


def _parse_batch_verdicts(text: str, acts: list[str]) -> dict:
    """Вердикт по каждому акту. Противоречие по акту → unclear;
    безадресный «ВЕРДИКТ: …» в батче не считается ничьим."""
    got: dict = {}
    for m in _BATCH_VERDICT_RE.finditer(text):
        act_pref, word = m.group(1).lower(), m.group(2).upper()
        kind = "approve" if "ОДОБР" in word else "refuted"
        # Только «акт начинается с написанного», минимум 8 знаков, и
        # ОДНОЗНАЧНО: двусторонний startswith матчил чужой id с теми же
        # первыми знаками, и вердикт уезжал не тому акту (нашли codex и
        # gemini). Опечатка ревьюера не должна открывать чужой merge.
        if len(act_pref) < 8:
            continue
        hit = [a for a in acts if a.startswith(act_pref)]
        if len(hit) == 1:
            got.setdefault(hit[0], set()).add(kind)
    return {a: (ks.pop() if len(ks := got.get(a, set())) == 1 else "unclear")
            for a in acts}


def review_batch(acts: list[str] | None = None, *, reviewers=None,
                 timeout: int = REVIEW_TIMEOUT, post=None) -> list[dict]:
    """Одна рассылка на ПАЧКУ актов; события edit_review — на каждый.

    Каждое событие несёт batch=True и то же поле sha, что поштучная
    ревизия, — гейт приёмки не различает происхождение одобрений, и это
    намеренно: условия merge одни для обоих путей.
    """
    post = post or edits.post
    explicit = acts is not None
    acts = list(acts) if acts else pending_acts()
    if not acts:
        raise GateRefused("нет актов, ждущих ревизии")
    skipped = []
    if len(acts) > BATCH_MAX_ACTS:
        if explicit:
            raise GateRefused(f"в пачке {len(acts)} актов — потолок "
                              f"{BATCH_MAX_ACTS}: внимание ревьюера не "
                              f"резина; лишние — следующей пачкой")
        # Автопачка режет себя сама: раньше пять ждущих актов делали
        # кнопку «батчем» вечно отказывающей, а подсказка «лишние
        # следующей пачкой» была невыполнима из окна (codex, gemini).
        acts, skipped = acts[:BATCH_MAX_ACTS], acts[BATCH_MAX_ACTS:]
        print(f"пачка урезана до {BATCH_MAX_ACTS}: {', '.join(acts)}; "
              f"ждут следующей: {', '.join(skipped)}", file=sys.stderr)
    bundle, executors, heads = [], set(), {}
    projects = set()
    total = 0
    for act in acts:
        st = act_state(act)
        if not st["open"]:
            raise GateRefused(f"{act}: edit_open нет")
        if st.get("merge"):
            raise GateRefused(f"{act}: уже принят")
        if not st["close"] and not st["adopt"]:
            raise GateRefused(f"{act}: не закрыт и не adopt")
        project = Path(st["open"].get("project", ""))
        diff, head, patch_sha = act_diff(project, act,
                                         _effective_base(st))
        if not diff.strip():
            raise GateRefused(f"{act}: диф пуст")
        total += len(diff)
        projects.add(str(project))
        executors.add(st["open"].get("voice"))
        heads[act] = (head, patch_sha, st)
        bundle.append(
            f"═══ АКТ {act} (исполнитель {st['open'].get('voice')}, "
            f"голова {head[:12]}) ═══\n"
            f"ЗАДАНИЕ: {st['open'].get('task') or st['open'].get('text')}\n"
            f"── diff ──\n{diff}")
    if len(projects) > 1:
        # cwd веера один; ревьюеры чужого проекта работали бы в чужом
        # каталоге, и относительные пути ломались молча (нашёл deepseek).
        raise GateRefused("пачка — в пределах ОДНОГО проекта: "
                          + ", ".join(sorted(projects)))
    if total > BATCH_MAX_DIFF:
        raise GateRefused(f"суммарный диф пачки {total} символов — потолок "
                          f"{BATCH_MAX_DIFF}: большие правки только "
                          f"поштучно (наказ: код читают глазами)")

    joint = f"""РЕВИЗИЯ ПАЧКИ ПРАВОК ({len(acts)} акта/актов) за столом.

По КАЖДОМУ акту дайте ОТДЕЛЬНЫЙ вердикт строкой вида:
ВЕРДИКТ {acts[0][:12]}: ОДОБРЯЮ
ВЕРДИКТ <act>: ОТКАЗ
(идентификатор акта обязателен — безадресный вердикт не считается;
не цитируйте оба варианта в одной строке). После вердиктов — по 2–5
строк на акт: что проверили; при отказе — что чинить. Найденный дефект
без отказа не бывает.

УСТРОЙСТВО: правки живут в ветках act/<id>; рабочая копия проекта до
приёмки стоит на базе — сверяйте содержимое ветки (git show <sha>:путь),
а не файл на диске. Голоса с диском могут открыть живые файлы.

{chr(10).join(bundle)}"""

    # Исполнители НЕ выбрасываются из веера: они судят ЧУЖИЕ акты
    # пачки (адресный скип своего — ниже). Прежнее исключение всех
    # исполнителей при 4 разных актах оставляло пул пустым, и
    # оплаченный батч не мог набрать кворум в принципе (нашли codex и
    # kimi).
    picked = dict(reviewers or REVIEWERS)
    pf = Path(tempfile.gettempdir()) / (
        f"edit-review-batch-{os.getpid()}-"
        f"{time.monotonic_ns() & 0xffffff:x}.md")
    pf.write_text(joint, encoding="utf-8")

    # тот же вызов, что у поштучной: собираем через её machinery
    events = []
    try:
        results = _run_reviewers(picked, pf, timeout,
                                 cwd=str(Path(heads[acts[0]][2]["open"]
                                              .get("project", "."))))
        for name, res in results:
            per = (_parse_batch_verdicts(res.get("text", ""), acts)
                   if res["status"] == "ok" else {a: "absent" for a in acts})
            full = res.get("text", "")
            if len(full) > 20000:
                full = full[:20000] + f"\n[…обрезано гейтом, было " \
                                      f"{len(res.get('text', ''))} символов]"
            for act in acts:
                head, patch_sha, st = heads[act]
                if st["open"].get("voice") == name:
                    continue             # исполнитель своё не судит
                events.append(post(
                    "edit_review",
                    f"ревизия {act} [{name}, батч {len(acts)}]: "
                    + (per[act] if res["status"] == "ok"
                       else f"({res['status']}) {res.get('detail', '')}"),
                    act=act, voice=name, sha=head, patch_sha=patch_sha,
                    verdict=per[act], status=res["status"],
                    elapsed_s=res.get("elapsed_s"), batch=True,
                    eyes=REVIEWER_EYES.get(name, "files"),
                    seat=REVIEWER_SEATS.get(name, name),
                    batch_acts=acts, full_text=full))
    finally:
        pf.unlink(missing_ok=True)
    return events
