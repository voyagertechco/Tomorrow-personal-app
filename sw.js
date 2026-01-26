// sw.js — Tomorrow PWA (navigation-safe + warmed HTMLs)
const CACHE_VERSION = 'v4';
const SHELL = `tomorrow-shell-${CACHE_VERSION}`;
const RUNTIME = `tomorrow-runtime-${CACHE_VERSION}`;

const ASSETS_TO_CACHE = [
  '/', '/index.html', '/offline.html', '/manifest.json',
  '/icons/icon-192.png', '/icons/icon-512.png', '/icons/maskable-icon-512.png',
  // static app assets (replace with your real filenames)
  '/styles.css', '/app.js',
  // warm all app pages so hub-card clicks work offline
  '/finance.html','/goals.html','/habits.html','/journal.html',
  '/task_reminders.html','/wishlist_health.html','/menustral_tracking.html',
  '/export.html','/more.html'
];

async function safeCacheAddAll(cache, assets) {
  for (const url of assets) {
    try {
      const req = new Request(url, { cache: 'no-store', credentials: 'same-origin' });
      const resp = await fetch(req);
      if (resp && (resp.ok || resp.type === 'opaque')) {
        await cache.put(req, resp.clone());
        console.log('[sw] cached', url);
      } else {
        console.warn('[sw] skipping (not ok):', url, resp && resp.status);
      }
    } catch (err) {
      console.warn('[sw] skip cache (fetch failed)', url, err && err.message);
    }
  }
}

async function trimCache(cacheName, maxEntries = 500) {
  try {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    if (keys.length <= maxEntries) return;
    const remove = keys.slice(0, keys.length - maxEntries);
    await Promise.all(remove.map(r => cache.delete(r)));
  } catch (e) { console.warn('[sw] trimCache err', e); }
}

self.addEventListener('install', (evt) => {
  evt.waitUntil((async () => {
    const c = await caches.open(SHELL);
    await safeCacheAddAll(c, ASSETS_TO_CACHE);
    await self.skipWaiting();
    console.log('[sw] installed');
  })());
});

self.addEventListener('activate', (evt) => {
  evt.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => (k !== SHELL && k !== RUNTIME)).map(k => caches.delete(k)));
    if (self.registration && self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) { console.warn('[sw] navpreload failed', e); }
    }
    await self.clients.claim();
    const clientsList = await self.clients.matchAll({ includeUncontrolled: true });
    clientsList.forEach(c => { try { c.postMessage({ type: 'SW_ACTIVATED', version: CACHE_VERSION }); } catch(e){} });
    console.log('[sw] activated');
  })());
});

// helper: try to respond from cache first for a matching request
async function tryCacheMatch(req) {
  try {
    const cache = await caches.open(SHELL);
    const match = await cache.match(req);
    if (match) return match;
    // try matching by pathname only (some navs include search/hash)
    const u = new URL(req.url);
    const path = u.pathname;
    const byPath = await cache.match(path);
    if (byPath) return byPath;
    return null;
  } catch (e) { return null; }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (req.url.startsWith('chrome-extension://') || req.url.startsWith('data:')) return;

  const accept = req.headers.get('accept') || '';
  const isNav = req.mode === 'navigate' || accept.includes('text/html');

  if (isNav) {
    // network-first navigation, but fall back to exact cached page (req) before index/offline
    event.respondWith((async () => {
      // prefer navigation preload response when available
      const preload = await event.preloadResponse;
      if (preload) {
        try { const cache = await caches.open(SHELL); await cache.put(req, preload.clone()); } catch(e){}
        return preload;
      }
      try {
        const networkResp = await fetch(req);
        // if network returned an HTML page, cache it (so next offline nav to same URL works)
        try {
          if (networkResp && (networkResp.ok || networkResp.type === 'opaque')) {
            const cache = await caches.open(SHELL);
            // store exact request so clicking hub -> /journal.html is available offline
            await cache.put(req, networkResp.clone());
          }
        } catch (e) { /* ignore cache put errors */ }
        return networkResp;
      } catch (err) {
        // network failed — try exact cached page first
        const exact = await tryCacheMatch(req);
        if (exact) return exact;
        // then fall back to index or offline page
        try {
          const cache = await caches.open(SHELL);
          const fallback = await cache.match('/index.html') || await cache.match('/') || await cache.match('/offline.html');
          if (fallback) return fallback;
        } catch (e) { /* ignore */ }
        return new Response('<h1>Offline</h1><p>Unable to reach network and no cached content.</p>', { headers:{ 'Content-Type':'text/html' }, status:503 });
      }
    })());
    return;
  }

  // Non-navigation: cache-first then network with background revalidate
  event.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(req);
    if (cached) {
      // background refresh
      event.waitUntil((async () => {
        try {
          const fresh = await fetch(req);
          if (fresh && (fresh.ok || fresh.type === 'opaque')) {
            await cache.put(req, fresh.clone());
            await trimCache(SHELL, 1000);
            const all = await clients.matchAll({ includeUncontrolled: true });
            all.forEach(c => c.postMessage({ type: 'ASSET_REFRESHED', url: req.url }));
          }
        } catch (e) { /* ignore */ }
      })());
      return cached;
    }
    try {
      const net = await fetch(req);
      if (net && (net.ok || net.type === 'opaque')) {
        try { await cache.put(req, net.clone()); } catch (e) {}
      }
      return net;
    } catch (e) {
      // image fallback
      if (req.destination === 'image') {
        const icon = await cache.match('/icons/icon-192.png') || await cache.match('/icons/icon-512.png');
        if (icon) return icon;
      }
      const fallback = await cache.match('/offline.html') || await cache.match('/index.html') || await cache.match('/');
      if (fallback) return fallback;
      return new Response('Offline', { status:503, statusText:'Offline' });
    }
  })());
});

self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (!data || !data.type) return;
  if (data.type === 'DOWNLOAD_OFFLINE') {
    event.waitUntil((async () => {
      const cache = await caches.open(SHELL);
      await safeCacheAddAll(cache, ASSETS_TO_CACHE);
      const all = await clients.matchAll({ includeUncontrolled: true });
      all.forEach(c => c.postMessage({ type: 'DOWNLOAD_OFFLINE_DONE' }));
    })());
  } else if (data.type === 'CLEAR_CACHES') {
    event.waitUntil((async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
      const all = await clients.matchAll({ includeUncontrolled: true });
      all.forEach(c => c.postMessage({ type: 'CLEAR_CACHES_DONE' }));
    })());
  } else if (data.type === 'CHECK_FOR_UPDATES') {
    event.waitUntil((async () => {
      try {
        const cache = await caches.open(SHELL);
        for (const url of ASSETS_TO_CACHE) {
          try {
            const r = await fetch(url, { cache: 'no-store' });
            if (r && (r.ok || r.type === 'opaque')) await cache.put(url, r.clone());
          } catch (e) {}
        }
        const all = await clients.matchAll({ includeUncontrolled: true });
        all.forEach(c => c.postMessage({ type: 'SW_UPDATED', version: CACHE_VERSION }));
      } catch (e) { console.warn('[sw] CHECK_FOR_UPDATES failed', e); }
    })());
  }
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const all = await clients.matchAll({ includeUncontrolled: true });
    if (all.length > 0) {
      all[0].focus();
      try { all[0].postMessage({ type: 'NOTIFICATION_CLICK', data: event.notification }); } catch(e){}
    } else {
      clients.openWindow('/');
    }
  })());
});
