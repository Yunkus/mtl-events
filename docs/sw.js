// Go Out Montreal service worker: opens instantly and works offline (e.g. in the metro).
// - the page (HTML) is network-first, falling back to the saved copy offline
// - the rest of the shell (font, logo, icons) is cached on install and served cache-first, refreshed in the background
// - events.json and the .ics feeds are network-first, falling back to the last copy when offline
// - event images (our own copies in img/, or a venue's) are cached as they're seen, up to a limit,
//   so recently viewed listings keep their pictures
const VERSION = "gom-v5";
const SHELL = ["./", "index.html", "fonts/ShareTech-Regular.ttf", "logo.webp?v=3", "wordmark.webp?v=5",
               "favicon.png?v=3", "icon-192.png", "manifest.webmanifest"];
const IMAGES = "gom-images";
const MAX_IMAGES = 300;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION && k !== IMAGES).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req) {
  const cache = await caches.open(VERSION);
  try {
    // "no-cache": ask the server whether it changed (GitHub Pages lets browsers reuse a copy for 10 minutes)
    const res = await fetch(req.mode === "navigate" ? req.url : req, { cache: "no-cache" });
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (err) {
    const page = req.mode === "navigate";   // offline: any saved copy of the page will do (?v=…, #tab)
    return (await cache.match(req, { ignoreSearch: page })) || (page && await cache.match("./")) || Response.error();
  }
}

async function shellFirst(req) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(req, { ignoreSearch: req.mode === "navigate" });
  const fresh = fetch(req).then(res => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => null);
  return hit || (await fresh) || Response.error();
}

async function imageCache(req) {
  const cache = await caches.open(IMAGES);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok || res.type === "opaque") {
      cache.put(req, res.clone());
      cache.keys().then(keys => { if (keys.length > MAX_IMAGES) cache.delete(keys[0]); });
    }
    return res;
  } catch (err) {
    return Response.error();
  }
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === location.origin) {
    if (/\.(json|ics)$/.test(url.pathname)) return e.respondWith(networkFirst(req));
    if (url.pathname.includes("/img/")) return e.respondWith(imageCache(req));
    // the page itself: always the latest when online (the saved copy is only for offline), so a site update
    // shows on the next visit instead of the one after
    if (req.mode === "navigate" || url.pathname.endsWith(".html") || url.pathname.endsWith("/"))
      return e.respondWith(networkFirst(req));
    return e.respondWith(shellFirst(req));
  }
  if (req.destination === "image") return e.respondWith(imageCache(req));
});
