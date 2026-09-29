const test = require('node:test');
const assert = require('node:assert/strict');
const {initializeSettlementUI} = require('../static/settlement-amount.js');

function fakeForm(items) {
  const listeners = {};
  const output = {textContent: ''};
  return {
    output,
    form: {
      querySelectorAll: () => items,
      querySelector: () => output,
      addEventListener: (name, listener) => { listeners[name] = listener; },
    },
    change: target => listeners.change({
      target: {...target, matches: selector => selector === '[data-settlement-item-amount-cents]'},
    }),
  };
}

function fakeDocument(items, select) {
  const {form, output, change} = fakeForm(items);
  const clientSelect = select ? {
    form: {requestSubmit: () => { clientSelect.submitted = true; }},
    addEventListener: (_name, listener) => { clientSelect.change = listener; },
  } : null;
  return {
    form, output, change, clientSelect,
    querySelectorAll: selector => selector === '[data-settlement-form]' ? [form] :
      clientSelect ? [clientSelect] : [],
  };
}

test('selecting a Client loads that Client settlement list', () => {
  const doc = fakeDocument([], true);
  initializeSettlementUI(doc);
  doc.clientSelect.change();
  assert.equal(doc.clientSelect.submitted, true);
});

test('selected settlement preview sums checked reconciliations in integer cents', () => {
  const items = [
    {checked: true, dataset: {settlementItemAmountCents: '12550'}},
    {checked: false, dataset: {settlementItemAmountCents: '20000'}},
    {checked: true, dataset: {settlementItemAmountCents: '349'}},
  ];
  const doc = fakeDocument(items);
  initializeSettlementUI(doc);
  assert.equal(doc.output.textContent, 'GH₵128.99');
  items[1].checked = true;
  doc.change(items[1]);
  assert.equal(doc.output.textContent, 'GH₵328.99');
});

test('invalid and non-positive item amounts cannot inflate the preview', () => {
  const doc = fakeDocument([
    {checked: true, dataset: {settlementItemAmountCents: 'invalid'}},
    {checked: true, dataset: {settlementItemAmountCents: '-100'}},
    {checked: true, dataset: {settlementItemAmountCents: '0'}},
  ]);
  initializeSettlementUI(doc);
  assert.equal(doc.output.textContent, 'GH₵0.00');
});
