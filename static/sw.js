const C = 'ramplez-v3';
const SHELL = ['/manifest.json', '/icon/192.png', '/icon/512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(k => Promise.all(k.filter(x => x !== C).map(x => caches.delete(x)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (u.pathname.startsWith('/api/') || u.pathname === '/sw.js') return;
  if (u.pathname.startsWith('/icon/') || u.pathname === '/manifest.json') {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
    return;
  }
  if (u.pathname.startsWith('/uploads/')) {
    e.respondWith(caches.open(C).then(c => c.match(e.request).then(r => r || fetch(e.request).then(res => {
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }))));
    return;
  }
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => new Response(
      '<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><body style="background:#0b1020;color:#eaf0ff;font:16px system-ui;display:grid;place-items:center;height:100vh;text-align:center"><div><div style="font-size:48px">📡</div><h2>Нет сети</h2><p style="color:#93a2c7">Ramplez подключится сам</p><button onclick="location.reload()" style="margin-top:16px;padding:12px 22px;border:0;border-radius:12px;background:#4f8cff;color:#fff;font-size:15px">Повторить</button></div></body>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
  }
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window' }).then(cs => {
    for (const c of cs) if ('focus' in c) return c.focus();
    return self.clients.openWindow('/chat');
  }));
});
