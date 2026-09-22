# Ревизия: длинный промпт голосу — не аргументом (2026-09-23)

Повод (правило 4): свод раунда `prodolzhit-lyuboe-v1` голосом claude
дважды упал за 0.0 с — «OSError: [Errno 7] Argument list too long:
'bwrap'». Промпт свода (весь раунд) шёл одним аргументом argv; предел
ОДНОГО аргумента execve — MAX_ARG_STRLEN = 131072 байт (ARG_MAX 2 МБ
здесь ни при чём). Клетка bwrap лишь первая в цепочке exec.

Сделано: `chamber/promptio.py` — `deliver(argv, prompt, pfile)`: промпт
длиннее 120000 байт снимается из argv; claude и codex получают его в
stdin (оба читают промпт оттуда без аргумента — справка codex: «[PROMPT]
… instructions are read from stdin»); kimi stdin не читает («argument
missing» живьём) — ему строка «Задание целиком — в файле <свой файл
промпта>» (в раунде — каталог вызова в карантине, ro в клетке; в
комнате — свой каталог голоса, rw); grok и HTTP-адаптеры промпт и так
берут файлом. `feed_stdin` — отдельная нить, EOF в finally. Факты:
`prompt_via` (stdin/file) в записи голоса и событии комнаты; `seen_sha`
— sha того, что голос реально получил, когда это не сам пакет (kimi).
`choir.run_watched(stdin_text)`, `live._run_capture(stdin_text)`.
`test/longprompt_test.py` 11/11 (140 КБ через stdin в обоих путях,
kimi-обёртка, codex без «-», basename, порог 120 КБ, EOF при ошибке
записи). Живьём: claude в клетке с промптом 245 КБ через stdin — «ОК»
за 12–18 с; свод раунда переписан заново тем же шагом окна (акт
405735ed, 165 с, 15 КБ карточки).

Формат: мини-ревизия read-only (grok, kimi; субагент построчно).
codex — подписка истекла; deepseek/gemini не звались (мелкая правка).

## Записи

**grok — «E2BIG у claude закрыт; обёртка бьёт и по Кодексу».** «Всякий
не-claude с промптом в argv получает обёртку вместо текста — Кодекс
устроен именно так: промпт аргумент, не --prompt-file; тест закрепляет
ложь «codex — файлом». prompt_sha и context_sha сняты с исходного текста
и выдают себя за «что голос видел» — при via=file он видел обёртку.
Порог 60000 — не запас до 128 КБ: длина уже в байтах, соседние
аргументы в MAX_ARG_STRLEN не входят. Не дефекты: дедлока нет, bwrap
трубу наследует, Кими видит файл ro, str в text-pipe / bytes в
бинарный, stream-json задаёт вывод, stdin остаётся текстом.»

**kimi — ДОПУСТИТЬ.** «Нить _feed без finally: close — UnicodeEncodeError
(ValueError) при text=True не ловится, CLI не увидит EOF и повиснет до
сторожа. Опознание claude по argv[0] == "claude" — с полным путём
сработает kimi-ветка. Дубль логики choir/live уже разошёлся — вынести в
общий модуль. stdin_text не инициализирован до try.»

**Субагент (построчно), первая попытка** — снят фильтром безопасности
моего канала («safeguards flagged this message», reasoning_extraction)
на тексте ревизии. **Вторая — ОДОБРЯЮ.** «Разбор argv по равенству
безопасен: других аргументов такой длины нет; basename — claude/codex
голым именем или ~/.local/bin; deliver до jail.wrap; with_retry —
согласованно; UnboundLocalError нет; stdin при kill — BrokenPipe →
finally close; seen_sha только когда seen ≠ prompt. Оговорки: `codex
exec resume` без [PROMPT] читает stdin — не проверено живьём (в бинаре
есть «Failed to read prompt from stdin»); recover для kimi с
prompt_via=file не найдёт сессию — сверка по prompt_sha, в логе обёртка.
Мелочь: в шапке стенограммы факт stdin не назван.»

## Что сделано

- Общий модуль `promptio.py` вместо двух копий (kimi); codex — stdin
  без аргумента (grok; живьём не проверен — подписка истекла, по
  справке codex).
- `seen_sha` от того, что голос получил; `prompt_sha`/`context_sha` —
  по-прежнему sha пакета (grok).
- Порог 120000 байт (grok).
- `feed_stdin`: finally close, ValueError пойман (kimi); basename
  argv[0] (kimi); `seen_text`/`prompt_via` инициализированы до try
  (kimi).
- Тесты: codex без «-», полный путь claude, 100 КБ ещё аргументом, EOF
  при ошибке записи.
- Шапка стенограммы: «промпт N симв. — через stdin/file» (субагент).

Долги (названы): `codex exec resume` со stdin — проверить живьём при
живой подписке; `recover` Кими при via=file — сверять и по seen_sha.
