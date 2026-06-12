/* Shelf service worker (M1).
 *
 * SECURITY (auth is a cookie session, so a cache key has NO auth/user dimension):
 *   - NEVER cache access-gated or per-user responses: /api/* JSON (chapters, library, /auth/me),
 *     and never cache non-GET, 401/403, or redirects. Caching /api/chapter would leak one user's
 *     content on a shared device and serve stale-permission content after access is revoked.
 *   - CACHE-FIRST only for content-addressed, non-sensitive assets: the hashed /assets/* build
 *     output and content-addressed images (/media/*, /api/cover). These are keyed by their URL,
 *     which already encodes their identity (filename hash / content hash).
 *   - The app shell (navigation → index.html) is NETWORK-FIRST so a deploy serves fresh HTML (and
 *     thus fresh hashed asset refs); the cached shell is the offline fallback. skipWaiting +
 *     clients.claim so a new SW takes over immediately and old shells don't linger.
 *
 * Bump CACHE_VERSION when the SW logic changes; asset caches are self-versioning via hashed names.
 */
const CACHE_VERSION = "shelf-v1";
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const ASSET_CACHE = `${CACHE_VERSION}-assets`;
const IMAGE_CACHE = `${CACHE_VERSION}-images`;
const OURS = [SHELL_CACHE, ASSET_CACHE, IMAGE_CACHE];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  // Warm the shell so a first offline launch works.
  event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.add("/")).catch(() => {}));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => !OURS.includes(k)).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function networkFirst(request, cacheName) {
  return fetch(request)
    .then((res) => {
      if (res && res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(cacheName).then((c) => c.put(request, copy));
      }
      return res;
    })
    .catch(() => caches.match(request).then((m) => m || caches.match("/")));
}

function cacheFirst(request, cacheName) {
  return caches.match(request).then((hit) => {
    if (hit) return hit;
    return fetch(request).then((res) => {
      // Only cache a clean, same-origin 200 (never an opaque/redirect/error).
      if (res && res.ok && res.status === 200 && res.type === "basic") {
        const copy = res.clone();
        caches.open(cacheName).then((c) => c.put(request, copy));
      }
      return res;
    });
  });
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  // Only GET is ever cacheable; everything else (mutations) goes straight to the network.
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;  // cross-origin → let the browser handle it

  // App shell: SPA navigations resolve to index.html — network-first so a deploy isn't stale.
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request, SHELL_CACHE));
    return;
  }

  // Content-addressed images (safe to cache-first, keyed by URL): cover proxy + media pages.
  if (url.pathname.startsWith("/media/") || url.pathname.startsWith("/api/cover")) {
    event.respondWith(cacheFirst(request, IMAGE_CACHE));
    return;
  }

  // NEVER cache access-gated / per-user API or auth responses — always hit the network.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) return;

  // Hashed, immutable build output: cache-first.
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(cacheFirst(request, ASSET_CACHE));
    return;
  }

  // Static root files (manifest, icons): cache-first, harmless + offline-friendly.
  if (/\.(?:png|ico|webmanifest|svg)$/.test(url.pathname)) {
    event.respondWith(cacheFirst(request, ASSET_CACHE));
  }
  // Everything else: default browser handling (network).
});
