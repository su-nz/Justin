// 只快取外殼；桶子狀態一律走網路（過期狀態比沒有狀態更糟）
const CACHE = 'smartbin-v1';
const SHELL = ['/static/index.html', '/static/manifest.json', '/static/icon.svg'];
self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL))));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
