// sw.js — Tomorrow PWA (improved, universal HTML caching + update-friendly)
const CACHE_VERSION = 'v4'; // bump on deploy or when assets change
const SHELL = `tomorrow-shell-${CACHE_VERSION}`;
const RUNTIME = `tomorrow-runtime-${CACHE_VERSION}`;

const ASSETS_TO_CACHE = [
  '/', '/index.html', '/offline.html', '/manifest.json',
  '/icons/icon-192.png', '/icons/icon-512.png', '/icons/maskable-icon-512.png',
  // common app assets (add your real hashed filenames in production)
  '/styles.css', '/app.js',
  // optional "warm" pages (these will be pre-cached at install)
  '/finance.html','/goals.html','/habits.html','/journal.html',
  '/task_reminders.html','/wishlist_health.html','/menustral_tracking.html','/export.html'
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
  } catch (e) {
    console.warn('[sw] trimCache err', e);
  }
}

self.addEventListener('install', (event) => {
  console.log('[sw] install');
  event.waitUntil((async () => {
    const c = await caches.open(SHELL);
    await safeCacheAddAll(c, ASSETS_TO_CACHE);
    await self.skipWaiting(); // activate faster
    console.log('[sw] install done');
  })());
});

self.addEventListener('activate', (event) => {
  console.log('[sw] activate');
  event.waitUntil((async () => {
    // delete old caches not matching current names
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => (k !== SHELL && k !== RUNTIME)).map(k => caches.delete(k)));

    // enable navigation preload if available
    if (self.registration && self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) { console.warn('[sw] navpreload enable failed', e); }
    }

    await self.clients.claim();

    // notify clients that a new SW is active (useful for update UX)
    const clientsList = await self.clients.matchAll({ includeUncontrolled: true });
    clientsList.forEach(c => {
      try { c.postMessage({ type: 'SW_ACTIVATED', version: CACHE_VERSION }); } catch (e) { console.warn(e); }
    });
    console.log('[sw] activate completed');
  })());
});

async function cacheAndReturn(req, resp) {
  try {
    const cache = await caches.open(RUNTIME);
    await cache.put(req, resp.clone());
    await trimCache(RUNTIME, 1000);
  } catch (e) { /* ignore */ }
  return resp;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (req.url.startsWith('chrome-extension://') || req.url.startsWith('data:')) return;

  const acceptHeader = req.headers.get('accept') || '';
  const isNavigation = req.mode === 'navigate' || acceptHeader.includes('text/html');

  if (isNavigation) {
    // network-first for navigations: prefer network, fallback to cache
    event.respondWith((async () => {
      try {
        // try navigation preload first (fast)
        const preload = await event.preloadResponse;
        if (preload) {
          try {
            const cache = await caches.open(SHELL);
            // cache the specific navigation URL (so any HTML page is cached)
            await cache.put(req, preload.clone());
          } catch (e) {}
          return preload;
        }

        // fetch from network
        const networkResp = await fetch(req);
        // if success, cache the actual requested HTML page (not just '/')
        try {
          const cache = await caches.open(SHELL);
          if (networkResp && (networkResp.ok || networkResp.type === 'opaque')) {
            await cache.put(req, networkResp.clone());
          }
        } catch (e) {}
        return networkResp;
      } catch (err) {
        // offline fallback -> try the exact cached URL, then index, then offline.html
        const cache = await caches.open(SHELL);
        const fallback = await cache.match(req) || await cache.match('/index.html') || await cache.match('/offline.html');
        if (fallback) return fallback;
        return new Response('<h1>Offline</h1><p>Unable to reach network and no cached content.</p>', { headers: { 'Content-Type': 'text/html' }, status: 503 });
      }
    })());
    return;
  }

  // non-navigation requests -> cache-first with background revalidate
  event.respondWith((async () => {
    const cache = await caches.open(SHELL);
    const cached = await cache.match(req);
    if (cached) {
      // revalidate in background
      event.waitUntil((async () => {
        try {
          const fresh = await fetch(req);
          if (fresh && (fresh.ok || fresh.type === 'opaque')) {
            await cache.put(req, fresh.clone());
            await trimCache(SHELL, 1000);
            // notify clients that a cached asset was refreshed (optional)
            const all = await clients.matchAll({ includeUncontrolled: true });
            all.forEach(c => c.postMessage({ type: 'ASSET_REFRESHED', url: req.url }));
          }
        } catch (e) { /* ignore */ }
      })());
      return cached;
    }

    try {
      const networkResponse = await fetch(req);
      if (networkResponse && (networkResponse.ok || networkResponse.type === 'opaque')) {
        try { await cache.put(req, networkResponse.clone()); } catch (e) {}
      }
      return networkResponse;
    } catch (err) {
      // image fallback
      if (req.destination === 'image') {
        const iconFallback = await cache.match('/icons/icon-192.png') || cache.match('/icons/icon-512.png');
        if (iconFallback) return iconFallback;
      }
      const fallbackIndex = await cache.match('/') || await cache.match('/index.html') || await cache.match('/offline.html');
      if (fallbackIndex) return fallbackIndex;
      return new Response('Offline', { status: 503, statusText: 'Offline' });
    }
  })());
});

// message handler — client can request offline download, cache clear, or explicit update check
self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (!data || !data.type) return;
  console.log('[sw] message', data.type);
  if (data.type === 'DOWNLOAD_OFFLINE') {
    event.waitUntil((async () => {
      const cache = await caches.open(SHELL);
      await safeCacheAddAll(cache, ASSETS_TO_CACHE);
      console.log('[sw] DOWNLOAD_OFFLINE completed');
      const all = await clients.matchAll({ includeUncontrolled: true });
      all.forEach(c => c.postMessage({ type: 'DOWNLOAD_OFFLINE_DONE' }));
    })());
  } else if (data.type === 'CLEAR_CACHES') {
    event.waitUntil((async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
      console.log('[sw] all caches cleared');
      const all = await clients.matchAll({ includeUncontrolled: true });
      all.forEach(c => c.postMessage({ type: 'CLEAR_CACHES_DONE' }));
    })());
  } else if (data.type === 'CHECK_FOR_UPDATES') {
    // attempt to fetch and replace known shell resources — used by client to force a check
    event.waitUntil((async () => {
      try {
        const cache = await caches.open(SHELL);
        for (const url of ASSETS_TO_CACHE) {
          try {
            const r = await fetch(url, { cache: 'no-store' });
            if (r && (r.ok || r.type === 'opaque')) await cache.put(url, r.clone());
          } catch (e) {}
        }
        // notify clients of update (they can decide to reload)
        const all = await clients.matchAll({ includeUncontrolled: true });
        all.forEach(c => c.postMessage({ type: 'SW_UPDATED', version: CACHE_VERSION }));
      } catch (e) {
        console.warn('[sw] CHECK_FOR_UPDATES failed', e);
      }
    })());
  }
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const all = await clients.matchAll({ includeUncontrolled: true });
    if (all.length > 0) {
      all[0].focus();
      try { all[0].postMessage({ type: 'NOTIFICATION_CLICK', data: event.notification }); } catch (e) {}
    } else {
      clients.openWindow('/');
    }
  })());
});
