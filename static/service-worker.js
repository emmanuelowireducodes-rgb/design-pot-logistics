/* Never persist authenticated pages, APIs, maps, financial data or write requests. */
const CACHE = 'design-pot-static-v3-2';
const ASSETS = __ASSETS__;
const ICON = __ICON__;
const OFFLINE = '/offline/';
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(async cache => {
    for (const url of [...ASSETS, OFFLINE]) {
      const response = await fetch(url, {credentials: 'omit', cache: 'reload'});
      if (!response.ok || response.redirected) throw new Error('Offline assets unavailable');
      await cache.put(url, response);
    }
  }));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('design-pot-static-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));
    return;
  }
  if (ASSETS.includes(url.pathname) && !url.search) {
    event.respondWith(caches.match(url.pathname).then(saved => saved || fetch(request)));
  }
});
self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = {}; }
  let safeUrl = '/';
  try {
    const url = new URL(typeof data.url === 'string' ? data.url : '/', self.location.origin);
    if (url.origin === self.location.origin && /^\/notifications\/[0-9a-f-]{36}\/open\/$/i.test(url.pathname) && !url.search && !url.hash) {
      safeUrl = url.pathname;
    }
  } catch { /* Invalid notification navigation falls back to the app root. */ }
  event.waitUntil(Promise.all([
    syncUnreadBadge(),
    self.registration.showNotification('Design-Pot Logistics', {
      body: 'You have a new update. Open the app to view it.',
      icon: ICON,
      tag: typeof data.tag === 'string' ? data.tag.slice(0, 100) : 'design-pot-update',
      data: {url: safeUrl},
      renotify: false,
    }),
  ]));
});

async function syncUnreadBadge() {
  try {
    if (!('setAppBadge' in self.navigator) && !('clearAppBadge' in self.navigator)) return;
    const response = await fetch('/notifications/state/', {credentials: 'include', cache: 'no-store'});
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) return;
    const state = await response.json();
    if (!Number.isSafeInteger(state.unread) || state.unread < 0) return;
    if (state.unread > 0 && 'setAppBadge' in self.navigator) await self.navigator.setAppBadge(state.unread);
    else if (state.unread === 0 && 'clearAppBadge' in self.navigator) await self.navigator.clearAppBadge();
  } catch { /* Background badge refresh is best-effort and never blocks the alert. */ }
}

self.addEventListener('notificationclick', event => {
  event.notification.close();
  let url = self.location.origin + '/';
  try {
    const candidate = new URL(event.notification.data?.url || '/', self.location.origin);
    if (candidate.origin === self.location.origin && /^\/notifications\/[0-9a-f-]{36}\/open\/$/i.test(candidate.pathname) && !candidate.search && !candidate.hash) url = candidate.href;
  } catch { /* Stale or malformed system notification uses the safe app root. */ }
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async clients => {
    const existing = clients.find(client => new URL(client.url).origin === self.location.origin);
    if (existing) {
      if (existing.navigate) await existing.navigate(url);
      return existing.focus();
    }
    return self.clients.openWindow(url);
  }));
});
