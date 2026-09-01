#!/usr/bin/env bash
# smoke.sh — интеграционный смок RoundTable: окно → сервер → live.py → лента.
#
# Зачем отдельным скриптом: слой надёжности пишется параллельно с этим смоком,
# и смок фиксирует ЦЕЛЕВОЙ контракт (SSE id, события act_status), а не текущее
# поведение. Шаг 7 поэтому WARN, а не FAIL: сервер может ещё не писать
# act_status, и это не поломка, а недостроенность.
#
# Идемпотентность: перед стартом глушим наш же осиротевший сервер на этом
# порту (и только его — чужой процесс на порту это ошибка, а не помеха),
# вычищаем несраскрытый сейф жребия, после себя не оставляем процессов (trap).
#
# Единственный платный шаг — №6 (~3 цента, deepseek): он и есть доказательство
# полного цикла; всё остальное — бесплатные проверки обвязки.
set -euo pipefail

PORT="${ROUNDTABLE_PORT:-8771}"
BASE="http://127.0.0.1:${PORT}"
RT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHOIR="${ROUNDTABLE_CHOIR:-$HOME/AiSandbox/Choir}"
FEED="$CHOIR/live.jsonl"
LOT_SAFE="$HOME/.cache/choir/roundtable-lot.json"
WORK="$(mktemp -d /tmp/rt-smoke.XXXXXX)"
SPID=""

PASS=0; FAIL=0; WARN=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$1"; }
warn() { WARN=$((WARN+1)); printf 'WARN  %s\n' "$1"; }
step() { printf '\n── шаг %s ─ %s\n' "$1" "$2"; }
die()  { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); summary; exit 1; }

cleanup() {
    # Не оставлять процессов: сначала дети сервера (live.py текущего хода),
    # потом сам сервер. SIGKILL — только если TERM не подействовал.
    if [ -n "$SPID" ] && kill -0 "$SPID" 2>/dev/null; then
        pkill -TERM -P "$SPID" 2>/dev/null || true
        kill -TERM "$SPID" 2>/dev/null || true
        for _ in 1 2 3 4 5; do kill -0 "$SPID" 2>/dev/null || break; sleep 0.4; done
        kill -KILL "$SPID" 2>/dev/null || true
    fi
    # ход deepseek мог осиротеть при таймауте шага 6 — добираем адресно
    pkill -f 'live\.py say .*смок RoundTable' 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

summary() {
    printf '\n══ сводка ══  PASS %d · FAIL %d · WARN %d\n' "$PASS" "$FAIL" "$WARN"
}

# HTTP-хелпер: код ответа в stdout, тело в файл. Таймаут обязателен —
# смок не имеет права висеть на зависшем сервере.
http() { # method path body_file_or_- outfile
    local m="$1" p="$2" data="$3" out="$4"
    if [ "$m" = GET ]; then
        curl -s --max-time 25 -o "$out" -w '%{http_code}' "$BASE$p" || echo 000
    else
        curl -s --max-time 25 -o "$out" -w '%{http_code}' -X POST \
             -H 'Content-Type: application/json' ${data:+-d "$data"} \
             "$BASE$p" || echo 000
    fi
}

[ -f "$FEED" ] || die "нет ленты $FEED — сначала python3 live.py ask …"

# ── шаг 0: расчистка (идемпотентность) ───────────────────────────────
step 0 "расчистка порта $PORT и сейфа жребия"
if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    if curl -s --max-time 2 "$BASE/" | grep -q RoundTable; then
        echo "      на порту висит прошлый RoundTable — глушу"
        fuser -k -TERM "$PORT/tcp" 2>/dev/null || true; sleep 1
        fuser -k -KILL "$PORT/tcp" 2>/dev/null || true; sleep 0.5
    else
        die "порт $PORT занят ЧУЖИМ процессом — не трогаю, выберите другой ROUNDTABLE_PORT"
    fi
fi
if [ -f "$LOT_SAFE" ]; then
    # нераскрытый сейф прошлого прогона дал бы 409 на первом же /lot;
    # его commit остаётся в ленте нераскрытым — честно говорим об этом
    warn "остался нераскрытый сейф жребия ($LOT_SAFE) — удаляю; его commit в ленте останется без reveal"
    rm -f "$LOT_SAFE"
fi

# ── шаг 1: старт сервера ─────────────────────────────────────────────
step 1 "старт сервера (ROUNDTABLE_PORT=$PORT)"
( cd "$RT_DIR" && ROUNDTABLE_PORT="$PORT" nohup python3 roundtable.py \
      >"$WORK/server.log" 2>&1 & echo $! >"$WORK/pid" )
SPID="$(cat "$WORK/pid")"
up=""
for _ in $(seq 1 20); do   # до 10 с, шаг 0.5
    if curl -sf --max-time 2 -o /dev/null "$BASE/"; then up=1; break; fi
    kill -0 "$SPID" 2>/dev/null || break
    sleep 0.5
done
if [ -n "$up" ]; then pass "сервер отвечает на $BASE (pid $SPID)"
else
    echo "      лог сервера:"; sed 's/^/      | /' "$WORK/server.log" | tail -15
    die "сервер не поднялся за 10 с"
fi

# ── шаг 2: GET / ─────────────────────────────────────────────────────
step 2 "GET / — страница"
code="$(http GET / '' "$WORK/page.html")"
if [ "$code" = 200 ] && grep -q RoundTable "$WORK/page.html"; then
    pass "200, страница содержит «RoundTable»"
else fail "GET / → $code, «RoundTable» $(grep -q RoundTable "$WORK/page.html" && echo есть || echo нет)"; fi

# ── шаг 3: GET /state ────────────────────────────────────────────────
step 3 "GET /state — 6 голосов"
code="$(http GET /state '' "$WORK/state.json")"
nvoices="$(python3 -c '
import json,sys
try: print(len(json.load(open(sys.argv[1]))["voices"]))
except Exception: print(-1)' "$WORK/state.json")"
if [ "$code" = 200 ] && [ "$nvoices" = 6 ]; then pass "валидный JSON, голосов: 6"
else fail "/state → $code, голосов: $nvoices (ждали 6)"; fi

# ── шаг 4: жребий commit-reveal ──────────────────────────────────────
step 4 "жребий: /lot из 3 кандидатов → 409 повторно → 425 рано → reveal → сверка sha256"
CANDS="$(python3 -c '
import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["voices"][:3]))' \
    "$WORK/state.json")"
