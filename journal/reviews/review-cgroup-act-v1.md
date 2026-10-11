# Ревизия: четыре долга свода и cgroup на акт (2026-09-17)

Повод: свод раунда «стол-v3-изоляция» назвал четыре факта о коде и
первый шаг порядка стола — «акт умирает целиком» через cgroup. Автор:
«начнём с них… systemd-run на акт и канареек».

Сделано:
1. `edits.codex_roots(common, act, gitdir)` — writable_roots Кодекса в
   кресле сужены с всего `.git` до gitdir дерева (по факту, `rev-parse
   --absolute-git-dir`), `objects`, `refs/heads/act`, `logs/refs/heads/act`;
   каталоги создаются заранее; кресло предупреждено про строку
   `packed-refs.lock` (`CODEX_CHAIR_NOTE`); в окружении кресла gc.auto=0.
2. `live.py`: `codex exec resume` — `-s read-only` и `--add-dir` ДО слова
   resume (после — «unexpected argument»; проверено на фиктивном id).
3. `choir.run_watched`: голос в своей группе процессов; `_killpg_tree`
   ждёт пустоты группы, SIGTERM → SIGKILL; `_on_term` перекидывает
   SIGTERM дирижёра группам голосов, как в live.py.
4. `roundtable.spawn`: каждый акт окна — transient scope systemd
   (`rt-act-<акт>-<pid окна>`); reap читает cgroup.procs по закрытии —
   пережившие процессы снимаются (SIGTERM, grace, SIGKILL через
   `systemctl kill`), заметка в ленту честно: «сняты» только при пустом
   cgroup; abort и закрытие окна бьют в scope независимо от killpg; при
   старте окна — заметка о живых scope прежних окон.
`/stop` не менялся: флаг между шагами — по замыслу, кнопка так и подписана.

Формат: мини-ревизия read-only по дифу (codex, deepseek, grok, kimi) +
построчная субагентом Claude со scratch-репо и bwrap-эмуляцией
песочницы; второй круг — субагент по дельте.

## Ответы дословно (первый круг)

### codex (rc=0)

Вывод: принимать рано — resume локально не запускается, а завершение всего дерева процессов не гарантировано.

