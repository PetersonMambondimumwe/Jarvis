// JARVIS PWA Service Worker
const CACHE_NAME = 'jarvis-pwa-v3';
const ASSETS = [
  '/',
  '/login',
  '/static/crypto.js',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/manifest.json'
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)).catch(() => {})
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/ws') || url.pathname.startsWith('/api')) return;

  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
