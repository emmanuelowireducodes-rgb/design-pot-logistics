function initializeSettlementUI(document) {
  document.querySelectorAll('[data-settlement-client-picker] select').forEach(select => {
    select.addEventListener('change', () => select.form.requestSubmit());
  });

  function sumSelectedCents(items) {
    return items.reduce((total, item) => {
      if (!item.checked) return total;
      const cents = Number.parseInt(item.dataset.settlementItemAmountCents, 10);
      return total + (Number.isFinite(cents) && cents > 0 ? cents : 0);
    }, 0);
  }

  document.querySelectorAll('[data-settlement-form]').forEach(form => {
    const inputs = [...form.querySelectorAll('[data-settlement-item-amount-cents]')];
    const output = form.querySelector('[data-settlement-total]');
    if (!output) return;
    const update = () => {
      const amount = (sumSelectedCents(inputs) / 100).toFixed(2);
      output.textContent = `GH₵${amount}`;
    };
    form.addEventListener('change', event => {
      if (event.target.matches('[data-settlement-item-amount-cents]')) update();
    });
    update();
  });
  return sumSelectedCents;
}

if (typeof module === 'object' && module.exports) {
  module.exports = {initializeSettlementUI};
}
if (typeof window !== 'undefined' && window.document) {
  initializeSettlementUI(window.document);
}
