#!/usr/bin/env bash
# HTTP-контракт вкладок (ревизия 2026-09-02: «новые тесты проверяют
# парсеры и argv, но не основной контракт правки» — codex). Поднимает
# ИЗОЛИРОВАННОЕ окно: своя комната (копии live.py/choir.py, пустая
# лента), свои rt-voices.json и кэш каталога, без сетевой разведки.
# Ничего живого не трогает и денег не тратит: ни одного вызова голоса.
set -u
RT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_CHOIR="${ROUNDTABLE_CHOIR:-$HOME/AiSandbox/Choir}"
PORT="${RT_HTTP_TEST_PORT:-8779}"
W="$(mktemp -d /tmp/rt-http.XXXXXX)"
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$1"; }
B="http://127.0.0.1:$PORT"
post() { curl -s -X POST "$B$1" -H 'Content-Type: application/json' -d "$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' -X POST "$B$1" -H 'Content-Type: application/json' -d "$2"; }

mkdir -p "$W/choir"
cp "$SRC_CHOIR"/*.py "$W/choir/" || { echo "нет $SRC_CHOIR/*.py"; exit 2; }
: > "$W/choir/live.jsonl"; : > "$W/choir/room.jsonl"
# Кэш каталога с лестницами ПО МОДЕЛЯМ — детерминированно и без сети.
cat > "$W/rt-models.json" <<'EOF'
{"codex": {"models": ["gpt-5.6-sol", "gpt-5.4"], "efforts": ["low","medium","high","xhigh","max","ultra"],
  "efforts_by_model": {"gpt-5.6-sol": ["low","medium","high","xhigh","max","ultra"], "gpt-5.4": ["low","medium","high","xhigh"]},
  "default_effort_by_model": {"gpt-5.6-sol": "low"}, "source": "тест", "fetched_at": "2026-09-02T00:00:00+00:00"},
 "grok": {"models": ["grok-4.6", "grok-4.5"], "efforts": ["low","medium","high","xhigh"],
  "efforts_by_model": {"grok-4.6": ["low","medium","high","xhigh"], "grok-4.5": ["low","medium","high"]},
  "default_effort_by_model": {"grok-4.6": "high", "grok-4.5": "high"}, "source": "тест", "fetched_at": "2026-09-02T00:00:00+00:00"},
 "kimi": {"models": ["kimi-k3", "kimi-k2.6"], "efforts": [], "default_model": "kimi-k3", "unaliased": ["kimi-k9-new"], "source": "тест", "fetched_at": "2026-09-02T00:00:00+00:00"}}
EOF
# Сохранённая пара, которую POST не принял бы: усилие ultra при
# модели умолчания gpt-5.4 — должна отброситься при чтении.
cat > "$W/rt-voices.json" <<'EOF'
{"codex": {"rounds": {"model": "gpt-5.4", "effort": "ultra"}, "exec": {"effort": "xhigh"}},
 "kimi": {"exec": {"effort": "high", "model": "kimi-k2.6"}},
 "gemini": {"exec": {"model": "gemini-3.7-flash"}},
 "grok": {"room": {"model": "grok-4.6", "effort": "xhigh"}}}
EOF
# Порт обязан быть свободен ДО старта: иначе тесты молча ходят в чужое
# (или прошлое) окно — ровно так первый прогон проверял старый код.
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "порт $PORT занят: $(ss -ltnp | grep ":$PORT ") — задайте RT_HTTP_TEST_PORT"; exit 2
fi
( cd "$RT_DIR" && ROUNDTABLE_CHOIR="$W/choir" CHOIR_RT_VOICES="$W/rt-voices.json" \
  CHOIR_RT_MODELS="$W/rt-models.json" CHOIR_RT_NO_DISCOVERY=1 CHOIR_DSH_PATCH_DIR="$W/dshp" \
  CHOIR_WT_DIR="$W/wt" ROUNDTABLE_PORT="$PORT" nohup python3 roundtable.py --no-project \
  > "$W/srv.log" 2>&1 ) &
for _ in $(seq 1 40); do sleep 0.25; curl -s -o /dev/null "$B/state" && break; done
if ! curl -s -o /dev/null "$B/state"; then echo "сервер не поднялся: $(tail -5 "$W/srv.log")"; exit 2; fi
# pid — ТОГО, КТО ДЕРЖИТ ПОРТ: `$!` за `( cd && … & )` был pid подоболочки,
# и kill по нему оставлял окно жить (поймано вторым прогоном).
ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 > "$W/pid"
[ -s "$W/pid" ] || { echo "pid окна не найден"; exit 2; }
# RT_HTTP_KEEP=1 — оставить окно и каталог для разбора (печатает путь).
if [ "${RT_HTTP_KEEP:-}" = 1 ]; then echo "KEEP: $W (pid $(cat "$W/pid"), порт $PORT)";
else trap 'kill "$(cat "$W/pid")" 2>/dev/null; rm -rf "$W"' EXIT; fi

curl -s "$B/voices" > "$W/v.json"
python3 - "$W/v.json" "$W/srv.log" <<'EOF' && pass "GET /voices: tabs у всех, exec=None у gemini, умолчания явные" || fail "GET /voices: карточки вкладок"
import json, sys
d = json.load(open(sys.argv[1])); log = open(sys.argv[2]).read()
by = {v["name"]: v for v in d["voices"]}
assert all("tabs" in v for v in d["voices"])
assert by["gemini"]["tabs"]["exec"] is None
assert by["claude"]["tabs"]["exec"]["default_model"] and by["claude"]["tabs"]["exec"]["can_effort"]
assert by["grok"]["tabs"]["exec"]["can_model"] and by["grok"]["tabs"]["exec"]["effort"] == "high"
assert by["kimi"]["tabs"]["exec"]["can_effort"] is False and by["kimi"]["tabs"]["exec"]["effort"] is None
assert by["deepseek"]["tabs"]["exec"]["can_effort"] is False
assert "kimi-k9-new" not in by["kimi"]["models"] and by["kimi"]["unaliased"] == ["kimi-k9-new"]
# чтение rt-voices.json той же строгости, что POST
assert by["codex"]["tabs"]["rounds"]["set_effort"] is None, "ultra при gpt-5.4 должен отпасть"
assert by["codex"]["tabs"]["rounds"]["set_model"] == "gpt-5.4"
assert by["codex"]["tabs"]["exec"]["set_effort"] == "xhigh"
assert by["kimi"]["tabs"]["exec"]["set_effort"] is None and by["kimi"]["tabs"]["exec"]["set_model"] == "kimi-k2.6"
assert by["grok"]["tabs"]["room"]["set_effort"] == "xhigh", "grok-4.6+xhigh допустима"
assert "codex/rounds/effort" in log and "kimi/exec/effort" in log and "gemini/exec/model" in log
EOF

[ "$(code /voices '{"voice":"kimi","scope":"exec","effort":"high"}')" = 400 ] && pass "exec kimi effort → 400 (рычага нет)" || fail "exec kimi effort"
[ "$(code /voices '{"voice":"deepseek","scope":"exec","effort":"max"}')" = 400 ] && pass "exec dsh effort → 400" || fail "exec dsh effort"
[ "$(code /voices '{"voice":"claude","scope":"exec","effort":"max"}')" = 200 ] && pass "exec claude effort → 200 (новый рычаг)" || fail "exec claude effort"
[ "$(code /voices '{"voice":"grok","scope":"exec","model":"grok-4.5"}')" = 200 ] && pass "exec grok model → 200 (новый рычаг)" || fail "exec grok model"
[ "$(code /voices '{"voice":"grok","scope":"exec","effort":"xhigh"}')" = 400 ] && pass "grok-4.5+xhigh → 400 (пара по модели)" || fail "grok pair"
[ "$(code /voices '{"voice":"codex","scope":"room","model":"gpt-5.4","effort":"max"}')" = 400 ] && pass "gpt-5.4+max → 400 (лестница модели codex)" || fail "codex pair"
[ "$(code /voices '{"voice":"codex","scope":"room","model":"gpt-5.6-sol","effort":"ultra"}')" = 200 ] && pass "gpt-5.6-sol+ultra → 200" || fail "codex ultra"
# сброс модели при несовместимом оставшемся усилии
R="$(post /voices '{"voice":"codex","scope":"room","model":""}')"
echo "$R" | grep -q '"reset": true' && pass "сброс модели → 200" || fail "сброс модели: $R"
curl -s "$B/voices" | python3 -c '
import json,sys; d=json.load(sys.stdin); t=[v for v in d["voices"] if v["name"]=="codex"][0]["tabs"]["room"]
sys.exit(0 if t["set_model"] is None and t["set_effort"] in (None,"ultra") and (t["set_effort"] is None or t["default_model"]=="gpt-5.6-sol") else 1)' \
  && pass "после сброса усилие сверено с моделью умолчания" || fail "сброс: усилие не сверено"
[ "$(code /voices '{"voice":"kimi","scope":"room","model":"kimi-k9-new"}')" = 200 ] && fail "kimi без алиаса принят как известный" || pass "kimi: имя без алиаса — не «известное» (принято лишь по форме или отклонено)"
grep -q '"voice_config"' "$W/choir/live.jsonl" && grep -q '\[кресло\]' "$W/choir/live.jsonl" && pass "событие кресла подписано [кресло]" || fail "событие кресла подписано не как кресло"
grep -c 'живая комната' "$W/choir/live.jsonl" | grep -q '^0$' || { grep '\[кресло\]' "$W/choir/live.jsonl" | grep -q 'живая комната' && fail "у события кресла комнатная область" || pass "у события кресла нет комнатной области"; }
[ "$(code /round '{"question":"q","name":"dup","voices":["claude","claude"]}')" = 400 ] && pass "/round: дубли — не два голоса" || fail "/round дубли"
[ "$(code /round '{"question":"q","name":"none","voices":[]}')" = 400 ] && pass "/round: пустой список → 400" || fail "/round пусто"
[ ! -e "$W/choir/ВОПРОС-dup.md" ] && pass "/round: отказ до записи файла вопроса" || fail "/round: файл вопроса записан при отказе"
[ "$(code /lot '{"candidates":["grok","grok"]}')" = 400 ] && pass "/lot: дубли кандидатов → 400" || fail "/lot дубли"
R="$(post /models_refresh '{"voices":["claude","claude","claude","nope"]}')"
echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if list(d["report"].keys())==["claude"] else 1)' \
  && pass "/models_refresh: дедуп по VOICES" || fail "/models_refresh дедуп: $R"
# пустой пул random не оставляет резерв проекта
for v in claude codex grok deepseek; do post /voices "{\"voice\":\"$v\",\"scope\":\"exec\",\"pool\":false}" > /dev/null; done
mkdir -p "$W/proj" && git -C "$W/proj" init -q && git -C "$W/proj" -c user.name=t -c user.email=t@t commit -q --allow-empty -m base
[ "$(code /edit "{\"task\":\"t\",\"voice\":\"random\",\"project\":\"$W/proj\"}")" = 409 ] && pass "/edit random при пустом пуле → 409" || fail "/edit пустой пул"
R="$(post /edit "{\"task\":\"t\",\"voice\":\"random\",\"project\":\"$W/proj\"}")"
echo "$R" | grep -q 'пул random пуст' && pass "/edit: повтор — снова «пул пуст», а не «проект занят» (резерв не завис)" || fail "/edit: резерв завис: $R"
# карточка раунда: /round_view читает room.jsonl, /round_step — шаги по человеку
cat >> "$W/choir/room.jsonl" <<'EOF'
{"id":"a1","ts":"2026-09-03T10:00:00+00:00","round":"t1","phase":"pick","voice":"choir","role":"lot","text":"grok","conductor":"grok","candidates":["claude","grok"],"drand_round":1,"project":"/tmp"}
{"id":"a2","ts":"2026-09-03T10:00:01+00:00","round":"t1","phase":"expand","voice":"grok","role":"seed_expanded","status":"ok","text":"# затравка"}
{"id":"a3","ts":"2026-09-03T10:00:02+00:00","round":"t1","phase":"blind","voice":"claude","role":"answer","status":"ok","text":"ответ клода","elapsed_s":12.5}
{"id":"a4","ts":"2026-09-03T10:00:03+00:00","round":"t1","phase":"blind","voice":"grok","role":"answer","status":"ok","text":"ответ грока","model":"grok-4.6"}
{"id":"a5","ts":"2026-09-03T10:00:04+00:00","round":"t1","phase":"summary","voice":"choir","role":"lot_summary","text":"grok"}
{"id":"a6","ts":"2026-09-03T10:00:05+00:00","round":"t2","phase":"pick","voice":"choir","role":"lot","text":"kimi","conductor":"kimi","candidates":["kimi","codex"]}
EOF
curl -s "$B/round_view?name=t1" | python3 -c '
import json,sys; d=json.load(sys.stdin)
assert d["found"] and d["conductor"]=="grok" and d["project"]=="/tmp", d
assert len(d["answers"])==2 and d["answers"][1]["model"]=="grok-4.6" and d["answers"][0]["text"]=="ответ клода"
assert d["seed"]["text"]=="# затравка" and d["rebuts"]==0 and d["summary"] is None, d
' && pass "/round_view: жребий, затравка, ответы дословно; lot_summary не считается сводом" || fail "/round_view t1"
curl -s "$B/round_view?name=nope" | grep -q '"found": false' && pass "/round_view: неизвестный раунд → found=false" || fail "/round_view nope"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/round_view?name=..%2Fetc")" = 400 ] && pass "/round_view: кривое имя → 400" || fail "/round_view имя"
[ "$(code /round_step '{"name":"t1","step":"merge"}')" = 400 ] && pass "/round_step: чужой шаг → 400" || fail "/round_step шаг"
[ "$(code /round_step '{"name":"t2","step":"rebut"}')" = 409 ] && pass "/round_step: без слепой фазы → 409" || fail "/round_step без ответов"
[ "$(code /round_step '{"name":"t9","step":"summarize"}')" = 409 ] && pass "/round_step: без жребия → 409" || fail "/round_step без жребия"
[ "$(code /round '{"question":"q","name":"pj","project":"/nonexistent/dir"}')" = 400 ] && pass "/round: проект не каталог → 400 до записи файла" || fail "/round проект"
[ ! -e "$W/choir/ВОПРОС-pj.md" ] && pass "/round: файл вопроса при отказе не создан" || fail "/round: файл вопроса создан при отказе"
# ── шаги раунда с ЗАГЛУШКОЙ дирижёра: argv в файл, сон 3 с, код 0 ──
# (ревизия 2026-09-03: без стаба «проверка занятости мертва с рождения» и
# «финал без поля round» проходили тесты — codex, grok, kimi, claude, субагент)
cat > "$W/choir/choir.py" <<'EOF'
import sys, time, os
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "argv.log"), "a").write(" ".join(sys.argv[1:]) + "\n")
time.sleep(3 if sys.argv[1] in ("rebut", "summarize") else 0)
EOF
R="$(post /round_step '{"name":"t1","step":"rebut"}')"
echo "$R" | grep -q '"act"' && pass "/round_step rebut → 200 (стаб дирижёра)" || fail "/round_step 200: $R"
[ "$(code /round_step '{"name":"t1","step":"summarize"}')" = 409 ] && pass "/round_step: второй шаг того же раунда во время первого → 409" || fail "/round_step: занятость не видна"
sleep 5
grep -q "^rebut --round t1$" "$W/choir/argv.log" && pass "/round_step: argv «rebut --round t1» без --by и без лишнего" || fail "argv rebut: $(cat "$W/choir/argv.log")"
tail -n 3 "$W/choir/live.jsonl" | grep -q '"status": "done"' && tail -n 3 "$W/choir/live.jsonl" | grep '"status": "done"' | grep -q '"round": "t1"' && pass "финал акта несёт round (карточка появится)" || fail "финал акта без round: $(tail -n 2 "$W/choir/live.jsonl" | cut -c1-200)"
tail -n 3 "$W/choir/live.jsonl" | grep '"status": "done"' | grep -q '"step": "rebut"' && pass "финал акта несёт step" || fail "финал без step"
[ "$(code /round_step '{"name":"t1","step":"summarize"}')" = 200 ] && pass "/round_step summarize после витка → 200" || fail "/round_step summarize"
sleep 5
grep -q "^summarize --round t1 --out СВОД-t1.md$" "$W/choir/argv.log" && pass "summarize: без --by (сводчика выбирает choir.py), с --out СВОД-<раунд>.md" || fail "argv summarize: $(cat "$W/choir/argv.log")"
mkdir -p "$W/proj2" && git -C "$W/proj2" init -q
R="$(post /round "{\"question\":\"q\",\"name\":\"pj2\",\"project\":\"$W/proj2\"}")"
echo "$R" | grep -q '"act"' && pass "/round с проектом → 200" || fail "/round с проектом: $R"
sleep 2
grep -q "^pick --round pj2 --seed .* --project $W/proj2$" "$W/choir/argv.log" && pass "/round: --project уходит в pick (цепочка по шагам)" || fail "argv pick без --project: $(grep pick "$W/choir/argv.log")"
grep -q "^ask --round pj2 --seed" "$W/choir/argv.log" && ! grep "^ask --round pj2" "$W/choir/argv.log" | grep -q -- "--project" && pass "/round: ask без --project (берёт из жребия)" || fail "argv ask: $(grep '^ask' "$W/choir/argv.log")"
# ── цель целеполагателя и адресная опция комнаты ──────────────────
R="$(post /goal '{"text":"Довести окно до релиза 0.2"}')"
echo "$R" | grep -q '"goal": "Довести окно до релиза 0.2"' && pass "/goal → событие goal" || fail "/goal: $R"
curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("goal")=="Довести окно до релиза 0.2" else 1)' && pass "/state несёт действующую цель" || fail "/state без цели"
post /goal '{"text":""}' > /dev/null; curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("goal")=="" else 1)' && pass "пустая цель снимает цель" || fail "цель не снялась"
[ "$(code /goal "{\"text\":\"$(python3 -c 'print("x"*2001)')\"}")" = 400 ] && pass "/goal: длиннее 2000 → 400" || fail "/goal длина"
[ "$(code /goal '{"text":["a"]}')" = 400 ] && pass "/goal: не строка → 400" || fail "/goal тип"
( cd "$W/choir" && python3 - <<'EOF'
import sys; sys.path.insert(0, "."); import live
f = live._status_of
assert f(3, "", "", 3) == "quota" and f(1, "", "HTTP 429 Too Many Requests: rate limit", None) == "quota"
assert f(1, "", "Not logged in · Please run /login", None) == "auth" and f(1, "", "Error: 403 Forbidden", None) == "auth"
assert f(1, "", "429 max organization concurrency", None) == "busy", "занятость линии — не квота"
assert f(0, "", "", None) == "empty" and f(0, "да", "", None) == "ok" and f(None, "", "", None, True) == "timeout" and f(2, "", "boom", None) == "error"
# ложные срабатывания по подстрокам (нашли все шестеро)
assert f(1, "", "failed to open /tmp/quota-report.txt", None) == "error"
assert f(1, "", "connection reset https://host/item/4032", None) == "error"
assert f(1, "", "PID 40382 died, line 429 of main.py", None) == "error"
assert f(1, "HTTP 429 rate limit exceeded", "", None) == "quota", "stdout тоже читается"
assert f(1, "You've reached your Fable 5 limit. Switch to another model or wait for reset.", "", None) == "quota", "дословный отказ Клода по лимиту (лента 2026-08-25)"
assert f(1, "", "ключ #0: 429 quota exceeded; ключ #1: 503 у шлюза", 3) == "error", "у адаптера с кодом квоты вердикт — код: смешанный отказ не квота"
assert f(3, "", "", 3) == "quota"
h = {"author": "arr", "text": "спроси @grok"}; v = {"author": "claude", "text": "как заметил @grok, спорно.\n@deepseek уточни"}
live.PEER = False
assert live.pick_voices(h, ["grok", "claude"])[1] == "по адресу"
assert live.pick_voices(v, ["deepseek", "grok", "claude"])[1] != "по адресу коллеги", "адрес голоса при выключенной опции не должен давать слово"
live.PEER = True
assert live.pick_voices(v, ["deepseek", "grok", "claude"]) == (["deepseek"], "по адресу коллеги"), "адрес голоса — только с начала строки"
assert live.pick_voices({"author": "claude", "text": "@claude сам"}, ["claude", "grok"])[1] != "по адресу коллеги", "самоадрес — не ход"
assert live.pick_voices(v, ["deepseek", "grok", "claude"], allow_address=False)[1] != "по адресу коллеги", "внутри ветки адрес — текст"
assert live.current_goal() == ""
EOF
) && pass "live.py: типы отказов (без ложных), адресная опция: начало строки, самоадрес, ветка" || fail "live.py: статусы/адресация"
# цель: окно и голоса читают одно и то же, и старая цель не теряется за хвостом
post /goal '{"text":"Цель-А для проверки"}' > /dev/null
python3 - "$W/choir/live.jsonl" <<'EOF'
import json, sys
# 600 КБ балласта после цели — хвост в 512 КБ её терял
with open(sys.argv[1], "a", encoding="utf-8") as f:
    for i in range(700):
        f.write(json.dumps({"id": 900000 + i, "ts": "2026-09-06T00:00:00+00:00", "author": "choir", "kind": "note", "text": "x" * 900}, ensure_ascii=False) + "\n")
EOF
curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("goal")=="Цель-А для проверки" else 1)' && pass "/state.goal не теряется за 600 КБ балласта (весь файл, не хвост)" || fail "/state.goal потерялся за хвостом"
( cd "$W/choir" && python3 -c 'import sys; sys.path.insert(0,"."); import live; assert live._read_goal()=="Цель-А для проверки"' ) && pass "live.current_goal видит ту же цель, что окно" || fail "live/окно расходятся в цели"
# сценарий комнаты с ПОДДЕЛЬНЫМИ голосами (turn подменён): адрес → ответ адресата → рук нет → ветка закрыта
( cd "$W/choir" && CHOIR_PEER=1 python3 - <<'EOF'
import sys, json, argparse
sys.path.insert(0, "."); import live
calls = []
def bump(name):
    # настоящий turn() считает ходы нити в state.json — фальшивый тоже
    st = live.load_state(name, None); st["turns"] = st.get("turns", 0) + 1; live.save_state(name, st, None)
def fake_turn(name, prompt):
    calls.append(name); bump(name)
    text = {"claude": "Спорно.\n@deepseek уточни, откуда цифра", "deepseek": "Цифра из README, строка 12."}.get(name, "ПАС")
    return {"author": name, "kind": "pass" if text == "ПАС" else "say", "text": text, "elapsed_s": 0.1}
live.turn = fake_turn
live.available = lambda n: True
live.PEER = True
live.cmd_say(argparse.Namespace(text="@claude что скажешь про цифру 142?", voices="claude,deepseek,grok", once=False))
evs = [json.loads(l) for l in open("live.jsonl", encoding="utf-8")]
tail = evs[-12:]
say = [e for e in tail if e.get("kind") == "say"]
by = {e["author"]: e for e in say if e["author"] != "arr"}
assert calls == ["claude", "deepseek"], f"вызовы: {calls} (третий голос без руки не должен звучать)"
assert "thread" not in by["claude"], "первый ответ — не в ветке"
assert by["deepseek"].get("thread") == by["claude"]["id"], "ответ адресата помечен веткой = id адресной реплики"
notes = [e for e in tail if e.get("kind") == "note" and "ветка закрыта" in (e.get("text") or "")]
assert notes and notes[-1].get("thread") == by["claude"]["id"], "ветка закрыта заметкой с тем же thread"
print("ok", calls)
# смена опции доезжает до начатой нити: второй акт при выключенной опции
live.PEER = False
prompts = {}
def fake_turn2(name, prompt):
    bump(name)
    prompts[name] = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
    return {"author": name, "kind": "pass", "text": "ПАС", "elapsed_s": 0.1}
live.turn = fake_turn2
live.cmd_say(argparse.Namespace(text="@claude ещё раз", voices="claude,deepseek,grok", once=True))
assert "Адресные вопросы коллегам сейчас ВЫКЛЮЧЕНЫ" in prompts.get("claude", ""), "уведомление о смене опции в промпте начатой нити"
print("peer-switch ok")
EOF
) && pass "комната (фальшивые голоса): адрес коллеги → ответ в ветке → рук нет → ветка закрыта, третий не оплачен; смена опции доезжает до нити" || fail "сценарий адресной ветки"
( cd "$W/choir" && python3 - <<'EOF'
import sys, json, importlib.util
# choir.py здесь заглушка — читаем ЖИВОЙ дирижёр по абсолютному пути
spec = importlib.util.spec_from_file_location("choir_live", "choir_real.py")
m = importlib.util.module_from_spec(spec); sys.modules["choir_live"] = m; spec.loader.exec_module(m)
import os
m.ROOM = __import__("pathlib").Path(os.getcwd()) / "room.jsonl"
m._append({"id": "s1", "ts": "2026-09-06T00:00:00+00:00", "round": "g1", "phase": "blind", "voice": "arr", "role": "seed", "text": "q", "packet_prefix": "ЦЕЛЬ (целеполагатель — Автор): строка 1\nстрока 2\n\n", "goal": None})
m._append({"id": "s2", "ts": "2026-09-06T00:00:01+00:00", "round": "g2", "phase": "blind", "voice": "arr", "role": "seed", "text": "q", "packet_prefix": "ЦЕЛЬ (целеполагатель — Автор): a\nb\n\n", "goal": "a\nb"})
assert m.goal_of_seed("g1") == "строка 1\nстрока 2", m.goal_of_seed("g1")
assert m.goal_of_seed("g2") == "a\nb"
m.snapshot_goal("зафиксированная"); assert m.goal_notice().startswith("ЦЕЛЬ (целеполагатель — Автор): зафиксированная")
print("goal_of_seed ok")
EOF
) && pass "choir.py: goal_of_seed многострочная (поле goal и запасной разбор префикса), снимок цели" || fail "choir.py: goal_of_seed"
grep -q "Traceback" "$W/srv.log" && fail "в логе сервера трейсбек: $(grep -A3 Traceback "$W/srv.log" | head -5)" || pass "трейсбеков в логе сервера нет"
printf '\nvoices_http: PASS %d · FAIL %d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
