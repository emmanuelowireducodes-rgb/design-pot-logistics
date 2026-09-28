/* Never persist authenticated pages, APIs, maps, financial data or write requests. */
const CACHE = 'design-pot-static-v3-1';
const ASSETS = __ASSETS__;
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
