# Ревизия: обрыв соединения клиентом — одна строка, не трасса (2026-09-16)

Повод: Автор — «попробовал продолжить добавление кнопки через стол.. он
упал». По ленте: продолжение 88cfaf98 (Codex) закрылось rc=1 — у Codex
снова кончилась квота («try again at 7:01 PM»), но правки в тесты он
успел закоммитить; гейт сам перенёс ветку на main (873) и запустил
авторевизию b4874e5c. В терминале в этот момент socketserver напечатал
трассу BrokenPipeError на 20 строк (браузер оборвал /voices), Автор
прочёл её как падение и нажал ^C — веер ревизии прерван (876). Окно не
падало.

Сделано: подкласс `_Server(ThreadingHTTPServer)` с `handle_error`: для
BrokenPipeError / ConnectionResetError / ConnectionAbortedError — одна
строка в stderr, остальное — штатная трасса. Воспроизведено на тестовом
окне: три обрыва — три строки, ноль трасс.

Формат: мини-ревизия read-only (deepseek, grok; Codex без квоты) +
построчная субагентом Claude.

## Ответы дословно

### deepseek (rc=0)

Ошибка: `TimeoutError` в списке «обрывов» — ложное срабатывание. Это таймаут сокета/ожидания (например, `settimeout`/`readline` в SSE), а не закрытие браузером; так вы скроете реальный зависший или медленный запрос и преждевременно закроете его. Уберите `TimeoutError`; если нужно — гасите точечно в `/events`, а не в центральном `handle_error`.

`sys.exc_info()[1]` корректен: `handle_error` вызывается из `except` в потоке-обработчике, контекст жив и потоково-локален, гонки нет. Остальные три исключения ловить можно, для прочих `super()` оставляет traceback.

Проверьте `/events` около 3289: если там уже ловится `BrokenPipe`/`ConnectionReset`, до `_Server.handle_error` не дойдёт.


### grok (rc=0)

Сначала сверю диф с живым кодом: `handle_error`, SSE `/events` и места, где может всплыть `TimeoutError`.**TimeoutError из тихого списка выкинуть: обрыв клиента здесь — BrokenPipe/ConnectionReset/Aborted; TimeoutError как «вкладку закрыли» не бывает и спрятал бы чужую аварию.**

Окно сокет не таймерит (`Handler.timeout is None`). Чтение/запись с дедлайном уже ловит `handle_one_request` и из‑за `log_message=pass` молчит — до `handle_error` не доходит. urllib/ThreadPool сидят в фоне `collect_limits`, не в запросе. Если TimeoutError всё же всплывёт из обработчика — это сбой логики, не клиент.

`sys.exc_info()` жив: `ThreadingMixIn.process_request_thread` зовёт `handle_error` из `except Exception`, как дефолтный `traceback.print_exc()`. Нить воркера, состояние локально.

`/events`: `_stream` (~3306) уже глотает BrokenPipe/ConnectionReset/**OSError** (Aborted и Timeout входят) — до `_Server` не доходит, двойного лога нет. Трасса была у `/voices` (`_json` без except); новый слой как раз для коротких ответов.


### субагент Claude (построчно, read-only)

Чисто: sys.exc_info() в handle_error жив (socketserver зовёт его из
except); KeyboardInterrupt мимо — ^C как прежде; прочие исключения
_json/wfile.write различимы (TypeError, EBADF — с трассой); /events
ловит OSError сам; daemon_threads/allow_reuse_address наследуются;
других конструкторов сервера нет. Одно замечание: TimeoutError в кортеже
не защищает ни от чего реального (сокетных таймаутов нет), а в будущем
подписал бы чужую ошибку словами «клиент оборвал».

## Что сделано по находкам

- **TimeoutError из тихого списка убран** (deepseek, grok, субагент —
  все трое).
- Остальное — без замечаний.

Тесты: autoreview 107, voices_http 69.
