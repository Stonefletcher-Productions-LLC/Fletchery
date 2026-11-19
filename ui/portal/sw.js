// Fletchery portal service worker: opens the newest page from the PC when it answers within a few seconds, else the copy
// saved on the phone (Tailscale still connecting, PC asleep), and shows notifications. Network first since 2026-11-18:
// cache first kept phones on an old page for several opens after an update. Status is never saved here:
// the page asks the PC for it after it has opened.
const CACHE = 'fletchery-portal-v21';
const NET_MS = 4000;
const SHELL = ['/', '/portal.css', '/kit.js', '/portal.js', '/live.js', '/manifest.webmanifest', '/icon-180.png', '/icon-192.png', '/icon-512.png'];

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
  const fresh = fetch(event.request, {cache: 'no-cache'}).then(response => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(key, copy)).catch(() => {});
    }
    return response;
  });
  const saved = () => caches.match(key);
  // Newest first; the saved copy only when the PC is slow (NET_MS) or unreachable. A refusal (403) is shown as it is.
  event.respondWith(new Promise(resolve => {
    let done = false;
    const use = r => { if (!done && r) { done = true; resolve(r); } };
    const timer = setTimeout(() => saved().then(hit => use(hit)), NET_MS);
    fresh.then(r => { clearTimeout(timer); use(r); },
               () => { clearTimeout(timer); saved().then(hit => use(hit || Response.error())); });
  }));
});

self.addEventListener('push', event => {
  let note = {};
  try { note = event.data ? event.data.json() : {}; } catch (e) { note = {}; }
  const title = typeof note.title === 'string' && note.title ? note.title : 'Fletchery';
  const tab = ['live', 'queue', 'builds', 'activity'].includes(note.tab) ? note.tab : 'live';
  event.waitUntil(self.registration.showNotification(title, {
    body: typeof note.body === 'string' ? note.body : '',
    tag: typeof note.tag === 'string' && note.tag ? note.tag : undefined,
    icon: '/icon-192.png', badge: '/icon-192.png', data: {tab}, silent: note.quiet === true,
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const tab = (event.notification.data && event.notification.data.tab) || 'live';
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