code="$(http POST /lot "{\"candidates\":$CANDS}" "$WORK/lot.json")"
if [ "$code" = 200 ] && python3 -c '
import json,sys; j=json.load(open(sys.argv[1]))
assert len(j["commit"])==64 and int(j["target"])>0' "$WORK/lot.json" 2>/dev/null
then pass "/lot → 200, есть commit и target ($(python3 -c '
import json,sys;j=json.load(open(sys.argv[1]));print(j["commit"][:16]+"… → "+str(j["target"]))' "$WORK/lot.json"))"
else fail "/lot → $code (ждали 200 с commit/target; маяк drand доступен?)"
     sed 's/^/      | /' "$WORK/lot.json" 2>/dev/null || true; fi

code="$(http POST /lot "{\"candidates\":$CANDS}" "$WORK/lot2.json")"
[ "$code" = 409 ] && pass "повторный /lot → 409 (жребий один за раз)" \
                  || fail "повторный /lot → $code (ждали 409)"

code="$(http POST /reveal '' "$WORK/rev0.json")"
[ "$code" = 425 ] && pass "/reveal до срока → 425" \
                  || fail "/reveal до срока → $code (ждали 425)"

echo "      жду целевой drand-раунд (~35 с)…"
sleep 35
revealed=""
deadline=$((SECONDS+45))
while [ $SECONDS -lt $deadline ]; do
    code="$(http POST /reveal '' "$WORK/rev.json")"
    if [ "$code" = 200 ]; then revealed=1; break; fi
    [ "$code" = 425 ] || break     # всё, кроме «рано», — не лечится ожиданием
    sleep 3
done
[ -n "$revealed" ] && pass "/reveal → 200" || fail "/reveal → $code (раунд так и не наступил или сейф пропал)"

