/* Shepr service worker.
   Goal: when the tailnet is unreachable, the user sees YOUR app with YOUR message,
   not Safari's error page. It is NOT a general offline database. */

const VERSION = 'shepr-v52';
// The app shell: everything needed to paint a useful screen with no network.
const SHELL = ['./', 'index.html', 'app.css', 'app.js', 'manifest.webmanifest', 'icons/icon-180.png', 'icons/icon-192.png'];

self.addEventListener('install', (e) => {
  // skipWaiting so a reload picks up the new version instead of waiting for every
  // tab to close. On a personal tool that is what you want.
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;             // never cache writes
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;   // never cache other hosts
  if (url.pathname.endsWith('/ping')) return;   // the reachability probe must be real

  // The queue: network first, so you get fresh data when home is reachable and the last known
  // answer (labelled with its time by app.js) when it is not.
  if (url.pathname === '/api/queue') {
    e.respondWith(
      fetch(req)
        .then((res) => {
          // Only a good answer is worth keeping: a cached 401 would hide the pairing screen.
          if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
          return res;
        })
        .catch(() => caches.match(req).then(async (hit) => {
          // Offline: the last queue, but LABELLED as old. Its own age_s was true when it was cached, so
          // recompute it from when it was built, or the app would show frozen statuses as current.
          if (!hit) return hit;
          try {
            const q = await hit.clone().json();
            q.cached = true;
            q.age_s = Math.max(0, Math.round(Date.now() / 1000 - (q.now || 0)));
            return new Response(JSON.stringify(q), { status: 200, headers: { 'Content-Type': 'application/json' } });
          } catch (e) { return hit; }
        }))
    );
    return;
  }

  // Everything else under /api is live-only: a pane tail is terminal text that may hold secrets,
  // and a stale one would look like the current question. Let it fail honestly when offline.
  if (url.pathname.startsWith('/api/')) return;

  // Shell: NETWORK first, so a change shows on the very next open instead of the one after. If the network
  // is gone or slower than 2.5s (a dead tailnet can hang for a long time), fall back to the cache, which is
  // what keeps the app opening offline.
  e.respondWith(
    new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error('slow')), 2500);
      fetch(req).then((res) => { clearTimeout(t); resolve(res); }, (err) => { clearTimeout(t); reject(err); });
    }).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match('index.html')))
  );
});


