#!/usr/bin/env bash
# HTTP-контракт вкладок (ревизия 2026-09-02: «новые тесты проверяют
# парсеры и argv, но не основной контракт правки» — codex). Поднимает
# ИЗОЛИРОВАННОЕ окно: своя комната (копии live.py/choir.py, пустая
# лента), свои rt-voices.json и кэш каталога, без сетевой разведки.
# Ничего живого не трогает и денег не тратит: ни одного вызова голоса.
set -u
RT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_CHAMBER="${ROUNDTABLE_CHAMBER:-$RT_DIR/chamber}"
PORT="${RT_HTTP_TEST_PORT:-8779}"
W="$(mktemp -d /tmp/rt-http.XXXXXX)"
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$1"; }
B="http://127.0.0.1:$PORT"
post() { curl -s -X POST "$B$1" -H 'Content-Type: application/json' -d "$2"; }
code() { curl -s -o /dev/null -w '%{http_code}' -X POST "$B$1" -H 'Content-Type: application/json' -d "$2"; }

mkdir -p "$W/chamber" "$W/journal/rounds"
cp "$SRC_CHAMBER"/*.py "$W/chamber/" || { echo "нет $SRC_CHAMBER/*.py"; exit 2; }
: > "$W/journal/live.jsonl"; : > "$W/journal/room.jsonl"
# Кэш каталога с лестницами ПО МОДЕЛЯМ — детерминированно и без сети.
cat > "$W/rt-models.json" <<'EOF'
{"codex": {"models": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.4"], "efforts": ["low","medium","high","xhigh","max","ultra"],
  "efforts_by_model": {"gpt-6-astra": ["low","medium","high","xhigh","max","ultra"], "gpt-5.6-sol": ["low","medium","high","xhigh","max","ultra"], "gpt-5.4": ["low","medium","high","xhigh"]},
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
( cd "$RT_DIR" && ROUNDTABLE_CHAMBER="$W/chamber" ROUNDTABLE_JOURNAL="$W/journal" CHOIR_RT_VOICES="$W/rt-voices.json" CHOIR_RT_ACTS="$W/acts" CHOIR_RT_ACTS_ARCHIVE="$W/acts-archive" CHOIR_LEASE_DIR="$W/leases" CHOIR_RT_NO_AUTOREVIEW=1 CHOIR_RT_NO_BACKUP=1 CHOIR_BACKUP_DIR="$W/backup" CHOIR_JEV=0 CHOIR_SEC_BIN="$W/claude-stub" CHOIR_RT_SEC_TOKEN=test-sec-token CHOIR_RT_WINDOWS="$W/windows" CHOIR_RT_LAST_RUN="$W/rt-last.json" \
  CHOIR_RT_MODELS="$W/rt-models.json" CHOIR_RT_NO_DISCOVERY=1 CHOIR_RT_NO_KIMI_WEB=1 CHOIR_DSH_PATCH_DIR="$W/dshp" CHOIR_RT_NO_BWRAP=1 \
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
python3 -c '
import json,sys; d=json.load(open(sys.argv[1])); v=d["voices"]; last=v[-1]
assert last["name"]=="typesafe" and last.get("not_voice") is True and last["tabs"]=={"room":None,"rounds":None,"exec":None}, last
assert isinstance(last.get("limit"), dict) and last["limit"].get("kind") in ("number","none","unknown"), last.get("limit")
assert [x["name"] for x in v[:-1]] and "typesafe" not in [x["name"] for x in v[:-1]], [x["name"] for x in v]' "$W/v.json" \
  && pass "/voices: TypeSafe последним — не голос (not_voice), вкладок нет, карточка счёта есть" || fail "/voices typesafe"
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
# память нити из ленты: только deepseek (без сессии), только комната, отдельным запросом
[ "$(code /voices '{"voice":"deepseek","scope":"room","memory":false}')" = 200 ] && pass "memory deepseek room → 200" || fail "memory deepseek"
[ "$(code /voices '{"voice":"claude","scope":"room","memory":false}')" = 400 ] && pass "memory claude → 400 (сессия есть)" || fail "memory claude"
[ "$(code /voices '{"voice":"deepseek","scope":"rounds","memory":true}')" = 400 ] && pass "memory rounds → 400 (только комната)" || fail "memory rounds"
[ "$(code /voices '{"voice":"deepseek","scope":"room","memory":true,"model":"deepseek-v4-pro"}')" = 400 ] && pass "memory вместе с model → 400" || fail "memory+model"
curl -s "$B/voices" | python3 -c '
import json,sys; d=json.load(sys.stdin)
v={x["name"]:x for x in d["voices"]}
assert v["deepseek"]["tabs"]["room"]["memory"] is False and v["deepseek"]["tabs"]["room"]["can_memory"], v["deepseek"]["tabs"]["room"]
assert "can_memory" not in v["claude"]["tabs"]["room"]
' && pass "GET /voices: memory=false после выключения, can_memory только у deepseek" || fail "GET /voices memory"
grep -q '"memory": false' "$W/rt-voices.json" && pass "memory сохранена в rt-voices.json (room)" || fail "memory не в файле"
grep -q 'память нити ВЫКЛЮЧЕНА' "$W/journal/live.jsonl" && pass "voice_config в ленте о памяти" || fail "voice_config memory"
post /voices '{"voice":"deepseek","scope":"room","memory":true}' >/dev/null
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
# умолчание codex берётся из ~/.codex/config.toml ЭТОЙ машины (было gpt-5.6-sol,
# с 2026-09-06 gpt-6-astra) — проверяем свойство, а не имя: оставшееся усилие
# либо снято, либо входит в лестницу модели умолчания.
sys.exit(0 if t["set_model"] is None and (t["set_effort"] is None or t["set_effort"] in (t.get("efforts") or [])) else 1)' \
  && pass "после сброса усилие сверено с моделью умолчания" || fail "сброс: усилие не сверено"
[ "$(code /voices '{"voice":"kimi","scope":"room","model":"kimi-k9-new"}')" = 200 ] && fail "kimi без алиаса принят как известный" || pass "kimi: имя без алиаса — не «известное» (принято лишь по форме или отклонено)"
grep -q '"voice_config"' "$W/journal/live.jsonl" && grep -q '\[кресло\]' "$W/journal/live.jsonl" && pass "событие кресла подписано [кресло]" || fail "событие кресла подписано не как кресло"
grep -c 'живая комната' "$W/journal/live.jsonl" | grep -q '^0$' || { grep '\[кресло\]' "$W/journal/live.jsonl" | grep -q 'живая комната' && fail "у события кресла комнатная область" || pass "у события кресла нет комнатной области"; }
[ "$(code /round '{"question":"q","name":"dup","voices":["claude","claude"]}')" = 400 ] && pass "/round: дубли — не два голоса" || fail "/round дубли"
[ "$(code /round '{"question":"q","name":"none","voices":[]}')" = 400 ] && pass "/round: пустой список → 400" || fail "/round пусто"
[ ! -e "$W/journal/rounds/RoundTable/QUESTION-dup.md" ] && pass "/round: отказ до записи файла вопроса" || fail "/round: файл вопроса записан при отказе"
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
cat >> "$W/journal/room.jsonl" <<'EOF'
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
# «снять сейчас» (ранние отказы): флаг только для идущего акта и pgid из early_warn ленты
[ "$(code /drop '{"act":"deadbeef01","pgid":4242}')" = 409 ] && pass "/drop: акт не идёт → 409" || fail "/drop 409"
[ "$(code /drop '{"act":"x","pgid":0}')" = 400 ] && pass "/drop: кривые act/pgid → 400" || fail "/drop 400"
[ "$(code /round_step '{"name":"t2","step":"rebut"}')" = 409 ] && pass "/round_step: без слепой фазы → 409" || fail "/round_step без ответов"
[ "$(code /round_step '{"name":"t9","step":"summarize"}')" = 409 ] && pass "/round_step: без жребия → 409" || fail "/round_step без жребия"
[ "$(code /round '{"question":"q","name":"pj","project":"/nonexistent/dir"}')" = 400 ] && pass "/round: проект не каталог → 400 до записи файла" || fail "/round проект"
[ ! -e "$W/journal/rounds/RoundTable/QUESTION-pj.md" ] && pass "/round: файл вопроса при отказе не создан" || fail "/round: файл вопроса создан при отказе"
# ── шаги раунда с ЗАГЛУШКОЙ дирижёра: argv в файл, сон 3 с, код 0 ──
# (ревизия 2026-09-03: без стаба «проверка занятости мертва с рождения» и
# «финал без поля round» проходили тесты — codex, grok, kimi, claude, субагент)
cp "$W/chamber/choir.py" "$W/chamber/choir_real.py"   # живой дирижёр — для проверок его функций
cat > "$W/chamber/choir.py" <<'EOF'
import sys, time, os
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "argv.log"), "a").write(" ".join(sys.argv[1:]) + "\n")
time.sleep(3 if sys.argv[1] in ("rebut", "summarize") else 0)
EOF
R="$(post /round_step '{"name":"t1","step":"rebut"}')"
echo "$R" | grep -q '"act"' && pass "/round_step rebut → 200 (стаб дирижёра)" || fail "/round_step 200: $R"
[ "$(code /round_step '{"name":"t1","step":"summarize"}')" = 409 ] && pass "/round_step: второй шаг того же раунда во время первого → 409" || fail "/round_step: занятость не видна"
sleep 5
grep -q "^rebut --round t1$" "$W/chamber/argv.log" && pass "/round_step: argv «rebut --round t1» без --by и без лишнего" || fail "argv rebut: $(cat "$W/chamber/argv.log")"
tail -n 3 "$W/journal/live.jsonl" | grep -q '"status": "done"' && tail -n 3 "$W/journal/live.jsonl" | grep '"status": "done"' | grep -q '"round": "t1"' && pass "финал акта несёт round (карточка появится)" || fail "финал акта без round: $(tail -n 2 "$W/journal/live.jsonl" | cut -c1-200)"
tail -n 3 "$W/journal/live.jsonl" | grep '"status": "done"' | grep -q '"step": "rebut"' && pass "финал акта несёт step" || fail "финал без step"
[ "$(code /round_step '{"name":"t1","step":"summarize"}')" = 200 ] && pass "/round_step summarize после витка → 200" || fail "/round_step summarize"
sleep 5
grep -q "^summarize --round t1 --out $W/journal/rounds/tmp/SUMMARY-t1.md$" "$W/chamber/argv.log" && pass "summarize: без --by (сводчика выбирает choir.py), --out абсолютный в journal/rounds/<проект жребия>/" || fail "argv summarize: $(cat "$W/chamber/argv.log")"
mkdir -p "$W/proj2" && git -C "$W/proj2" init -q
# (ниже заглушки дирижёра: happy-path /round поднимает choir.py — с настоящим
# он звал бы drand и голоса; нашёл grok в ревизии кнопки)
# «Развить тему»: свод родителя — в вопрос дословно, parent — полем события
cat >> "$W/journal/room.jsonl" <<'ROOM'
{"id":"b1","ts":"2026-09-03T11:00:00+00:00","round":"t3","phase":"pick","voice":"choir","role":"lot","text":"grok","conductor":"grok","candidates":["claude","grok"]}
{"id":"b2","ts":"2026-09-03T11:00:05+00:00","round":"t3","phase":"summary","voice":"grok","role":"summary","status":"ok","text":"# Свод t3\n\n**Нерешено.** Спор о ширине колонки.  \n\n","coverage":{"phases":[{"phase":"blind","called":["claude","grok"],"ok":["grok"],"pass":[],"fell":{"claude":{"status":"error","detail":"упал"}},"stubs":{},"flags":{}}],"debts":["claude (blind, error)"]}}
{"id":"b3","ts":"2026-09-03T11:00:06+00:00","round":"t4","phase":"summary","voice":"grok","role":"summary","status":"error","text":""}
ROOM
[ "$(code /round '{"question":"а если колонку убрать?","name":"t3-2","parent":"t3","voices":["claude","grok"]}')" = 200 ] && pass "/round parent: развитие раунда со сводом → 200" || fail "/round parent 200"
QF="$W/journal/rounds/RoundTable/QUESTION-t3-2.md"
grep -q "^а если колонку убрать?" "$QF" && grep -q "РАЗВИТИЕ РАУНДА «t3»" "$QF" && grep -q "ведущий: grok, свод писал: grok" "$QF" && grep -q "Спор о ширине колонки" "$QF" && pass "/round parent: вопрос Автора первым, свод родителя дословно, ведущий назван" || fail "/round parent: файл вопроса: $(cat "$QF" 2>/dev/null | head -c 300)"
python3 - "$QF" <<'PY' && pass "/round parent: свод перенесён БАЙТ В БАЙТ (хвостовые пробелы и переводы строк целы)" || fail "/round parent: свод не дословно"
import sys; t=open(sys.argv[1],encoding="utf-8").read()
assert "Спор о ширине колонки.  \n\n\n\n--- КОНЕЦ СВОДА" in t, repr(t[-80:])
PY
grep -q '"parent": "t3"' "$W/journal/live.jsonl" && grep -q 'развитие «t3»' "$W/journal/live.jsonl" && pass "/round parent: поле parent и метка в событии" || fail "/round parent: событие без parent"
[ "$(code /round '{"question":"q","name":"t4-2","parent":"t4","voices":["claude","grok"]}')" = 409 ] && pass "/round parent: свод не удался → 409" || fail "/round parent t4"
[ "$(code /round '{"question":"q","name":"n-2","parent":"nope","voices":["claude","grok"]}')" = 404 ] && pass "/round parent: неизвестный родитель → 404" || fail "/round parent nope"
[ "$(code /round '{"question":"q","name":"t3","parent":"t3","voices":["claude","grok"]}')" = 400 ] && pass "/round parent: то же имя → 400" || fail "/round parent same"
[ "$(code /round '{"question":"q","name":"bad-2","parent":"../t3","voices":["claude","grok"]}')" = 400 ] && [ ! -e "$W/journal/rounds/RoundTable/QUESTION-bad-2.md" ] && [ ! -e "$W/journal/rounds/RoundTable/QUESTION-t3.md" ] && pass "/round parent: кривое имя родителя → 400, файлов нет (и для same-name)" || fail "/round parent bad name"
grep -q "КОНЕЦ СВОДА раунда «t3»" "$QF" && pass "/round parent: свод ограждён с конца (инъекция из свода не сливается с концом вопроса)" || fail "/round parent: нет ограды"
grep -q "^> Карта покрытия (по журналу): слепая фаза — звали 2, ответили 1, упали: claude (error); долги: claude (blind, error). Свод писал grok.$" "$QF" && pass "/round parent: шапка покрытия родителя из журнала — перед сводом" || fail "/round parent: шапки покрытия нет: $(grep -n Карта "$QF")"
curl -s "$B/round_view?name=t3" | python3 -c '
import json,sys; d=json.load(sys.stdin)
assert d["summary_head"].startswith("> Карта покрытия (по журналу): слепая фаза — звали 2, ответили 1"), d["summary_head"]
assert d["summary"]["coverage"]["debts"]==["claude (blind, error)"], d["summary"]
' && pass "/round_view: summary_head из coverage свода, поле coverage не срезано slim" || fail "/round_view summary_head"
curl -s "$B/round_view?name=t1" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["summary_head"] is None' && pass "/round_view: без свода summary_head=null" || fail "/round_view t1 summary_head"
curl -s "$B/round_view?name=t1" | python3 -c '
import json,sys; d=json.load(sys.stdin)
assert d["coverage_now"].startswith("> Карта покрытия (по журналу): слепая фаза — звали 2, ответили 2") and "Свод писал" not in d["coverage_now"], d["coverage_now"]' && pass "/round_view: coverage_now считается до свода по записям раунда" || fail "/round_view coverage_now"
[ ! -e "$W/journal/rounds/RoundTable/QUESTION-t4-2.md" ] && [ ! -e "$W/journal/rounds/RoundTable/QUESTION-n-2.md" ] && pass "/round parent: отказ до записи файла вопроса" || fail "/round parent: файл при отказе"
R="$(post /round "{\"question\":\"q\",\"name\":\"pj2\",\"project\":\"$W/proj2\"}")"
echo "$R" | grep -q '"act"' && pass "/round с проектом → 200" || fail "/round с проектом: $R"
sleep 2
grep -q "^pick --round pj2 --seed .* --project $W/proj2$" "$W/chamber/argv.log" && pass "/round: --project уходит в pick (цепочка по шагам)" || fail "argv pick без --project: $(grep pick "$W/chamber/argv.log")"
grep -q "^ask --round pj2 --seed" "$W/chamber/argv.log" && ! grep "^ask --round pj2" "$W/chamber/argv.log" | grep -q -- "--project" && pass "/round: ask без --project (берёт из жребия)" || fail "argv ask: $(grep '^ask' "$W/chamber/argv.log")"
# ── резерв Кими по ключам API (подписка Kimi Code первой линией, 2026-09-23) ──
[ "$(code /voices '{"voice":"kimi","scope":"room","reserve":false}')" = 200 ] && pass "reserve kimi room → 200" || fail "reserve kimi room"
[ "$(code /voices '{"voice":"kimi","scope":"rounds","reserve":true}')" = 200 ] && pass "reserve kimi rounds → 200 (одна галочка на обе вкладки)" || fail "reserve kimi rounds"
[ "$(code /voices '{"voice":"claude","scope":"room","reserve":false}')" = 400 ] && pass "reserve claude → 400 (резервной линии нет)" || fail "reserve claude"
[ "$(code /voices '{"voice":"kimi","scope":"exec","reserve":false}')" = 400 ] && pass "reserve exec → 400" || fail "reserve exec"
[ "$(code /voices '{"voice":"kimi","scope":"room","reserve":"no"}')" = 400 ] && pass "reserve не bool → 400" || fail "reserve not bool"
[ "$(code /voices '{"voice":"kimi","scope":"room","reserve":false,"model":"kimi-k3"}')" = 400 ] && pass "reserve вместе с model → 400" || fail "reserve+model"
post /voices '{"voice":"kimi","scope":"room","reserve":false}' >/dev/null
curl -s "$B/voices" > "$W/reserve.json"
python3 - "$W/reserve.json" <<'PY' && pass "GET /voices: reserve=false на room и rounds, can_reserve только у kimi" || fail "GET /voices reserve: $(head -c 300 "$W/reserve.json")"
import json,sys; d=json.load(open(sys.argv[1])); v={x["name"]: x for x in d["voices"]}
k=v["kimi"]["tabs"]
assert k["room"]["reserve"] is False and k["room"]["can_reserve"] and "plan_line" in k["room"], k["room"]
assert k["rounds"]["reserve"] is False and k["rounds"]["can_reserve"], k["rounds"]
assert "can_reserve" not in v["claude"]["tabs"]["room"]
PY
grep -q '"reserve": false' "$W/rt-voices.json" && pass "reserve сохранён в rt-voices.json (room)" || fail "reserve не в файле"
grep -q 'резерв по ключам API ВЫКЛЮЧЕН' "$W/journal/live.jsonl" && pass "voice_config в ленте о резерве" || fail "voice_config reserve"
python3 - "$W/rt-voices.json" <<'PY' && pass "rt-voices: reserve у claude отбрасывается валидацией при чтении" || fail "rt-voices reserve validation"
import json,sys,subprocess,os
p=sys.argv[1]; d=json.load(open(p)); d.setdefault("claude",{}).setdefault("room",{})["reserve"]=False; json.dump(d,open(p,"w"))
PY
[ "$(code /voices '{"voice":"kimi","scope":"room","model":"k3-256k"}')" = 200 ] && pass "модель подписки k3-256k проходит model_re" || fail "model_re k3"
post /voices '{"voice":"kimi","scope":"room","reserve":true}' >/dev/null
post /voices '{"voice":"kimi","scope":"room","model":""}' >/dev/null 2>&1 || true

# ── «продолжить отсюда» (раунд prodolzhit-lyuboe-v1; решения Автора 2026-09-23) ──
# адрес → текст из журнала на сервере; отказы словами; в раунд — без имени автора,
# автор вне жребия (--exclude у pick), поле anchor в событии запуска
cat >> "$W/journal/room.jsonl" <<'ROOM'
{"id":"c1","ts":"2026-09-03T12:00:00+00:00","round":"t5","phase":"blind","voice":"kimi","role":"answer","status":"ok","text":"тезис кими про якорь"}
{"id":"c2","ts":"2026-09-03T12:00:01+00:00","round":"t5","phase":"blind","voice":"grok","role":"answer","status":"pass","text":"ПАС"}
ROOM
[ "$(code /round '{"question":"а если якорь?","name":"t5-a","anchor":"room:c1","voices":["claude","grok","kimi"]}')" = 200 ] && pass "/round anchor: ответ раунда как якорь → 200" || fail "/round anchor 200"
QA="$W/journal/rounds/RoundTable/QUESTION-t5-a.md"
grep -q "^а если якорь?" "$QA" && grep -q "тезис кими про якорь" "$QA" && grep -q "один из голосов стола" "$QA" && ! grep -q "kimi" "$QA" && pass "/round anchor: текст дословно, БЕЗ имени автора (решение Автора)" || fail "/round anchor: вопрос: $(cat "$QA")"
grep -q "без мыслей модели" "$QA" && pass "/round anchor: названо, что мыслей модели в якоре нет" || fail "/round anchor: мысли не названы"
sleep 2
grep -q "^pick --round t5-a --seed .* --anchor room:c1 --exclude kimi$" "$W/chamber/argv.log" && pass "/round anchor: pick получает --anchor и --exclude автора" || fail "argv pick anchor: $(grep t5-a "$W/chamber/argv.log")"
grep "^ask --round t5-a" "$W/chamber/argv.log" | grep -q -- "--voices claude,grok,kimi" && ! grep "^ask --round t5-a" "$W/chamber/argv.log" | grep -q -- "--exclude" && pass "/round anchor: ask — полный состав, автор якоря участвует" || fail "argv ask anchor"
grep -q '"anchor": {"addr": "room:c1", "author": "kimi", "sha": "[0-9a-f]*", "kind": "answer", "round": "t5", "phase": "blind"}' "$W/journal/live.jsonl" && pass "/round anchor: поле anchor (скаляр, с автором) в событии запуска" || fail "/round anchor: поля нет"
[ "$(code /round '{"question":"q","name":"t5-b","anchor":"room:c2","voices":["claude","grok"]}')" = 400 ] && [ ! -e "$W/journal/rounds/RoundTable/QUESTION-t5-b.md" ] && pass "/round anchor: ПАС → 400, файла нет" || fail "/round anchor pass"
[ "$(code /round '{"question":"q","name":"t5-c","anchor":"room:c1","parent":"t3","voices":["claude","grok"]}')" = 400 ] && pass "/round anchor + parent → 400" || fail "/round anchor+parent"
[ "$(code /round '{"question":"q","name":"t5-d","anchor":"room:c1","voices":["kimi","claude"]}')" = 200 ] && pass "/round anchor: автор в составе из двух — жребий из одного, 200" || fail "/round anchor two"
[ "$(code /round '{"question":"q","name":"t5-e","anchor":"live:999999","voices":["claude","grok"]}')" = 400 ] && pass "/round anchor: нет события → 400" || fail "/round anchor 404"
# /act: якорь на реплику ленты; сверх потолка — отказ. Комната — СТАБ:
# happy-path /act поднимает live.py, а с настоящим он звал бы claude платно
cp "$W/chamber/live.py" "$W/chamber/live_real.py"
cat > "$W/chamber/live.py" <<'EOF2'
import sys, os
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "argv.log"), "a").write("live " + " ".join(sys.argv[1:]) + "\n")
EOF2
python3 - "$W/journal/live.jsonl" <<'PY'
import json,sys,time
p=sys.argv[1]; ids=[json.loads(l)["id"] for l in open(p,encoding="utf-8") if l.strip()]
n=max(ids)+1
with open(p,"a",encoding="utf-8") as f:
    f.write(json.dumps({"id":n,"ts":time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),"author":"grok","kind":"say","text":"реплика грока"},ensure_ascii=False)+"\n")
    f.write(json.dumps({"id":n+1,"ts":time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),"author":"grok","kind":"say","text":"x"*6001},ensure_ascii=False)+"\n")
    f.write(json.dumps({"id":n+2,"ts":time.strftime("%Y-%m-%dT%H:%M:%S+00:00"),"author":"kimi","kind":"pass","text":"ПАС"},ensure_ascii=False)+"\n")
open("/dev/stdout","w").write(str(n))
PY
AN="$(python3 -c "import json;print(max(json.loads(l)['id'] for l in open('$W/journal/live.jsonl',encoding='utf-8') if l.strip())-2)")"
# ждём, пока предыдущие акты стенда отпустят claude: окно честно отвечает
# «уже отвечают: claude» — это и был редкий «флак» этой проверки
IDLE=0; for _ in $(seq 1 120); do curl -s "$B/state" | python3 -c 'import json,sys; sys.exit(0 if not json.load(sys.stdin)["running"] else 1)' && { IDLE=1; break; }; sleep 0.25; done
[ "$IDLE" = 1 ] || fail "перед проверкой якоря окно так и не освободилось за 30 с (идущие акты: $(curl -s "$B/state" | python3 -c 'import json,sys; print([r["label"] for r in json.load(sys.stdin)["running"]])'))"
R="$(post /act "{\"text\":\"@claude продолжим\",\"voices\":[\"claude\"],\"anchor\":\"live:$AN\"}")"
echo "$R" | grep -q '"act"' && pass "/act anchor: реплика ленты как якорь → 200" || fail "/act anchor: $R"
# ждём событие accepted и вызов live.py, а не 2 секунды: на длинной ленте спавн медленнее (флак)
for _ in $(seq 1 240); do grep -q '"anchor_addr": "live:'"$AN"'"' "$W/journal/live.jsonl" 2>/dev/null && grep -q "^live say" "$W/chamber/argv.log" 2>/dev/null && break; sleep 0.25; done
grep -q '"anchor_addr": "live:'"$AN"'"' "$W/journal/live.jsonl" && pass "/act anchor: адрес в событии accepted" || fail "/act anchor accepted"
grep -q "^live say @claude продолжим --anchor live:$AN --voices claude$" "$W/chamber/argv.log" && pass "/act anchor: live.py получает --anchor адресом (текст вставит комната)" || fail "argv live anchor: $(grep '^live' "$W/chamber/argv.log")"
R="$(post /act "{\"text\":\"q\",\"voices\":[\"claude\"],\"anchor\":\"live:$((AN+1))\"}")"
echo "$R" | grep -q "обрезки нет" && pass "/act anchor: сверх потолка 6000 → отказ словами, не обрезка" || fail "/act anchor cap: $R"
[ "$(code /act "{\"text\":\"q\",\"voices\":[\"claude\"],\"anchor\":\"live:$((AN+2))\"}")" = 400 ] && pass "/act anchor: ПАС → 400" || fail "/act anchor pass"
[ "$(code /act '{"text":"q","voices":["claude"],"anchor":"live:1; rm -rf /"}')" = 400 ] && pass "/act anchor: кривой адрес → 400" || fail "/act anchor bad"
curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["anchor_cap"]==6000, d.get("anchor_cap")' && pass "/state: anchor_cap=6000 (потолок из сервера)" || fail "/state anchor_cap"
mv "$W/chamber/live_real.py" "$W/chamber/live.py"    # дальше секции читают настоящий live.py

# ── цель целеполагателя и адресная опция комнаты ──────────────────
R="$(post /goal '{"text":"Довести окно до релиза 0.2"}')"
echo "$R" | grep -q '"goal": "Довести окно до релиза 0.2"' && pass "/goal → событие goal" || fail "/goal: $R"
curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("goal")=="Довести окно до релиза 0.2" else 1)' && pass "/state несёт действующую цель" || fail "/state без цели"
post /goal '{"text":""}' > /dev/null; curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("goal")=="" else 1)' && pass "пустая цель снимает цель" || fail "цель не снялась"
[ "$(code /goal "{\"text\":\"$(python3 -c 'print("x"*2001)')\"}")" = 400 ] && pass "/goal: длиннее 2000 → 400" || fail "/goal длина"
[ "$(code /goal '{"text":["a"]}')" = 400 ] && pass "/goal: не строка → 400" || fail "/goal тип"
( cd "$W/chamber" && python3 - <<'EOF'
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
python3 - "$W/journal/live.jsonl" <<'EOF'
import json, sys
# 600 КБ балласта после цели — хвост в 512 КБ её терял
with open(sys.argv[1], "a", encoding="utf-8") as f:
    for i in range(700):
        f.write(json.dumps({"id": 900000 + i, "ts": "2026-09-06T00:00:00+00:00", "author": "choir", "kind": "note", "text": "x" * 900}, ensure_ascii=False) + "\n")
EOF
curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("goal")=="Цель-А для проверки" else 1)' && pass "/state.goal не теряется за 600 КБ балласта (весь файл, не хвост)" || fail "/state.goal потерялся за хвостом"
( cd "$W/chamber" && python3 -c 'import sys; sys.path.insert(0,"."); import live; assert live._read_goal()=="Цель-А для проверки"' ) && pass "live.current_goal видит ту же цель, что окно" || fail "live/окно расходятся в цели"
# сценарий комнаты с ПОДДЕЛЬНЫМИ голосами (turn подменён): адрес → ответ адресата → рук нет → ветка закрыта
( cd "$W/chamber" && CHOIR_PEER=1 python3 - <<'EOF'
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
evs = [json.loads(l) for l in open("../journal/live.jsonl", encoding="utf-8")]
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
( cd "$W/chamber" && python3 - <<'EOF'
import sys, json, importlib.util
# choir.py здесь заглушка — читаем ЖИВОЙ дирижёр по абсолютному пути
spec = importlib.util.spec_from_file_location("choir_live", "choir_real.py")
m = importlib.util.module_from_spec(spec); sys.modules["choir_live"] = m; spec.loader.exec_module(m)
import os
m.ROOM = __import__("pathlib").Path(os.getcwd()).parent / "journal" / "room.jsonl"
m._append({"id": "s1", "ts": "2026-09-06T00:00:00+00:00", "round": "g1", "phase": "blind", "voice": "arr", "role": "seed", "text": "q", "packet_prefix": "ЦЕЛЬ (целеполагатель — Автор): строка 1\nстрока 2\n\n", "goal": None})
m._append({"id": "s2", "ts": "2026-09-06T00:00:01+00:00", "round": "g2", "phase": "blind", "voice": "arr", "role": "seed", "text": "q", "packet_prefix": "ЦЕЛЬ (целеполагатель — Автор): a\nb\n\n", "goal": "a\nb"})
assert m.goal_of_seed("g1") == "строка 1\nстрока 2", m.goal_of_seed("g1")
assert m.goal_of_seed("g2") == "a\nb"
m.snapshot_goal("зафиксированная"); assert m.goal_notice().startswith("ЦЕЛЬ (целеполагатель — Автор): зафиксированная")
print("goal_of_seed ok")
EOF
) && pass "choir.py: goal_of_seed многострочная (поле goal и запасной разбор префикса), снимок цели" || fail "choir.py: goal_of_seed"

# ── кресло секретаря: POST /secretary → акт secretary_turn.py со стабом CLI; события sec в ленте ──
cat > "$W/claude-stub" <<'EOF2'
#!/usr/bin/env python3
import sys, json
p = sys.stdin.read()
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "стаб думает"}]}}, ensure_ascii=False))
print(json.dumps({"type": "result", "result": "стаб-секретарь: принято — " + p[-30:]}, ensure_ascii=False))
EOF2
chmod +x "$W/claude-stub"
# без токена кресла — 403 и заметка в ленте (голос из клетки мог бы слать «реплики Автора»)
[ "$(code /secretary '{"text":"взломать"}')" = 403 ] && pass "/secretary без токена → 403" || fail "/secretary без токена"
grep -q 'POST /secretary отклонён' "$W/journal/live.jsonl" && pass "/secretary без токена: заметка в ленте" || fail "/secretary заметка об отказе"
grep -q '"text": "взломать"' "$W/journal/live.jsonl" && fail "/secretary без токена: реплика попала в ленту" || pass "/secretary без токена: реплика в ленту не попала"
curl -s "$B/windows" | grep -q 'test-sec-token' && fail "/windows отдаёт токен кресла" || pass "/windows: токена кресла в карточках нет"
# с 2026-10-08 токен постоянный: один файл sec-token (стенд даёт его средой — файла нет), файла <pid>.sec-token больше нет
[ -z "$(ls "$W/windows/"*.sec-token 2>/dev/null)" ] && pass "токен кресла: второго файла с секретом на pid в реестре нет" || fail "файл <pid>.sec-token: $(ls "$W/windows/"*.sec-token)"
( cd "$RT_DIR" && CHOIR_RT_WINDOWS="$W/windows2" CHOIR_RT_NO_DISCOVERY=1 python3 -c '
import os, sys; os.environ.pop("CHOIR_RT_SEC_TOKEN", None); sys.path.insert(0, "."); sys.path.insert(0, "chamber")
import roundtable as rt; t1 = rt._sec_token(); t2 = rt._sec_token(); f = rt._sec_token_file()
assert t1 == t2 and f.read_text().strip() == t1 and oct(f.stat().st_mode & 0o777) == "0o600", (t1, t2)' ) \
  && pass "токен кресла: постоянный файл sec-token 0600, тот же при повторном чтении" || fail "sec-token постоянный"
( cd "$RT_DIR" && CHOIR_RT_WINDOWS="$W/windows-proj" CHOIR_RT_NO_DISCOVERY=1 python3 -c '
import os, sys; os.environ.pop("CHOIR_RT_SEC_TOKEN", None); sys.path.insert(0, "."); sys.path.insert(0, "chamber")
import roundtable as rt
d = rt.WINDOWS_DIR; d.mkdir(parents=True, exist_ok=True); (d / "sec-token").write_text("legacy-token-123\n")
rt.PROJECT = "/tmp/proj-a"; fa = rt._sec_token_file(); ta = rt._sec_token()
assert ta != "legacy-token-123" and fa.exists() and not (d / "sec-token").exists(), ta   # общий не раздаётся и удалён
rt.PROJECT = "/tmp/proj-b"; fb = rt._sec_token_file(); tb = rt._sec_token()
assert fb != fa and tb != ta, (ta, tb)                                   # у проектов разные токены
nb = rt.rotate_sec_token()                                              # смена у проекта b
assert fb.read_text().strip() == nb and fa.read_text().strip() == ta, "смена задела чужой проект"
rt.PROJECT = "/tmp/proj-a"; assert rt._sec_token_current() == ta
' ) && pass "токен на проект: у проектов разные токены, общий файл не раздаётся и удалён, смена у одного проекта не трогает другой" || fail "токен на проект"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/secretary/token" -H 'Content-Type: application/json' -d '{}')" = 403 ] && pass "/secretary/token без токена → 403" || fail "/secretary/token 403"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/secretary/token" -H 'Content-Type: application/json' -H 'X-RT-Token: test-sec-token' -d '{}')" = 409 ] && pass "/secretary/token: токен задан средой стенда → 409, не меняется" || fail "/secretary/token 409"
( cd "$RT_DIR" && CHOIR_RT_WINDOWS="$W/windows3" CHOIR_RT_NO_DISCOVERY=1 python3 -c '
import os, sys, time; os.environ.pop("CHOIR_RT_SEC_TOKEN", None); sys.path.insert(0, "."); sys.path.insert(0, "chamber")
import roundtable as rt
rt.SEC_TOKEN = rt._sec_token(); old = rt._sec_token_current(); assert rt._sec_token_ok(old)   # как main() после --project
new = rt.rotate_sec_token(); f = rt._sec_token_file()
assert new != old and rt._sec_token_current() == new and rt._sec_token_ok(new) and not rt._sec_token_ok(old), (old, new)
assert oct(f.stat().st_mode & 0o777) == "0o600" and not [p for p in rt.WINDOWS_DIR.iterdir() if p.name.startswith(".sec-token.") and not p.name.endswith(".lock")]
f.write_text("abcdef0123456789\n"); assert rt._sec_token_current() == "abcdef0123456789"   # смена другим окном видна без перезапуска
f.unlink(); assert rt._sec_token_current() == "abcdef0123456789" and rt._sec_token_current() != old   # файл пропал — последний прочитанный, не отменённый токен старта
' ) && pass "rotate_sec_token: новый действует, прежний нет, файл 0600 без хвостов; смена из другого окна видна сразу" || fail "rotate_sec_token"
postt() { curl -s -X POST "$B$1" -H 'Content-Type: application/json' -H 'X-RT-Token: test-sec-token' -d "$2"; }
codet() { curl -s -o /dev/null -w '%{http_code}' -X POST "$B$1" -H 'Content-Type: application/json' -H 'X-RT-Token: test-sec-token' -d "$2"; }
[ "$(codet /secretary '{"text":"   "}')" = 400 ] && pass "/secretary: пустая реплика → 400" || fail "/secretary пусто"
R="$(postt /secretary '{"text":"запиши решение: тест кресла"}')"
echo "$R" | grep -q '"act"' && pass "/secretary: реплика принята, акт запущен" || fail "/secretary 200: $R"
SECACT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["act"])')
for _ in $(seq 1 60); do sleep 0.25; grep -q '"author": "secretary"' "$W/journal/live.jsonl" 2>/dev/null && break; done
python3 - "$W/journal/live.jsonl" <<'PY' && pass "/secretary: в ленте реплика Автора (sec/arr) и ответ стаба (sec/secretary, rights=full, act)" || fail "/secretary события"
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
sec = [e for e in evs if e.get("kind") == "sec"]
assert [e["author"] for e in sec][-2:] == ["arr", "secretary"], [e.get("author") for e in sec]
assert sec[-1]["rights"] == "full" and sec[-1]["text"].startswith("стаб-секретарь") and sec[-1].get("act"), sec[-1]
assert sec[-2]["text"] == "запиши решение: тест кресла", sec[-2]
PY
curl -s "$B/secretary?n=5" | python3 -c '
import json,sys; d=json.load(sys.stdin); dl=d.get("dialogue") or []; assert [e["author"] for e in dl][-2:] == ["arr","secretary"], dl' \
  && pass "/secretary: диалог отдаётся вкладке" || fail "/secretary dialogue"
# вендор кресла (решение Автора 2026-10-08): список агентов с моделью/усилием комнаты, текущий агент нити; gemini — нет кресла
curl -s "$B/secretary?n=5" | python3 -c '
import json,sys; d=json.load(sys.stdin); ag={a["name"]:a for a in d["agents"]}
assert set(ag) >= {"claude","codex","grok","kimi","deepseek","gemini"}, list(ag)
assert ag["gemini"]["can"] is False and "кресла" in ag["gemini"]["why"], ag["gemini"]
assert ag["claude"]["can"] and ag["codex"]["can"] and "model" in ag["claude"], ag["claude"]
assert d["agent"] == "claude", d.get("agent")' \
  && pass "/secretary: agents (кто умеет, модель/усилие комнаты), gemini без кресла, текущий агент нити — claude" || fail "/secretary agents"
[ "$(codet /secretary '{"text":"а ты?","agent":"gemini"}')" = 400 ] && pass "/secretary: agent=gemini → 400 (кресла нет)" || fail "/secretary gemini 400"
[ "$(codet /secretary '{"text":"а ты?","agent":"nobody"}')" = 400 ] && pass "/secretary: неизвестный агент → 400" || fail "/secretary nobody 400"
curl -s "$B/act_log?id=$SECACT&kind=act" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert "→ secretary" in d["text"] and "стаб думает" in d["text"], d["text"][:300]' \
  && pass "/secretary: стенограмма хода секретаря (шапка, мысли) читается вкладкой" || fail "/secretary лог акта"
# «продолжить отсюда» → секретарю: якорь на ответ стаба; кривой якорь → 400 до запуска
SECID=$(python3 -c 'import json,sys; evs=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]; print([e for e in evs if e.get("kind")=="sec" and e.get("author")=="secretary"][-1]["id"])' "$W/journal/live.jsonl")
[ "$(codet /secretary "{\"text\":\"продолжи\",\"anchor\":\"live:999999\"}")" = 400 ] && pass "/secretary: кривой якорь → 400 до запуска" || fail "/secretary якорь 400"
R="$(postt /secretary "{\"text\":\"продолжи вот это\",\"anchor\":\"live:$SECID\"}")"
echo "$R" | grep -q '"act"' && pass "/secretary с якорем: акт запущен" || fail "/secretary якорь: $R"
# ждём ОТВЕТ секретаря с полем anchor, а не вторую строку с подстрокой "secretary": она
# есть уже в anchor.author реплики Автора (ревизия: гонка на быстром стабе)
for _ in $(seq 1 80); do sleep 0.25; python3 -c '
import json,sys; evs=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]
sys.exit(0 if any(e.get("kind")=="sec" and e.get("author")=="secretary" and e.get("anchor") for e in evs) else 1)' "$W/journal/live.jsonl" && break; done
python3 - "$W/journal/live.jsonl" "$SECID" <<'PY' && pass "/secretary с якорем: у реплики Автора и ответа поле anchor с адресом" || fail "/secretary якорь события"
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
sec = [e for e in evs if e.get("kind") == "sec"][-2:]
assert [e["author"] for e in sec] == ["arr", "secretary"], sec
assert all(e.get("anchor", {}).get("addr") == "live:" + sys.argv[2] for e in sec), sec
PY
# статус-отказ: событие с текстом, но status=timeout — кнопки нет и сервер отказывает (одно правило)
TOID=$(cd "$RT_DIR/chamber" && CHOIR_JOURNAL="$W/journal" ROUNDTABLE_JOURNAL="$W/journal" python3 -c 'import live,sys; e=live.post("roundtable","edit_review","ревизия x [kimi]: (timeout) не уложился", voice="kimi", status="timeout", project=sys.argv[1]); print(e["id"])' "$W/proj")
[ "$(codet /secretary "{\"text\":\"продолжи\",\"anchor\":\"live:$TOID\"}")" = 400 ] && pass "/secretary: якорь на событие со статусом timeout → 400" || fail "/secretary якорь timeout"
curl -s "$B/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert "timeout" in d["anchor_bad_status"] and "error" in d["anchor_bad_status"], d.get("anchor_bad_status")' && pass "/state: anchor_bad_status — список статусов-отказов для кнопок окна" || fail "/state anchor_bad_status"
STATEF=$(ls "$W/journal/secretary/"state.*.json 2>/dev/null | head -1)
[ -n "$STATEF" ] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["turns"]==2 and d.get("session_id")' "$STATEF" && pass "/secretary: нить проекта сохранена (session_id, два хода)" || fail "/secretary нить"

# ── /act_log без файла — из ленты (2026-10-08): since отдаёт пусто, когда всё отдано; 404 без событий ──
( cd "$RT_DIR/chamber" && CHOIR_JOURNAL="$W/journal" ROUNDTABLE_JOURNAL="$W/journal" python3 -c '
import live
live.post("roundtable","act_status","act feed0001 принят: быстрый: вопрос без файла", act_id="feed0001", status="accepted", project="'"$W/proj"'")
live.post("grok","say","ответ без стенограммы", act="feed0001", project="'"$W/proj"'")
live.post("roundtable","act_status","act feed0001 done: быстрый", act_id="feed0001", status="done", project="'"$W/proj"'")' )
SZ=$(curl -s "$B/act_log?id=feed0001&kind=raw" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["source"]=="feed" and d["missing"] is True and d["done"] is True and "СТЕНОГРАММЫ АКТА feed0001 НЕТ" in d["text"] and "ответ без стенограммы" in d["text"], d; print(d["size"])') \
  && pass "/act_log без файла: текст из ленты, source=feed, missing, done" || fail "/act_log feed"
curl -s "$B/act_log?id=feed0001&kind=raw&since=$SZ" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["text"]=="" and d["next"]==d["size"], d' \
  && pass "/act_log без файла: since=размер → пусто (вкладка не дописывает заново)" || fail "/act_log feed since"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/act_log?id=feed9999&kind=raw")" = 404 ] && pass "/act_log: ни файла, ни событий → 404" || fail "/act_log 404"

# ── GET /secretary: решения Автора, очередь, отчёты (SECRETARY.md, 2026-10-06) ──
curl -s "$B/secretary" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert set(d) >= {"decisions","queue","reports","queue_all"}, d; assert d["decisions"] == [] and d["queue_all"] == [], d' \
  && pass "/secretary: пустая лента — пустые разделы" || fail "/secretary пусто"
( cd "$RT_DIR" && ROUNDTABLE_JOURNAL="$W/journal" CHOIR_JOURNAL="$W/journal" python3 chamber/secretary.py --project "$W/proj" decision "решение из теста" --by arr --recorded-by claude --about "тест" >/dev/null \
  && ROUNDTABLE_JOURNAL="$W/journal" CHOIR_JOURNAL="$W/journal" python3 chamber/secretary.py --project "$W/proj" queue add "пункт из теста" >/dev/null \
  && ROUNDTABLE_JOURNAL="$W/journal" CHOIR_JOURNAL="$W/journal" python3 chamber/secretary.py --project "$W/proj" report --text "отчёт из теста" --by claude >/dev/null ) \
  && pass "secretary.py: decision/queue/report записаны в ленту стенда" || fail "secretary.py запись"
curl -s "$B/secretary?n=5" | python3 -c '
import json,sys; d=json.load(sys.stdin)
assert [x["text"] for x in d["decisions"]] == ["решение из теста"] and d["decisions"][0]["author"] == "arr" and d["decisions"][0]["recorded_by"] == "claude", d["decisions"]
assert [x["text"] for x in d["queue"]] == ["пункт из теста"] and d["queue"][0]["status"] == "open", d["queue"]
assert [x["text"] for x in d["reports"]] == ["отчёт из теста"], d["reports"]' \
  && pass "/secretary: решение, открытый пункт и отчёт видны окну без проекта (вся лента)" || fail "/secretary содержимое"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/secretary?n=abc")" = 200 ] && pass "/secretary: кривой n → умолчание, не 500" || fail "/secretary n"

# ── GET /act_log: хвост лога акта для вкладок вывода (раунд вкладки-вывода-v1) ──
mkdir -p "$W/acts" "$W/leases"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/act_log?id=zz")" = 400 ] && pass "/act_log: кривой id → 400" || fail "/act_log id"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/act_log?id=deadbeef")" = 404 ] && pass "/act_log: лога нет → 404" || fail "/act_log 404"
printf 'строка 1\n\033[31mкрасное\033[0m %s/секрет sk-ABCDEFGHIJKLMNOP key=abcdefghijklmnop\nстрока 3\n' "$HOME" > "$W/acts/deadbeef.log"
curl -s "$B/act_log?id=deadbeef" | python3 -c '
import json,sys; d=json.load(sys.stdin)
assert "\x1b" not in d["text"] and "красное" in d["text"], d
import os; assert os.path.expanduser("~") not in d["text"] and "~/секрет" in d["text"], d
assert "sk-ABCDEFGHIJKLMNOP" not in d["text"] and "abcdefghijklmnop" not in d["text"] and "‹вырезано›" in d["text"], d
assert d["done"] is True and d["source"]=="act" and d["next"]==d["size"] and not d["reset"], d
print("scrub ok")' && pass "/act_log: ANSI, домашний путь и ключи вырезаны; done, next=size" || fail "/act_log: чистка/поля"
curl -s "$B/act_log?id=deadbeef&since=999999" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["reset"] is True and d["text"].startswith("строка 1"), d' \
  && pass "/act_log: смещение больше размера → reset, читаем с начала" || fail "/act_log reset"
N=$(curl -s "$B/act_log?id=deadbeef" | python3 -c 'import json,sys; print(json.load(sys.stdin)["next"])'); printf 'строка 4\n' >> "$W/acts/deadbeef.log"
curl -s "$B/act_log?id=deadbeef&since=$N" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["text"]=="строка 4\n" and not d["reset"], d' \
  && pass "/act_log: дочитывание с смещения отдаёт только новое" || fail "/act_log tail"
python3 -c 'open("'"$W"'/acts/deadbeef.log","w").write("x"*300000+"\nхвост\n")'
curl -s "$B/act_log?id=deadbeef" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["skipped"] is True and d["text"]=="хвост\n", (d["skipped"], d["text"][:40])' \
  && pass "/act_log: большой файл при первом чтении — только хвост, skipped" || fail "/act_log skipped"
# head=1 — «показать с начала» (Автор 2026-10-06): голова не пропускается, порция 64 КиБ, дальше по next
curl -s "$B/act_log?id=deadbeef&head=1" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["skipped"] is False and d["text"].startswith("xxxx") and d["more"] is True and 0 < d["next"] <= 65536, (d["skipped"], d["next"], d["more"])' \
  && pass "/act_log head=1: большой файл читается с начала порциями" || fail "/act_log head"
# архив стенограмм (CHOIR_RT_ACTS_ARCHIVE): файла в acts нет — берётся из архива, род источника тот же
mkdir -p "$W/acts-archive"; printf 'из архива\n' > "$W/acts-archive/abcd0001.raw.log"; printf 'лог из архива\n' > "$W/acts-archive/abcd0002.log"
curl -s "$B/act_log?id=abcd0001&kind=raw" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["source"]=="raw" and d["text"]=="из архива\n", d' && pass "/act_log: стенограмма из архива, когда в acts её нет" || fail "/act_log archive raw"
curl -s "$B/act_log?id=abcd0002" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["source"]=="act" and d["text"]=="лог из архива\n", d' && pass "/act_log: лог акта из архива" || fail "/act_log archive act"
printf 'свежий\n' > "$W/acts/abcd0001.raw.log"
curl -s "$B/act_log?id=abcd0001&kind=raw" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["text"]=="свежий\n", d' && pass "/act_log: acts сильнее архива" || fail "/act_log archive precedence"
curl -s "$B/act_log?id=deadbeef&kind=raw" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["source"]=="act", d' && pass "/act_log kind=raw без сырого файла → лог акта" || fail "/act_log raw fallback"
printf 'сырой вывод\n' > "$W/acts/deadbeef.raw.log"
curl -s "$B/act_log?id=deadbeef&kind=raw" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["source"]=="raw" and d["text"]=="сырой вывод\n", d' && pass "/act_log kind=raw: сырой вывод CLI" || fail "/act_log raw"
printf 'старая эпоха\n' > "$W/leases/edit-abc123def456.1.log"; sleep 0.02; printf 'кресло пишет\n' > "$W/leases/edit-abc123def456.2.log"
curl -s "$B/act_log?id=deadbeef&kind=chair&edit=abc123def456" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["source"]=="chair" and d["text"]=="кресло пишет\n", d' && pass "/act_log kind=chair: лог CLI кресла, свежая эпоха" || fail "/act_log chair"
curl -s "$B/" | grep -q "viewtabs" && pass "страница: вкладки вывода в скрипте" || fail "страница без вкладок вывода"
# ── канарейка слепоты в раунде (--canary) ──
[ "$(code /round "{\"question\":\"q\",\"name\":\"cn1\",\"auto\":true,\"voices\":[\"kimi\",\"codex\"],\"canary\":\"grok\",\"project\":\"$W\"}")" = 200 ] && pass "/round: канарейка grok принята" || fail "/round canary"
sleep 1; grep -q "^run --round cn1 .*--canary grok" "$W/chamber/argv.log" && pass "/round: --canary grok ушёл в choir.py run" || fail "argv canary: $(grep cn1 "$W/chamber/argv.log")"
[ "$(code /round "{\"question\":\"q\",\"name\":\"cn2\",\"auto\":true,\"voices\":[\"kimi\",\"codex\"],\"canary\":\"kimi\",\"project\":\"$W\"}")" = 400 ] && pass "/round: канарейка из участников — отказ" || fail "/round canary participant"
[ "$(code /round "{\"question\":\"q\",\"name\":\"cn3\",\"auto\":true,\"voices\":[\"kimi\",\"codex\"],\"canary\":\"gemini\",\"project\":\"$W\"}")" = 400 ] && pass "/round: канарейка без диска — отказ" || fail "/round canary no_files"

# ── /acts, /windows, проект как поле события (наказ Автора 2026-09-10) ──
python3 - "$W/journal/live.jsonl" <<'PY'
import json,sys
p=sys.argv[1]; n=max(json.loads(l)["id"] for l in open(p,encoding="utf-8") if l.strip())   # от максимального id: балласт теста нумерован 900000+
evs=[{"id":n+1,"ts":"2026-09-10T12:00:00+00:00","author":"roundtable","kind":"act_status","text":"act aa00bb11 принят: round: старый","schema":1,"live":"0.1","act_id":"aa00bb11","status":"accepted"},
     {"id":n+2,"ts":"2026-09-10T12:00:01+00:00","author":"roundtable","kind":"act_status","text":"act aa00bb11 done: round: старый","schema":1,"live":"0.1","act_id":"aa00bb11","status":"done"},
     {"id":n+3,"ts":"2026-09-10T12:00:02+00:00","author":"roundtable","kind":"act_status","text":"act cc22dd33 принят: быстрый: чужой проект","schema":1,"live":"0.1","act_id":"cc22dd33","status":"accepted","project":"/nowhere/other"},
     {"id":n+4,"ts":"2026-09-10T12:00:03+00:00","author":"arr","kind":"say","text":"реплика чужого проекта","schema":1,"live":"0.1","project":"/nowhere/other"}]
open(p,"a",encoding="utf-8").write("".join(json.dumps(e,ensure_ascii=False)+"\n" for e in evs))
PY
curl -s "$B/acts" | python3 -c '
import json,sys; d=json.load(sys.stdin); ids=[a["id"] for a in d["acts"]]
assert "aa00bb11" in ids and "cc22dd33" in ids, ids   # окно без проекта видит всё
a=[x for x in d["acts"] if x["id"]=="aa00bb11"][0]; assert a["label"].startswith("round: старый") and a["status"]=="done" and a["running"] is False, a' \
  && pass "/acts: акты из всей ленты (окно без проекта видит все), метка и статус" || fail "/acts"
# второе окно С ПРОЕКТОМ ($W — «стол» этого стенда: chamber/ лежит в нём, старые события без поля — его):
# чужой проект скрыт и в /acts, и в /events; своё событие видно
P2=$((PORT+1))
( cd "$RT_DIR" && ROUNDTABLE_CHAMBER="$W/chamber" ROUNDTABLE_JOURNAL="$W/journal" CHOIR_RT_VOICES="$W/rt-voices.json" CHOIR_RT_ACTS="$W/acts" CHOIR_RT_ACTS_ARCHIVE="$W/acts-archive" CHOIR_LEASE_DIR="$W/leases" CHOIR_RT_NO_AUTOREVIEW=1 CHOIR_RT_NO_BACKUP=1 CHOIR_BACKUP_DIR="$W/backup" CHOIR_JEV=0 CHOIR_SEC_BIN="$W/claude-stub" CHOIR_RT_SEC_TOKEN=test-sec-token \
  CHOIR_RT_NO_DISCOVERY=1 CHOIR_RT_MODELS="$W/rt-models.json" CHOIR_DSH_PATCH_DIR="$W/dsh" CHOIR_WT_DIR="$W/wt" CHOIR_RT_WINDOWS="$W/windows" CHOIR_RT_LAST_RUN="$W/rt-last.json" ROUNDTABLE_PORT="$P2" nohup python3 roundtable.py --project "$W" > "$W/srv2.log" 2>&1 ) &
B2="http://127.0.0.1:$P2"
for _ in $(seq 1 40); do sleep 0.25; curl -s -o /dev/null "$B2/state" && break; done
if curl -s -o /dev/null "$B2/state"; then
  curl -s "$B2/acts" | python3 -c '
import json,sys; d=json.load(sys.stdin); ids=[a["id"] for a in d["acts"]]
assert "aa00bb11" in ids and "cc22dd33" not in ids, ids' && pass "/acts (окно с проектом): старые акты видны, чужой проект скрыт" || fail "/acts фильтр"
  curl -s "$B2/events" --max-time 2 2>/dev/null | grep -c "реплика чужого проекта" | grep -q "^0$" && pass "/events (окно с проектом): чужое событие не уходит" || fail "/events: чужой проект просочился"
  # /secretary у окна с проектом: своё решение видно, решение проекта $W/proj — нет
  ( cd "$RT_DIR" && ROUNDTABLE_JOURNAL="$W/journal" CHOIR_JOURNAL="$W/journal" python3 chamber/secretary.py --project "$W" decision "решение проекта W" --by arr >/dev/null )
  curl -s "$B2/secretary" | python3 -c '
import json,sys; d=json.load(sys.stdin); tx=[x["text"] for x in d["decisions"]]
assert tx == ["решение проекта W"], tx; assert d["queue_all"] == [], d["queue_all"]' \
    && pass "/secretary (окно с проектом): своё решение видно, чужие решение и очередь скрыты" || fail "/secretary фильтр проекта"
  curl -s -X POST "$B2/goal" -H 'Content-Type: application/json' -d '{"text":"цель проекта W"}' > /dev/null
  # метка проекта у детей окна (live.py при пустом поле, executor_run, merge_gate) — юнит в autoreview_test (без платных вызовов)
  curl -s "$B2/events" --max-time 2 2>/dev/null | grep -q "цель проекта W" && pass "/events (окно с проектом): своё событие видно" || fail "/events: своё событие пропало"
  curl -s "$B/state" | python3 -c '
import json,sys; assert json.load(sys.stdin).get("goal") == "цель проекта W"' && pass "окно без проекта видит цель любого проекта (belongs: None — всё)" || fail "окно без проекта не увидело цель проекта W"
  python3 - "$W/journal/live.jsonl" <<'PY' && pass "id событий уникальны и растут по всей ленте (окно с проектом не начало с 1)" || fail "id ленты: дубли или откат"
import json,sys
evs=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]
ids=[e["id"] for e in evs]
goal=[e for e in evs if e.get("kind")=="goal" and e.get("goal")=="цель проекта W"]
# балласт теста имеет искусственные id 900000+, поэтому проверяется уникальность и то,
# что событие окна с проектом получило номер выше всех, а не 1
assert len(ids)==len(set(ids)), (len(ids), len(set(ids)))
assert goal and goal[-1]["id"]==max(ids), (goal and goal[-1]["id"], max(ids))
PY
  PID2=$(ss -ltnp 2>/dev/null | grep ":$P2 " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); [ -n "$PID2" ] && kill "$PID2"
else
  fail "второе окно с проектом не поднялось: $(tail -3 "$W/srv2.log")"
fi
# ── дополнения голосов: накопитель и «в очередь» (2026-10-08) ──
( cd "$RT_DIR/chamber" && python3 -c '
import json, sys
rec = {"id": "ab12cd34ef56", "ts": "2026-10-08T10:00:00+00:00", "round": "rid", "phase": "ideas", "voice": "choir",
       "role": "idea_scores", "status": "ok", "project": None, "worthy": 3, "levels": ["0","1","2","3","4"],
       "ideas": [{"id": "0123456789ab", "voice": "kimi", "phase": "blind", "text": "проверить хвост", "score": 3, "in_summary": 0.1, "lost": True}]}
open(sys.argv[1], "a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")' "$W/journal/room.jsonl" )
curl -s "$B/secretary?n=5" -H 'X-RT-Token: test-sec-token' | python3 -c '
import json,sys; d=json.load(sys.stdin)["ideas"]; it=d["items"][0]; assert it["id"]=="0123456789ab" and it["lost"] and d["lost"]==1, d' \
  && pass "/secretary: накопитель дополнений — потерянное ценное сверху" || fail "/secretary ideas"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/secretary/idea" -H 'Content-Type: application/json' -d '{"id":"0123456789ab"}')" = 403 ] && pass "/secretary/idea без токена → 403" || fail "/secretary/idea 403"
[ "$(codet /secretary/idea '{"id":"zz"}')" = 400 ] && pass "/secretary/idea: кривой id → 400" || fail "/secretary/idea 400"
[ "$(codet /secretary/idea '{"id":"ffffffffffff"}')" = 404 ] && pass "/secretary/idea: нет такого → 404" || fail "/secretary/idea 404"
R1=$(codet /secretary/idea '{"id":"0123456789ab"}' & codet /secretary/idea '{"id":"0123456789ab"}' & wait)
grep -c '"idea_id": "0123456789ab"' "$W/journal/live.jsonl" | grep -q '^1$' && pass "/secretary/idea: два одновременных нажатия — один пункт очереди" || fail "/secretary/idea гонка: $(grep -c '"idea_id": "0123456789ab"' "$W/journal/live.jsonl")"
[ "$(codet /secretary/idea '{"id":"0123456789ab"}')" = 409 ] && pass "/secretary/idea: повтор → 409" || fail "/secretary/idea 409"

# смена токена по HTTP — у третьего окна БЕЗ токена из среды (свой реестр окон)
P3=$((PORT+2))
( cd "$RT_DIR" && ROUNDTABLE_CHAMBER="$W/chamber" ROUNDTABLE_JOURNAL="$W/journal" CHOIR_RT_VOICES="$W/rt-voices.json" CHOIR_RT_ACTS="$W/acts" CHOIR_RT_ACTS_ARCHIVE="$W/acts-archive" CHOIR_LEASE_DIR="$W/leases" CHOIR_RT_NO_AUTOREVIEW=1 CHOIR_RT_NO_BACKUP=1 CHOIR_BACKUP_DIR="$W/backup" CHOIR_JEV=0 \
  CHOIR_RT_NO_DISCOVERY=1 CHOIR_RT_MODELS="$W/rt-models.json" CHOIR_DSH_PATCH_DIR="$W/dsh" CHOIR_WT_DIR="$W/wt" CHOIR_RT_WINDOWS="$W/windows-rot" CHOIR_RT_LAST_RUN="$W/rt-last.json" ROUNDTABLE_PORT="$P3" \
  env -u CHOIR_RT_SEC_TOKEN nohup python3 roundtable.py --project "$W" > "$W/srv3.log" 2>&1 ) &
B3="http://127.0.0.1:$P3"
for _ in $(seq 1 40); do sleep 0.25; curl -s -o /dev/null "$B3/state" && break; done
TOKF3=$(ls "$W/windows-rot/"sec-token.* 2>/dev/null | head -1)
if [ -n "$TOKF3" ] && [ -s "$TOKF3" ]; then
  T0=$(tr -d '\n' < "$TOKF3")
  [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B3/secretary/token" -H 'Content-Type: application/json' -H 'X-RT-Token: неверный-токен' -d '{}')" = 403 ] && pass "/secretary/token: неверный токен → 403" || fail "/secretary/token неверный"
  R=$(curl -s -X POST "$B3/secretary/token" -H 'Content-Type: application/json' -H "X-RT-Token: $T0" -d '{}')
  T1=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')
  [ -n "$T1" ] && [ "$T1" != "$T0" ] && [ "$(tr -d '\n' < "$TOKF3")" = "$T1" ] && pass "/secretary/token: 200, новый токен в ответе и в файле" || fail "/secretary/token 200: $R"
  [ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B3/secretary/token" -H 'Content-Type: application/json' -H "X-RT-Token: $T0" -d '{}')" = 403 ] && pass "/secretary/token: прежний токен после смены → 403" || fail "старый токен жив"
  grep -q "токен кресла секретаря проекта сменён" "$W/journal/live.jsonl" && ! grep -q "$T1" "$W/journal/live.jsonl" && pass "лента: факт смены без самого токена" || fail "лента и токен"
  grep -q "POST /secretary/token отклонён" "$W/journal/live.jsonl" && pass "отказ /secretary/token — заметка в ленте" || fail "отказ /secretary/token без заметки"
  PID3=$(ss -ltnp 2>/dev/null | grep ":$P3 " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); [ -n "$PID3" ] && kill "$PID3"
else
  fail "третье окно (смена токена) не поднялось: $(tail -3 "$W/srv3.log")"
fi
curl -s "$B/windows" | python3 -c '
import json,sys; d=json.load(sys.stdin); me=[w for w in d["windows"] if w.get("self")]
assert me and me[0]["pid"]==d["self"] and me[0].get("port"), d' && pass "/windows: это окно в реестре с портом" || fail "/windows"
[ "$(code /windows/stop "{\"pid\": $(curl -s "$B/windows" | python3 -c 'import json,sys; print(json.load(sys.stdin)["self"])')}")" = 400 ] && pass "/windows/stop: своё окно — отказ" || fail "/windows/stop self"
[ "$(code /windows/stop '{"pid": 1}')" = 400 ] && pass "/windows/stop: чужой pid — отказ" || fail "/windows/stop pid 1"
[ "$(code /windows/stop '{"pid": "x"}')" = 400 ] && pass "/windows/stop: кривой pid → 400" || fail "/windows/stop bad"


# ── /access: ACCESS.txt проекта из окна (наказ Автора 2026-09-20) ──
# Окно без проекта: каталог берётся из поля; отказы поимённо; файл
# пишется целиком; событие access в ленте; клетка и пакет читают тот же
# разбор (access_test.py) — здесь контракт HTTP.
mkdir -p "$W/aproj" "$W/adata" "$W/adata2"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/access")" = 400 ] && pass "/access без проекта и без поля → 400" || fail "/access без проекта"
curl -s "$B/access?project=$W/aproj" | python3 -c '
import json,sys; d=json.load(sys.stdin)
assert d["exists"] is False and d["ro"]==[] and d["rw"]==[] and d["text"]=="" and d["sha"] is None, d' && pass "GET /access: проект без файла — пусто, exists=false" || fail "GET /access пустой"
[ "$(code /access "{\"project\":\"$W/aproj\",\"text\":5}")" = 400 ] && pass "POST /access: text не строка → 400" || fail "/access text int"
[ "$(code /access "{\"project\":\"$W/nope-dir\",\"text\":\"\"}")" = 400 ] && pass "POST /access: нет такого каталога → 400" || fail "/access nope dir"
python3 -c "print('{\"project\":\"$W/aproj\",\"text\":\"' + 'r /tmp\\\\n'*700 + '\"}')" > "$W/big.json"
[ "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/access" -H 'Content-Type: application/json' --data-binary @"$W/big.json")" = 400 ] && pass "POST /access: больше 4000 символов → 400" || fail "/access big"
post /access "{\"project\":\"$W/aproj\",\"text\":\"# k\\nr $W/adata\\nrw $W/adata2\\nr /nope-$$\\nrw $HOME\\nr $W/aproj\"}" > "$W/acc.json"
python3 - "$W/acc.json" "$W/aproj/ACCESS.txt" "$W/adata" "$W/adata2" <<'PY' && pass "POST /access: файл записан целиком, ro/rw разобраны, три отказа поимённо, событие есть" || fail "POST /access: $(cat "$W/acc.json")"
import json,sys
d=json.load(open(sys.argv[1])); txt=open(sys.argv[2],encoding="utf-8").read()
assert d["exists"] and d["ro"]==[sys.argv[3]] and d["rw"]==[sys.argv[4]], d
assert len(d["rejects"])==3 and all(r.startswith("строка ") for r in d["rejects"]), d["rejects"]
assert any("не существует" in r for r in d["rejects"]) and any("дом" in r for r in d["rejects"]) and any("открыт и так" in r for r in d["rejects"]), d["rejects"]
assert txt.startswith("# k\n") and "r /nope-" in txt and txt.endswith("\n"), repr(txt)   # отвергнутое остаётся в файле, как написал Автор
assert d["text"]==txt and isinstance(d["event"], int) and d["sha"] and len(d["sha"])==16, d
PY
python3 - "$W/journal/live.jsonl" "$W/aproj" <<'PY' && pass "событие access в ленте: проект, ro/rw, отвергнутые, access_sha, by=arr" || fail "событие access в ленте"
import json,sys
evs=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]
a=[e for e in evs if e.get("kind")=="access"]
assert a, "нет события access"
e=a[-1]; assert e["project"]==sys.argv[2] and len(e["ro"])==1 and len(e["rw"])==1 and len(e["rejects"])==3 and e["access_sha"] and e.get("by")=="arr", e
assert "отвергнуто строк: 3 — строка " in e["text"] and "не существует" in e["text"] and "rw " in e["text"], e["text"]
PY
curl -s "$B/access?project=$W/aproj" | python3 -c '
import json,sys; d=json.load(sys.stdin); t=open(sys.argv[1],encoding="utf-8").read()
assert d["exists"] and d["text"]==t and len(d["rejects"])==3, d' "$W/aproj/ACCESS.txt" && pass "GET /access после записи: тот же текст и тот же разбор" || fail "GET /access после POST"
post /access "{\"project\":\"$W/aproj\",\"text\":\"\"}" | python3 -c '
import json,sys; d=json.load(sys.stdin); assert d["exists"] and d["ro"]==[] and d["text"]=="", d' && pass "POST /access пустым текстом: файл пуст, доступа нет, файл остаётся" || fail "/access пустой текст"
printf 'control' > "$W/adata/ctrl.txt"; rm -f "$W/aproj/ACCESS.txt"; ln "$W/adata/ctrl.txt" "$W/aproj/ACCESS.txt"
post /access "{\"project\":\"$W/aproj\",\"text\":\"r $W/adata\"}" > /dev/null
[ "$(cat "$W/adata/ctrl.txt")" = control ] && pass "POST /access на жёсткую ссылку: чужой inode не тронут (replace, не write)" || fail "запись ушла в жёсткую ссылку: $(cat "$W/adata/ctrl.txt")"
[ -z "$(ls -A "$W/aproj" | grep -v '^ACCESS.txt$')" ] && pass "после записи в каталоге проекта нет временных файлов" || fail "остатки в проекте: $(ls -A "$W/aproj")"
# Много отказов: текст события режется с многоточием, поле rejects полное (grok, субагент 22.09)
python3 - "$W/aproj" > "$W/many.json" <<'PY'
import json,sys
print(json.dumps({"project": sys.argv[1], "text": "\n".join("r /nope-%d-%s" % (i, "x"*40) for i in range(20))}))
PY
curl -s -X POST "$B/access" -H 'Content-Type: application/json' --data-binary @"$W/many.json" > "$W/many.out"
python3 - "$W/many.out" "$W/journal/live.jsonl" <<'PY' && pass "POST /access: 20 отказов — ответ несёт все 20, текст события обрезан с «…»" || fail "обрезка отказов: $(head -c 300 "$W/many.out")"
import json,sys
d=json.load(open(sys.argv[1])); assert len(d["rejects"])==20, len(d["rejects"])
evs=[json.loads(l) for l in open(sys.argv[2],encoding="utf-8") if l.strip()]
e=[x for x in evs if x.get("kind")=="access"][-1]
assert len(e["rejects"])==20 and e["text"].endswith("…") and len(e["text"]) < 800, (len(e["rejects"]), e["text"][-40:], len(e["text"]))
PY
# Синтаксис JS страницы: PAGE — raw-строка, ошибка в скрипте иначе всплыла бы только в браузере (субагент)
if command -v node >/dev/null 2>&1; then
python3 - "$RT_DIR/roundtable.py" "$W/page.js" <<'PY' && node --check "$W/page.js" 2>/dev/null && pass "JS страницы окна синтаксически цел (node --check)" || fail "JS страницы: $(node --check "$W/page.js" 2>&1 | tail -3)"
import re,sys
src=open(sys.argv[1],encoding="utf-8").read(); i=src.index('PAGE = r"""')+len('PAGE = r"""'); j=src.index('"""',i)
open(sys.argv[2],"w",encoding="utf-8").write("\n".join(re.findall(r"<script>(.*?)</script>", src[i:j], re.S)))
PY
fi
rm -f "$W/aproj/ACCESS.txt"; ln -s "$W/adata/x.txt" "$W/aproj/ACCESS.txt"
[ "$(code /access "{\"project\":\"$W/aproj\",\"text\":\"r $W/adata\"}")" = 409 ] && pass "POST /access: ACCESS.txt-ссылка → 409, по ссылке не пишем" || fail "/access symlink"
[ ! -e "$W/adata/x.txt" ] && pass "по ссылке ничего не записано" || fail "запись ушла по ссылке"
rm -f "$W/aproj/ACCESS.txt"

cat "$W/srv2.log" 2>/dev/null >> "$W/srv.log"   # трейсбеки второго окна — в ту же проверку (ревьюер)
grep -q "Traceback" "$W/srv.log" && fail "в логе сервера трейсбек: $(grep -A3 Traceback "$W/srv.log" | head -5)" || pass "трейсбеков в логе сервера нет"
printf '\nvoices_http: PASS %d · FAIL %d\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
