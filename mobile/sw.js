/* ============================================================================
 * Service worker: network-first with a cache fallback for everything.
 *
 * Network-first because this is a personal tool whose data (and occasionally
 * the app shell) changes daily: an online phone must always see the latest
 * build and the latest scan. The cache exists so the last thing you saw is
 * still readable on the MRT with no signal.
 *
 * F20 -- the bug this file is rewritten for. The app used to request
 * `scan_result.json?t=<Date.now()>`, and the worker cached the request object
 * verbatim. Two consequences, both silent:
 *
 *   1. every load wrote a NEW cache entry under a URL that would never be
 *      requested again, so the cache grew forever with dead copies;
 *   2. `caches.match(e.request)` offline looked up a timestamp that had never
 *      been seen before, missed, and the app showed "load failed" while a
 *      perfectly good copy of the data sat in the cache.
 *
 * The fix is one idea applied everywhere: THE CACHE KEY IS THE URL WITHOUT ITS
 * QUERY STRING. The app now defeats the HTTP cache with `cache: "no-store"`
 * instead of a cache-busting query, and `?v=NN` on the shell assets stays
 * useful for the browser's own cache without fragmenting ours.
 * ==========================================================================*/

const VERSION = "v14";
const CACHE = "yentool-" + VERSION;

// Data files are cached under their bare URL too, so an offline start finds
// them. quotes.json is listed but may legitimately 404 on an older deploy,
// which is why the install below tolerates individual failures.
const SHELL = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./manifest.webmanifest",
  "./scan_result.json",
  "./quotes.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/apple-touch-icon.png",
];

// Strip the query so "styles.css?v=12", "styles.css?v=13" and "styles.css" are
// one entry. Returns a Request usable for both put() and match().
function cacheKey(request) {
  const url = new URL(typeof request === "string" ? request : request.url,
                      self.location.href);
  url.search = "";
  url.hash = "";
  return new Request(url.toString(), { method: "GET" });
}

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // addAll() is all-or-nothing: one missing file (quotes.json on an older
    // deploy) would abort the whole install and leave the PWA uncached.
    await Promise.all(SHELL.map(async (path) => {
      try {
        const res = await fetch(path, { cache: "no-store" });
        if (res.ok) await cache.put(cacheKey(path), res.clone());
      } catch (err) { /* offline install: fetch handler will fill it in later */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    // Delete every cache from an older version. Without this the old
    // "yentool-v11" bucket -- including its pile of ?t=... entries -- stayed
    // on the device forever.
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // never cache third parties

  e.respondWith((async () => {
    const key = cacheKey(req);
    try {
      const res = await fetch(req);
      if (res && res.ok) {
        const copy = res.clone();
        // Fire-and-forget: a full quota must not fail the navigation.
        caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {});
      }
      return res;
    } catch (err) {
      const hit = await caches.match(key) ||
                  await caches.match(req, { ignoreSearch: true });
      if (hit) return hit;
      // A navigation with nothing cached still has to render something.
      if (req.mode === "navigate") {
        const shell = await caches.match(cacheKey("./index.html"));
        if (shell) return shell;
      }
      throw err;
    }
  })());
});
