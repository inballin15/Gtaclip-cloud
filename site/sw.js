// Clip Studio app: opens instantly (the app itself is kept on the phone) and always shows fresh clips.
// Videos are never touched here (they need byte-range answers straight from the server).
const V = 'gc-cliphq-3';
const SHELL = ['/app', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/apple-touch-icon.png',
  '/vendor/render.mjs?v=9', '/vendor/mp4-muxer.min.mjs', '/vendor/Inter-Bold.otf', '/vendor/mp4box.all.js'];
self.addEventListener('install', (e) => { e.waitUntil(caches.open(V).then((c) => c.addAll(SHELL)).catch(() => {})); self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== V).map((k) => caches.delete(k))))
  .then(() => self.clients.claim())); });
self.addEventListener('fetch', (e) => {
  const req = e.request; const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || req.headers.has('range') || url.pathname.startsWith('/v/')) return;
  const isPage = req.mode === 'navigate' || /\.html$|^\/app$|^\/$/.test(url.pathname);
  const isData = /\.json$/.test(url.pathname);
  if (isPage || isData) {   // newest first; the saved copy only when there's no signal
    e.respondWith(fetch(req).then((res) => { if (res.ok && isPage) { const cp = res.clone(); caches.open(V).then((c) => c.put('/app', cp)); } return res; })
      .catch(() => caches.match(isPage ? '/app' : req)));
    return;
  }
  if (/^\/(vendor|icons|fonts)\//.test(url.pathname)) {   // never change for a version: saved copy first
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => { if (res.ok) { const cp = res.clone(); caches.open(V).then((c) => c.put(req, cp)); } return res; })));
  }
});
