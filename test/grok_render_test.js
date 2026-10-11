#!/usr/bin/env node
// Отрисовка потока grok во вкладке дирижёра (render/GROKRUN): дельты одного
// рода склеиваются в одну строку И внутри одной порции, и между порциями;
// смена рода и любая другая строка ставят перевод строки. Повод — ревизия
// 2026-10-08: isConnected узла во фрагменте ложен, и в одной порции каждая
// дельта становилась отдельным куском без переводов строк.
// Запуск: node RoundTable/test/grok_render_test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '../roundtable.py'), 'utf8');
const a = src.indexOf("  function line(cls,text){");
const b = src.indexOf("  async function tick(){", a);
assert.ok(a > 0 && b > a, 'render не найден');
const code = src.slice(a, b);

function el(tag){return {tag, className:'', textContent:'', parent:null, get isConnected(){return this.parent===PRE}}}
const PRE = {children:[]};
const document = {
  createElement: el,
  createDocumentFragment: () => ({children:[], appendChild(c){c.parent=this; this.children.push(c); return c}}),
};
const ctx = {document, JSON, String, Number, Math};
vm.createContext(ctx);
vm.runInContext(code + '\nthis.render=render;', ctx);
function attach(frag){frag.children.forEach(c => {c.parent = PRE; PRE.children.push(c)})}
const J = o => JSON.stringify(o);
// порция 1: мысль дельтами, текст дельтами, вызов инструмента — в ОДНОЙ порции
attach(ctx.render([J({type:'thought',data:'Посчитаю '}), J({type:'thought',data:'файлы'}),
  J({type:'text',data:'Счи'}), J({type:'text',data:'таю.'}),
  J({type:'tool_call',toolName:'run_terminal_command',rawInput:{command:'ls'}}), ''].join('\n')));
// порция 2: продолжение текста отдельной порцией, потом итог голоса (не JSON)
attach(ctx.render([J({type:'text',data:'ЧИСЛО: '}), ''].join('\n')));
attach(ctx.render([J({type:'text',data:'3'}), '● grok   1.0 с   9 симв.', ''].join('\n')));
const texts = PRE.children.map(c => c.textContent);
assert.equal(texts[0], '💭 Посчитаю файлы\n', 'мысль — одна строка с переводом в конце');
assert.equal(texts[1], '📝 Считаю.\n', 'текст — одна строка, закрыт перед инструментом');
assert.ok(texts[2].startsWith('🔧 run_terminal_command'), 'инструмент — своей строкой');
assert.equal(texts[3], '📝 ЧИСЛО: 3\n', 'текст между порциями склеен и закрыт итогом голоса');
assert.ok(texts[4].startsWith('● grok'), 'итог голоса — своей строкой, не приклеен к тексту');
assert.equal(PRE.children.length, 5);
console.log('grok_render: PASS 6 · FAIL 0');
