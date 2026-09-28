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
  const url = new URL(typeof data.url === 'string' ? data.url : '/', self.location.origin);
  const safeUrl = url.origin === self.location.origin && /^\/(admin|client|field)\//.test(url.pathname)
    ? `${url.pathname}${url.search}${url.hash}` : '/';
  event.waitUntil(self.registration.showNotification('Design-Pot Logistics', {
    body: 'You have a new update. Open the app to view it.',
    icon: ICON,
    badge: ICON,
    tag: typeof data.tag === 'string' ? data.tag.slice(0, 100) : 'design-pot-update',
    data: {url: safeUrl},
    renotify: false,
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const candidate = new URL(event.notification.data?.url || '/', self.location.origin);
  const url = candidate.origin === self.location.origin && /^\/(admin|client|field)\//.test(candidate.pathname)
    ? candidate.href : self.location.origin + '/';
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async clients => {
    const existing = clients.find(client => new URL(client.url).origin === self.location.origin);
    if (existing) {
      await existing.navigate(url);
      return existing.focus();
    }
    return self.clients.openWindow(url);
  }));
});
