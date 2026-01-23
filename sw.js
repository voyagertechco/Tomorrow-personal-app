// sw.js — Tomorrow PWA (enhanced)
const CACHE_VERSION = 'v1';
const CACHE_NAME = `tomorrow-shell-${CACHE_VERSION}`;
const ASSETS_TO_CACHE = [
  '/',                // root (index.html)
  '/index.html',
  '/offline.html',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
  // add other core static assets here (styles, critical JS, images)
];

// safe add all (skips missing assets)
async function safeCacheAddAll(cache, assets){
  for(const url of assets){
    try{
      // use no-store for fresh fetch; rely on cache.put to save
      const resp = await fetch(url, { cache: 'no-store' });
      if(resp && (resp.ok || resp.type === 'opaque')) await cache.put(url, resp.clone());
    }catch(err){
      console.warn('[sw] skip cache', url, err && err.message);
    }
  }
}

// limit cache entries helper
async function trimCache(cacheName, maxEntries = 100){
  try{
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    if(keys.length <= maxEntries) return;
    const remove = keys.slice(0, keys.length - maxEntries);
    await Promise.all(remove.map(r => cache.delete(r)));
  }catch(e){
    console.warn('[sw] trimCache err', e);
  }
}

// install: cache shell + offline page
self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await safeCacheAddAll(cache, ASSETS_TO_CACHE);
    await self.skipWaiting();
  })());
});

// activate: clean old caches and enable navigation preload
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // delete old caches not matching current name
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)));
    // enable navigation preload for network-first navigations
    if(self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch(e) {}
    }
    await self.clients.claim();
  })());
});

// fetch handler
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if(req.method !== 'GET') return;
  // ignore devtools / extensions
  if(req.url.startsWith('chrome-extension://')) return;

  // ---------- NAVIGATION: network-first with navigation preload ----------
  if(req.mode === 'navigate'){
    event.respondWith((async () => {
      // Try navigation preload response first (if available)
      try {
        const preloadResponse = await event.preloadResponse;
        if(preloadResponse) {
          // update index cache and return preload
          try {
            const cache = await caches.open(CACHE_NAME);
            await cache.put('/', preloadResponse.clone());
          } catch (e) {}
          return preloadResponse;
        }

        // network fetch
        const networkResponse = await fetch(req);
        // update cached index for offline fallback
        try {
          const cache = await caches.open(CACHE_NAME);
          if(networkResponse && (networkResponse.ok || networkResponse.type === 'opaque')){
            await cache.put('/', networkResponse.clone());
          }
        } catch(e){}
        return networkResponse;
      } catch (err) {
        // network failed -> fallback to cached index or offline page
        const cache = await caches.open(CACHE_NAME);
        const fallback = await cache.match('/') || await cache.match('/index.html') || await cache.match('/offline.html');
        if(fallback) return fallback;
        return new Response('<h1>Offline</h1><p>Unable to reach network and no cached content.</p>', { headers:{ 'Content-Type':'text/html' }, status: 503 });
      }
    })());
    return;
  }

  // ---------- OTHER GETs: cache-first with background revalidation ----------
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(req);
    if(cached){
      // background revalidate
      event.waitUntil((async () => {
        try{
          const fresh = await fetch(req);
          if(fresh && (fresh.ok || fresh.type === 'opaque')) await cache.put(req, fresh.clone());
          // optional: trim cache if needed
          await trimCache(CACHE_NAME, 200);
        }catch(e){}
      })());
      return cached;
    }

    // if not cached, attempt network fetch, then cache if ok
    try{
      const networkResponse = await fetch(req);
      if(networkResponse && (networkResponse.ok || networkResponse.type === 'opaque')){
        try{ await cache.put(req, networkResponse.clone()); }catch(e){}
      }
      return networkResponse;
    }catch(err){
      // fallback for images
      if(req.destination === 'image'){
        const fallback = await cache.match('/icons/icon-192.png');
        if(fallback) return fallback;
      }
      const fallbackIndex = await cache.match('/') || await cache.match('/index.html') || await cache.match('/offline.html');
      if(fallbackIndex) return fallbackIndex;
      return new Response('Offline', { status: 503, statusText: 'Offline' });
    }
  })());
});

// message handler: DOWNLOAD_OFFLINE, CLEAR_CACHES
self.addEventListener('message', (event) => {
  const data = event.data;
  if(!data) return;
  if(data.type === 'DOWNLOAD_OFFLINE'){
    event.waitUntil((async () => {
      const cache = await caches.open(CACHE_NAME);
      // you could extend ASSETS_TO_CACHE here before adding
      await safeCacheAddAll(cache, ASSETS_TO_CACHE);
    })());
  } else if(data.type === 'CLEAR_CACHES'){
    event.waitUntil((async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
    })());
  }
});

// notification click forwarding + focus
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async ()=>{
    const all = await clients.matchAll({ includeUncontrolled:true });
    if(all.length > 0){
      all[0].focus();
      try { all[0].postMessage({ type:'NOTIFICATION_CLICK', data: event.notification }); } catch(e){}
    } else {
      clients.openWindow('/');
    }
  })());
});
