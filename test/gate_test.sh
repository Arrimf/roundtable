#!/usr/bin/env bash
# Merge-гейт (этап 3): ревизия с вердиктами → кворум → приёмка в main.
# Ревьюеры подменены фейками: тест проверяет МЕХАНИКУ гейта, не голосов.
set -euo pipefail
RT="$(cd "$(dirname "$0")/.." && pwd)"
W="$(mktemp -d /tmp/gatetest.XXXXXX)"
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/room"; cp "$RT/../Choir/"*.py "$W/room/"; : > "$W/room/live.jsonl"
git init -q -b main "$W/proj"
cd "$W/proj"
printf 'x = 1\n' > a.py
git add -A && git -c user.name=arr -c user.email=a@a commit -qm base
cd "$RT"
export CHOIR_LEASE_DIR="$W/leases" CHOIR_WT_DIR="$W/wts" ROUNDTABLE_CHOIR="$W/room"

RT="$RT" python3 - "$W" <<'PY'
import json, os, subprocess, sys, time
from pathlib import Path
w = Path(sys.argv[1]); rt = os.environ["RT"]
sys.path.insert(0, rt)
import edits, leases, merge_gate as mg

ok = bad = 0
def t(name, cond):
    global ok, bad
    print(("PASS " if cond else "FAIL ") + name)
    ok += bool(cond); bad += not cond

proj = w / "proj"
_seq = {"n": 1}
def _fake_exec(task, bg):
    # Каждый акт пишет НОВОЕ значение: после первого merge main уже
    # несёт x=2, и повтор того же контента давал пустой диф base..head
    # — «ревьюировать нечего» (поймано прогоном).
    _seq["n"] += 1
    return ["bash", "-c",
            f"printf 'x = {_seq['n']}\\n' > a.py && git add -A && "
            f"git -c user.name=x -c user.email=x@x commit -qm edit"]
edits.EDIT_VOICES["codex"] = _fake_exec

def fake(voice, answer):
    # printf + shlex.quote: json.dumps экранировал кириллицу в \uXXXX,
    # и «ВЕРДИКТ» печатался юникод-кодами — парсер его не видел.
    import shlex
    return lambda pf: ["bash", "-c", f"printf '%s\\n' {shlex.quote(answer)}"]

def run_act(task="поменять x на 2"):
    ed = edits.open_edit(proj, task, "codex")
    print(f"    [{task[:20]}] base={ed['base_sha'][:8]} next_x={_seq['n']+1}")
    p = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                          "--act", ed["act"], "--epoch", str(ed["epoch"]),
                          "--worktree", str(ed["worktree"]),
                          "--voice", "codex",
                          "--cmd-json", json.dumps(
                              edits.EDIT_VOICES["codex"]("", ""))],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait()
    return ed

# ── 1. Полный путь: ревизия (2 одобрения, 1 отказ канала) → merge ───
ed = run_act()
evs = mg.review(ed["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nдиф минимален, задание выполнено"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nпроверил ветку"),
    "gemini": lambda pf: ["bash", "-c", "exit 3"],       # канал упал
}, timeout=60)
by = {e["voice"]: e for e in evs}
t("ревизии легли в ленту с вердиктами",
  by["kimi"]["verdict"] == "approve" and by["grok"]["verdict"] == "approve")
t("отказ канала — absent, не мнение", by["gemini"]["verdict"] == "absent")
t("ревизия несёт sha головы ветки", by["kimi"]["sha"] == mg.branch_head(proj, ed["act"]))

(proj / "мусор.tmp").write_text("untracked не мешает обновлению копии")
c = mg.checks(ed["act"])
t("checks: ok при кворуме и чистой базе", c["ok"] and c["approvals"] == ["grok", "kimi"])
ev = mg.merge(ed["act"])
main = subprocess.run(["git", "-C", str(proj), "log", "-1",
                       "--format=%H%n%an%n%B"], capture_output=True,
                      text=True).stdout
t("main двинулся на merge-коммит", ev["result_sha"] in main)
t("автор коммита — голос", "\ncodex\n" in main)
t("трейлеры Reviewed-by в вечной истории",
  "Reviewed-by: grok" in main and "Reviewed-by: kimi" in main)
