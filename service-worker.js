// Service worker для офлайн-работы GPX Repair.
// Всё, что нужно для запуска инструмента (HTML, Leaflet, иконки), кэшируется —
// поход/трек можно чинить без интернета.
//
// Leaflet вендорится локально (./vendor/leaflet/), а не грузится с unpkg CDN:
// если внешний CDN отвалится или изменит контент без смены версии в URL,
// офлайн-режим PWA не должен молча сломаться.
//
// Декоративные картинки с erudit23.ru сюда НЕ входят: они всегда пытаются
// загрузиться напрямую с сайта, а если сети/доступа нет — просто не
// показываются (см. onerror в index.html), без подстановки кэша-заглушки.

const CACHE_NAME = "gpx-repair-v38";

const PRECACHE_URLS = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./assets/logo.svg",
  "./assets/done.svg",
  "./assets/icon-192.png",
  "./assets/icon-512.png",
  "./vendor/leaflet/leaflet.css",
  "./vendor/leaflet/leaflet.js",
  "./vendor/leaflet/images/marker-icon.png",
  "./vendor/leaflet/images/marker-icon-2x.png",
  "./vendor/leaflet/images/marker-shadow.png",
  "./vendor/leaflet/images/layers.png",
  "./vendor/leaflet/images/layers-2x.png",
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

  // Перехватываем только GET-запросы к файлам самого приложения. Всё
  // внешнее (тайлы карт, маршрутизация, высоты, прокси, картинки
  // erudit23.ru) идёт напрямую в сеть: ответы динамические, их не нужно
  // кэшировать — иначе кэш бесконтрольно растёт, а маршруты/высоты
  // отдаются устаревшими (высоты и так кэшируются в памяти приложения).
  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
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
        .catch(() => cached || Response.error());
      return cached || network;
    }),
  );
});
