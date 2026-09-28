const menu = document.querySelector('.menu-toggle');
const drawer = document.querySelector('#navigation');
const workspace = document.querySelector('.workspace');
const backdrop = document.querySelector('.drawer-backdrop');
const closeDrawer = document.querySelector('.drawer-close');
if (drawer && window.matchMedia('(max-width: 760px)').matches) drawer.inert = true;
function setDrawerOpen(open) {
  if (!menu || !drawer) return;
  document.body.classList.toggle('nav-open', open);
  menu.setAttribute('aria-expanded', String(open));
  menu.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
  drawer.setAttribute('role', open ? 'dialog' : 'complementary');
  drawer.setAttribute('aria-modal', String(open));
  drawer.inert = !open && window.matchMedia('(max-width: 760px)').matches;
  if (workspace) workspace.inert = open;
  document.body.style.overflow = open ? 'hidden' : '';
  if (open) (closeDrawer || drawer.querySelector('a'))?.focus();
  else menu.focus();
}
menu?.addEventListener('click', () => setDrawerOpen(menu.getAttribute('aria-expanded') !== 'true'));
closeDrawer?.addEventListener('click', () => setDrawerOpen(false));
backdrop?.addEventListener('click', () => setDrawerOpen(false));
drawer?.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', () => setDrawerOpen(false)));
document.addEventListener('keydown', event => {
  if (!document.body.classList.contains('nav-open')) return;
  if (event.key === 'Escape') { event.preventDefault(); setDrawerOpen(false); return; }
  if (event.key === 'Tab') {
    const focusable = [...drawer.querySelectorAll('a[href],button:not([disabled])')].filter(item => !item.hidden);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});
window.matchMedia('(min-width: 761px)').addEventListener?.('change', () => {
  if (document.body.classList.contains('nav-open')) setDrawerOpen(false);
  drawer && (drawer.inert = false);
});
const offline = document.querySelector('#offline');
function updateConnection() { if (offline) offline.hidden = navigator.onLine; }
window.addEventListener('online', updateConnection);
window.addEventListener('offline', updateConnection);
updateConnection();
document.querySelectorAll('form').forEach(form => {
  form.addEventListener('submit', event => {
    const confirmation = form.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) event.preventDefault();
  });
  form.addEventListener('submit', event => {
    if (event.defaultPrevented || !form.checkValidity()) return;
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

const notificationBell = document.querySelector('.notification-bell');
const toast = document.querySelector('[data-notification-toast]');
let notificationCursor = document.body.dataset.notificationCursor || '';
function updateUnreadCount(count) {
  if (!notificationBell) return;
  notificationBell.dataset.unreadCount = String(count);
  notificationBell.setAttribute('aria-label', count ? `Notifications, ${count} unread` : 'Notifications');
  let badge = notificationBell.querySelector('.unread-count');
  if (count && !badge) { badge = document.createElement('span'); badge.className = 'unread-count'; notificationBell.append(badge); }
  if (badge) { badge.textContent = count > 99 ? '99+' : String(count); badge.hidden = !count; }
}
function playAlert() {
  if (localStorage.getItem('dpot-mute-alerts') === '1' || (navigator.userActivation && !navigator.userActivation.hasBeenActive)) return;
  try {
    const audio = new AudioContext();
    const tone = audio.createOscillator(), volume = audio.createGain();
    tone.frequency.value = 660; volume.gain.value = 0.035;
    tone.connect(volume); volume.connect(audio.destination);
    tone.start(); volume.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.13);
    tone.stop(audio.currentTime + 0.13); tone.onended = () => audio.close();
  } catch { /* The browser may block sound until a user gesture. */ }
}
async function pollNotificationState() {
  if (!notificationBell || document.hidden) return;
  try {
    const suffix = notificationCursor ? `?since=${encodeURIComponent(notificationCursor)}` : '';
    const response = await fetch(`/notifications/state/${suffix}`, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) return;
    const state = await response.json();
    updateUnreadCount(state.unread);
    for (const item of state.items || []) {
      if (toast) {
        toast.replaceChildren();
        const title = document.createElement('strong'), body = document.createElement('span');
        const link = document.createElement('a');
        title.textContent = item.title; body.textContent = item.message;
        link.textContent = 'View notifications'; link.href = `${notificationBell.href}#notification-${encodeURIComponent(item.id)}`;
        toast.append(title, body, link); toast.hidden = false;
        window.setTimeout(() => { toast.hidden = true; }, 7000);
      }
      playAlert();
    }
    if (state.latest) notificationCursor = state.latest;
  } catch { /* In-app notification refresh is best-effort. */ }
}
window.setInterval(pollNotificationState, 30000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollNotificationState(); });

const csrfToken = () => document.querySelector('input[name="csrfmiddlewaretoken"]')?.value ||
  document.cookie.split('; ').find(value => value.startsWith('csrftoken='))?.split('=').slice(1).join('=') || '';
const pushButton = document.querySelector('[data-push-enable]');
const pushStatus = document.querySelector('[data-push-status]');
if (pushButton && pushStatus) {
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
  const installed = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    pushButton.disabled = true; pushStatus.textContent = 'This browser does not support Web Push.';
  } else if (ios && !installed) {
    pushButton.disabled = true;
    pushStatus.textContent = 'On iPhone or iPad, add this site to your Home Screen first (iOS/iPadOS 16.4 or later).';
  } else if (Notification.permission === 'denied') {
    pushButton.disabled = true; pushStatus.textContent = 'Notifications are blocked in browser settings. Change that setting to enable them.';
  } else {
    if (Notification.permission === 'granted') pushStatus.textContent = 'Browser permission is enabled. This device may receive delivery updates.';
    fetch('/notifications/push/subscription/', {credentials: 'same-origin'}).then(response => response.ok ? response.json() : null)
      .then(state => {
        if (state?.enabled) { pushButton.textContent = 'Notifications enabled'; pushStatus.textContent = 'This browser is subscribed to delivery updates.'; }
        else if (Notification.permission === 'granted') pushStatus.textContent = 'Browser permission is granted. Turn on notifications to register this device.';
        else pushStatus.textContent = 'Optional: turn on notifications to receive delivery updates when the app is closed.';
      })
      .catch(() => {});
  }
  pushButton.addEventListener('click', async () => {
    const permission = Notification.permission === 'default' ? Notification.requestPermission() : Promise.resolve(Notification.permission);
    pushButton.disabled = true; pushStatus.textContent = 'Setting up notifications…';
    try {
      if (await permission !== 'granted') { pushStatus.textContent = 'Permission was not granted. You can change this in browser settings.'; return; }
      const registration = await navigator.serviceWorker.ready;
      const key = pushButton.dataset.publicKey.replace(/-/g, '+').replace(/_/g, '/');
      const padded = key + '='.repeat((4 - key.length % 4) % 4);
      const applicationServerKey = Uint8Array.from(atob(padded), character => character.charCodeAt(0));
      const subscription = await registration.pushManager.getSubscription() ||
        await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey});
      const response = await fetch('/notifications/push/subscription/', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
        body: JSON.stringify(subscription.toJSON()),
      });
      if (!response.ok) throw new Error('The subscription could not be saved.');
      pushButton.textContent = 'Notifications enabled'; pushStatus.textContent = 'This browser is subscribed to delivery updates.';
    } catch (error) { pushStatus.textContent = error.message || 'Notifications could not be enabled. Try again later.'; }
    finally { pushButton.disabled = false; }
  });
}

