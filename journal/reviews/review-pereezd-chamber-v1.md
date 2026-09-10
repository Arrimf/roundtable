# ревизия-переезд-chamber-v1 — переезд Choir/ → RoundTable/chamber + journal (вер1, 2026-09-06)

Что ревизовали: перенос кода стола в `chamber/`, журналов в `journal/`
(раунды по проектам), резолверы путей окна/кресла/исполнителя,
`publish.sh` с переписью о столе наружу. Формат: вер1 — пять
параллельных read-only вызовов (codex, grok, kimi, deepseek, gemini) по
одному пакету с выдержками; grok и субагент-ревьюер (Claude, отдельный
экземпляр) читали живые файлы. Ответы дословно ниже (правило 3); Грок
отвечал 543 с и видел код уже с первыми правками по замечаниям других.

## Свод (Клод, дирижёр — журналирует, не судит)

**Нашли все или почти все (5 из 6):** `find_round_file` брал первый
попавшийся файл по имени из любого проекта — catchup мог отдать голосу
чужую затравку; `rounds_dir()` создавал каталог побочным эффектом даже
в читающих командах; эвристика старой раскладки `ROUNDTABLE_CHOIR`
зависела от того, родилась ли уже лента, и расходилась между окном и
`choir.py`/`live.py` (split-brain журнала — kimi назвал самым опасным).

**Нашёл только один:** grok — свод по кнопке из окна писался
относительным `--out` при `cwd=chamber/`, то есть в каталог кода
(субагент нашёл то же и добавил: тест закреплял дефект); первый `run`
нового проекта падал бы на затравке без mkdir; в catchup проект
ставился ПОСЛЕ поиска затравки; `setdefault` отдавал приоритет
унаследованному `CHOIR_JOURNAL`; относительный `--project` резолвился
от `chamber/`. Субагент — путь лотка `inbox` остался у кода;
`~` уезжал в `journal/reviews/*.txt` (проверил прогоном
конвейера); `voice_cwd` отдавал корень песочницы вместо каталога
стола; `ROOM`/`DEBTS` потеряли `.resolve()`; пустое имя проекта;
`stol.py` ссылался на непубликуемый `draw_ui.py`. deepseek —
устаревшая `ROUNDTABLE_CHOIR` в окружении вела окно в пустоту.

**Где разошлись:**

| Вопрос | Позиции |
|---|---|
| Отрицание в `.gitignore` (`journal/rounds/*` + `!journal/rounds/RoundTable/`) | gemini (догадка): может не сработать; codex, kimi, grok: корректно; субагент проверил в scratch-репозитории — работает |
| `sed ~/ → ~/` в markdown | codex, deepseek, grok: ломает ссылки; kimi: относительных не трогает. Принято как есть: ссылка на домашний путь мертва снаружи в любом виде, `~/` честнее |
| Каталог раунда по `PROJECT.name` | codex и субагент: два проекта с одним basename сольются. Оставлено как известное ограничение; добавлен только запрет пустого имени |

**Исправлено** (все пункты выше, кроме оговорённых): явная семантика
legacy без проверки файлов, абсолютные пути из env, `CHOIR_JOURNAL`
присваивается детям окна, `rounds_dir(create=…)`, поиск затравки
сначала в проекте и ошибка при неоднозначности, старая запись пути вне
`journal/rounds/` не считается истиной, абсолютный `--out` свода,
`SANDBOX` для относительного проекта, страховка публикации по всему
staging и по форме ключей, обезличивание всех текстовых файлов
переписки, лоток в `journal/`, `voice_cwd` = каталог стола, стерильные
комментарии. Тесты после: smoke 16, voices_http 49, executor 14, edits
31, gate 47, lease 51, catalog 45, exec_argv 29 — без провалов.

**СВОИ ИДЕИ:** codex — коллизия basename проектов; gemini — прогон на
read-only ФС (chmod -R a-w) ловит любой неявный mkdir; kimi —
рукопожатие раскладки `choir.py --print-config` при старте окна;
deepseek — тест read-only команд с подменённым `Path.mkdir`; grok —
однострочник по `room.jsonl`: для каждого `seed_file` (exists, glob,
project) плюс `catchup` на раунде, чей файл лежит только в `Film/`.
Ни одна не внедрена сегодня; записаны как долг.

## Вопрос (пакет)

ВОПРОС СТОЛУ ВЕР1 — ревизия кода (read-only, 300–450 слов, вывод первой строкой). Инструменты не вызывать: ответ текстом по приведённым выдержкам (агентные голоса могут открыть живые файлы).

Что сделано (не закоммичено): каталог `Choir/` песочницы `~/AiSandbox` переехал. Код стола → `RoundTable/chamber/` (choir.py, live.py, channels.py, serial_gate.py, dossier.py, stol.py, столы.json). Журналы → `RoundTable/journal/`: live.jsonl, room.jsonl, debts.jsonl, voices/, inbox/, `rounds/<проект>/` (ВОПРОС/ЗАТРАВКА/СВОД и папки ответов раскладываются по проектам: RoundTable, Cursor_W, Film), reviews/, notes/. Раскладка: журнал переопределяется `CHOIR_JOURNAL` (choir.py/live.py) и `ROUNDTABLE_JOURNAL` (окно), код — `ROUNDTABLE_CHAMBER`; прежний `ROUNDTABLE_CHOIR` понимается как старая раскладка «всё в одной папке». Окно запускает `choir.py` с `cwd=CHOIR` (теперь chamber/), поэтому файлы раунда в цепочке по шагам передаются абсолютными путями. Публичный репозиторий (`publish.sh`) теперь везёт chamber/, docs/, journal/rounds/RoundTable/ и journal/reviews/ с заменой `~/` → `~/` в markdown; комната, долги, нити, notes/ и раунды других проектов не едут.

Тесты после переезда: smoke 16/16, catalog 45/45, exec_argv 29/29, voices_http 49/49.