# сверка по ленте: commit == sha256(salt:candidates:target) последнего reveal
if [ -n "$revealed" ] && python3 -c '
import hashlib, json, sys
ev = None
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line: continue
    try: e = json.loads(line)
    except json.JSONDecodeError: continue
    if e.get("kind") == "lot_reveal": ev = e
assert ev, "в ленте нет lot_reveal"
h = hashlib.sha256("{}:{}:{}".format(
    ev["salt"], ",".join(ev["candidates"]), ev["drand_target"]
).encode()).hexdigest()
assert h == ev["commit"], f"sha256 НЕ сошёлся: {h} != {ev['commit']}"
print("      дирижёр по жребию:", ev["conductor"])' "$FEED"
then pass "commit == sha256(salt:candidates:target) — сошлось по ленте"
else [ -n "$revealed" ] && fail "сверка sha256 по последнему lot_reveal НЕ сошлась" \
                        || warn "сверка пропущена: reveal не состоялся"; fi

# ── шаг 5: SSE ───────────────────────────────────────────────────────
step 5 "SSE /events — data и id за 3 с"
curl -sN --max-time 3 "$BASE/events" >"$WORK/sse.txt" || true  # 28 = таймаут, это норма
grep -q '^data: {' "$WORK/sse.txt" \
    && pass "есть «data: {» (хвост ленты идёт)" \
    || fail "нет ни одной строки «data: {» за 3 с"
grep -q '^id: ' "$WORK/sse.txt" \
    && pass "есть «id: » (SSE-курсор для Last-Event-ID)" \
    || fail "нет строки «id: » — слой надёжности ещё не отдаёт SSE id"

# ── шаг 6: полный цикл окно → голос → лента (ПЛАТНЫЙ, ~3 цента) ──────
step 6 "/act → deepseek → новое событие в ленте (до 180 с)"
BASE_ID="$(python3 -c '
import json,sys
last=0
for line in open(sys.argv[1],encoding="utf-8"):
    try: last=max(last,int(json.loads(line).get("id") or 0))
    except Exception: pass
print(last)' "$FEED")"
# mode=quick (--once): без него ответ deepseek — это say, и цикл
# разговора передавал слово дальше по столу — второй платный вызов на
# каждый прогон смока, который тут же убивался остановкой сервера
# (замечено в ленте 2026-08-25: «слово → grok» и «прерван» следом).
code="$(http POST /act '{"text":"@deepseek смок RoundTable: ответь словом ОК","voices":["deepseek"],"mode":"quick"}' "$WORK/act.json")"
if [ "$code" = 200 ]; then
    pass "/act → 200 (task $(python3 -c '
import json,sys;print(json.load(open(sys.argv[1])).get("task","?"))' "$WORK/act.json"))"
    got=""
    deadline=$((SECONDS+180))
    while [ $SECONDS -lt $deadline ]; do
        if python3 -c '
import json,sys
base=int(sys.argv[2])
for line in open(sys.argv[1],encoding="utf-8"):
    try: e=json.loads(line)
    except Exception: continue
    if int(e.get("id") or 0)>base and e.get("author")=="deepseek":
        print(e.get("text","")[:600]); sys.exit(0)
sys.exit(1)' "$FEED" "$BASE_ID" >"$WORK/ds.txt"; then got=1; break; fi
        sleep 3
    done
    if [ -n "$got" ]; then
        pass "deepseek ответил в ленту (полный цикл окно→голос→лента доказан)"
        echo "      ответ deepseek:"; sed 's/^/      | /' "$WORK/ds.txt"
    else fail "за 180 с новое событие author=deepseek в ленте не появилось"; fi
else fail "/act → $code"; sed 's/^/      | /' "$WORK/act.json" 2>/dev/null || true; fi

# ── шаг 7: act_status (новый контракт — WARN, не FAIL) ───────────────
step 7 "события act_status accepted/done в ленте"
st="$(python3 -c '
import json,sys
base=int(sys.argv[2]); seen=set()
for line in open(sys.argv[1],encoding="utf-8"):
    try: e=json.loads(line)
    except Exception: continue
    if int(e.get("id") or 0)>base and e.get("kind")=="act_status":
        blob=(str(e.get("status",""))+" "+e.get("text","")).lower()
        for s in ("accepted","done","error","interrupted","unknown"):
            if s in blob: seen.add(s)
