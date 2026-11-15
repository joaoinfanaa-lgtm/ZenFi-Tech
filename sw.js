/* ZenFi - service worker: abre o app mesmo sem internet. Dados (/api) NUNCA são guardados aqui. */
const VERSAO = "zenfi-v2";
const BASICO = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSAO).then((c) => c.addAll(BASICO)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSAO).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;           // fontes do Google etc.: rede normal
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/webhook/")) return;  // dados: sempre rede
  if (req.mode === "navigate") {                                                // página: rede primeiro, cópia se offline
    e.respondWith(fetch(req).then((r) => { const c = r.clone(); caches.open(VERSAO).then((x) => x.put("/", c)); return r; })
      .catch(() => caches.match("/")));
    return;
  }
  e.respondWith(caches.match(req).then((c) => c || fetch(req).then((r) => {
    if (r.ok) { const c2 = r.clone(); caches.open(VERSAO).then((x) => x.put(req, c2)); }
    return r;
  })));
});
