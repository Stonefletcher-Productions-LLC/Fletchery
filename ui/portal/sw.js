// Fletchery portal service worker: keeps the page itself available when the PC cannot be reached (so it can say so),
// and shows notifications. Status is never cached: what you see is always what the PC just said.
const CACHE = 'fletchery-portal-v1';
const SHELL = ['/', '/portal.css', '/portal.js', '/manifest.webmanifest', '/icon-180.png', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()).catch(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys()
    .then(names => Promise.all(names.filter(n => n !== CACHE).map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  const key = url.pathname === '/index.html' ? '/' : url.pathname;
  if (!SHELL.includes(key)) return;
  // Newest first; the saved copy only when the PC cannot be reached. A refusal (403) is shown, never papered over.
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(key, copy));
    }
    return response;
  }).catch(() => caches.match(key).then(hit => hit || Response.error())));
});

self.addEventListener('push', event => {
  let note = {};
  try { note = event.data ? event.data.json() : {}; } catch (e) { note = {}; }
  const title = typeof note.title === 'string' && note.title ? note.title : 'Fletchery';
  const tab = ['now', 'tasks', 'merges', 'builds'].includes(note.tab) ? note.tab : 'now';
  event.waitUntil(self.registration.showNotification(title, {
    body: typeof note.body === 'string' ? note.body : '',
    tag: typeof note.tag === 'string' && note.tag ? note.tag : undefined,
    icon: '/icon-192.png', badge: '/icon-192.png', data: {tab},
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const tab = (event.notification.data && event.notification.data.tab) || 'now';
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const client of list) {
      if ('focus' in client) {
        client.postMessage({tab});
        return client.focus();
      }
    }
    return self.clients.openWindow('/#' + tab);
  }));
});
