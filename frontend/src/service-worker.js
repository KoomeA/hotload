/// <reference types="@sveltejs/kit" />
import { version } from '$app/env';
import { assets, immutable, prerendered } from '$app/manifest';

// App shell only. API calls, downloads, and anything with credentials are never cached.
const CACHE = `hotload-${version}`;
// Manifest paths can be relative ("_app/x.js", "account/"), while request pathnames are absolute: normalise, or nothing matches.
const pathname = (p) => new URL(p, self.location.origin + '/').pathname;
const SHELL = [...immutable, ...assets, ...prerendered].map((e) => pathname(e.path));

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') || url.pathname === '/metrics') return;
  if (SHELL.includes(url.pathname) && !req.mode.includes('navigate')) {
    event.respondWith(caches.match(req).then((hit) => hit ?? fetch(req)));
    return;
  }
  if (req.mode === 'navigate') {
    // network first so users always get fresh pages; fall back to the cached shell when offline
    event.respondWith(fetch(req).catch(() => caches.match(url.pathname.startsWith('/account') ? '/account/' : '/')));
  }
});
