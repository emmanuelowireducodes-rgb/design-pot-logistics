const test = require('node:test');
const assert = require('node:assert/strict');
const {initializeZoneBandSelection} = require('../static/pricing-bands.js');

function fakePage(count) {
  const boxes = Array.from({length: count}, () => ({checked: false, listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }}));
  const buttons = Object.fromEntries(['select-all-zones', 'clear-zones'].map((id) => [id, {listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }}]));
  const output = {textContent: ''};
  return {
    boxes, buttons, output,
    querySelectorAll: (selector) => selector === '.zone-select' ? boxes : [],
    getElementById: (id) => id === 'selected-zone-count' ? output : buttons[id],
  };
}

test('Select All selects every zone in the rendered result set and updates count', () => {
  const page = fakePage(115);
  initializeZoneBandSelection(page);
  page.buttons['select-all-zones'].listeners.click();
  assert.equal(page.boxes.filter((box) => box.checked).length, 115);
  assert.equal(page.output.textContent, 115);
});

test('Clear All clears every selected zone and updates count', () => {
  const page = fakePage(3);
  initializeZoneBandSelection(page);
  page.buttons['select-all-zones'].listeners.click();
  page.buttons['clear-zones'].listeners.click();
  assert.equal(page.boxes.filter((box) => box.checked).length, 0);
  assert.equal(page.output.textContent, 0);
});
