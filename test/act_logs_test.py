#!/usr/bin/env python3
"""Стенограммы актов: хранение бессрочно, уборка только по явному сроку,
архив как запасной источник, чтение большого файла с начала (head).

Повод (Автор, 2026-10-06): уборка окна по возрасту (14 суток) стёрла
стенограммы 94 из 95 актов окна RoundTable — список актов из ленты цел,
внутри пусто. Тест стоит на изолированных каталогах: CHOIR_RT_ACTS и
CHOIR_RT_ACTS_ARCHIVE под временной папкой, настоящий ~/.cache не трогается.
"""
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="rt-actlogs."))
os.environ.update(ROUNDTABLE_CHAMBER=str(T / "chamber"), ROUNDTABLE_JOURNAL=str(T / "journal"),
                  CHOIR_RT_ACTS=str(T / "acts"), CHOIR_RT_ACTS_ARCHIVE=str(T / "acts-archive"),
                  CHOIR_LEASE_DIR=str(T / "leases"), CHOIR_WT_DIR=str(T / "wts"),
                  CHOIR_RT_NO_DISCOVERY="1", CHOIR_RT_MODELS=str(T / "models.json"),
                  CHOIR_RT_VOICES=str(T / "voices.json"), CHOIR_RT_WINDOWS=str(T / "windows"),
                  CHOIR_RT_NO_AUTOREVIEW="1", CHOIR_RT_NO_BACKUP="1", CHOIR_BACKUP_DIR=str(T / "backup"),
                  CHOIR_JEV="0", CHOIR_RT_NO_BWRAP="1", CHOIR_ROOM=str(T / "journal" / "room.jsonl"))
os.environ.pop("CHOIR_RT_ACTS_KEEP_DAYS", None)
os.environ.pop("CHOIR_RT_SEC_TOKEN", None)            # токен — из файла реестра стенда, не из среды
RT = Path(__file__).resolve().parent.parent
for d in ("chamber", "journal"):
    (T / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(RT))
sys.path.insert(0, str(RT / "chamber"))
import roundtable as rt   # noqa: E402

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"PASS  {name}")
    else:
        FAIL += 1; print(f"FAIL  {name}")


ACTS, ARCH = rt.ACT_DIR, rt.ACT_ARCHIVE_DIR
ACTS.mkdir(parents=True, exist_ok=True); ARCH.mkdir(parents=True, exist_ok=True)
check(ACTS == T / "acts" and ARCH == T / "acts-archive", "каталоги актов и архива — из переменных стенда")

# ── срок хранения: умолчание 0, кривые значения → 0 без трассы ──
check(rt.ACT_KEEP_DAYS == 0, "ACT_KEEP_DAYS без переменной = 0 (бессрочно)")
check(rt._keep_days("14d") == 0 and rt._keep_days("-3") == 0 and rt._keep_days(" ") == 0,
      "_keep_days: не целое, отрицательное, пустое → 0 (предупреждение, не трасса)")
check(rt._keep_days("14") == 14 and rt._keep_days(" 7 ") == 7, "_keep_days: целое разбирается")

# ── уборка: при 0 ничего не трогает, при >0 режет старое, живое и архив — нет ──
old_t = time.time() - 40 * 86400
for name in ("old1.log", "old1.raw.log", "live9.log", "fresh.raw.log"):
    (ACTS / name).write_text("x\n", encoding="utf-8")
for name in ("old1.log", "old1.raw.log", "live9.log"):
    os.utime(ACTS / name, (old_t, old_t))
(ARCH / "arch1.raw.log").write_text("архив\n", encoding="utf-8")
os.utime(ARCH / "arch1.raw.log", (old_t, old_t))
check(rt._sweep_act_logs({"live9"}) == 0 and sorted(p.name for p in ACTS.glob("*.log")) ==
      ["fresh.raw.log", "live9.log", "old1.log", "old1.raw.log"],
      "уборка при ACT_KEEP_DAYS=0: старые логи (40 суток) не стёрты")
rt.ACT_KEEP_DAYS = 14
try:
    gone = rt._sweep_act_logs({"live9"})
    left = sorted(p.name for p in ACTS.glob("*.log"))
    check(gone == 2 and left == ["fresh.raw.log", "live9.log"],
          "уборка при ACT_KEEP_DAYS=14: старые стёрты, живой акт и свежий лог остались")
    check((ARCH / "arch1.raw.log").exists(), "уборка не трогает архив")
finally:
    rt.ACT_KEEP_DAYS = 0

# ── архив как запасной источник, acts сильнее архива ──
check(rt._act_log_path("arch1", "raw") == (ARCH / "arch1.raw.log", "raw"),
      "_act_log_path: стенограмма из архива, когда в acts её нет")
(ARCH / "arch2.log").write_text("лог\n", encoding="utf-8")
check(rt._act_log_path("arch2", "act") == (ARCH / "arch2.log", "act"), "_act_log_path: лог акта из архива")
(ARCH / "arch3.chair.log").write_text("кресло\n", encoding="utf-8")
check(rt._act_log_path("arch3", "chair") == (ARCH / "arch3.chair.log", "chair"), "_act_log_path: стенограф кресла из архива")
(ACTS / "arch1.raw.log").write_text("свежий\n", encoding="utf-8")
check(rt._act_log_path("arch1", "raw") == (ACTS / "arch1.raw.log", "raw"), "_act_log_path: acts сильнее архива")
check(rt._act_log_path("nope00", "raw") == (None, ""), "_act_log_path: нет ни в acts, ни в архиве → None")

