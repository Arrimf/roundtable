// Прогон СИНХРОННОГО пути страницы: инициализация + applyVoices на
// живом ответе /voices. Коды выхода — единственная правда для смока
// (grep по подстроке пропускал смешанный вывод — нашли codex и
// deepseek): 0 — чисто, 1 — ошибка исполнения, 2 — негодные данные.
require(require('path').resolve(process.argv[2])); // domstub: ставит globals
const __rt_fs = require('fs');
const __rt_page = __rt_fs.readFileSync(process.argv[3], 'utf8');
const __rt_data = JSON.parse(__rt_fs.readFileSync(process.argv[4], 'utf8'));
if (!Array.isArray(__rt_data.voices) || !__rt_data.voices.length) {
  console.log('ОШИБКА ДАННЫХ: /voices пуст — applyVoices нечем проверить');
  process.exit(2);
}
try {
  // new Function: своя область видимости — const страницы не столкнутся
  // с нашими (codex: «прямой eval хрупок»).
  new Function('data', __rt_page + '\n;applyVoices(data.voices);')(__rt_data);
  console.log('без ошибок:', __rt_data.voices.length, 'голосов');
  process.exit(0);
} catch (e) {
  console.log('ОШИБКА:', e.message);
  console.log((e.stack || '').split('\n').slice(0, 5).join('\n'));
  process.exit(1);
}