print(",".join(sorted(seen)) or "-")' "$FEED" "$BASE_ID")"
# Терминальным считается ЛЮБОЙ финал, не только done: смок сам глушит
# сервер, не дождавшись конца хода, и тогда честный итог — interrupted.
# Требовать именно done значило требовать от кода соврать.
case "$st" in
    *accepted*) pass "act_status: действие принято и записано в ленту" ;;
    -) warn "act_status в ленте нет — сервер их не пишет" ;;
    *) warn "act_status без accepted: $st" ;;
esac

# ── шаг 10: JS страницы ИСПОЛНЯЕТСЯ, а не только парсится ────────────
# Урок 2026-08-31: висячая ссылка на удалённую переменную (nm.title
# после сноса nm) прошла node --check, обе ревизии стола — и уронила
# applyVoices на живых данных: вся панель голосов показывала прочерки.
# Ловит только исполнение: заглушка DOM + прогон applyVoices настоящим
# ответом /voices.
step 7.5 "JS: инициализация и applyVoices на живых данных /voices"
if command -v node >/dev/null; then
    curl -s "$BASE/" | python3 -c '
import re,sys
m=re.search(r"<script>(.*)</script>", sys.stdin.read(), re.S)
open(sys.argv[1],"w",encoding="utf-8").write(m.group(1))' "$WORK/page.js"
    curl -s "$BASE/voices" > "$WORK/vdata.json"
    # Правда — код выхода, не подстрока: вывод с ошибкой И маркером
    # успеха проходил grep (нашли codex и deepseek).
    if node "$RT_DIR/test/replay.js" "$RT_DIR/test/domstub.js" \
            "$WORK/page.js" "$WORK/vdata.json" > "$WORK/replay.out" 2>&1; then
        pass "страница исполняется: $(tail -1 "$WORK/replay.out")"
    else
        fail "JS падает на живых данных: $(head -3 "$WORK/replay.out" | tr '\n' ' ')"
    fi
else
    warn "node не найден — исполнение JS не проверено"
fi

# ── шаг 8: глушим сервер, сводка ─────────────────────────────────────
step 8 "остановка сервера"
pkill -TERM -P "$SPID" 2>/dev/null || true
kill -TERM "$SPID" 2>/dev/null || true
for _ in 1 2 3 4 5; do kill -0 "$SPID" 2>/dev/null || break; sleep 0.4; done
if kill -0 "$SPID" 2>/dev/null; then
    kill -KILL "$SPID" 2>/dev/null || true
    warn "сервер не ушёл по TERM, добит KILL"
else pass "сервер остановлен, порт свободен"; fi
SPID=""
sleep 1        # дать finally дописать итоги прерванных ходов

# ── шаг 9: у каждого действия есть ФИНАЛ ─────────────────────────────
# Проверять финал до остановки сервера бессмысленно: ход ещё идёт.
# Здесь же виден весь жизненный цикл, включая `interrupted` — честный
# итог для хода, прерванного закрытием окна.
step 9 "у принятых действий есть терминальный статус"
fin="$(python3 -c '
import json,sys
base=int(sys.argv[2]); opened={}; closed=set()
for line in open(sys.argv[1],encoding="utf-8"):
    try: e=json.loads(line)
    except Exception: continue
    if not isinstance(e,dict) or e.get("kind")!="act_status": continue
    if int(e.get("id") or 0)<=base: continue
    a,st=e.get("act_id"),e.get("status")
    if st=="accepted": opened[a]=1
    elif st in ("done","error","interrupted","unknown"): closed.add(a)
stale=[a for a in opened if a not in closed]
print("OK" if not stale else "STALE:"+",".join(stale))' "$FEED" "$BASE_ID")"
case "$fin" in
    OK) pass "все действия закрыты (done/error/interrupted)" ;;
    STALE:*) fail "без финала остались: ${fin#STALE:}" ;;
    *) warn "не удалось проверить финалы: $fin" ;;
esac

summary
[ "$FAIL" -eq 0 ]