# ── head: большой файл читается с начала порциями; без head — хвост ──
big = ACTS / "big1.raw.log"
big.write_bytes(b"".join(f"строка {i:06d}\n".encode() for i in range(40000)))   # ~600 КБ
size = big.stat().st_size
tail = rt._read_log_tail(big, 0)
check(tail["skipped"] is True and tail["next"] <= size and not tail["text"].startswith("строка 000000"),
      "без head: первое чтение — хвост, skipped")
head = rt._read_log_tail(big, 0, head=True)
check(head["skipped"] is False and head["text"].startswith("строка 000000\n") and head["more"] is True
      and 0 < head["next"] <= rt.ACT_LOG_CHUNK, "head: голова не пропущена, порция ≤ 64 КиБ, more")
# дочитывание до конца по next — целые строки, без потерь и дублей
pos, got, pages = 0, [], 0
while True:
    r = rt._read_log_tail(big, pos, head=True)
    got.append(r["text"]); pages += 1
    if not r["more"] or r["next"] == pos or pages > 100:
        break
    pos = r["next"]
text = "".join(got)
check(text.count("\n") == 40000 and text.endswith("строка 039999\n") and pages > 1,
      f"head: дочитано целиком по next ({pages} порций), строки не потеряны и не удвоены")
reset = rt._read_log_tail(big, size + 10, head=True)
check(reset["reset"] is True and reset["text"].startswith("строка 000000\n"), "head + смещение больше размера → reset и чтение с начала")
alive = rt._read_log_tail(big, 0, alive=True, head=True)
check(alive["text"].endswith("\n") and alive["skipped"] is False, "head у живого акта: порция режется по границе строки")


# ── файла нет — «стенограмма» из ленты (наказ Автора 2026-10-08) ──
import json as _json
import live as _live
_live.LIVE.parent.mkdir(parents=True, exist_ok=True)
_live.post("roundtable", "act_status", "act abcd1234 принят: round: r-lost [авто]", act_id="abcd1234", status="accepted")
_live.post("grok", "say", "ответ грока по акту", act="abcd1234")
_live.post("kimi", "say", "старая реплика без поля act — между принят и done")      # как до 2026-10-08
_live.post("roundtable", "act_status", "act abcd1234 done: round: r-lost", act_id="abcd1234", status="done")
_live.post("deepseek", "say", "реплика после конца акта — не его")
os.environ["RT_ACT_ID"] = "feedonly1"
_e_st = _live.post("grok", "say", "реплика из акта окна")
del os.environ["RT_ACT_ID"]
check(_e_st.get("act") == "feedonly1", "live.post: событие ребёнка акта несёт act из RT_ACT_ID")
(T / "journal" / "room.jsonl").write_text("\n".join(_json.dumps(x, ensure_ascii=False) for x in (
    {"id": "r1", "ts": "2026-10-01T10:00:00+00:00", "round": "r-lost", "phase": "blind", "role": "answer", "voice": "kimi", "status": "ok", "text": "слепой ответ кими"},
    {"id": "r2", "ts": "2026-10-01T10:01:00+00:00", "round": "other", "phase": "blind", "role": "answer", "voice": "kimi", "status": "ok", "text": "чужой раунд"})) + "\n", encoding="utf-8")
txt = rt._act_from_feed("abcd1234")
check(txt.startswith("⚠ СТЕНОГРАММЫ АКТА abcd1234 НЕТ") and "ответ грока по акту" in txt and "accepted" in txt and "done" in txt,
      "_act_from_feed: шапка называет источник лентой, события акта с текстом и статусами")
check("старая реплика без поля act" in txt and "(по времени)" in txt and "по времени между «принят»" in txt and "реплика после конца акта" not in txt,
      "_act_from_feed: старые реплики без act — по времени между «принят» и done, с пометкой; после конца — нет")
check(rt._act_from_feed_cached("abcd1234") == txt and rt._act_from_feed_cached("abcd1234") == txt and "abcd1234" in rt._FEED_ACT_CACHE,
      "_act_from_feed_cached: кэш по отпечатку ленты")
check("РАУНД «r-lost»" in txt and "слепой ответ кими" in txt and "чужой раунд" not in txt,
      "_act_from_feed: у раунда — записи room.jsonl этого раунда, чужие нет")
check(rt._act_from_feed("ffffffff") == "", "_act_from_feed: нет событий — пусто (404 у /act_log)")

# ── токен кресла постоянный: файл реестра, 0600, тот же при повторном чтении ──
tok1 = rt._sec_token(); tokf = rt._sec_token_file()
check(len(tok1) == 32 and tokf.exists() and oct(tokf.stat().st_mode & 0o777) == "0o600", "_sec_token: создан файл реестра 0600")
check(rt._sec_token() == tok1 and tokf.read_text().strip() == tok1, "_sec_token: повторное чтение — тот же токен (переживает перезапуск)")
tokf.write_text("мусор\n")
check(rt._sec_token() != "мусор" and len(rt._sec_token()) == 32, "_sec_token: кривой файл → новый токен записан")

# ── сторож объёма: заметка при переходе порога, один раз; снятие при спаде ──
rt.ACTS_SIZE_LIMIT = 10
notes = []
rt.feed_append = lambda kind, text, **kw: notes.append((kind, text))
check(rt._acts_size_watch() is True and len(notes) == 1 and "больше" in notes[0][1] and "📦" in notes[0][1], "размер стенограмм > порога → одна заметка в ленту")
check(rt._acts_size_watch() is False and len(notes) == 1, "повторная проверка — молчит (метка)")
rt.ACTS_SIZE_LIMIT = 10 ** 12
check(rt._acts_size_watch() is False and not (rt.ACT_DIR / ".size-notified").exists(), "объём ниже порога — метка снята")
print(f"\nact_logs: PASS {PASS} · FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
