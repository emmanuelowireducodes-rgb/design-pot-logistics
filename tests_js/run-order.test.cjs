const test = require('node:test');
const assert = require('node:assert/strict');
const { moveBefore } = require('../static/run-order.js');

test('manual drag ordering moves a stop before or after its target', () => {
  assert.deepEqual(moveBefore(['a', 'b', 'c'], 'c', 'a', false), ['c', 'a', 'b']);
  assert.deepEqual(moveBefore(['a', 'b', 'c'], 'a', 'b', true), ['b', 'a', 'c']);
});

test('manual drag ordering ignores missing IDs without losing existing stops', () => {
  assert.deepEqual(moveBefore(['a', 'b'], 'x', 'a', false), ['a', 'b']);
  assert.deepEqual(moveBefore(['a', 'b'], 'b', 'x', false), ['a', 'b']);
});
