// Deliberately does no caching. Its only job is to exist with a fetch handler, which Chrome requires before
// it will offer "Add to Home Screen" - so a deploy is never masked by a stale cached page or a stuck old
// worker. skipWaiting/clients.claim make a changed sw.js take over immediately rather than waiting for every
// open tab to close first.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(fetch(event.request)));