Найди ошибку. Особенно интересуют: (1) путь, который по-прежнему смотрит в старое или несуществующее место, или зависит от cwd; (2) `rounds_dir()` создаёт каталог побочным эффектом — где это вредно (read-only команды, чужой проект, `--project` не задан); (3) `find_round_file` при одинаковом имени раунда в двух проектах и при старой записи `seed_file` с путём `Choir/…`; (4) резолвер legacy `ROUNDTABLE_CHOIR` — согласован ли между roundtable.py, edits.py, executor_run.py, и что будет, если переменная задана, а live.jsonl в ней ещё не создан; (5) `publish.sh`: что лишнее или личное уезжает наружу (журналы, пути, ключи), ломает ли sed ссылки, верно ли отрицание в `.gitignore` (`journal/rounds/*` + `!journal/rounds/RoundTable/`); (6) `qfile`/`rdir` в `POST /round` — определены ли во всех ветках, где используются. Вывод первой строкой: «дефектов не вижу» или список дефектов по убыванию опасности, каждый — файл, строка/фрагмент, чем грозит, фикс в одну строку. Догадки помечать как догадки. Последним пунктом — СВОЯ ИДЕЯ: одна проверка или ход, которого, по-твоему, не предложит никто другой.

Живые файлы (для агентных голосов): ~/AiSandbox/RoundTable/chamber/choir.py, ~/AiSandbox/RoundTable/chamber/live.py, ~/AiSandbox/RoundTable/roundtable.py, ~/AiSandbox/RoundTable/edits.py, ~/AiSandbox/RoundTable/executor_run.py, ~/AiSandbox/RoundTable/merge_gate.py, ~/AiSandbox/RoundTable/smoke.sh, ~/AiSandbox/RoundTable/publish.sh, ~/AiSandbox/RoundTable/test/voices_http_test.sh; полный диф — ~/.claude/jobs/4883b2dd/tmp/move-diff.patch.

## Выдержки

### chamber/choir.py (строки 64–75)
```
# РАСКЛАДКА (переезд 2026-09-06, решение Автора): код стола — chamber/,
# журналы и раунды — journal/ рядом (CHOIR_JOURNAL переопределяет),
# раунды раскладываются по проектам: journal/rounds/<проект>/.
HERE = Path(__file__).resolve().parent
JOURNAL = Path(os.environ.get("CHOIR_JOURNAL") or HERE.parent / "journal")
ROOM = Path(os.environ.get("CHOIR_ROOM") or JOURNAL / "room.jsonl")
# Песочница — родитель каталога стола: затравки ссылаются на её файлы
# (--add-dir голоса claude). Вне песочницы это родитель каталога стола.
SANDBOX = Path(os.environ.get("ROUNDTABLE_SANDBOX") or HERE.parent.parent)
KIMI_BIN = Path.home() / ".kimi-code" / "bin" / "kimi"

# ПРОЕКТ РАУНДА (--project; 2026-09-03). До этого раунд, запущенный из
```

### chamber/choir.py (строки 204–225)
```
def rounds_dir() -> Path:
    """Каталог файлов раунда (ВОПРОС/ЗАТРАВКА/СВОД): journal/rounds/<проект>;
    без проекта — раунд о самом столе (RoundTable)."""
    name = PROJECT.name if PROJECT else "RoundTable"
    d = JOURNAL / "rounds" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_round_file(path_or_name) -> Path:
    """Файл раунда по записи журнала: путь как есть, иначе поиск по имени
    в journal/rounds/*/ (раунды переехали по проектам 2026-09-06)."""
    p = Path(str(path_or_name))
    if p.exists():
        return p
    for cand in (JOURNAL / "rounds").glob(f"*/{p.name}"):
        return cand
    return p


def voice_cwd(name: str) -> str:
    """Рабочий каталог процесса голоса: claude — нейтральный (см. выше),
```

### chamber/choir.py (строки 96–113)
```
def _claude_dirs() -> list[str]:
    """--add-dir для голоса claude: песочница целиком (затравки ссылаются
    на её файлы) и проект раунда, если он не внутри неё. Флаг принимает
    НЕСКОЛЬКО каталогов, поэтому стоит до промпта и закрыт следующим
    флагом (см. порядок в лямбде)."""
    root = SANDBOX.resolve()
    dirs = [root]
    if PROJECT and not PROJECT.resolve().is_relative_to(root):
        dirs.append(PROJECT)
    out: list[str] = []
    for d in dirs:
        out += ["--add-dir", str(d)]
    return out


def _project_of_round(round_id: str) -> Path | None:
    """Проект из последнего жребия раунда; исчезнувший каталог — None
    с предупреждением, не падение (read-only команды проекту не нужны)."""
```

### chamber/choir.py (строки 1013–1014)
```
DEBTS = Path(os.environ.get("CHOIR_DEBTS") or JOURNAL / "debts.jsonl")

```

### chamber/choir.py cmd_expand (строки 2029–2031)
```
    out = Path(a.out) if a.out else rounds_dir() / f"ЗАТРАВКА-{a.round}.md"
    out.write_text(
        f"# Вопрос Автора, дословно\n\n"
```

### chamber/choir.py cmd_summarize (строки 2495–2497)
```
    card = Path(a.out) if a.out else rounds_dir() / f"СВОД-{a.round}.md"
    answered = sorted({r["voice"] for r in read_round(a.round)
                       if r.get("role") == "answer" and r.get("status") == "ok"})
```

### chamber/choir.py cmd_catchup (строки 2817–2819)
```
                seed = find_round_file(d["seed_file"]).read_text(encoding="utf-8")
            except Exception as e:                                  # noqa: BLE001
                print(f"  {voice}/{d['round']}: затравка недоступна ({e})")
```

### chamber/live.py (строки 65–71)
```
HERE = Path(__file__).resolve().parent
# Журнал — в journal/ рядом с chamber/ (переезд 2026-09-06); CHOIR_JOURNAL
# переопределяет (тесты, чужая комната).
JOURNAL = Path(os.environ.get("CHOIR_JOURNAL") or HERE.parent / "journal")
LIVE = JOURNAL / "live.jsonl"              # лента комнаты, append-only
VOICEDIR = JOURNAL / "voices"              # рабочий каталог на голос
KIMI_BIN = Path.home() / ".kimi-code" / "bin" / "kimi"
```