1. **P1 — [live.py:523](~/AiSandbox/RoundTable/chamber/live.py:523).** Проверенный `codex-cli 0.154.0` отвергает оба флага после `resume`: `unexpected argument`. Парсер принимает порядок `codex exec -s read-only --add-dir … resume …`. `--add-dir` описывает дополнительный доступ записи, а не подключение проекта для чтения; read-only не становится workspace-write. [OpenAI Docs](https://developers.openai.com/codex/cli/reference).

2. **P1 — [choir.py:633](~/AiSandbox/RoundTable/chamber/choir.py:633).** После SIGTERM `proc.wait()` ждёт только лидера; его смерть вызывает `break`, поэтому устойчивый потомок не получает SIGKILL. Подтверждено изолированным вызовом с подставным процессом. Потомок, удерживающий pipe, оставляет `_pump` заблокированным: `join(timeout=5)` поток не завершает. `pgid==pid` первоначально верно, но вечной защиты от переиспользования номера нет. Отдельные группы голосов также выпадают из fallback-killpg акта без systemd.

3. **P1 — [roundtable.py:4083](~/AiSandbox/RoundTable/roundtable.py:4083).** Abort забирает запись из `RUNNING`: reap больше не очищает scope. Abort/закрытие посылают только TERM; ошибка killpg или завершившийся лидер могут вообще пропустить scope. Нужна независимая обязательная очистка с эскалацией.

4. **P2 — [roundtable.py:987](~/AiSandbox/RoundTable/roundtable.py:987).** `stop` не означает немедленный SIGKILL. Результат игнорируется, окончательная пустота не проверяется, но журнал утверждает «сняты». Ошибка чтения также превращается в «пусто».

5. **Git.** Для обычного files-backend commit корней достаточно: `worktree add -b` заранее создаёт gitdir и каталог ветки; packed-refs переписывать не требуется. Но gitdir может получить числовой суффикс — нужен фактический `--absolute-git-dir`. Доступны **все** `act/*`; continue повторяет ограничение. Автообслуживание требует отдельных проверок/отключения. [Git](https://git-scm.com/docs/git-worktree), [gc](https://git-scm.com/docs/git-gc).

6. **Scope/тесты.** `--collect` не собирает scope с живыми процессами: предполагаемая гонка здесь неверна. [systemd](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.scope.xml). Сессия совместима со scope; act+PID снижает коллизии. Проба покрывает недоступный manager, однако CI тогда пропускает scope-тесты. Не покрыты TERM-resistant потомки, abort/закрытие, ошибки manager, настоящий sandbox-commit и CLI-парсинг. Полный набор тестов не запускал: он пишет файлы.

### deepseek (rc=0)

Вывод: критично в `_killpg_tree` — SIGKILL уходит только если лидер не умер по SIGTERM; при смерти лидера `proc.wait` завершается досрочно, и внуки, игнорирующие SIGTERM, выживают. Надо ждать grace/проверять пустоту группы, затем SIGKILL.

а) codex_roots: для commit в linked worktree, вероятно, хватает worktree-gitdir, objects, refs/heads/act и logs/refs/heads/act; packed-refs/gc.log не войдут — auto-gc может ругаться, но commit едва ли блокирует. Риски: несуществующий `refs/heads/act` зависит от семантики writable_roots; `base_git` из `rev-parse` может быть относительным — нужно канонизировать в absolute.
б) `-s read-only` похоже на `--sandbox-mode`, но требует проверки, что это не `--session`; `--add-dir` после session id корректен.
в) Основная ошибка выше. `pgid == os.getpgrp()` при `process_group=0` практически не срабатывает; на abort-ветке `os.killpg(t["pgid"])` остаётся гонкой переиспользования pgid.
г) `_scope_reap` после `proc.wait`/`--collect` может вернуть `[]` уже собранного scope; живых сирот это не теряет, но диагностика слепнет. `_scope_procs` молча глотает cgroup v1/ошибки. `systemctl kill` на собранном unit возвращает ненулевой статус — вызывающие не проверяют.
д) `start_new_session` + scope: killpg бьёт systemd-run, scope-kill — детей; допустимо, но двойная сигнализация и гонка pgid.
е) Не покрыто: реальный Codex-commit с новыми roots; сценарий «лидер умер от SIGTERM, внуки игнорируют SIGTERM»; abort при переиспользовании pgid; cgroup v1/отсутствие user-manager.


### grok (rc=0)

Проверяю живые файлы по пунктам а–е из свода.Проверяю семантику resume, структуру worktree и гонку в `_killpg_tree` / `--collect`.**Ошибка: `_killpg_tree` считает группу мёртвой, как только умер лидер — SIGKILL не уходит, внук с игнором SIGTERM жив.**

**(в)** Подтверждено: `bash`+`sleep` и python с `SIG_IGN` в одной группе → после SIGTERM `proc.wait` успешен, цикл делает `break`, сирота жив. Тест в `autoreview_test` этого не ловит: `sleep` умирает на TERM. `pgid=proc.pid` при `process_group=0` верно; гонка переиспользования pgid после пустой группы редка (ESRCH), но номер держат дольше жизни лидера. `_pump` после kill — join 5 с, ок.

**(д)+(г)** В `live.py` на SIGTERM есть раздача SIGKILL по `_CHILD_PGIDS`; в `choir.py` — нет. `process_group=0` вынимает голос из pgid акта: без systemd `killpg` по акту голос не снимает. Scope это чинит; без user-bus (проба/`CHOIR_RT_NO_SYSTEMD`) — регресс. `Popen.pid` — это `systemd-run`, не команда; код выхода обычно пробрасывается. `--collect` не прячет сирот внутри cgroup (пока они там — unit жив); пустой `_scope_procs` после collect = «никого», не «не успели». Риск: `show`/чтение cgroup сбойнули → `[]` → тихий пропуск. Ветка `_STOPPING` при spawn шлёт только `killpg`, без `_scope_kill`. Имя unit с pid окна — ок.