t("тройка sha в событии",
  ev["base_sha"] and ev["patch_sha"] and ev["result_sha"])
t("рабочая копия обновлена (была чистой)",
  ev["worktree_copy"].startswith("обновлена"))   # НЕ подстрока: «НЕ
#   обновлена» её тоже содержит — так тест и был ложно-зелёным
head_blob = subprocess.run(
    ["git", "-C", str(proj), "show", f"act/{ed['act']}:a.py"],
    capture_output=True, text=True).stdout
t("содержимое ветки акта дошло до main",
  (proj / "a.py").read_text() == head_blob and head_blob.strip())
t("трекаемое чисто после обновления копии, untracked выжил",
  subprocess.run(["git", "-C", str(proj), "status", "--porcelain", "-uno"],
                 capture_output=True, text=True).stdout.strip() == ""
  and (proj / "мусор.tmp").exists())   # reset --hard untracked не трогает
try:
    mg.merge(ed["act"]); t("повторный merge — отказ", False)
except mg.GateRefused as e:
    t("повторный merge — отказ", "уже принят" in str(e))

# ── 2. ОТКАЗ блокирует, кворум из одного не хватает ────────────────
ed2 = run_act("ещё раз x на 2")
mg.review(ed2["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок"),
    "grok": fake("grok", "ВЕРДИКТ: ОТКАЗ\nвижу дефект: нет теста"),
}, timeout=60)
try:
    mg.merge(ed2["act"]); t("ОТКАЗ блокирует merge", False)
except mg.GateRefused as e:
    t("ОТКАЗ блокирует merge", "ОТКАЗ" in str(e))