### roundtable.py (строки 66–81)
```
# РАСКЛАДКА (переезд 2026-09-06): код стола — chamber/ (live.py, choir.py),
# журналы и раунды — journal/ рядом. ROUNDTABLE_CHAMBER и ROUNDTABLE_JOURNAL
# переопределяют; ROUNDTABLE_CHOIR — прежнее имя: если в нём лежит
# live.jsonl (старая раскладка «всё в одной папке»), журнал берётся оттуда же.
_HERE = Path(__file__).resolve().parent
_legacy = os.environ.get("ROUNDTABLE_CHOIR")
CHAMBER = Path(os.environ.get("ROUNDTABLE_CHAMBER") or _legacy or _HERE / "chamber")
JOURNAL = Path(os.environ.get("ROUNDTABLE_JOURNAL")
               or (_legacy if _legacy and (Path(_legacy) / "live.jsonl").exists()
                   and not (Path(_legacy).parent / "journal").exists() else "")
               or (CHAMBER.parent / "journal"))
CHOIR = CHAMBER                      # прежнее имя: каталог кода стола
FEED = JOURNAL / "live.jsonl"
PORT = int(os.environ.get("ROUNDTABLE_PORT", "8770"))
# Каталог, о котором идёт разговор. Умолчание — ТОТ, ИЗ КОТОРОГО ОКНО
# ЗАПУСТИЛИ: Автор набирает `roundtable` в папке проекта и ждёт, что
```

### roundtable.py (строки 78–79)
```
FEED = JOURNAL / "live.jsonl"
PORT = int(os.environ.get("ROUNDTABLE_PORT", "8770"))
```

### roundtable.py POST /round (вопрос, цепочка по шагам, run) (строки 2884–2923)
```
            rdir = JOURNAL / "rounds" / (rp.name if rp else "RoundTable")
            rdir.mkdir(parents=True, exist_ok=True)
            qfile = rdir / f"ВОПРОС-{name}.md"
            # Не переписываем молча: в room.jsonl уже лежит pick с
            # question_sha от старого текста, и файл разошёлся бы с
            # журналом беззвучно (нашёл ревьюер дифа).
            if qfile.exists() and not req.get("force"):
                return self._json(409, {"error": f"{qfile.name} уже есть — "
                                        "выберите другое имя раунда"})
            qfile.write_text(question + "\n", encoding="utf-8")
            # Флаг-стоп с прошлого раза снимаем ЗДЕСЬ и ВСЛУХ. Файл живёт
            # дольше раунда: забытый стоп остановил бы новый такт с тем
            # же именем на первой же паузе — молча, будто дирижёр сам
            # решил не продолжать. Но и молчаливое снятие не годится: в
            # ленте уже стоит round_stop, и без парной записи она врёт,
            # что раунд остановлен.
            stale = stop_file(name)
            if stale.exists():
                stale.unlink(missing_ok=True)
                feed_append("round_stop",
                            f"флаг-стоп раунда {name} снят: начат новый такт "
                            f"с тем же именем",
                            round=name, status="cleared")
            if auto:
                # Аргументы списком, без bash -c: имя уже проверено, но
                # лишний слой кавычек — лишний способ ошибиться.
                cmd = [sys.executable, "choir.py", "run", "--round", name,
                       "--seed", str(qfile), "--rebuts", str(rebuts),
                       *(["--voices", ",".join(rvoices)] if rvoices else []),
                       *pflag]
                label = f"round: {name} [авто, витков: {rebuts}]"
                note = (f"АВТОПРОГОН: такт идёт сам — pick → expand → ask → "
                        f"rebut ×{rebuts} → summarize, без остановки на "
                        f"человека. Остановить: кнопка «Стоп» "
                        f"(флаг {stale.name}).")
            else:
                py = shlex.quote(sys.executable)
                rn = shlex.quote(name)
                seed = shlex.quote(str(qfile))
                zt = shlex.quote(str(rdir / f"ЗАТРАВКА-{name}.md"))  # его создаст expand
```

### roundtable.py round_view (строки 333–335)
```
    room = Path(os.environ.get("CHOIR_ROOM") or (JOURNAL / "room.jsonl"))
    recs: list[dict] = []
    needle = json.dumps(name, ensure_ascii=False)
```

### edits.py (строки 58–66)
```
_HERE = Path(__file__).resolve().parent
_legacy = os.environ.get("ROUNDTABLE_CHOIR")
CHAMBER = Path(os.environ.get("ROUNDTABLE_CHAMBER") or _legacy or _HERE / "chamber")
JOURNAL = Path(os.environ.get("ROUNDTABLE_JOURNAL")
               or (_legacy if _legacy and (Path(_legacy) / "live.jsonl").exists()
                   and not (Path(_legacy).parent / "journal").exists() else "")
               or (CHAMBER.parent / "journal"))
CHOIR = CHAMBER
WT_DIR = Path(os.environ.get("CHOIR_WT_DIR")
```

### edits.py (строки 490–491)
```
    live_path = JOURNAL / "live.jsonl"
    try:
```

### executor_run.py (строки 56–64)
```
_HERE = Path(__file__).resolve().parent
_legacy = os.environ.get("ROUNDTABLE_CHOIR")
CHAMBER = Path(os.environ.get("ROUNDTABLE_CHAMBER") or _legacy or _HERE / "chamber")
JOURNAL = Path(os.environ.get("ROUNDTABLE_JOURNAL")
               or (_legacy if _legacy and (Path(_legacy) / "live.jsonl").exists()
                   and not (Path(_legacy).parent / "journal").exists() else "")
               or (CHAMBER.parent / "journal"))
CHOIR = CHAMBER

```

### merge_gate.py (строки 974–975)
```
    live_path = edits.JOURNAL / "live.jsonl"
    try:
```

### smoke.sh (строки 20–22)
```
JOURNAL="${ROUNDTABLE_JOURNAL:-$RT_DIR/journal}"     # журналы стола (переезд 2026-09-06)
FEED="$JOURNAL/live.jsonl"
LOT_SAFE="$HOME/.cache/choir/roundtable-lot.json"
```

