# Ревизия: гейт обновляет рабочую копию, журнал не мешает (2026-09-16)

Повод: гейт принял правку 140adf4a5a91 в main (запись 887), но рабочую
копию Автора не обновил: `git status -uno` всегда показывает
`RoundTable/journal/live.jsonl`, и гейт считал копию «грязной», советуя
`git reset --hard` — который стёр бы недописанные строки журнала. Автор
перезапускал окно и не видел принятой кнопки: код в main, в checkout
старый. Копию синхронизировал руками (`git reset -q` + checkout файлов
правки), Codex-тесты кнопки прошли (4 python, 8 node), они добавлены в
publish.sh.

Сделано в merge_gate: журналы стола не считаются грязью; вместо
`reset --hard` — `_sync_checkout` (индекс на result, файлы правки из
индекса, удалённые убраны, журналы и чужое не тронуты); пути через NUL
и без rename-детекции; литеральные pathspec; проверка ветки checkout
перед reset; подсказка Автору в правильном порядке.

Формат: мини-ревизия read-only (deepseek, grok; gemini отпал — 503 у
шлюза на всех пяти ключах; codex без квоты) + построчная субагентом
Claude, который воспроизводил сценарии в scratch-репозитории.

## Ответы дословно

### deepseek (rc=0)

Ошибка: `_sync_checkout` не фильтрует `moved` по `journal_rel`. Если журнал попал в дифф `base..result` (он версионируется), `git checkout --` перезапишет `RoundTable/journal/live.jsonl` и потеряет недописанные строки — тот же data loss, который хотели убрать. Нужно исключать журнальные пути из `moved`.

Дополнительно: `_dirty_paths` не разбирает rename/copy-строки porcelain (`R  old -> new`): `ln[3:]` вернёт `"old -> new"` вместо реальных путей, такой путь не совпадёт ни с `journal_rel`, ни с `moved`, и копия ложно останется «грязной». Также `unlink` в `_sync_checkout` может удалить untracked-файл, скрытый `-uno`, если правка удаляет одноимённый tracked-путь.


### grok (rc=0)

Читаю `merge()` и соседние тесты, чтобы проверить порядок `update-ref`/`reset` и что ещё, кроме журналов, может оставаться грязным.Сверяю тесты, разбор porcelain и то, какие ещё пути постоянно грязные.**Ошибка: журналы вычтены из «грязи», но не из `moved` — checkout/unlink их сотрёт; `_journal_rel` через `resolve()` на симлинке/`ROUNDTABLE_JOURNAL` вне repo даёт `""` и снова заморозит копию.**

Кроме журналов «всегда грязного» в индексе нет: `room.jsonl`/`debts`/`rounds` под тем же префиксом; leases в `~/.cache`; prompt/state в gitignore; `-uno` режет untracked. Чужие правки в Film/ и т.п. блокируют намеренно. Если `journal_rel==""`, git всё равно показывает `RoundTable/journal/live.jsonl` — фильтр его не узнаёт (`startswith` не бьётся с quoted/`R  old -> new`).

Порядок верный: `update-ref`, затем sync. На `branch_now` HEAD уже `result`, `reset --mixed` только индекс. Ветку `merge()` не сверяет (вопреки шапке): не на main — `reset -q result` **переставит текущую ветку**. Надёжнее `git read-tree`.

Не-moved worktree mixed не трогает; staged-only хуки из индекса выпадут. При skip-sync индекс остаётся от старого дерева.

После reset удалённый файл уже не staged — unlink уместен. Сначала checkout, потом unlink: замена файла каталогом падает. `diff --name-only` без `--no-renames` не даёт старый путь — регрессия vs `--hard`. Подсказка режет до 6 имён и зовёт `checkout` удалённых.

Тест: нет `merge()` с грязным `live.jsonl`, нет удаления и грязного не-moved tracked; `_sync_checkout` не проверяет содержимое.


### gemini (rc=1 — отказ канала)

gemini-http: ключ #3: TimeoutError: The read operation timed out, повтор через 4 с
gemini-http: ключ #3: 503 у шлюза, повтор через 4 с
gemini-http: ВСЕ 5 ключей отбиты (по квоте 0, шлюзом или сетью 5). Последняя ошибка: 503 This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.


### субагент Claude (построчно, read-only, со scratch-репо)

1. Rename в правке → копия никогда не синхронизируется, с ложным
   обвинением Автора (`R old -> new` в porcelain и один путь в diff).
2. Подсказка при грязной копии вредна по порядку: «закоммитьте, потом
   reset» — коммит из старого индекса откатил бы правку акта.
3. Пути с пробелом/кириллицей — `.split()` и кавычки git → тихий
   пропуск файла.
4. `checkout --` не исключал журнал из moved — стёр бы дописанные строки.
5. `reset -q result` двигает ТЕКУЩУЮ ветку checkout, ветка не сверяется.
6. Untracked-файл Автора по добавляемому правкой пути перезаписывается
   (было и при reset --hard).
7. Pathspec-глоб (`* ? [`) в ls-files/checkout — трекаемый файл мог быть
   удалён с диска.
8. Тест `_sync_checkout` был no-op (HEAD=index=result).
Чисто: `_git -C project`, mixed reset не трогает дерево, `_journal_rel`
в живой раскладке, symlink/каталоги, argv, publish.sh.

## Что сделано по находкам

- **Журнал в moved** (deepseek, grok, субагент 4) — журнальные пути не
  выписываются и не удаляются; сообщение называет, сколько пропущено.
- **Rename и пути с пробелом/кириллицей** (субагент 1, 3; deepseek, grok)
  — `diff --name-only --no-renames -z`, `status --porcelain --no-renames
  -z -uno`, разбор по NUL.
- **Ветка checkout** (субагент 5, grok) — `_sync_checkout` сверяет
  `rev-parse --abbrev-ref HEAD` с веткой приёмки, иначе отказ с рецептом.
- **Pathspec-глоб** (субагент 7) — `git --literal-pathspecs`.
- **Порядок подсказки** (субагент 2, grok) — reset → checkout → свой
  коммит; список до 20 файлов с многоточием; удалённые названы.
- **Тест** (субагент 8, grok) — scratch-репо: журнал внутри, HEAD на
  result, индекс на base, правка с пробелом в имени и удалением,
  дописанная строка журнала; проверяется содержимое, статус и отказ на
  чужой ветке.
- **Untracked по добавляемому пути** (субагент 6) — не менялось,
  поведение прежнее; редкий узор.
- `_journal_rel` при `ROUNDTABLE_JOURNAL` вне репо (grok) — тогда
  журнал и не в этом репозитории, фильтр не нужен; симлинк git трекает
  как ссылку.

Тесты: gate 55, autoreview 107, voices_http 69.
