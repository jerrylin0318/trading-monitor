const CACHE_NAME = 'trademon-v32';
const ASSETS = ['/', '/static/index.html', '/static/style.css', '/static/app.js', '/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Skip API and WebSocket requests entirely (don't cache)
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) {
    return;  // Let browser handle normally
  }
  
  // Network first, fallback to cache for static assets
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
