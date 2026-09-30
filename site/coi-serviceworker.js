// Adds the COOP/COEP headers GitHub Pages can't set, so the page becomes
// "cross-origin isolated" and FFmpeg can use multiple CPU threads.
// Without it everything still works, just slower.
if (typeof window === 'undefined') {
  self.addEventListener('install', () => self.skipWaiting());
  self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
  self.addEventListener('fetch', (e) => {
    const r = e.request;
    if (r.cache === 'only-if-cached' && r.mode !== 'same-origin') return;
    e.respondWith(
      fetch(r).then((res) => {
        if (res.status === 0) return res;
        const h = new Headers(res.headers);
        h.set('Cross-Origin-Embedder-Policy', 'credentialless');
        h.set('Cross-Origin-Opener-Policy', 'same-origin');
        return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
      })
    );
  });
} else {
  (() => {
    if (window.crossOriginIsolated || !window.isSecureContext || !('serviceWorker' in navigator)) return;
    let tried = false;
    try { tried = sessionStorage.getItem('coiReload') === '1'; } catch {}
    navigator.serviceWorker.register(document.currentScript.src).then((reg) => {
      const reload = () => {
        if (tried) return;
        try { sessionStorage.setItem('coiReload', '1'); } catch {}
        location.reload();
      };
      if (reg.active && !navigator.serviceWorker.controller) reload();
      navigator.serviceWorker.addEventListener('controllerchange', reload);
    }).catch((err) => console.warn('COI service worker failed:', err));
  })();
}
