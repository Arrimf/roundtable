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
grep -q "^summarize --round t1$" "$W/choir/argv.log" && pass "summarize: --by не передаётся (сводчика выбирает choir.py)" || fail "argv summarize: $(cat "$W/choir/argv.log")"
mkdir -p "$W/proj2" && git -C "$W/proj2" init -q
R="$(post /round "{\"question\":\"q\",\"name\":\"pj2\",\"project\":\"$W/proj2\"}")"
echo "$R" | grep -q '"act"' && pass "/round с проектом → 200" || fail "/round с проектом: $R"
sleep 2
grep -q "^pick --round pj2 --seed .* --project $W/proj2$" "$W/choir/argv.log" && pass "/round: --project уходит в pick (цепочка по шагам)" || fail "argv pick без --project: $(grep pick "$W/choir/argv.log")"
grep -q "^ask --round pj2 --seed" "$W/choir/argv.log" && ! grep "^ask --round pj2" "$W/choir/argv.log" | grep -q -- "--project" && pass "/round: ask без --project (берёт из жребия)" || fail "argv ask: $(grep '^ask' "$W/choir/argv.log")"
grep -q "Traceback" "$W/srv.log" && fail "в логе сервера трейсбек: $(grep -A3 Traceback "$W/srv.log" | head -5)" || pass "трейсбеков в логе сервера нет"
printf '\nvoices_http: PASS %d · FAIL %d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