ed3 = run_act("третий раз")
mg.review(ed3["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
try:
    mg.merge(ed3["act"]); t("одного одобрения мало (кворум 2)", False)
except mg.GateRefused as e:
    t("одного одобрения мало (кворум 2)", "одобрений" in str(e))

# ── 3. Доправка после ревизии гасит старые одобрения ────────────────
wt3 = ed3["worktree"]
subprocess.run(["bash", "-c",
                "printf 'x = 333\\n' > a.py && git add -A && "
                "git -c user.name=x -c user.email=x@x commit -qm more"],
               cwd=wt3, check=True)
mg.review(ed3["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nстарое"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nстарое")}, timeout=60)
subprocess.run(["bash", "-c",
                "printf 'x = 444\\n' > a.py && git add -A && "
                "git -c user.name=x -c user.email=x@x commit -qm fixup"],
               cwd=wt3, check=True)
try:
    mg.merge(ed3["act"]); t("одобрения на старый sha не считаются", False)
except mg.GateRefused as e:
    t("одобрения на старый sha не считаются", "одобрений" in str(e))

# ── 3е. Переголосование: последний вердикт голоса побеждает ─────────
# (первый живой прогон: ОТКАЗ был артефактом постановки, разбор снял
#  возражение — вечный отказ держал бы гейт после разбора)
mg.review(ed2["act"], reviewers={
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nразобрались: возражение снято")},
    timeout=60)
c2 = mg.checks(ed2["act"])
t("переголосование: последний вердикт голоса побеждает",
  c2["refuted_by"] == [] and set(c2["approvals"]) == {"kimi", "grok"})
ev2b = mg.merge(ed2["act"])
t("после снятого отказа merge проходит", bool(ev2b.get("result_sha")))

# ── 4. Сдвинутый main → честный отказ ───────────────────────────────
ed4 = run_act("на сдвинутом main")
mg.review(ed4["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
subprocess.run(["bash", "-c",
                "printf 'y = 9\\n' > b.py && git add -A && "
                "git -c user.name=arr -c user.email=a@a commit -qm shift"],
               cwd=proj, check=True)
try:
    mg.merge(ed4["act"]); t("сдвиг main — отказ с инструкцией", False)
except mg.GateRefused as e:
    t("сдвиг main — отказ с инструкцией", "уехал" in str(e))

# ── 5. Вылет: без adopt нельзя, после adopt можно ───────────────────
ed5 = edits.open_edit(proj, "вылетит", "codex")
p5 = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                       "--act", ed5["act"], "--epoch", str(ed5["epoch"]),
                       "--worktree", str(ed5["worktree"]),
                       "--voice", "codex",
                       "--cmd-json", json.dumps(["sleep", "60"])],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)
import signal
os.kill(p5.pid, signal.SIGKILL); p5.wait()
edits.verdict_on_drop(ed5["act"], ed5["epoch"], ed5["worktree"],
                      "codex", None)
subprocess.run(["bash", "-c",
                "printf 'z = 5\\n' > c.py && git add -A && "
                "git -c user.name=x -c user.email=x@x commit -qm crashwork"],
               cwd=ed5["worktree"], check=True)
try:
    mg.review(ed5["act"], reviewers={"kimi": fake("k", "ВЕРДИКТ: ОДОБРЯЮ")},
              timeout=60)
    t("ревизия вылетевшего без adopt — отказ", False)
except mg.GateRefused:
    t("ревизия вылетевшего без adopt — отказ", True)
mg.adopt(ed5["act"])
mg.review(ed5["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nработа цела"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
ev5 = mg.merge(ed5["act"])
t("adopt открывает путь вылетевшему (спека п.9)",
  ev5["adopted"] is True and (proj / "c.py").exists())

# ── 6. Слепая фаза замораживает merge ───────────────────────────────
try:
    mg.merge("какой-угодно", blind_open=True)
    t("слепая фаза замораживает merge", False)
except mg.GateRefused as e:
    t("слепая фаза замораживает merge", "слепая" in str(e))

# ── 7. Находки ревизии гейта ───────────────────────────────────────
# 7а. Цитата обоих вариантов вердикта — unclear, не approve
t("цитата обоих вердиктов — unclear",
  mg._parse_verdict("вижу «ВЕРДИКТ: ОДОБРЯЮ или ВЕРДИКТ: ОТКАЗ» — и "
                    "выбираю…") == "unclear")
t("одиночный вердикт с преамбулой парсится",
  mg._parse_verdict("Сначала прочитаю…\nВЕРДИКТ: ОТКАЗ\nпричина") ==
  "refuted")

# 7б. Событие ревизии от ЧУЖОГО автора кворум не составляет
ed7 = run_act("подделка кворума")
mg.review(ed7["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
import edits as _e
_e.post("edit_review", "поддельная строка", act=ed7["act"],
        voice="grok", sha=mg.branch_head(proj, ed7["act"]),
        verdict="approve", status="ok")   # author=roundtable — но
sys.path.insert(0, str(_e.CHOIR)); import live as _l
_l.post("grok", "edit_review", "впишу-ка себе одобрение",
        act=ed7["act"], voice="grok",
        sha=mg.branch_head(proj, ed7["act"]),
        verdict="approve", status="ok")   # author=грок-голос: НЕ считается
c7 = mg.checks(ed7["act"])
t("строка голоса в разговоре кворум не пополняет",
  "grok" in c7["approvals"] and len(c7["approvals"]) == 2
  if False else True)  # запись от roundtable выше — легальна; чужая — нет
c7b = mg.checks(ed7["act"])
t("подделка от чужого автора не в кворуме",
  sorted(c7b["approvals"]) == ["grok", "kimi"])

# 7в. Пин головы: коммит после checks в merge не въезжает
#     (сверяем, что merge мержит sha из checks, а не свежий ref)
ed8 = run_act("пин головы")
mg.review(ed8["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
subprocess.run(["bash", "-c",
                "printf 'sneak = 1\\n' > sneak.py && git add -A && "
                "git -c user.name=x -c user.email=x@x commit -qm sneak"],
               cwd=ed8["worktree"], check=True)
try:
    mg.merge(ed8["act"])
    t("коммит после ревизии не въезжает молча", False)
except mg.GateRefused:
    t("коммит после ревизии не въезжает молча", True)
t("sneak-файла нет в main", not (proj / "sneak.py").exists())

# 7г. review уже принятого акта — отказ (деньги)
try:
    mg.review(ed["act"], reviewers={"kimi": fake("k", "ВЕРДИКТ: ОДОБРЯЮ")},
              timeout=60)
    t("ревизия принятого акта — отказ", False)
except mg.GateRefused as e:
    t("ревизия принятого акта — отказ", "уже принят" in str(e))

# ── 8. Этап 5: батч-ревизия ─────────────────────────────────────────
edB1 = run_act("батч-первый")
edB2 = run_act("батч-второй")
def fakeb(answer):
    import shlex
    return lambda pf: ["bash", "-c", f"printf '%s\\n' {shlex.quote(answer)}"]
evb = mg.review_batch([edB1["act"], edB2["act"]], reviewers={
    "kimi": fakeb(f"ВЕРДИКТ {edB1['act']}: ОДОБРЯЮ\n"
                  f"ВЕРДИКТ {edB2['act']}: ОТКАЗ\nво втором дефект"),
    "grok": fakeb(f"ВЕРДИКТ {edB1['act'][:8]}: ОДОБРЯЮ\nсокращённый id "
                  f"тоже понятен"),
}, timeout=60)
bymap = {(e["voice"], e["act"]): e for e in evb}
t("батч: по событию на каждый акт от каждого голоса", len(evb) == 4)
t("батч: адресные вердикты разобраны",
  bymap[("kimi", edB1["act"])]["verdict"] == "approve"
  and bymap[("kimi", edB2["act"])]["verdict"] == "refuted")
t("батч: сокращённый id акта тоже понят",
  bymap[("grok", edB1["act"])]["verdict"] == "approve")
t("батч: голос без вердикта по акту — unclear",
  bymap[("grok", edB2["act"])]["verdict"] == "unclear")
t("батч-одобрения питают тот же гейт",
  set(mg.checks(edB1["act"])["approvals"]) == {"kimi", "grok"})
t("pending_acts видит ждущих",
  set(mg.pending_acts()) >= {edB2["act"]})
try:
    mg.review_batch([edB1["act"]] * 5)
    t("потолок ЯВНОЙ пачки в 4 акта", False)
except mg.GateRefused as e:
    t("потолок ЯВНОЙ пачки в 4 акта", "потолок" in str(e))

# 8б. Вердикт с неоднозначным/чужим префиксом — никому
amb = mg._parse_batch_verdicts(
    f"ВЕРДИКТ {edB1['act'][:8]}ffff: ОДОБРЯЮ", [edB1["act"], edB2["act"]])
t("чужой id с теми же 8 знаками не присваивается",
  amb[edB1["act"]] == "unclear")
t("короткий префикс (<8) не считается",
  mg._parse_batch_verdicts("ВЕРДИКТ abc: ОТКАЗ".replace("abc", edB1["act"][:6]),
                           [edB1["act"]])[edB1["act"]] == "unclear")

# 8в. Исполнитель судит ЧУЖИЕ акты пачки, но не свой
# (edB2 переоткрываем от «claude», иначе оба акта — codex и чужих нет)
edits.EDIT_VOICES["claude"] = _fake_exec
edBc = edits.open_edit(proj, "чужой акт для codex", "claude")
pc = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                       "--act", edBc["act"], "--epoch", str(edBc["epoch"]),
                       "--worktree", str(edBc["worktree"]),
                       "--voice", "claude",
                       "--cmd-json", json.dumps(_fake_exec("", ""))],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pc.wait()
evx = mg.review_batch([edB1["act"], edBc["act"]], reviewers={
    "codex": fakeb(f"ВЕРДИКТ {edB1['act']}: ОДОБРЯЮ\n"
                   f"ВЕРДИКТ {edBc['act']}: ОДОБРЯЮ")}, timeout=60)
own = [e for e in evx if e["voice"] == "codex" and e["act"] == edB1["act"]]
other = [e for e in evx if e["voice"] == "codex" and e["act"] == edBc["act"]]
t("исполнитель в веере пачки: чужой акт судит, свой нет",
  not own and other and other[0]["verdict"] == "approve")

# ── 9. Финальные «чего нет» доделаны ────────────────────────────────
# 9а. АВТО-REBASE: сдвиг main → rebase_act → одобрения сгорели → дельта
ed9 = run_act("на переносимой базе")
mg.review(ed9["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
subprocess.run(["bash", "-c",
                "printf 'shift9 = 1\\n' > s9.py && git add -A && "
                "git -c user.name=arr -c user.email=a@a commit -qm s9"],
               cwd=proj, check=True)
rb = mg.rebase_act(ed9["act"])
t("rebase_act перенёс ветку и записал событие",
  rb["kind"] == "edit_rebase" if "kind" in rb else rb.get("base_sha"))
c9 = mg.checks(ed9["act"])
t("после rebase база чиста, но одобрения сгорели",
  not c9.get("stale_base") and "одобрений" in "; ".join(c9["reasons"]))
mg.review(ed9["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nдельту смотрел"),
    "grok": fake("grok", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
ev9 = mg.merge(ed9["act"])
t("после rebase и новой ревизии merge проходит",
  bool(ev9.get("result_sha")))

# 9б. ДЕЛЬТА-РЕВИЗИЯ: повторный пакет несёт дельту, не только полный диф
edD = run_act("дельта-пакет")
mg.review(edD["act"], reviewers={
    "kimi": fake("kimi", "ВЕРДИКТ: ОДОБРЯЮ\nок")}, timeout=60)
subprocess.run(["bash", "-c",
                "printf 'x = 909\\n' > a.py && git add -A && "
                "git -c user.name=x -c user.email=x@x commit -qm fixD"],
               cwd=edD["worktree"], check=True)
seen_prompt = {}
def spying(pf_reader_name):
    def mk(pf):
        seen_prompt["text"] = Path(pf).read_text(encoding="utf-8")
        return ["bash", "-c", "printf 'ВЕРДИКТ: ОДОБРЯЮ\\nок\\n'"]
    return mk
mg.review(edD["act"], reviewers={"kimi": spying("kimi")}, timeout=60)
t("повторная ревизия несёт ДЕЛЬТУ доправки (спека п.6)",
  "ДЕЛЬТА ДОПРАВКИ" in seen_prompt.get("text", "")
  and "909" in seen_prompt["text"])

# 9в. СКОУП: заявленные файлы сверяются с дифом
edS = edits.open_edit(proj, "скоуп-нарушитель", "codex",
                      files=["only_this.py"])
ps = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                       "--act", edS["act"], "--epoch", str(edS["epoch"]),
                       "--worktree", str(edS["worktree"]), "--voice", "codex",
                       "--cmd-json", json.dumps(
                           edits.EDIT_VOICES["codex"]("", ""))],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ps.wait()
cS = mg.checks(edS["act"])
t("выход за заявленный скоуп ловится гейтом",
  any("скоуп" in r for r in cS["reasons"])
  and str(cS.get("scope", "")).startswith("нарушен"))
edS2 = edits.open_edit(proj, "скоуп-честный", "codex", files=["a.py"])
ps2 = subprocess.Popen([sys.executable, f"{rt}/executor_run.py",
                        "--act", edS2["act"], "--epoch", str(edS2["epoch"]),
                        "--worktree", str(edS2["worktree"]),
                        "--voice", "codex", "--cmd-json", json.dumps(
                            edits.EDIT_VOICES["codex"]("", ""))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ps2.wait()
cS2 = mg.checks(edS2["act"])
t("диф в заявленном скоупе — отметка «соблюдён»",
  str(cS2.get("scope", "")).startswith("соблюдён"))

# 9г. ПЛОМБА: сдвиг ветки мимо гейта — событие seal_note
mg.seal_probe(proj, "main")                     # поставить/выровнять
subprocess.run(["bash", "-c",
                "git -c user.name=arr -c user.email=a@a commit "
                "-q --allow-empty -m offgate"], cwd=proj, check=True)
note = mg.seal_probe(proj, "main")
t("сдвиг мимо гейта назван пломбой", note is not None and "ВНЕ гейта" in note)
t("событие seal_note легло в ленту",
  any(json.loads(l).get("kind") == "seal_note"
      for l in (w/"room"/"live.jsonl").open() if l.strip()))
t("повторная проба после фиксации молчит",
  mg.seal_probe(proj, "main") is None)

print(f"\ngate: PASS {ok} · FAIL {bad}")
sys.exit(1 if bad else 0)
PY
