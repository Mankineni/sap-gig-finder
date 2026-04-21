const CACHE_NAME = 'gig-radar-v5';
const APP_SHELL  = ['/', '/index.html', '/manifest.json'];
const DATA_FILE  = '/gigs_latest.json';

const NETWORK_TIMEOUT = 8000; // ms

// ---------------------------------------------------------------------------
// Install — cache the app shell, activate immediately
// ---------------------------------------------------------------------------
self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL);
    })
  );
  self.skipWaiting();
});

// ---------------------------------------------------------------------------
// Activate — purge old caches, claim clients
// ---------------------------------------------------------------------------
self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names
          .filter(function (n) { return n !== CACHE_NAME; })
          .map(function (n) { return caches.delete(n); })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

// ---------------------------------------------------------------------------
// Fetch — two strategies
// ---------------------------------------------------------------------------

/**
 * Race a fetch against a timeout. Rejects if the network doesn't respond
 * within `ms` milliseconds.
 */
function fetchWithTimeout(request, ms) {
  return new Promise(function (resolve, reject) {
    var timer = setTimeout(function () {
      reject(new Error('Network timeout'));
    }, ms);

    fetch(request).then(function (response) {
      clearTimeout(timer);
      resolve(response);
    }).catch(function (err) {
      clearTimeout(timer);
      reject(err);
    });
  });
}

/**
 * Network-first for the data file (gigs_latest.json).
 * Falls back to cache, then to a 503 JSON error.
 */
function networkFirstData(request) {
  return fetchWithTimeout(request, NETWORK_TIMEOUT)
    .then(function (response) {
      var clone = response.clone();
      caches.open(CACHE_NAME).then(function (cache) {
        cache.put(request, clone);
      });
      return response;
    })
    .catch(function () {
      return caches.match(request).then(function (cached) {
        if (cached) return cached;
        return new Response(
          JSON.stringify({ error: 'Offline and no cached data available' }),
          {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      });
    });
}

/**
 * Cache-first for app shell assets.
 * Falls back to network (and caches the response), then to an offline page.
 */
function cacheFirstShell(request) {
  return caches.match(request).then(function (cached) {
    if (cached) return cached;

    return fetch(request)
      .then(function (response) {
        var clone = response.clone();
        caches.open(CACHE_NAME).then(function (cache) {
          cache.put(request, clone);
        });
        return response;
      })
      .catch(function () {
        return new Response(
          '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">' +
          '<meta name="viewport" content="width=device-width,initial-scale=1">' +
          '<title>Offline</title>' +
          '<style>body{font-family:sans-serif;display:flex;align-items:center;' +
          'justify-content:center;height:100vh;margin:0;background:#111;color:#e5e5e5;}' +
          '</style></head><body><p>SAP Gig Radar is offline</p></body></html>',
          {
            status: 503,
            headers: { 'Content-Type': 'text/html' },
          }
        );
      });
  });
}

self.addEventListener('fetch', function (event) {
  var url = new URL(event.request.url);

  if (url.pathname.endsWith('gigs_latest.json')) {
    event.respondWith(networkFirstData(event.request));
    return;
  }

  event.respondWith(cacheFirstShell(event.request));
});

// ---------------------------------------------------------------------------
// Message — allow the page to trigger an immediate SW update
// ---------------------------------------------------------------------------
self.addEventListener('message', function (event) {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ---------------------------------------------------------------------------
// Background Sync — refresh data when connectivity is restored
// ---------------------------------------------------------------------------
self.addEventListener('sync', function (event) {
  if (event.tag === 'refresh-gigs') {
    event.waitUntil(
      fetch(DATA_FILE).then(function (response) {
        return caches.open(CACHE_NAME).then(function (cache) {
          return cache.put(DATA_FILE, response);
        });
      })
    );
  }
});
