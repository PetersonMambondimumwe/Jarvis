// JARVIS PWA Service Worker — Always-On Wake & Window Focus Handler
const CACHE_NAME = 'jarvis-pwa-v13';
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

async function focusOrLaunchPWA(commandText = '') {
  const clientList = await self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true,
  });

  for (const client of clientList) {
    try {
      if ('focus' in client) {
        await client.focus();
      }
      client.postMessage({
        type: 'ACTIVATE_VOICE',
        command: commandText || '',
      });
      return true;
    } catch (_) {}
  }

  if (self.clients.openWindow) {
    try {
      const target = commandText
        ? `/?voice=1&cmd=${encodeURIComponent(commandText)}`
        : '/?voice=1';
      await self.clients.openWindow(target);
      return true;
    } catch (_) {}
  }
  return false;
}

self.addEventListener('message', (e) => {
  const data = e.data || {};
  if (data.type === 'WAKE_PWA') {
    e.waitUntil((async () => {
      const focused = await focusOrLaunchPWA(data.command || '');
      if (data.notify && self.registration && self.registration.showNotification) {
        try {
          await self.registration.showNotification('J.A.R.V.I.S. // WAKE OVERRIDE', {
            body: data.command
              ? `Executing: "${data.command}"`
              : 'Combat Core Online — Listening for directive',
            icon: '/static/icon-192.png',
            badge: '/static/icon-192.png',
            tag: 'jarvis-wake',
            renotify: true,
            data: { command: data.command || '' },
          });
        } catch (_) {}
      }
      return focused;
    })());
  }
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const cmd = (e.notification.data && e.notification.data.command) || '';
  e.waitUntil(focusOrLaunchPWA(cmd));
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/ws') || url.pathname.startsWith('/api')) return;

  e.respondWith(
    fetch(e.request).catch(async () => {
      const cached = await caches.match(e.request);
      if (cached) return cached;
      if (e.request.mode === 'navigate') {
        const fallback = await caches.match('/');
        if (fallback) return fallback;
      }
      return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
    })
  );
});
