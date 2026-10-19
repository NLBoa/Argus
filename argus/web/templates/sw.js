// Argus service worker. Static assets: cache-first (their URLs are content-hashed).
// Pages: network-first, falling back to the last copy, then to /offline.
const CACHE = "argus-{{ version }}";
const PRECACHE = {{ urls|tojson }};

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/kiosk/stream")) return;
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    })));
    return;
  }
  if (req.mode === "navigate" || req.headers.get("hx-request") === "true") {
    event.respondWith(
      fetch(req).then((res) => {
        if (res.ok && !req.headers.get("hx-request")) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || caches.match("/offline")))
    );
  }
});
