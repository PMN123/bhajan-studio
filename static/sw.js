"use strict";

// Keeps the app installable and lets its page shell open offline. It is deliberately dumb:
// the shell is fetched network-first, so a git pull or a new Docker image is picked up on the next load,
// and the cache is only a fallback for when the network is gone. Songs, audio and the API are never touched.
const CACHE = "bhajan-shell-v1";   // bump to throw the old shell away
const SHELL = [
  "/", "/index.html", "/style.css", "/stems.css", "/app.js", "/stems.js", "/pwa.js",
  "/vendor/abcjs-basic-min.js", "/manifest.webmanifest", "/icons/icon-192.png",
];
const LIVE = ["/api/", "/media/", "/stems/"];   // data and audio: always straight to the network

self.addEventListener("install", (event) => {
  self.skipWaiting();
  // addAll fails as a whole if one file does (say a 401 behind a password); that must not block the install.
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).catch(() => {}));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (LIVE.some((prefix) => url.pathname.startsWith(prefix))) return;
  // Audio seeking sends Range requests and needs the server's real 206 answers.
  if (request.headers.has("range")) return;

  event.respondWith(networkFirst(event, request));
});

async function networkFirst(event, request) {
  try {
    const response = await fetch(request);
    // Only keep good same-origin answers: never an error page, a 401, a redirect (a navigation can't be
    // answered with one from the cache) or an opaque cross-origin response.
    if (response.ok && response.type === "basic" && !response.redirected) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {}));
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request) || (request.mode === "navigate" ? await caches.match("/") : undefined);
    if (cached) return cached;
    throw err;
  }
}
