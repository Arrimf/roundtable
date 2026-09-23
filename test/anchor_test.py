#!/usr/bin/env python3
"""«Продолжить отсюда»: адрес → текст из журнала (сервер, не клиент),
отказы вместо обрезки, блок с именем и без, поле anchor, свежая сессия
голоса на первом ходе, автор якоря вне жребия и свода, мягкий сигнал
о монологе. Живых голосов не зовёт, живых журналов не трогает."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

T = Path(tempfile.mkdtemp(prefix="anchor."))
J = T / "journal"
J.mkdir(parents=True)
os.environ.update(ROUNDTABLE_JOURNAL=str(J), CHOIR_JOURNAL=str(J),
                  CHOIR_ROOM=str(J / "room.jsonl"), CHOIR_RT_NO_BWRAP="1",
                  CHOIR_CANARY_HOME=str(T / "canary"))
os.environ.pop("CHOIR_DEEPSEEK_MEMORY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chamber"))
import anchor                                   # noqa: E402
import live                                     # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS  " if cond else "FAIL  ") + name)
    PASS += bool(cond)
    FAIL += not cond


def err(addr, **kw):
    try:
        anchor.resolve(addr, live_path=live.LIVE, room_path=Path(os.environ["CHOIR_ROOM"]))
    except anchor.AnchorError as e:
        return str(e)
    return None


live.LIVE.parent.mkdir(parents=True, exist_ok=True)
e_q = live.post("arr", "say", "вопрос номер один")
e_a = live.post("claude", "say", "ответ клода: тезис про сессии")
e_p = live.post("grok", "pass", "ПАС")
e_e = live.post("kimi", "error", "", detail="quota")
e_n = live.post("chamber", "note", "слово → claude")
e_long = live.post("codex", "say", "x" * (anchor.ANCHOR_CAP + 1))
e_v = live.post("codex", "verdict", "сверил: расхождений нет")

# ── разрешение адреса ─────────────────────────────────────────────
a = anchor.resolve(f"live:{e_a['id']}", live_path=live.LIVE, room_path=Path("/nonexistent"))
check("live:<id> → текст, автор, sha, род", a["author"] == "claude" and a["text"] == e_a["text"]
      and a["kind"] == "say" and len(a["sha"]) == 16)
check("вердикт контролёра — якорь", anchor.resolve(f"live:{e_v['id']}", live_path=live.LIVE,
                                                    room_path=Path("/x"))["kind"] == "verdict")
check("реплика Автора — якорь", anchor.resolve(f"live:{e_q['id']}", live_path=live.LIVE,
                                                 room_path=Path("/x"))["author"] == "arr")
check("ПАС — отказ словами", "ПАС" in (err(f"live:{e_p['id']}") or ""))
check("отказ канала — отказ", "отказ канала" in (err(f"live:{e_e['id']}") or ""))
check("служебная заметка — отказ", "не реплика" in (err(f"live:{e_n['id']}") or ""))
check("нет такого события — отказ", "в ленте нет" in (err("live:999999") or ""))
check("кривой адрес — отказ", err("foo:1") and err("live:abc") and err("") and err("live:1; rm"))
m = err(f"live:{e_long['id']}")
check("сверх потолка — ОТКАЗ, не обрезка (решение стола 4:1, потолок Автора 6000)",
      m and "обрезки нет" in m and str(anchor.ANCHOR_CAP) in m and anchor.ANCHOR_CAP == 6000)

# room.jsonl: ответы раунда
ROOM = Path(os.environ["CHOIR_ROOM"])
recs = [
    {"id": "aaaaaaaaaaa1", "round": "r1", "phase": "blind", "role": "answer", "voice": "grok",
     "status": "ok", "text": "слепой ответ грока"},
    {"id": "aaaaaaaaaaa2", "round": "r1", "phase": "blind", "role": "answer", "voice": "kimi",
     "status": "pass", "text": "ПАС"},
    {"id": "aaaaaaaaaaa3", "round": "r1", "phase": "rebut1", "role": "answer", "voice": "codex",
     "status": "ok", "text": "Голос В прав в том, что…"},
    {"id": "aaaaaaaaaaa4", "round": "r1", "phase": "seed", "role": "seed", "voice": "choir",
     "status": "ok", "text": "затравка"},
    {"id": "aaaaaaaaaaa5", "round": "r1", "phase": "summary", "role": "summary", "voice": "claude",
     "status": "ok", "text": "свод раунда"},
    {"id": "aaaaaaaaaaa6", "round": "r1", "phase": "blind", "role": "answer", "voice": "deepseek",
     "status": "timeout", "text": ""},
]
with ROOM.open("w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
r1 = anchor.resolve("room:aaaaaaaaaaa1", live_path=live.LIVE, room_path=ROOM)
check("room:<id> ответ ok → автор, раунд, фаза", r1["author"] == "grok" and r1["round"] == "r1"
      and r1["phase"] == "blind" and not r1.get("labels_note"))
r3 = anchor.resolve("room:aaaaaaaaaaa3", live_path=live.LIVE, room_path=ROOM)
check("ответ витка помечен: метки «Голос В» живут внутри раунда", r3.get("labels_note") is True
      and "расшифровки нет" in anchor.block(r3, with_author=True))
check("свод ok — якорь", anchor.resolve("room:aaaaaaaaaaa5", live_path=live.LIVE, room_path=ROOM)["kind"] == "summary")
check("ПАС раунда — отказ", "ПАС" in (err("room:aaaaaaaaaaa2") or ""))
check("затравка — отказ", "не ответ и не свод" in (err("room:aaaaaaaaaaa4") or ""))
check("timeout — отказ", "timeout" in (err("room:aaaaaaaaaaa6") or ""))
check("нет записи — отказ", "нет" in (err("room:bbbbbbbbbbb1") or ""))

# формат строки иной (id не первый ключ) — полный разбор всё равно находит
with live.LIVE.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": "2026-09-23T00:00:00+00:00", "author": "grok", "kind": "say",
                        "text": "нестандартный порядок ключей", "id": 424242}, ensure_ascii=False) + "\n")
check("событие с иным порядком ключей находится полным разбором (deepseek)",
      anchor.resolve("live:424242", live_path=live.LIVE, room_path=ROOM)["author"] == "grok")

# ── блок и поле ───────────────────────────────────────────────────
b1 = anchor.block(a, with_author=True)
b0 = anchor.block(a, with_author=False)
check("блок с именем: «Голос claude», текст дословно, sha, мысли названы",
      "СООБЩЕНИЕ (Голос claude)" in b1 and e_a["text"] in b1 and a["sha"] in b1
      and "без мыслей модели" in b1)
check("блок без имени (пакет раунда — решение Автора): имени нет, текст тот же",
      "claude" not in b0 and e_a["text"] in b0 and "один из голосов стола" in b0)
check("блок Автора — «Автор (человек)»",
      "СООБЩЕНИЕ (Автор (человек))" in anchor.block(
          anchor.resolve(f"live:{e_q['id']}", live_path=live.LIVE, room_path=ROOM), with_author=True))
f = anchor.field(r1)
check("поле anchor: адрес, автор, sha, род, раунд, фаза — скаляр",
      f == {"addr": "room:aaaaaaaaaaa1", "author": "grok", "sha": r1["sha"], "kind": "answer",
            "round": "r1", "phase": "blind"})
check("поле anchor ленты — без раунда", set(anchor.field(a)) == {"addr", "author", "sha", "kind"})

# ── монолог: мягкий сигнал ────────────────────────────────────────
evs = [{"author": "arr", "kind": "say", "anchor": {"author": "claude"}, "anchor_targets": ["claude"]},
       {"author": "claude", "kind": "say"},
       {"author": "arr", "kind": "say", "anchor": {"author": "claude"}, "anchor_targets": ["claude"]},
       {"author": "claude", "kind": "say"}]
check("цепочка: два прошлых звена + это = 3 → сигнал", anchor.chain_mono(evs, "claude", ["claude"]) == 3
      and anchor.mono_note(3, "claude") and "не запрет" in anchor.mono_note(3, "claude"))
check("адресат другой — цепочка 0", anchor.chain_mono(evs, "claude", ["grok"]) == 0)
check("двое адресатов — не монолог", anchor.chain_mono(evs, "claude", ["claude", "grok"]) == 0)
check("реплика Автора без якоря рвёт цепочку",
      anchor.chain_mono(evs + [{"author": "arr", "kind": "say"}], "claude", ["claude"]) == 1)
check("ниже порога — заметки нет", anchor.mono_note(2, "claude") is None)

# ── комната: say --anchor → событие, свежая сессия, промпт ──────────
seen = {}
sess = {}


def fake_turn(name, prompt, acc=None):
    seen[name] = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
    return {"author": name, "kind": "say", "text": "ок от " + name, "elapsed_s": 0.1}


live.turn = fake_turn
live.available = lambda n: True
live.beacon = lambda: (_ for _ in ()).throw(RuntimeError("офлайн"))
live.post("grok", "say", "реплика ПОСЛЕ якоря — её голос видеть не должен")
live.save_state("claude", {"session": "old-uuid", "cursor": 1, "turns": 3, "cwd": None})
rc = live.cmd_say(argparse.Namespace(text="@claude а если сессия свежая?", voices=None,
                                     anchor=f"live:{e_a['id']}", once=False, prefer=None))
evs = [json.loads(l) for l in live.LIVE.open(encoding="utf-8")]
mine = [e for e in evs if e["author"] == "arr"][-1]
check("событие Автора несёт поле anchor (скаляр) и адресатов хода",
      rc == 0 and mine.get("anchor", {}).get("author") == "claude"
      and mine["anchor"]["sha"] == a["sha"] and mine.get("anchor_targets") == ["claude"])
p = seen.get("claude", "")
check("промпт: устав, блок якоря дословно ПЕРЕД репликой, реплика после",
      live.CHARTER[:30] in p and e_a["text"] in p and "@claude а если" in p
      and p.index(e_a["text"]) < p.index("@claude а если"))
check("промпт: событий после якоря нет (дельта не подаётся)",
      "реплика ПОСЛЕ якоря" not in p and "ПРОДОЛЖЕНИЕ ОТСЮДА" in p)
check("ANCHOR живёт до следующего cmd_*; claude отмечен как сделавший ход «отсюда»",
      live.ANCHOR is not None and "claude" in live._ANCHORED and live.ANCHOR_EV["id"] == mine["id"])
pg = seen.get("grok", "")
check("второй голос акта: тоже «отсюда» — блок якоря, реплика Автора И ответ claude на неё, без событий до",
      "КОНТЕКСТ — сообщение" in pg and "@claude а если" in pg and "ок от claude" in pg
      and "реплика ПОСЛЕ якоря" not in pg)
st_c = live.load_state("claude")
check("состояние claude на диске: session_prev = старый id, session снята, anchor = адрес, origin на реплике Автора",
      st_c.get("session_prev") == "old-uuid" and st_c.get("session") is None
      and st_c.get("anchor") == f"live:{e_a['id']}" and st_c.get("origin") == mine["id"] - 1)
check("сброс — один на голос за акт (_ANCHORED); состояние с turns>0 больше не «свежее»",
      "claude" in live._ANCHORED and not live.anchored_state({"anchor": live.ANCHOR["addr"], "turns": 1})
      and live.anchored_state({"anchor": live.ANCHOR["addr"], "turns": 0}))
note_after = [e for e in evs if e["author"] == "chamber" and "монолог" in (e.get("text") or "")]
check("заметка о монологе (если есть) лежит ПОСЛЕ реплики Автора, не до",
      all(e["id"] > mine["id"] for e in note_after))
# kimi: две линии — сброшены обе (на диске), а не одна под воротами
for c in live.channels_of("kimi") or ():
    stk = live.load_state("kimi", c)
    check(f"kimi линия {c['name']}: нить заново (session_prev/anchor/origin)",
          stk.get("anchor") == f"live:{e_a['id']}" and stk.get("origin") == mine["id"] - 1)
live.cmd_say(argparse.Namespace(text="обычная реплика @grok", voices=None, anchor=None,
                                once=False, prefer=None))
check("следующий акт без --anchor: ANCHOR сброшен, промпт без блока",
      live.ANCHOR is None and not live._ANCHORED and "КОНТЕКСТ — сообщение" not in seen.get("grok", ""))
rc2 = live.cmd_say(argparse.Namespace(text="@claude ?", voices=None, anchor=f"live:{e_p['id']}",
                                      once=False, prefer=None))
check("якорь на ПАС — код 2, реплика в ленту не легла",
      rc2 == 2 and not any(e.get("text") == "@claude ?" for e in
                           (json.loads(l) for l in live.LIVE.open(encoding="utf-8"))))

# свежая сессия — через настоящий turn() со стабом _voice_cmd? Дорого:
# проверяем ветку состояния прямым вызовом логики turn на копии.
st = {"session": "old-uuid", "cursor": 5, "turns": 3, "cwd": "/x", "origin": 0}
prev = live.anchor_fresh(st, live.VOICES["claude"], "live:7", 41)
check("anchor_fresh: прежний id → session_prev, session=None, turns=0, cwd снят, адрес, cursor и origin на якоре",
      prev == "old-uuid" and st["session_prev"] == "old-uuid" and st["session"] is None
      and st["turns"] == 0 and st["cwd"] is None and st["anchor"] == "live:7"
      and st["cursor"] == 41 and st["origin"] == 41)
st = {"session": None, "cursor": 5, "turns": 2, "cwd": "/x"}
check("anchor_fresh у голоса с сессией по каталогу — prev «по каталогу»",
      live.anchor_fresh(st, live.VOICES["grok"], "live:7", 3) == "по каталогу")
src = Path(live.__file__).read_text(encoding="utf-8")
check("turn(): факты anchor/session_fresh/session_prev — из состояния, сброшенного deliver'ом; deepseek без session_fresh",
      'anchored = anchored_state(st)' in src and 'ev["session_fresh"] = True' in src
      and 'not v.get("no_session")' in src and 'ev["session_prev"] = anchor_prev' in src)
# чужой проект — не якорь
e_other = live.post("grok", "say", "реплика другого проекта", project="/somewhere/else")
try:
    anchor.resolve(f"live:{e_other['id']}", live_path=live.LIVE, room_path=ROOM, project="/this/project")
    check("событие другого проекта — отказ", False)
except anchor.AnchorError as ex:
    check("событие другого проекта — отказ", "другого проекта" in str(ex))
check("событие без поля проекта — общее, якорь разрешён",
      anchor.resolve(f"live:{e_a['id']}", live_path=live.LIVE, room_path=ROOM, project="/this/project")["author"] == "claude")

# ── дирижёр: автор якоря вне жребия и свода ─────────────────────────
import choir                                    # noqa: E402
choir.beacon = lambda: {"round": 1, "signature": "ab" * 48}
seed = T / "Q.md"
seed.write_text("вопрос", encoding="utf-8")
rc = choir.cmd_pick(argparse.Namespace(round="anc1", seed=str(seed), voices="claude,grok,kimi",
                                       exclude="claude", anchor=f"live:{e_a['id']}", project=None))
lots = [r for r in choir.read_round("anc1") if r.get("role") == "lot"]
check("pick --exclude: автор якоря не в кандидатах, поле excluded и anchor в записи жребия",
      rc == 0 and lots and "claude" not in lots[-1]["candidates"]
      and lots[-1]["excluded"] == ["claude"] and lots[-1]["anchor"]["sha"] == a["sha"]
      and lots[-1]["conductor"] in ("grok", "kimi"))
rc = choir.cmd_pick(argparse.Namespace(round="anc2", seed=str(seed), voices="claude",
                                       exclude="claude", anchor=None, project=None))
check("все исключены — жребий не из кого, код 2", rc == 2)
rc = choir.cmd_pick(argparse.Namespace(round="anc3", seed=str(seed), voices="claude,grok",
                                       exclude="nobody", anchor=None, project=None))
check("неизвестный голос в --exclude — код 2", rc == 2)
rc = choir.cmd_pick(argparse.Namespace(round="anc4", seed=str(seed), voices="claude,grok",
                                       exclude=None, anchor="live:999999", project=None))
check("якорь без записи — код 2, жребия нет", rc == 2 and not choir.read_round("anc4"))
check("lot_excluded читает запись жребия", choir.lot_excluded(choir.read_round("anc1")) == ["claude"]
      and choir.lot_excluded([]) == [])
# summarize --by автор якоря — отказ до всякого вызова
called = []
choir.ask_one = lambda *a_, **k: called.append(a_) or {"status": "error"}
rc = choir.cmd_summarize(argparse.Namespace(round="anc1", by="claude", out=str(T / "S.md"),
                                            effort=None, project=None))
check("summarize --by автор якоря — код 2, голос не зван", rc == 2 and not called)

print(f"\n{PASS} PASS, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
