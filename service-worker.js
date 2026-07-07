// Service worker для офлайн-работы GPX Repair.
// Всё, что нужно для запуска инструмента (HTML, Leaflet, иконки), кэшируется —
// поход/трек можно чинить без интернета.
//
// Декоративные картинки с erudit23.ru сюда НЕ входят: они всегда пытаются
// загрузиться напрямую с сайта, а если сети/доступа нет — просто не
// показываются (см. onerror в index.html), без подстановки кэша-заглушки.

const CACHE_NAME = "gpx-repair-v3";

const PRECACHE_URLS = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./assets/logo.svg",
  "./assets/done.svg",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .catch(() => {})
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Декоративные картинки erudit23.ru не перехватываем: пусть грузятся
  // напрямую с сайта или проваливаются "как обычно" (без кэша-заглушки).
  if (url.hostname === "www.erudit23.ru" || url.hostname === "erudit23.ru") {
    return;
  }

  if (event.request.method !== "GET") {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches
              .open(CACHE_NAME)
              .then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    }),
  );
});