### publish.sh (строки 14–43)
```
mkdir -p "$OUT/chamber" "$OUT/adapters" "$OUT/test" "$OUT/examples" "$OUT/docs" "$OUT/journal/rounds/RoundTable" "$OUT/journal/reviews"

# ядро стола
cp "$RT"/{roundtable.py,edits.py,leases.py,merge_gate.py,executor_run.py,catalog.py} "$OUT/"
cp "$RT/dsh-model-pro.yaml" "$RT/smoke.sh" "$RT/README.md" "$RT/LICENSE" "$OUT/"
# цитирование: CITATION.cff читает GitHub («Cite this repository»),
# .zenodo.json — Zenodo при выпуске релиза (DOI как внешний таймстамп)
cp "$RT/CITATION.cff" "$RT/.zenodo.json" "$OUT/"
cp "$RT"/test/{domstub.js,replay.js,lease_test.py,edit_test.sh,executor_test.sh,gate_test.sh,catalog_test.py,exec_argv_test.py,voices_http_test.sh} "$OUT/test/"

# комната и дирижёр: chamber/ (код стола)
cp "$RT/chamber"/{live.py,choir.py,serial_gate.py,channels.py,dossier.py,stol.py,столы.json} "$OUT/chamber/"

# ПЕРЕПИСКА О САМОМ СТОЛЕ едет наружу (решение Автора 2026-09-06): раунды
# journal/rounds/RoundTable/, ревизии journal/reviews/, проектные документы
# docs/. Комната (live.jsonl, room.jsonl), долги, нити голосов, раунды
# других проектов и личные заметки — НЕ едут. Домашние пути в копиях
# заменяются на ~ — это переписка, не код, страховка ниже их не читает.
cp -r "$RT/docs/." "$OUT/docs/"
cp -r "$RT/journal/rounds/RoundTable/." "$OUT/journal/rounds/RoundTable/"
cp -r "$RT/journal/reviews/." "$OUT/journal/reviews/"
find "$OUT/docs" "$OUT/journal" -type f -name "*.md" -exec sed -i 's#~/#~/#g' {} +
cp "$RT/README.ru.md" "$OUT/" 2>/dev/null || true

# наши HTTP-адаптеры (вызовы, без которых не работает)
cp "$HOME/.local/bin/gemini-http" "$HOME/.local/bin/deepseek-http" "$OUT/adapters/"

# примеры конфигурации — ФОРМАТ, не содержимое
cat > "$OUT/examples/gemini-keys.txt.example" <<'EOF'
# ~/.gemini/keys.txt — по одному ключу Generative Language API на строку.
```

### publish.sh (строки 49–68)
```
cat > "$OUT/.gitignore" <<'EOF'
# Журналы стола — ЛИЧНОЕ: живая переписка голосов не публикуется.
# Наружу едут только раунды о самом столе (journal/rounds/RoundTable/),
# ревизии его кода (journal/reviews/) и проектные документы (docs/).
journal/live.jsonl
journal/room.jsonl
journal/debts.jsonl
journal/voices/
journal/notes/
journal/inbox/
journal/archive/
journal/ДОСЬЕ-*
journal/rounds/*
!journal/rounds/RoundTable/
__pycache__/
*.pyc
EOF

# страховка: личные строки не должны доехать
if grep -rn "<handle>\|@<mail>\|<host>\|~" "$OUT" \
```

### test/voices_http_test.sh (строки 19–22)
```
mkdir -p "$W/chamber" "$W/journal/rounds"
cp "$SRC_CHAMBER"/*.py "$W/chamber/" || { echo "нет $SRC_CHAMBER/*.py"; exit 2; }
: > "$W/journal/live.jsonl"; : > "$W/journal/room.jsonl"
# Кэш каталога с лестницами ПО МОДЕЛЯМ — детерминированно и без сети.
```

### test/voices_http_test.sh (строки 45–46)
```
( cd "$RT_DIR" && ROUNDTABLE_CHAMBER="$W/chamber" ROUNDTABLE_JOURNAL="$W/journal" CHOIR_RT_VOICES="$W/rt-voices.json" \
  CHOIR_RT_MODELS="$W/rt-models.json" CHOIR_RT_NO_DISCOVERY=1 CHOIR_DSH_PATCH_DIR="$W/dshp" \
```


## Статусы вызовов

```
gemini rc=0 26s
codex rc=0 42s
kimi rc=0 111s
deepseek rc=0 286s
grok rc=0 543s
ALL DONE 543s
```

## Ответы дословно


=================== codex
Дефекты по убыванию опасности: `choir.py` — неоднозначный поиск затравки; `roundtable.py`/`edits.py`/`executor_run.py` — неверный выбор legacy-журнала; пути из переменных окружения зависят от cwd; `smoke.sh` расходится с резолвером окна; `publish.sh` ломает ссылки.

1. **`chamber/choir.py:214–222`, `find_round_file`** — возвращается первое совпадение basename среди всех проектов, без проверки проекта и уникальности. При одинаковых именах `catchup` может передать голосу чужую затравку; порядок обхода не задаёт смыслового приоритета. Старая запись `Choir/…` после переезда теряет привязку и попадает именно в этот поиск. **Фикс:** восстанавливать проект из записи раунда, искать внутри него, при неоднозначности завершаться ошибкой; legacy-пути мигрировать явно.

2. **`roundtable.py:70–75`, `edits.py:59–64`, `executor_run.py:57–62`** — резолверы согласованы между собой, но ошибочна сама эвристика: заданный `ROUNDTABLE_CHOIR` без `live.jsonl` направляет журнал в соседний `journal/`; наличие соседнего каталога перебивает даже существующий legacy-журнал. Это нарушает заявленную поддержку раскладки «всё в одной папке». **Фикс:** задавать семантику legacy-переменной явно, независимо от наличия файлов, сохранив приоритет нового override.

3. **`roundtable.py:71–75, 2884–2923`; `chamber/choir.py:68`** — `Path(...)` не делает относительные значения окружения абсолютными. Поэтому обещание абсолютного `qfile` не выполняется при относительном `ROUNDTABLE_JOURNAL`: после запуска дочернего процесса с `cwd=CHOIR` путь означает другое место. Относительный `CHOIR_JOURNAL` также зависит от каталога запуска. **Фикс:** нормализовать настройки относительно одной документированной базы до смены cwd и передачи аргументов.