// ---------------------------------------------------------------------------------------------------------------------
// Push. The host sends an EMPTY push ("something changed"). Nothing about your sessions travels through Apple's servers:
// this worker asks the host what changed, signing the request with the key in this phone's IndexedDB, and shows the answer.
// ---------------------------------------------------------------------------------------------------------------------
const swEnc = new TextEncoder();
const swB64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const swKv = (key) => new Promise((res, rej) => {
  const r = indexedDB.open('shepr', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('kv');
  r.onsuccess = () => { const g = r.result.transaction('kv', 'readonly').objectStore('kv').get(key); g.onsuccess = () => res(g.result); g.onerror = () => rej(g.error); };
  r.onerror = () => rej(r.error);
});
const swDevice = () => swKv('device');
async function swSignedGet(dev, path, skew) {
  const ts = String(Math.round(Date.now() + skew));
  const nonce = swB64u(crypto.getRandomValues(new Uint8Array(12)));
  const bh = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new Uint8Array(0)))).map((b) => b.toString(16).padStart(2, '0')).join('');
  const msg = [ts, nonce, 'GET', path, bh].join('\n');
  const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, dev.key, swEnc.encode(msg));
  return fetch(path, { cache: 'no-store', headers: { 'X-Shepr-Device': dev.id, 'X-Shepr-Ts': ts, 'X-Shepr-Nonce': nonce, 'X-Shepr-Sig': swB64u(sig) } });
}
async function swQueue() {
  const dev = await swDevice();
  if (!dev || !dev.key) throw new Error('this phone has no key');
  let skew = 0;
  for (let i = 0; i < 2; i++) {
    const r = await swSignedGet(dev, '/api/queue?fresh=1', skew);
    const j = await r.json().catch(() => ({}));
    if (r.ok) return j;
    if (j.error === 'stale' && i === 0) { const h = await (await fetch('/api/hello', { cache: 'no-store' })).json(); skew = h.t * 1000 - Date.now(); continue; }
    throw new Error(j.message || ('HTTP ' + r.status));
  }
  throw new Error('no answer');
}
// Names only, never a conversation title: a notification can be read on a locked screen.
// "Finished" means finished AND not yet opened, the same rule as the Finished list in the app. A worker cannot read localStorage, so the
// app mirrors what you have opened into IndexedDB under 'seen' ({"host|pane": {seq}}). Without it the lock screen counted sessions you had read.
function swDescribe(q, seen) {
  const mine = seen && typeof seen === 'object' && !Array.isArray(seen) ? seen : {};
  const unread = (a) => { const e = mine[a.host + '|' + a.pane]; return !(e && e.seq === a.seq); };
  const name = (a) => a.wlabel || a.project || 'a session';
  const list = (l) => { const n = [...new Set(l.map(name))]; return n.slice(0, 3).join(', ') + (n.length > 3 ? ' +' + (n.length - 3) : ''); };
  const blocked = q.agents.filter((a) => a.status === 'blocked'), done = q.agents.filter((a) => a.status === 'done' && unread(a));
  // The push itself is empty, so the host says (in the queue this worker just fetched) what it pushed for. The watch's news comes first,
  // because the usual "N agents are waiting" would hide it. Still names and our own labels only: never a command, never a title.
  const why = (q.pushed && q.pushed.why) || '', badge = blocked.length;
  if (/risk/.test(why)) {
    const r = q.agents.filter((a) => a.risk && a.risk.level === 'danger');
    return r.length ? { title: name(r[0]) + ' ran something risky', body: String(r[0].risk.what || '').replace(/\s*\(.*$/, '') + (r.length > 1 ? ' (and ' + (r.length - 1) + ' more)' : ''), target: r[0], count: badge }
                    : { title: 'An agent ran something risky', body: 'Open Shepr, then Activity, to see what.', target: null, count: badge };
  }
  if (/stopped/.test(why) && !q.agents.some((a) => a.status === 'working') && (blocked.length || done.length)) {
    return { title: 'Everything has stopped', body: [blocked.length ? blocked.length + ' waiting on you' : '', done.length ? done.length + ' finished' : ''].filter(Boolean).join(', '), target: blocked[0] || done[0], count: badge };
  }
  if (/quiet/.test(why)) {
    const lim = (q.quiet_min || 15) * 60, qs = q.agents.filter((a) => a.status === 'working' && a.quiet_s >= lim);
    if (qs.length) return { title: qs.length === 1 ? name(qs[0]) + ' has gone quiet' : qs.length + ' agents have gone quiet', body: qs.length === 1 ? 'No output for ' + Math.round(qs[0].quiet_s / 60) + ' minutes.' : list(qs), target: qs[0], count: badge };
  }
  if (blocked.length) return { title: blocked.length === 1 ? name(blocked[0]) + ' is waiting on you' : blocked.length + ' agents are waiting on you', body: list(blocked), target: blocked[0], count: blocked.length };
  if (done.length) return { title: done.length === 1 ? name(done[0]) + ' finished' : done.length + ' agents finished', body: list(done), target: done[0], count: 0 };
  return { title: 'Shepr', body: 'Nothing is waiting on you now.', target: null, count: 0 };
}
self.addEventListener('push', (e) => e.waitUntil((async () => {
  let n = { title: 'Shepr', body: 'An agent needs you. Open Shepr to see which.', target: null, count: null };
  try { const q = await swQueue(); n = swDescribe(q, await swKv('seen').catch(() => null)); } catch (err) { /* offline, unpaired or rejected: the generic text above still has to be shown */ }
  await self.registration.showNotification(n.title, { body: n.body, tag: 'shepr-queue', renotify: true, icon: 'icons/icon-192.png',
    data: n.target ? { host: n.target.host, pane: n.target.pane } : {} });
  try {
    if (self.navigator && n.count !== null) { if (n.count && self.navigator.setAppBadge) await self.navigator.setAppBadge(n.count); else if (self.navigator.clearAppBadge) await self.navigator.clearAppBadge(); }
  } catch (err) { /* the badge is a nicety */ }
})()));
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const d = e.notification.data || {};
  e.waitUntil((async () => {
    const list = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (list.length) { await list[0].focus(); if (d.host) list[0].postMessage({ type: 'open', host: d.host, pane: d.pane }); return; }
    await clients.openWindow(d.host ? '/#' + encodeURIComponent(d.host) + '/' + encodeURIComponent(d.pane) : '/');
  })());
});