**(а)** Для commit на `act/<id>` списка хватает: `worktrees/<act>` (index/HEAD/logs), `objects`, loose ref+reflog под `refs/heads/act` — проверено урезанными правами. `worktree add` до кресла уже создаёт каталоги; `continue_edit` — тот же список. `packed-refs` обычно не нужен (loose перекрывает). `gc --auto` может хотеть `gc.pid` в корне common — чаще warning, не отказ commit.

**(б)** `-s read-only` на resume согласован со `start`; `--add-dir` — нужный фикс. В `live.py:509–518` комментарий всё ещё врёт («нет ни `-s`, ни `--add-dir`»).

**(е)** Нет: ранний break `_killpg_tree`, abort/choir без scope, живой `git commit` под sandbox, exit-код через `systemd-run`, `_STOPPING`+scope, CI без systemd (только SKIP).


### kimi (rc=0)

• Проверю живые файлы — ключевые места: имя ветки в `worktree add` и пути ref при commit.

• **Ошибка: в `_killpg_tree` SIGKILL уходит только если жив ЛИДЕР — внук, проигнорировавший SIGTERM, переживает и SIGTERM, и SIGKILL: цикл делает `break` по `proc.wait()` лидера, группа уже не сигналится.**

  Разбор по пунктам:

  - **(а) `codex_roots` — в целом хватает.** Ветка — `act/<id>` (edits.py:457), ref/reflog под `refs/heads/act` — покрыты; index, HEAD, `COMMIT_EDITMSG`, `logs/HEAD` — в `<common>/worktrees/<act>`; объекты — в `objects`. Путь абсолютный (`--path-format=absolute`, edits.py:466). Ветка существует к запуску Кодекса (`worktree add -b`), continue её проверяет (edits.py:576) — сценарий «не существует» закрыт. Дыра одна: **`git gc --auto`/maintenance при commit пишет `packed-refs` в корень common-дира — вне roots**. Не фатально (auto-gc detached, код commit не портит), но шум в логе и вечный retry. Добавить `<c>/packed-refs` или `gc.auto=0` в окружении кресла.

  - **(б)** Семантика совпадает с прежней (read-only). Но `--add-dir` под `-s read-only` — **no-op**: read-only и так читает всё; флаг имеет смысл только в workspace-write. Безвредно, но комментарий врёт о его роли.

  - **(в)** Гонка «pgid переиспользован» закрыта: лидер-зомби не реаплен до kill — pgid не освобождается. Основная ошибка — выше: `break` по выходу лидера (choir.py:646-647). Упрямый внук остаётся сиротой — ровно тот кейс, ради которого всё затевалось; тест его не ловит, т.к. `sh` умирает от SIGTERM по умолчанию. Чинить: после SIGTERM ждать не лидера, а исчезновения группы (`killpg(0)`→ESRCH или poll), потом SIGKILL.

  - **(г)** Scope: pid/exit-код сохраняются (exec), `--collect` не прячет сирот — scope жив, пока cgroup не пуст, reap их видит. Проба падает корректно, CI пропустит. Два замечания: проба закэширована, но при сбое systemd-run **в момент spawn** фолбэка нет — акт умрёт мгновенной ошибкой; abort/закрытие окна шлют только SIGTERM, без эскалации и reap — сироты в scope доживают до SIGKILL ядром при сборке.

  - **(д)** Конфликта нет: setsid внутри scope разрешён, killpg остаётся запасным каналом.

  - **(е)** Не покрыто: проводка `codex_roots` через `continue_edit`, упрямый внук в `_killpg_tree`, spawn→reap сквозь RUNNING целиком, abort-путь `_scope_kill`, `CHOIR_RT_NO_SYSTEMD=1`.



### субагент Claude (построчно, scratch-репо, bwrap)

1. КРИТИЧНО: `codex exec resume` не принимает `-s`/`--add-dir` после
   resume (codex-cli 0.154.0: «unexpected argument»); первая редакция
   ломала бы каждое продолжение нити Кодекса; тест был зелёным, потому
   что проверял только сборку argv.
2. `_scope_kill("SIGKILL")` через `systemctl stop` — это SIGTERM и 90 с
   ожидания; замер: stop висел >30 с, `kill --signal=SIGKILL` — 10 мс.