4. **`smoke.sh:20`** — учитывается только `ROUNDTABLE_JOURNAL`, тогда как окно выводит журнал также из chamber/legacy-настроек. При нестандартном `ROUNDTABLE_CHAMBER` smoke может проверять другую ленту и давать нерелевантный результат. **Фикс:** использовать общий резолвер раскладки для приложения и проверок.

5. **`publish.sh:34–37`, массовая замена путей** — Markdown-ссылка вида `[код](~/AiSandbox/…)` превращается в `[код](~/AiSandbox/…)`; Markdown не раскрывает домашний каталог, и ссылка становится относительной. **Фикс:** отдельно преобразовывать цели ссылок в корректные относительные либо публичные URL; обезличивание обычного текста выполнять отдельно.

6. **`publish.sh`, `cp -r` — догадка о риске публикации:** копируется всё содержимое разрешённых деревьев, включая возможные вложения, журналы и ключи. Видимая проверка нескольких личных строк не доказывает отсутствие секретов; продолжение проверки не приведено. **Фикс:** проверять полный состав экспортируемых файлов и секреты перед публикацией. Отрицание `journal/rounds/*` + `!journal/rounds/RoundTable/` корректно: родитель не исключён; уже отслеживаемые файлы `.gitignore` не защищает.

7. **Границы вывода:** `rounds_dir():204–210` действительно пишет на диск, но показанные вызовы относятся к созданию файлов. Вред для read-only команд не доказан; неверная раскладка без восстановленного `PROJECT` — догадка. **Фикс:** отделить вычисление пути от создания каталога. В показанном `POST /round` оба имени определены до `if auto`; дефект `qfile`/`rdir` здесь отсутствует.

8. **СВОЯ ИДЕЯ:** создать два разных проекта с одинаковым basename, например `/a/Film` и `/b/Film`: использование `PROJECT.name` объединяет их раунды ещё до поиска. Проверить коллизию и заменить basename устойчивым идентификатором проекта.
=================== grok
Читаю живые файлы и полный диф — ищу пути, побочные mkdir, коллизии раундов и рассинхрон legacy-резолвера.Дальше сверю резолвер, вызовы `rounds_dir`/`find_round_file` и ветки `POST /round`.Тесты ещё ссылаются на `Choir/` — проверяю это и остальные стыки путей.Файлы уже чуть разошлись с выдержками — перечитываю актуальные резолверы и `cmd_ask`/`--out`.Код ещё правится на лету — снимаю финальный снимок резолверов и спорных мест.`/round_step --out СВОД-{name}.md` (cwd=chamber/); `cmd_run` `rounds_dir()` без `create=True`; catchup зовёт `find_round_file` до `PROJECT=`; `exists()` на 63 путях `Choir/`; `ROUNDTABLE_CHOIR` окно≠CLI; `setdefault(CHOIR_JOURNAL)`; `rp = CHOIR / rp`; publish: sed/`grep` мимо md и адаптеров.

1. **`roundtable.py:3708` + `spawn(..., cwd=CHOIR)` (`3718`)** — кнопка свода пишет `--out СВОД-{name}.md` относительно `chamber/`, не `journal/rounds/<проект>/`. Автопрогон кладёт карточку правильно (`cmd_run` → `rounds_dir(create=True)`), шаг из окна — нет: файл не туда, в `chamber/` мусор, в раскладке журнала дыра. Фикс: абсолютный `--out` в `JOURNAL/rounds/<проект>/СВОД-…md` (проект из жребия/`round_view`) либо убрать `--out` и дать `summarize` то же умолчание, что у `run`.

2. **`choir.py:2430` `zt = rounds_dir()` без `create=True`, затем `cmd_expand(..., out=str(zt))` (`2058`)** — при заданном `--out` родитель не создаётся (`write_text` без `mkdir`). Первый CLI-`run` нового проекта: `FileNotFoundError` на затравке. Окно маскирует это `rdir.mkdir` в `/round` (`2889–2891`). Фикс: `rounds_dir(create=True)` в `cmd_run` (и/или `out.parent.mkdir` в `expand`, как уже сделано в `summarize:2160`).

3. **`choir.py:2846` vs `2855`** — `find_round_file` вызывается **до** `PROJECT = _project_of_round(d["round"])`. Ветка «свой проект» (`240–243`) на массовом `catchup` мертва; при одном имени в двух проектах — `SystemExit` (`247–249`), хотя жребий долга уже знает проект. Фикс: сначала `PROJECT` с этого долга, потом поиск.

4. **`choir.py:238–239` `if p.exists(): return p`** — в `room.jsonl` **63** `seed_file` вида `~/AiSandbox/Choir/ЗАТРАВКА-…` (сейчас каталога нет: glob находит 50, 6 miss — смоки и `/tmp/…/scratchpad`). Воскресший старый путь (копия, mount) → catchup читает **устаревшую** затравку и не идёт в `journal/rounds/`. Фикс: не доверять `exists()` вне `JOURNAL/rounds/`; для префикса `…/Choir/` сразу glob.

5. **Резолвер `ROUNDTABLE_CHOIR`** — `roundtable.py:77–80`, `edits.py:65–68`, `executor_run.py:63–66` **согласованы**: `JOURNAL = ROUNDTABLE_JOURNAL or _legacy or CHAMBER.parent/journal`, без проверки `live.jsonl`. Если переменная задана, а ленты ещё нет — журнал **есть** `_legacy`, лента родится там (это ответ на вопрос: больше не уезжает в `parent/journal`). `choir.py`/`live.py` (`73–75` / `71–73`) **`ROUNDTABLE_CHOIR` не читают**. Прямой CLI при забытом `export ROUNDTABLE_CHOIR=/…/Choir` смотрит в `RoundTable/journal`, окно — в мёртвый `Choir/` (и код, и лента). Фикс: тем же `_journal_dir` везде, либо `os.environ["CHOIR_JOURNAL"]=str(JOURNAL)` не `setdefault`.

