const CACHE_NAME = "flashshare-pwa-v61";
const CORE_ASSETS = ["/offline", "/site.webmanifest", "/static/images/og-image.png", "/static/images/favicon.ico", "/static/images/apple-touch-icon.png"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(CORE_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== location.origin || request.method !== "GET") return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) return;

  // Navigation: network-first, only fallback to cache when offline.
  // Do NOT cache HTML responses — CSS/JS are inlined and must always be fresh.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(request).then(cached => cached || caches.match("/offline"))));
    return;
  }

  // Static assets only: cache-first
  if (["/static/images/og-image.png", "/static/images/favicon.ico", "/static/images/apple-touch-icon.png", "/static/images/icon-192.png", "/static/images/icon-512.png", "/site.webmanifest"].includes(url.pathname)) {
    event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => {
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
      return response;
    })));
  }
});