3. `codex_roots`: commit под суженными корнями проходит (bwrap:
   меняются только gitdir, objects, refs/heads/act, reflog); отказ на
   branch/update-ref main/config/hooks — как задумано; но каждый commit
   печатает «Unable to create packed-refs.lock: Read-only file system»
   (rc 0, транзакция удаления CHERRY_PICK_HEAD); `git pack-refs`
   Автора удаляет каталог refs/heads/act — кресло падало бы; имя gitdir
   может получить числовой суффикс.
4. Abort вынимает запись из RUNNING — `_scope_reap` не вызывался;
   killpg мог бросить раньше `_scope_kill`.
5. Scope живут в app.slice, не в сессии терминала — акты переживают
   вкладку и падение окна.
Чисто: `_killpg_tree` семантика групп, `systemd-run --scope` exec'ит
команду (pid, код выхода), `--collect` не прячет сирот, тесты DEVNULL.

## Что сделано по находкам

- **Порядок флагов resume** (субагент 1, codex P1) — флаги до resume;
  тест проверяет порядок.
- **`_killpg_tree` ждёт пустоты группы** (все четверо, субагент) — тест с
  внуком, игнорирующим SIGTERM.
- **Голоса вне pgid акта без systemd** (grok) — `_CHILD_PGIDS` и
  `_on_term` в choir.py.
- **SIGKILL по-настоящему** (субагент 2) — `systemctl kill --signal`;
  `_scope_procs` None при ошибке (codex P2, deepseek); заметка «НЕ
  сняты», если cgroup не опустел.
- **Abort и закрытие** (codex P1, субагент 4, kimi) — scope независимо
  от killpg, `_scope_reap` из abort, SIGKILL упрямым при закрытии,
  `_STOPPING`-ветка.
- **gitdir по факту, каталоги refs, packed-refs, gc.auto** (codex 5,
  субагент 3, kimi) — `--absolute-git-dir`, mkdir, `CODEX_CHAIR_NOTE`,
  `GIT_CONFIG_*` в окружении кресла.
- **Scope в app.slice** (субагент 5) — заметка при старте окна о живых
  scope прежних окон.
- Не менялось: `--add-dir` под read-only — no-op (kimi), оставлен как
  документация намерения; `/stop` — флаг по замыслу.

Тесты: autoreview 133 (в т.ч. упрямый внук, SIGKILL scope, порядок
resume, roots по gitdir), voices_http 69, gate 55, executor 14, edits 31,
argv 29, lease 51, smoke 13 PASS · 2 WARN.

## Второй круг (субагент Claude по дельте, со scratch-замерами)

1. `_scope_procs`: каталог cgroup снесён systemd раньше, чем `show`
   перестал отдавать путь — на каждом втором нормально завершившемся
   акте ложная заметка «cgroup не прочитан» (16 из 30 прогонов).
2. abort: `killed = True` безусловно после `_scope_kill` — умерший сам с
   rc≠0 читался бы как «прерван».
3. choir.py ставил обработчик SIGTERM при импорте — live.py импортирует
   choir лениво из turn(), и обработчик комнаты подменялся пустым.
4. Закрытие окна: detached-шаг раунда объявлялся «продолжается» и через
   секунду добивался SIGKILL по cgroup.
5. `_CHILD_LOCK` в обработчике сигнала — самозахват в главной нити.
6. Abort-нить `_scope_reap` при выходе окна умирает посреди grace —
   scope всплывёт у следующего окна (приемлемо).
Чисто: D-state в `_killpg_tree`, порядок флагов resume (проверено с
фиктивным id), exec у systemd-run, `CODEX_CHAIR_NOTE` не в ленте,
`GIT_CONFIG_*` только для кресла, тест с упрямым внуком.

Сделано по второму кругу: FileNotFoundError на cgroup.procs → пусто;
`killed` только если акт был жив до сигнала; обработчик choir — только
при SIG_DFL; фильтры закрытия окна согласованы (auto или detach);
обработчики без замка. Пункт 6 — принято как известная граница.

Итоговые тесты: autoreview 133, voices_http 69, gate 55, executor 14,
smoke 13 PASS · 2 WARN.