6. **`setdefault("CHOIR_JOURNAL")` (`roundtable.py:82`)** — уже заданный `CHOIR_JOURNAL` сильнее `ROUNDTABLE_JOURNAL` в choir/live. Тестовое окно пишет в изоляцию, дирижёр — в живой журнал. Фикс: присваивать, не `setdefault`; в `_journal_dir` не давать `CHOIR_JOURNAL` перекрывать явно заданный `ROUNDTABLE_JOURNAL` в тестах.

7. **`roundtable.py:2876` `rp = CHOIR / rp`** — относительный проект резолвится в `chamber/Film` (400). Фикс: от cwd окна / `SANDBOX`, не от `CHAMBER`.

8. **`publish.sh`**: отрицание `journal/rounds/*` + `!journal/rounds/RoundTable/` **работает** (проверено: вложенные `ответы-…/` едут, `Film/` нет). `sed s#~/#~/#g` только в md — ссылки `[…](~/…)` на GitHub мертвы, в docs остаются `Choir/`. Страховка `--include py/sh/js` не видит `adapters/gemini-http` (без расширения) и reviews/*.md. Фикс: сканировать без фильтра расширений; в md менять только `~/AiSandbox/` → относительные пути репо.

**Не дефект:** `qfile`/`rdir` заданы до обеих веток `/round` и в `auto`, и в `else`.

**СВОЯ ИДЕЯ:** не смотреть код, а прогнать однострочник по `room.jsonl`: для каждого `seed_file` — `(old.exists, glob_hits, lot.project)` плюс `catchup --round` на раунде, чей файл только в `Film/`, с `cwd=chamber/` и без `--project`. Это единственная проверка, которая одновременно ловит 6 miss, будущую коллизию имён и мёртвый порядок `PROJECT` — ни diff, ни smoke этого не видят.

=================== kimi
• **Дефекты (по убыванию опасности): 1) рассинхрон legacy-резолвера между окном и chamber — split-brain журнала; 2) относительный `choir.py` в POST /round зависит от cwd; 3) `find_round_file` молча берёт чужой проект при совпадении имён; 4) `rounds_dir()` творит каталоги в read-only контекстах; 5) publish: sed только по *.md и адаптеры без проверки ключей (догадки).**

  1. **`roundtable.py:71–76` (+ edits.py, executor_run.py) против `chamber/choir.py:67` и `live.py:68`** — самое опасное. Три файла окна понимают `ROUNDTABLE_CHOIR` (старая раскладка: live.jsonl внутри → журнал = сам legacy-каталог), а `choir.py`/`live.py` про `ROUNDTABLE_CHOIR` не знают вовсе — только `CHOIR_JOURNAL`, иначе `HERE.parent/"journal"`. Сценарий: задан `ROUNDTABLE_CHOIR=/old/Choir`, там лежит live.jsonl → окно читает/пишет `/old/Choir/live.jsonl`, а запущенный голос пишет в `/old/journal/live.jsonl`. Две ленты, молча. На свежем legacy-каталоге (live.jsonl ещё нет) условие `(legacy/live.jsonl).exists()` ложно и обе стороны сойдутся на `legacy.parent/journal` — то есть поведение зависит от того, успел ли родиться файл. Фикс: окно экспортирует ребёнку `CHOIR_JOURNAL=str(JOURNAL)` (и `CHOIR_ROOM`/`CHOIR_DEBTS`) — один источник истины.

  2. **`roundtable.py:2910` — `cmd = [sys.executable, "choir.py", "run", ...]`**. Относительный путь: работает, только если spawn идёт с `cwd=CHAMBER`. Это не видно из выдержки и не закреплено в коде — зависимость от соглашения, которое ничто не проверяет. Фикс: `str(CHAMBER / "choir.py")`.

  3. **`choir.py:215–221 find_round_file`**. `for cand in glob(...): return cand` — первый попавшийся. При одинаковом имени раунда в `rounds/RoundTable/` и `rounds/Film/` catchup прочитает затравку чужого проекта (порядок glob недетерминирован). Фикс: сначала `JOURNAL/"rounds"/PROJECT.name/p.name`, при нескольких совпадениях — ошибка, не молчаливый выбор. Старая запись `seed_file` вида `Choir/…` — относительный `p.exists()` зависит от cwd процесса; спасает только glob по `p.name`, т.е. пункт 3 становится единственной линией защиты.

  4. **`choir.py:204–210 rounds_dir()`** — mkdir побочным эффектом. Вредно: опечатка в `--project` создаёт пустой `rounds/<опечатка>/`; при `CHOIR_JOURNAL` на чужую/смонтированную read-only комната даже «читающий» вызов упадёт с PermissionError; без `--project` раунд молча уезжает в `rounds/RoundTable/`. Фикс: разделить на `rounds_dir(create=False)` для чтения и mkdir только в write-командах.

  5. **`publish.sh`** (догадки): sed — только `*.md` в docs/journal; пути `~` в `.json`/`.txt` внутри папок ответов не чистятся (страховка-grep их поймает → fail-closed, приемлемо). Адаптеры из `~/.local/bin` копируются без sed, а grep не ищет API-ключи — если ключ зашит в скрипт, уедет наружу. Отрицание в `.gitignore` корректно: родитель `journal/rounds` не исключён, `!journal/rounds/RoundTable/` сработает. Sed ссылки не ломает (относительные не трогает).

  6. **`qfile`/`rdir`** — догадка: в показанной ветке определены до использования; проверить, что ветки «цепочка по шагам» и run-без-вопроса не ссылаются на `qfile`/`rdir` до определения — из выдержек не видно.

  **СВОЯ ИДЕЯ:** рукопожатие раскладки при старте: окно запускает `choir.py --print-config` (JOURNAL/ROOM/DEBTS резолвнутые) и падает при расхождении со своими — split-brain пункта 1 ловится до первой записи, а не по пропавшим репликам.


=================== deepseek
1. `roundtable.py:67,69-71` (и те же строки в `edits.py`, `executor_run.py`) — `CHAMBER = Path(... or _legacy or _HERE / "chamber")`: если после переезда в окружении остался `ROUNDTABLE_CHOIR=.../Choir`, `CHOIR` ведёт в старый/несуществующий каталог, и Popen с `cwd=CHOIR` падает или запускает старый код. Там же условие `and (Path(_legacy)/"live.jsonl").exists()`: если переменная задана, а `live.jsonl` ещё не создан, `JOURNAL` уезжает в `_legacy.parent/journal`, а не в `_legacy`. Фикс: `CHAMBER = Path(os.environ.get("ROUNDTABLE_CHAMBER") or _HERE / "chamber")`; в fallback `JOURNAL` убрать проверку `live.jsonl`.

2. `chamber/choir.py:209-213` `find_round_file` — `glob` отдаёт первый попавшийся файл; при одинаковом имени раунда в двух проектах или старой записи `seed_file` `Choir/…` вернёт не тот проект. Грозит неверной затравкой в `cmd_catchup`. Фикс: `return max((JOURNAL / "rounds").glob(f"*/{p.name}"), key=lambda c: (c.parent.name == (PROJECT.name if PROJECT else 'RoundTable'), c.stat().st_mtime), default=p)`.

