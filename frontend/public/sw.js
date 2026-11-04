// ClipForge service worker: caches the app shell only, so the installed app opens instantly and shows a friendly
// page when the laptop can't be reached. The API (/api/*) is NEVER cached: data, videos and logins always come live.
const CACHE = 'clipforge-shell-v1'
const SHELL = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png', '/favicon.svg']

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

const OFFLINE = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#0c0c0f;color:#eee;font-family:system-ui;padding:32px;line-height:1.5">
<h2>Can't reach your laptop</h2><p>ClipForge runs on your laptop. Check that it's on and online, that ClipForge is running,
and that Tailscale is connected on this phone and on the laptop.</p><button onclick="location.reload()"
style="background:#ffe600;border:0;border-radius:8px;padding:10px 16px;font-weight:600">Try again</button></body>`

self.addEventListener('fetch', (event) => {
  const req = event.request
  const url = new URL(req.url)
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return
  if (req.mode === 'navigate') {
    // network first (fresh app after updates), cached shell when the laptop is unreachable
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone()
        if (res.ok) caches.open(CACHE).then((c) => c.put('/', copy))
        return res
      }).catch(async () => (await caches.match('/')) || new Response(OFFLINE, { headers: { 'Content-Type': 'text/html' } })),
    )
    return
  }
  if (url.pathname.startsWith('/assets/') || url.pathname.startsWith('/icons/')) {
    // hashed build files never change: cache first
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone()
        if (res.ok) caches.open(CACHE).then((c) => c.put(req, copy))
        return res
      })),
    )
  }
})
