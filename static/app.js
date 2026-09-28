const menu = document.querySelector('.menu-toggle');
menu?.addEventListener('click', () => {
  const expanded = menu.getAttribute('aria-expanded') !== 'true';
  menu.setAttribute('aria-expanded', String(expanded));
  menu.setAttribute('aria-label', expanded ? 'Close navigation' : 'Open navigation');
  document.body.classList.toggle('nav-open', expanded);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    document.body.classList.remove('nav-open');
    menu?.setAttribute('aria-expanded', 'false');
  }
});
const offline = document.querySelector('#offline');
function updateConnection() { if (offline) offline.hidden = navigator.onLine; }
window.addEventListener('online', updateConnection);
window.addEventListener('offline', updateConnection);
updateConnection();
document.querySelectorAll('form').forEach(form => {
  form.addEventListener('submit', () => {
    if (!form.checkValidity()) return;
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (button) { button.setAttribute('aria-busy', 'true'); button.textContent = 'Please wait…'; }
  });
});
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });

document.querySelectorAll("[data-open-dialog]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.openDialog)?.showModal()));

const bookingForm = document.querySelector("[data-booking-form]");
if (bookingForm) {
  const service = bookingForm.querySelector("#id_service");
  const payment = bookingForm.querySelector("#id_payment_method");
  const toggleFields = () => {
    bookingForm.querySelector("[data-premium-fields]").hidden = service.value !== "premium";
    bookingForm.querySelector("[data-cod-fields]").hidden = payment.value !== "cod";
  };
  service.addEventListener("change", toggleFields);
  payment.addEventListener("change", toggleFields);
  toggleFields();
}