3. `chamber/choir.py:207` (`rounds_dir`) — `mkdir` как побочный эффект: read-only или чужой `--project` создают `journal/rounds/<project>` / `RoundTable` без записи. Фикс: убрать `mkdir` из `rounds_dir()`; создавать каталог в `cmd_expand`/`cmd_summarize` только перед `write_text`, когда `--out` не задан.

4. `publish.sh:40` — `cp "$HOME/.local/bin/gemini-http" ... "$OUT/adapters/"` публикует личные адаптеры из домашнего каталога; возможны ключи/эндпоинты. Фикс: копировать из `$RT/adapters` или публиковать `*.example`.

5. `publish.sh:38` — `sed 's#~/#~/#g'` в markdown может ломать ссылки: `[x](~/...)` станет `[x](~/...)`, а `~` не URL. Фикс: `s#~/#./#g` или замена только в code-span. — Догадка.

Отрицание в `.gitignore` корректно: `!journal/rounds/RoundTable/` снимает игнор каталога.

СВОЯ ИДЕЯ: прогнать все read-only команды `choir` в тесте с подменённым `Path.mkdir`, бросающим `AssertionError`; так отловится любое неявное создание каталогов в командах, которые обязаны только читать.

=================== gemini
1. `chamber/choir.py:68` и `chamber/live.py:68` — `choir.py`/`live.py` читают только `CHOIR_JOURNAL`, игнорируя `ROUNDTABLE_JOURNAL`. Если окно или тесты переопределяют путь через `ROUNDTABLE_JOURNAL` не по стандартному смещению `../journal`, `choir.py` продолжит писать в дефолтный `HERE.parent / "journal"`, вызвав рассинхрон ленты и раундов.
   *Фикс:* `JOURNAL = Path(os.environ.get("CHOIR_JOURNAL") or os.environ.get("ROUNDTABLE_JOURNAL") or HERE.parent / "journal")`.

2. `roundtable.py:73-75` (а также `edits.py:62-64`, `executor_run.py:60-62`) — резолвер legacy `ROUNDTABLE_CHOIR` требует `(Path(_legacy) / "live.jsonl").exists()`. Если старая монолитная папка пуста (первый запуск/чистый тест), условие ложно, и `JOURNAL` ошибочно уходит в `CHAMBER.parent / "journal"`.
   *Фикс:* `or (_legacy if _legacy and not (Path(_legacy).parent / "journal").exists() else "")`.

3. `chamber/choir.py:218-219` — недетерминизм `find_round_file` при коллизии имён в разных проектах. `(JOURNAL / "rounds").glob(f"*/{p.name}")` вернёт файл из случайного проекта по порядку `os.scandir`, проигнорировав текущий `PROJECT`.
   *Фикс:* проверять `if PROJECT and (JOURNAL / "rounds" / PROJECT.name / p.name).exists(): return JOURNAL / "rounds" / PROJECT.name / p.name` перед общим `glob`.

4. `chamber/choir.py:208` — побочный эффект `rounds_dir().mkdir(...)`. Создаёт папку `journal/rounds/RoundTable` (или проектную) даже при read-only обращениях либо вычислении путей по умолчанию.
   *Фикс:* убрать `mkdir` из `rounds_dir()` и вызывать `.parent.mkdir(parents=True, exist_ok=True)` точечно перед записью файлов (`write_text`).

5. *(Догадка)* `publish.sh:58-59` — маска `journal/rounds/*` в `.gitignore` игнорирует содержимое каталога верхнего уровня; правило `!journal/rounds/RoundTable/` разблокирует сам каталог, но в некоторых версиях git файлы внутри могут оставаться исключёнными без явного шаблона.
   *Фикс:* заменить пару строк на `journal/rounds/*` и `!journal/rounds/RoundTable/**`.

*(По п. 1 и 6: `qfile`/`rdir` в `POST /round` инициализируются безусловно до ветвления `auto`/ручной шаг; `SANDBOX` через `HERE.parent.parent` вычисляется корректно).*

---

### СВОЯ ИДЕЯ
**Тест на «просачивание путей» через временный chroot/overlay:** запустить `roundtable` + `choir run` с `ROUNDTABLE_JOURNAL=/tmp/test-j` и `ROUNDTABLE_CHAMBER=/tmp/test-c`, предварительно сделав рабочий репозиторий `chmod -R a-w`. Если любая часть кода (включая сторонние утилиты или legacy-ветки) попытается неявно создать каталог в `./journal` или обратиться к относительному `cwd`, тест немедленно упадёт с `EACCES`, выявив скрытые зависимости от ФС.


=================== субагент-ревьюер (Claude, opus, отдельный экземпляр; построчно по дифу)
Ревизия закончена. Отчёт ниже.

**Важная оговорка о состоянии дерева:** пока я читал, файлы правились из соседней сессии (roundtable.py, edits.py, executor_run.py, publish.sh, smoke.sh, choir.py, live.py менялись между 02:48 и 03:09). Всё сказанное относится к состоянию на 03:09.

# Находки (сначала самые опасные)

