#!/usr/bin/env node
// Реальные обработчик кнопки и loadVoices, подменены HTTP/часы/DOM.
// Запуск: node RoundTable/test/limits_refresh_ui_test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../roundtable.py'), 'utf8');
const start = source.indexOf("document.getElementById('lrefresh').onclick=");
assert.ok(start >= 0);
const end = source.indexOf('function setScope(', start);
assert.ok(end > start);
const handler = source.slice(start, end);
const loadStart = source.indexOf('async function loadVoices(){');
const loadEnd = source.indexOf('loadVoices();setInterval(', loadStart);
assert.ok(loadStart >= 0 && loadEnd > loadStart);
const loader = source.slice(loadStart, loadEnd);

async function click(replies, postStatus = 202) {
  const button = {disabled: false, textContent: '⟳ лимиты'};
  const notes = [];
  const applied = [];
  let now = 0, polls = 0, posts = 0;
  const context = {
    document: {getElementById: id => {assert.equal(id, 'lrefresh'); return button}},
    vnote: message => notes.push(message),
    fetch: async (url, options) => {
      assert.equal(button.disabled, true);
      assert.ok(options.signal);
      if(url === '/voices') {
        const reply = replies[Math.min(polls++, replies.length - 1)];
        if(!reply)throw new Error('network unavailable');
        return {ok: true, json: async () => ({voices: [{name: 'test'}], ...reply})};
      }
      posts++;
      assert.equal(url, '/limits_refresh');
      assert.equal(options.method, 'POST');
      if(postStatus instanceof Error)throw postStatus;
      return {ok: postStatus === 202, status: postStatus};
    },
    applyVoices: list => applied.push(list),
    LIM_AGE: null,
    VOICES_SRC: false,
    AbortSignal,
    Date: class extends Date {static now() {return now}},
    setTimeout: (resolve, ms) => {now += ms; resolve()},
  };
  vm.runInNewContext(loader + handler, context);
  await button.onclick.call(button);
  assert.equal(posts, 1);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '⟳ лимиты');
  return {notes, polls, applied, age: context.LIM_AGE};
}

test('завершение замера обновляет карточки и возраст показаний', async () => {
  const voices = [{name: 'test', limits: {kind: 'ok', note: 'fresh'}}];
  const {notes, applied, age} = await click([
    {limits_refreshing: true, limits_age_s: 200},
    {limits_refreshing: false, limits_error: false, limits_age_s: 0, voices},
  ]);
  assert.equal(applied.length, 2);
  assert.equal(applied.at(-1), voices);
  assert.equal(age, 0);
  assert.match(notes.at(-1), /замер лимитов завершён/);
});

test('ошибка прошлого замера и единичный сбой опроса не прерывают ожидание', async () => {
  const {notes, polls} = await click([
    {limits_refreshing: true, limits_error: true},
    undefined,
    {limits_refreshing: false, limits_error: false},
  ]);
  assert.equal(polls, 3);
  assert.match(notes.at(-1), /замер лимитов завершён/);
  assert.ok(notes.every(n => !n.includes('ошибк')));
});

test('завершившийся сбой сообщает о сохранении прежнего замера', async () => {
  const {notes, polls} = await click([{limits_refreshing: false, limits_error: true}]);
  assert.equal(polls, 1);
  assert.match(notes.at(-1), /сбор лимитов завершился ошибкой; сохранён прежний замер/);
});

test('отказ POST возвращает кнопку в рабочее состояние', async () => {
  const {notes, polls} = await click([], 503);
  assert.equal(polls, 0);
  assert.match(notes.at(-1), /ошибка 503/);
});

test('обрыв POST возвращает кнопку в рабочее состояние', async () => {
  const {notes, polls} = await click([], new Error('request timed out'));
  assert.equal(polls, 0);
  assert.match(notes.at(-1), /request timed out/);
});

test('ответ без карточек не выдаётся за завершение замера', async () => {
  const {notes, polls, applied} = await click([
    {voices: [], limits_refreshing: false},
    {limits_refreshing: false, limits_error: false},
  ]);
  assert.equal(polls, 2);
  assert.equal(applied.length, 1);
  assert.match(notes.at(-1), /замер лимитов завершён/);
});

test('долгий сбор ограничен минутой ожидания', async () => {
  const {notes, polls} = await click([{limits_refreshing: true, limits_error: false}]);
  assert.equal(polls, 60);
  assert.match(notes.at(-1), /замер ещё идёт/);
});

test('недоступность опроса не выдаётся за сбой самого замера', async () => {
  const {notes, polls} = await click([undefined]);
  assert.equal(polls, 60);
  assert.match(notes.at(-1), /не удалось проверить завершение замера/);
});