const soundToggle = document.querySelector('[data-sound-toggle]');
if (soundToggle) {
  const updateSoundToggle = () => {
    const muted = localStorage.getItem('dpot-mute-alerts') === '1';
    soundToggle.setAttribute('aria-pressed', String(muted));
    soundToggle.textContent = muted ? 'Turn on in-app alert sound' : 'Mute in-app alert sound';
  };
  updateSoundToggle();
  soundToggle.addEventListener('click', () => {
    localStorage.setItem('dpot-mute-alerts', soundToggle.getAttribute('aria-pressed') !== 'true' ? '1' : '0');
    updateSoundToggle();
  });
}

document.querySelectorAll('[data-logout]').forEach(form => form.addEventListener('submit', async event => {
  if (!('serviceWorker' in navigator)) return;
  event.preventDefault();
  try {
    const registration = await navigator.serviceWorker.getRegistration('/');
    const subscription = await registration.pushManager?.getSubscription();
    if (subscription) {
      await Promise.race([
        fetch('/notifications/push/subscription/', {
          method: 'DELETE', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken()},
          body: JSON.stringify({endpoint: subscription.endpoint}),
        }),
        new Promise(resolve => window.setTimeout(resolve, 700)),
      ]);
      await subscription.unsubscribe();
    }
  } catch { /* Logout must proceed even if device unsubscribe fails. */ }
  form.submit();
}));


document.querySelectorAll('[data-mobile-filter]').forEach(panel => {
  if (window.matchMedia('(max-width: 760px)').matches && panel.dataset.active !== 'true') {
    panel.removeAttribute('open');
  }
});


document.querySelectorAll('[data-booking-form]').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.dataset.submitting === 'true') { event.preventDefault(); return; }
    form.dataset.submitting = 'true';
    document.querySelectorAll(`button[form="${form.id}"], button[type="submit"]`).forEach(button => {
      if (!form.contains(button) && button.getAttribute('form') !== form.id) return;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.textContent = 'Preparing quote...';
    });
  });
});