1. `chamber/choir.py:4110` — лоток указывает в несуществующий каталог: `INBOX = Path(__file__).resolve().parent / "inbox"` → RoundTable/chamber/inbox. Лоток переехал в `RoundTable/journal/inbox/`. Проверено фактом: `choir.INBOX = …/chamber/inbox, существует: False`. Чем грозит: `choir.py inbox` печатает «лотка нет» — предложения голосов невидимы, ошибка выглядит как «голоса ничего не клали». Фикс: `INBOX = Path(os.environ.get("CHOIR_INBOX") or JOURNAL / "inbox")`.

2. `roundtable.py:3714` — свод из окна пишется в `chamber/`, а не в `journal/rounds/<проект>/`: `cmd += ["--out", f"СВОД-{name}.md"]` относительный, cwd подпроцесса — chamber/. Автопрогон (`run`) пишет правильно, то есть две кнопки одного такта кладут свод в разные места. Не ловится тестом намеренно: `test/voices_http_test.sh:154` проверяет ровно `--out СВОД-t1.md` — тест закрепляет дефект. Фикс: `round_view` уже отдаёт `project` из жребия — абсолютный путь `JOURNAL/rounds/<имя проекта>/СВОД-{name}.md`, в тесте сверять абсолютный путь.

3. `publish.sh:35,68` — `~` уезжает наружу в `journal/reviews/*.txt`: санитайзер режет только `*.md`, страховка-grep смотрит только py/sh/js. Проверено прогоном самого конвейера на копии в tmp: после sed остаются `journal/reviews/t1-codex.txt:3` (workdir …), `t2-codex.txt:3`, `t1-gemini.txt:5` (~/.gemini/choir-http/…jsonl), `t2-gemini.txt:9`. Страховка молчит. Фикс: убрать `-name "*.md"` из find; расширить grep.

4. `chamber/choir.py:213-221` — `find_round_file` игнорирует проект раунда и берёт первое совпадение. Сегодня коллизий на уровне rounds/<проект>/<файл> нет (проверил), но одноимённый раунд в двух проектах даёт два ЗАТРАВКА-<имя>.md, порядок glob — порядок ФС. Чем грозит: catchup молча дошлёт голосу затравку ЧУЖОГО проекта — платный вызов с не тем вопросом, нарушение правила 1 невидимо в журнале. Фикс: сначала rounds/<PROJECT.name>/<имя>, потом glob; при >1 кандидате — не выбирать молча.

5. `chamber/choir.py:73-77`, `chamber/live.py:68-71` — тихий автоопределитель старой раскладки: случайный room.jsonl/live.jsonl в chamber/ молча переводит ВЕСЬ стол на другой журнал без строки в stderr. По правилу 8.5 это ровно тот случай, когда оркестрация решает молча. Фикс: печатать в stderr.

6. `chamber/choir.py:238-244` — `voice_cwd` обещает Choir/, а отдаёт корень песочницы (SANDBOX = HERE.parent.parent). Раньше было ROOM.parent = каталог стола. Чем грозит: раунд без --project стартует codex/grok/kimi из корня песочницы; Кими подхватит корневой CLAUDE.md, но не chamber/CLAUDE.md; относительные пути в затравках перестают резолвиться. Фикс: решить намеренно.

7. `roundtable.py:60-68,87`, `edits.py:55-57`, `executor_run.py:53-55` — комментарии противоречат коду и друг другу: старая шапка «Комната остаётся в Choir… env → каталог choir/ → путь песочницы Автора»; «если в нём лежит live.jsonl…» противоречит абзацу ниже; хвост комментария «прежнее имя: каталог кода стола» приклеился к чужой строке. Фикс: снести устаревшее, вернуть хвост.

8. `chamber/choir.py:69,1012` — ROOM и DEBTS потеряли `.resolve()`: `CHOIR_ROOM=room.jsonl` теперь «относительно cwd в момент открытия», путь в имени гейт-замка может быть относительным. Фикс: вернуть.

9. `roundtable.py:2895-2903` — каталог проекта создаётся до отказа 409: отклонённый запрос оставляет пустой journal/rounds/<проект>/. Фикс: mkdir перед write_text.

10. `roundtable.py:2895` и `chamber/choir.py:204-210` — каталог раундов по PROJECT.name: два проекта с одинаковым basename сольются; `--project /` даёт пустое имя, файлы лягут прямо в journal/rounds/. Фикс: хэш пути при коллизии либо запрет пустого имени.

11. `smoke.sh:66` — подсказка ведёт по старому пути (`python3 live.py ask`), в свежем клоне journal/live.jsonl в .gitignore, смок падает первым шагом. README уже правильный.

12. `publish.sh:25` — уезжает stol.py, но не draw_ui.py, на который он ссылается (TOOLS); в draw_ui.py зашит личный путь — значит убрать запись из TOOLS, а не добавить файл.

# Что проверил и проблем не нашёл

- .gitignore публичного репо: отрицание работает (scratch-репозиторий: rounds/RoundTable/a.md и вложенные добавляются, rounds/Film/, notes/, live.jsonl — нет).
- Согласованность резолверов roundtable.py/edits.py/executor_run.py — идентичны; 5 сценариев env дают одинаковые пути; CHOIR_JOURNAL уходит подпроцессам через _spawn_env; merge_gate.py переведён на edits.JOURNAL.
- rounds_dir(): при импорте не вызывается; в read-only командах — тоже.
- NameError: rp/rdir/qfile определены до использования; py_compile чист по 12 модулям.
- Старые seed_file из журналов: 8 из 12 в debts.jsonl — старые Choir/…; через живой find_round_file все найдены; исчезнувший /tmp/… даёт честное «затравка недоступна».
- ROOM.parent нигде не осталось; ссылок на Choir/ в исполняемом коде нет.
- Тесты: edit 31/31, executor 14/14, gate 47/47, catalog 45/45, voices_http 49/49 (на порту 8788).
- smoke.sh: RT_DIR определён раньше JOURNAL=; ROUNDTABLE_JOURNAL передаётся серверу явно.
- Секреты в публикуемом: ключей нет (sk-create — подкоманда Orca, api_key — проза); почты/handle Автора нет; адаптеры чисты.

Ограничения прогона: порт 8770 не трогал; живых вызовов моделей не делал; в журналы не писал; ничего не правил и не коммитил.
