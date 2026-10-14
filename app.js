/* Shepr app.js
   Template jobs (service worker, reachability) plus: the device key, QR pairing, the attention
   queue, the session view, gesture navigation, hosts, and the confirm-first send flow.

   Rules this file keeps:
     1. Text that came from a pane or a host is DATA. It only ever goes through textContent.
     2. Nothing is sent without the preview sheet. Taps, double-taps and long-presses only NAVIGATE.
     3. No timer, retry or "auto" anything ever sends. A failed send is reported, never repeated.
     4. The private key never leaves the phone: it is generated non-extractable and only signs. */
'use strict';
const BUILD = 'v45';   // keep equal to sw.js VERSION: shown in Settings and logged by the host, so you can see which app a phone is running

// ---------------------------------------------------------------------------
// 1. Service worker. Without this, a dead tailnet shows Safari's error page and
//    the user thinks YOUR app is broken.
// ---------------------------------------------------------------------------
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('sw.js').catch(function (e) {
      console.warn('[shepr] service worker not registered:', e.message);
    });
  });
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode: fine */ } },
};
// Unsent text in a session's message box, kept per session so moving to another project and back finds it again. It lives
// on this phone only (localStorage) and is never sent anywhere until you press Send.
const DRAFT_MAX = 30, DRAFT_DAYS = 14;
const drafts = {
  all() { const d = store.get('shepr.drafts', {}); return d && typeof d === 'object' && !Array.isArray(d) ? d : {}; },
  get(k) { const e = this.all()[k]; return e && typeof e.t === 'string' ? e.t : ''; },
  put(k, text) {
    const d = this.all(), now = Date.now();
    if (!text.trim()) delete d[k]; else d[k] = { t: text.slice(0, 4000), at: now };
    Object.keys(d).forEach((x) => { if (!d[x] || typeof d[x].t !== 'string' || now - (d[x].at || 0) > DRAFT_DAYS * 86400000) delete d[x]; });
    const keys = Object.keys(d).sort((x, y) => d[y].at - d[x].at);
    keys.slice(DRAFT_MAX).forEach((x) => delete d[x]);
    store.set('shepr.drafts', d);
  },
  has(k) { return !!this.get(k); },
};
// ---- Settings: small choices kept on this phone (localStorage), each one wired to something real. See Settings > Display.
const PREFS = {
  splash:  { def: 'full',   opts: [['full', 'Full'], ['short', 'Short'], ['off', 'Off']] },
  motion:  { def: 'on',     opts: [['on', 'On'], ['off', 'Off']] },
  open:    { def: 'last',   opts: [['last', 'Last'], ['waiting', 'Queue'], ['all', 'Radar'], ['subs', 'Subs']] },
  refresh: { def: 'normal', opts: [['fast', 'Fast'], ['normal', 'Normal'], ['slow', 'Slow'], ['manual', 'Manual']] },
  slash:   { def: 'show',   opts: [['show', 'Show'], ['hide', 'Hide']] },
};
const REFRESH_MS = { fast: [5000, 3000], normal: [10000, 6000], slow: [30000, 15000], manual: [0, 0] };   // [the queue, the open session]
const prefs = {
  get(k) { const all = store.get('shepr.prefs', {}), v = all && typeof all === 'object' ? all[k] : undefined; return PREFS[k].opts.some((o) => o[0] === v) ? v : PREFS[k].def; },
  set(k, v) { const all = store.get('shepr.prefs', {}); store.set('shepr.prefs', Object.assign({}, all && typeof all === 'object' ? all : {}, { [k]: v })); },
};
const osReduced = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
let REDUCED = osReduced() || prefs.get('motion') === 'off';   // the iPhone's own Reduce Motion, or Settings > Animations off
function applyMotion() { document.documentElement.dataset.motion = prefs.get('motion'); }
applyMotion();
const clock = (sec) => new Date(sec * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const keyOf = (a) => a.host + '|' + a.pane;
// What a session is CALLED everywhere: the name Fred gave its herdr workspace (the sidebar name, "Shepr"), or its
// project directory when the workspace has no name. Never herdr's session title, which is a summary of the last
// conversation and changes under you. Two sessions with one name on one host get their pane id added.
const nm = (a) => a.wlabel || a.project;
function label(a) {
  const same = (S.q ? S.q.agents : []).filter((x) => x.host === a.host && nm(x) === nm(a));
  return same.length > 1 ? nm(a) + ' · ' + a.pane : nm(a);
}
// No pinch zoom and no sideways scrolling. iOS ignores user-scalable=no, so also stop the gesture itself (the old Safari
// gesture events, any multi-finger move, a trackpad pinch that arrives as ctrl+wheel), and snap any sideways scroll back.
['gesturestart', 'gesturechange', 'gestureend'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault(), { passive: false }));
document.addEventListener('touchmove', (e) => { if (e.touches && e.touches.length > 1) e.preventDefault(); }, { passive: false });
document.addEventListener('wheel', (e) => { if (e.ctrlKey) e.preventDefault(); }, { passive: false });
document.addEventListener('scroll', (e) => {
  const t = e.target === document ? document.scrollingElement : e.target;
  if (t && t.scrollLeft) t.scrollLeft = 0;
}, true);
const enc = new TextEncoder();
const b64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const hex = (buf) => Array.prototype.map.call(new Uint8Array(buf), (x) => x.toString(16).padStart(2, '0')).join('');

function fmtAge(s, exact) {
  const lead = exact === false ? '≥' : '';
  if (s < 60) return { main: 'now', sub: '' };   // no ≥ here: under a minute is not worth hedging
  const m = Math.floor(s / 60);
  if (m < 60) return { main: lead + m + 'm', sub: '' };
  const h = Math.floor(m / 60);
  if (h < 24) return { main: lead + h + 'h', sub: String(m % 60).padStart(2, '0') + 'm' };
  return { main: lead + Math.floor(h / 24) + 'd', sub: (h % 24) + 'h' };
}
const ageClass = (s) => (s >= 3600 ? 'hot' : s >= 600 ? 'warm' : '');
const STATE_LABEL = { blocked: 'BLOCKED', working: 'WORKING', idle: 'IDLE', done: 'DONE', unknown: 'UNKNOWN' };

const S = {
  q: null,            // last /api/queue
  hello: null,        // /api/hello
  dev: null,          // {id, key, jwk, name}: this phone's identity
  skew: 0,            // server clock minus phone clock, ms
  tab: store.get('shepr.tab', 'waiting'),
  host: store.get('shepr.all2') ? store.get('shepr.host', null) : null,   // host filter; the saved one is dropped once, so the Queue starts combined (Fred, 2026-10-10)
  hosts: null,        // /api/hosts
  cur: null,          // {host, pane} of the open session
  prevKey: null,      // the session you were on before this one (hold the title to flip back)
  ctx: 'waiting',     // which list the open session came from; prev/next walk it
  idx: 0,
  keybuf: [],
  tailText: null,
  sheetOpen: false,
  menu: null,
  agent: null,
  shown: new Set(),   // rows already animated in, so a poll does not replay the entrance
};
if (S.tab === 'go') S.tab = 'waiting';
if (S.tab === 'keys') S.tab = 'settings';   // the Keys tab is now Settings
if (prefs.get('open') !== 'last') S.tab = prefs.get('open');   // Settings > Open on

// ---------------------------------------------------------------------------
// 2. The device key. ECDSA P-256, generated non-extractable: the phone can sign with it and
//    nothing, not even this script, can read it back out. Kept in IndexedDB (a CryptoKey cannot
//    go in localStorage). The host holds only the public half, like an authorized_keys entry.
// ---------------------------------------------------------------------------
function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open('shepr', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function kv(mode, k, v) {
  const db = await idb();
  return new Promise((res, rej) => {
    const tx = db.transaction('kv', mode === 'get' ? 'readonly' : 'readwrite');
    const st = tx.objectStore('kv');
    const q = mode === 'get' ? st.get(k) : mode === 'set' ? st.put(v, k) : st.delete(k);
    // A write is only done when the transaction COMPLETES: a request can succeed and its transaction still abort.
    tx.oncomplete = () => res(q.result);
    tx.onerror = () => rej(tx.error || q.error);
    tx.onabort = () => rej(tx.error || new Error('storage write was aborted'));
  });
}
const loadDevice = () => kv('get', 'device');
const saveDevice = (d) => kv('set', 'device', d);
const forgetDevice = () => kv('del', 'device');

async function newKey() {
  const kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const j = await crypto.subtle.exportKey('jwk', kp.publicKey);
  return { key: kp.privateKey, jwk: { kty: j.kty, crv: j.crv, x: j.x, y: j.y } };
}

// Every request to the host is signed over: time, a fresh nonce, the method, the exact path and
// a hash of the body. Replaying it, or changing any part of it, fails on the host.
async function signHeaders(method, path, body) {
  const u = new URL(path, location.href);
  const ts = String(Math.round(Date.now() + S.skew));
  const nonce = b64u(crypto.getRandomValues(new Uint8Array(12)));
  const bh = hex(await crypto.subtle.digest('SHA-256', body instanceof Uint8Array ? body : enc.encode(body || '')));
  const msg = [ts, nonce, method, u.pathname + u.search, bh].join('\n');
  const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, S.dev.key, enc.encode(msg));
  return { 'X-Shepr-Device': S.dev.id, 'X-Shepr-Ts': ts, 'X-Shepr-Nonce': nonce, 'X-Shepr-Sig': b64u(sig) };
}

const AUTH_CODES = ['unpaired', 'unknown_device', 'bad_signature'];
async function api(path, opts) {
  opts = opts || {};
  const method = (opts.method || 'GET').toUpperCase();
  const body = opts.body || '';
  const headers = Object.assign({}, opts.headers);
  if (!opts.public) {
    if (!S.dev) { const e = new Error('This phone is not paired yet.'); e.code = 'unpaired'; e.status = 401; throw e; }
    Object.assign(headers, await signHeaders(method, path, body));
  }
  // A READ gives up after a while: when the tunnel to the host is down (Tailscale stuck on the phone), iOS lets a fetch
  // hang for a minute or more, and the app sat on the splash and then a blank page. A write never times out here: one that
  // timed out may still have landed, and writes are never retried.
  const ctl = method === 'GET' && typeof AbortController === 'function' ? new AbortController() : null;
  const tmo = ctl ? setTimeout(() => ctl.abort(), opts.public ? 8000 : 25000) : null;
  let r;
  try { r = await fetch(path, { method, headers, body: body || undefined, cache: 'no-store', signal: ctl ? ctl.signal : undefined }); }
  catch (e) { if (ctl && ctl.signal.aborted) { const t = new Error('The host did not answer in time.'); t.code = 'timeout'; throw t; } throw e; }
  finally { if (tmo) clearTimeout(tmo); }
  const j = await r.json().catch(() => ({ error: 'bad_response', message: 'HTTP ' + r.status }));
  if (!r.ok || j.error) {
    const e = new Error(j.message || ('HTTP ' + r.status));
    e.code = j.error; e.status = r.status;
    // The phone clock drifted: learn the host's time and try a READ once more. Never a write.
    if (e.code === 'stale' && method === 'GET' && !opts.retried) {
      await syncClock();
      return api(path, Object.assign({}, opts, { retried: true }));
    }
    if (!opts.public && AUTH_CODES.indexOf(e.code) >= 0) showPair(e.code === 'unknown_device'
      ? 'This phone is not authorised on the host any more. Pair it again.' : null);
    throw e;
  }
  return j;
}
function post(path, body, extra) {
  return api(path, Object.assign({ method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Shepr': '1' },
                                   body: JSON.stringify(body) }, extra));
}
async function syncClock() {
  const t0 = Date.now();
  const h = await api('api/hello', { public: true, headers: { 'X-Shepr-Build': BUILD } });
  S.hello = h; S.skew = h.t * 1000 - (t0 + Date.now()) / 2;
  if (S.dev) appIsStale(h.build);
}

// ---------------------------------------------------------------------------
// 3. Reachability. navigator.onLine only tells you the RADIO is up, so we ask the host.
// ---------------------------------------------------------------------------
const netDot = $('net'), banner = $('offline');
let online = true;
function ping() {
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;   // a dead tunnel must read as "can't reach", not hang
  const tmo = ctl ? setTimeout(() => ctl.abort(), 8000) : null;
  return fetch('ping?t=' + Date.now(), { cache: 'no-store', signal: ctl ? ctl.signal : undefined }).then((r) => r.ok).catch(() => false)
    .finally(() => { if (tmo) clearTimeout(tmo); });
}
function setOnline(up) {
  online = up;
  netDot.classList.toggle('off', !up);
  paintBanner();
}
function paintBanner() {
  const q = S.q;
  const stale = q && q.age_s != null && q.age_s > 3 * (q.poll_s || 15);
  banner.hidden = online && !stale;
  if (banner.hidden) return;
  banner.textContent = '';
  const at = el('b', null, q ? clock(q.now) : '--:--');
  if (online) banner.append('herdr has not answered for ' + Math.round(q.age_s / 60) + ' min. Showing what I knew at ', at, '.');
  else banner.append("Can't reach ", el('b', null, location.hostname || 'home'), '. Showing what I knew at ', at, '.');
}
function checkNet() { ping().then(setOnline); }
setInterval(checkNet, 20000);
window.addEventListener('online', checkNet);
window.addEventListener('offline', () => setOnline(false));
const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

// ---------------------------------------------------------------------------
// 4. Gestures. Tap, double-tap and long-press are how you MOVE; none of them ever writes.
//    h = { tap, dbl, long, instant }. With `dbl` and not `instant`, tap waits DBL_MS to see
//    whether a second one follows (so a double-tap does not also fire the single-tap action).
// ---------------------------------------------------------------------------
const LONG_MS = 450, DBL_MS = 260, SLOP = 10;
function gesture(node, h) {
  let x0 = 0, y0 = 0, down = false, moved = false, fired = false;
  let longT = null, pressT = null, tapT = null, lastUp = 0, lastX = 0, lastY = 0;
  const clear = () => { clearTimeout(longT); clearTimeout(pressT); node.classList.remove('pressing'); };
  node.addEventListener('pointerdown', (e) => {
    if (e.button > 0) return;
    down = true; moved = false; fired = false; x0 = e.clientX; y0 = e.clientY;
    if (h.long) {
      pressT = setTimeout(() => node.classList.add('pressing'), 140);
      longT = setTimeout(() => {
        fired = true; clear();
        node.classList.add('pressed'); setTimeout(() => node.classList.remove('pressed'), 250);
        h.long();
      }, LONG_MS);
    }
  });
  node.addEventListener('pointermove', (e) => {
    if (down && Math.hypot(e.clientX - x0, e.clientY - y0) > SLOP) { moved = true; clear(); }
  });
  node.addEventListener('pointercancel', () => { down = false; moved = true; clear(); });
  node.addEventListener('pointerup', (e) => {
    if (!down) return;
    down = false; clear();
    if (fired || moved) return;
    const now = performance.now(), near = Math.hypot(e.clientX - lastX, e.clientY - lastY) < 30;
    const second = h.dbl && now - lastUp < DBL_MS && near;
    lastX = e.clientX; lastY = e.clientY;
    if (!h.dbl) { h.tap && h.tap(e); return; }
    if (h.instant) {
      lastUp = second ? 0 : now;
      if (second) h.dbl(e); else h.tap && h.tap(e);
      return;
    }
    if (second) { clearTimeout(tapT); tapT = null; lastUp = 0; h.dbl(e); return; }
    lastUp = now;
    tapT = setTimeout(() => { tapT = null; h.tap && h.tap(e); }, DBL_MS);
  });
  // WHY: a long-press on iOS raises its own context menu; ours is the navigation gesture.
  node.addEventListener('contextmenu', (e) => e.preventDefault());
  // Keyboard and assistive tech produce click with detail 0.
  node.addEventListener('click', (e) => { if (e.detail === 0 && h.tap) h.tap(e); });
}

// ---------------------------------------------------------------------------
// 5. Queue data
// ---------------------------------------------------------------------------
// The host says which build is current. A Home Screen app stays alive in iOS memory for days, so without this a phone
// keeps running old code until you fully close it. Reload at the first SAFE moment (list view, nothing typed, no sheet
// open); otherwise offer it. Never reload under someone's thumb.
function appIsStale(serverBuild) {
  if (!serverBuild || serverBuild === BUILD) return;
  const busy = ($('msg') && $('msg').value.trim()) || S.sheetOpen || S.keybuf.length || !$('pair').hidden;
  // One automatic reload per advertised build. If the page still comes back old (a cached shell, a half-finished
  // deploy), asking again would loop forever, so the second time it is the Update button.
  let tried = null;
  try { tried = sessionStorage.getItem('shepr.reloadedFor'); } catch (e) { /* storage may be blocked */ }
  if (!busy && !S.cur && tried !== serverBuild) {
    try { sessionStorage.setItem('shepr.reloadedFor', serverBuild); } catch (e) { /* ignore */ }
    location.reload(); return;
  }
  notify('A new version of Shepr (' + serverBuild + ') is ready.', false, ['Update', () => location.reload()], true);
}
// One read at a time. A fresh read asked for while one is in flight (Send, pull to refresh) is not dropped: the read in flight
// may have left before the change it needs to show, so it runs again once that one is back.
let fetching = null, fetchAgain = false;
function fetchQueue(fresh) {
  if (!S.dev) return Promise.resolve();
  if (fetching) { if (fresh) fetchAgain = true; return fetching; }
  $('mini').classList.add('busy');
  fetching = api('api/queue' + (fresh ? '?fresh=1' : '')).then((q) => {
    S.q = q; setOnline(!q.cached); render(); if (!q.cached) { appIsStale(q.build); updateBadge(); if (!S.ledgerAt || Date.now() - S.ledgerAt > 60000) loadLedger(); }
    // The app files can be newer than the server process behind them (files are served live, the server only changes when
    // it restarts). New buttons then fail in confusing ways, so say it plainly once.
    if (!q.cached && q.server_build && q.build && q.server_build !== q.build && !S.skewShown) {
      S.skewShown = true;
      notify('This host is running Shepr ' + q.server_build + ' but the app is ' + q.build + '. Restart Shepr on the host, or newer buttons will fail.', true);
    }
  }).catch((e) => { console.warn('queue fetch failed:', e && e.message, e && e.stack && e.stack.split('\n').slice(0, 3).join(' | ')); if (!e.status) setOnline(false); if (!S.q) paintMain(); })
    .finally(() => { fetching = null; setTimeout(() => $('mini').classList.remove('busy'), 400); if (fetchAgain) { fetchAgain = false; fetchQueue(true); } });
  return fetching;
}
const agents = () => (S.q ? S.q.agents : []);
const byKey = (k) => agents().find((a) => keyOf(a) === k);
if (!store.get('shepr.all2')) { store.set('shepr.all2', true); store.set('shepr.host', null); }
const inHost = (a) => !S.host || a.host === S.host;
// Every machine has its own colour (never an amber, green or red: those mean a session's state), shown as a pill wherever a session is named.
const HOST_COLORS = ['#4dd0ff', '#c084fc', '#f472b6', '#5eead4', '#93c5fd', '#fdba74'];
function hostColor(id) { const hs = (S.q && S.q.hosts) || [], i = hs.findIndex((h) => h.id === id); return HOST_COLORS[(i < 0 ? 0 : i) % HOST_COLORS.length]; }
function hostPill(id) { const p = el('span', 'hpill', id); p.style.setProperty('--hc', hostColor(id)); return p; }
const waiting = () => agents().filter((a) => a.status === 'blocked' && inHost(a));
// A finished session you have opened is "looked at": it stays listed, but no longer says it is unread. Kept on this phone, per finish
// (the state_change_seq), so the next time it finishes it is unread again.
const seenStore = {
  all() { const d = store.get('shepr.seen', {}); return d && typeof d === 'object' && !Array.isArray(d) ? d : {}; },
  mark(k, seq) { const d = this.all(); if (d[k] && d[k].seq === seq) return; d[k] = { seq, at: Date.now() }; const ks = Object.keys(d).sort((x, y) => d[y].at - d[x].at); ks.slice(80).forEach((x) => delete d[x]); store.set('shepr.seen', d); },
};
const isSeen = (a) => a.status === 'done' && (seenStore.all()[keyOf(a)] || {}).seq === a.seq;
const finished = () => agents().filter((a) => a.status === 'done' && !isSeen(a) && inHost(a));
const finishedSeen = () => agents().filter((a) => a.status === 'done' && isSeen(a) && inHost(a));
const byName = (a, b) => a.host.localeCompare(b.host) || nm(a).localeCompare(nm(b));
const sleeping = () => agents().filter((a) => a.status === 'idle' && inHost(a)).sort(byName);
const working = () => agents().filter((a) => a.status === 'working' && inHost(a)).sort(byName);
// The Queue, in the order that matters: needs you, finished, working, and sleeping last.
const queueList = () => waiting().concat(finished(), finishedSeen(), working(), sleeping());

// ---------------------------------------------------------------------------
// 6. Icon tab bar
// ---------------------------------------------------------------------------
const ICON = {
  waiting: '<path d="M6 9a6 6 0 0 1 12 0c0 6 2 7 2 7H4s2-1 2-7"/><path d="M10 19a2 2 0 0 0 4 0"/>',
  all: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4.8"/><path d="M12 12 18.4 5.6"/><circle cx="16.2" cy="15.4" r="1.1" fill="currentColor" stroke="none"/>',
  subs: '<path d="M3.5 17a8.5 8.5 0 0 1 17 0"/><path d="m12 17 4.2-5.4"/><circle cx="12" cy="17" r="1.3" fill="currentColor" stroke="none"/><path d="M6.2 12.6l1 .7M17.8 12.6l-1 .7M12 8.5v1.2"/>',
  hosts: '<rect x="3.5" y="4" width="17" height="6.5" rx="2"/><rect x="3.5" y="13.5" width="17" height="6.5" rx="2"/><path d="M7 7.25h.01M7 16.75h.01"/>',
  settings: '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>',
};
const TABS = [
  { id: 'waiting', label: 'Queue' }, { id: 'all', label: 'Radar' }, { id: 'subs', label: 'Subs' },
  { id: 'hosts', label: 'Hosts' }, { id: 'settings', label: 'Settings' },
];
function buildTabs() {
  const nav = $('tabs');
  nav.textContent = '';
  const ind = el('div', 'tab-ind'); ind.id = 'tabind'; ind.style.width = (100 / TABS.length) + '%';
  nav.append(ind);
  TABS.forEach((t) => {
    const b = el('button', 'tab' + (t.action ? ' go' : '')); b.dataset.view = t.id; b.type = 'button';
    b.setAttribute('aria-label', t.label);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.innerHTML = ICON[t.id];   // constant markup defined above, never data
    b.append(svg, el('span', null, t.label));
    if (t.id === 'waiting') { const bd = el('span', 'badge'); bd.id = 'badge'; bd.hidden = true; b.append(bd); }
    gesture(b, {
      instant: true,
      tap: () => (t.action ? openGoTo('') : switchTab(t.id)),
      dbl: () => { $('main').scrollTop = 0; fetchQueue(true); if (S.tab === 'hosts' || S.tab === 'settings') loadHosts(); },
      long: () => openGoTo(''),
    });
    nav.append(b);
  });
}
function switchTab(id) {
  if (id !== S.tab) S.shown.clear();
  S.tab = id; store.set('shepr.tab', id);
  const b = document.querySelector('.tab[data-view="' + id + '"]');
  if (b) { b.classList.remove('bump'); void b.offsetWidth; b.classList.add('bump'); }
  render();
  if (id === 'hosts' || id === 'settings') loadHosts();
}
function paintTabs() {
  let i = 0;
  document.querySelectorAll('.tab').forEach((t, n) => {
    const on = t.dataset.view === S.tab;
    t.classList.toggle('on', on);
    if (on) i = n;
  });
  $('tabind').style.transform = 'translateX(' + (i * 100) + '%)';
}

// ---------------------------------------------------------------------------
// 7. Main screen
// ---------------------------------------------------------------------------
function ageBox(a) {
  const box = el('div', 'age');
  if (a.for_s != null) {
    const f = fmtAge(a.for_s, a.exact);
    box.textContent = f.main;
    const c = ageClass(a.for_s); if (c) box.classList.add(c);
    box.append(el('small', null, f.sub || (a.status === 'done' ? 'done' : 'blocked')));
  } else {
    box.append(el('span', 'sdot ' + a.status), el('small', null, a.status));
  }
  return box;
}

// One line saying what is waiting in a session's message box: your first words, and how many photos are with them.
function draftGlance(text) {
  if (!text || !text.trim()) return '';
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean), isPath = (l) => /\/uploads\/[^\s]+\.(png|jpe?g|gif|webp|pdf|txt|md|log|json|csv)$/i.test(l);
  const photos = lines.filter(isPath).length, words = lines.filter((l) => !isPath(l) && !/^Photo \d+: /.test(l))[0] || (lines.find((l) => /^Photo \d+: /.test(l)) || '').replace(/^Photo \d+: /, '');
  const tail = photos ? ' (+' + photos + (photos === 1 ? ' file)' : ' files)') : '';
  return ((words.length > 60 ? words.slice(0, 59) + '\u2026' : words) || 'unsent') + tail;
}
function itemNode(a, ctx, compact, idx) {
  const n = el('div', 'item ' + a.status + (compact ? ' compact' : ''));
  n.dataset.key = keyOf(a);
  const k = ctx + '/' + keyOf(a);
  if (!S.shown.has(k) && !REDUCED) { n.classList.add('enter'); n.style.setProperty('--i', Math.min(idx || 0, 9)); }
  S.shown.add(k);
  if (compact) {
    const d = el('div', 'age'); d.append(el('span', 'sdot ' + a.status)); d.style.flexBasis = '18px';
    n.append(d);
  } else n.append(ageBox(a));
  const m = el('div', 'meta');
  const t = el('div', 'ttl');
  if (S.q && S.q.hosts.length > 1) t.append(hostPill(a.host));
  t.append(label(a));
  if (a.agent && a.agent !== 'claude' && a.agent !== '?') t.append(el('span', 'atag', a.agent));   // Claude is the default; anything else says what it is
  let sub = el('div', 'sub', a.title);
  if (a.status === 'blocked' && a.ask) {   // say WHAT it is waiting on, so a wrong "waiting" is obvious at a glance
    sub = a.ask.menu && a.ask.q ? el('div', 'sub ask', a.ask.q + (a.ask.n > 1 ? '  \u00b7  ' + a.ask.n + ' options' : ''))
                                : el('div', 'sub nomenu', 'Blocked, but no question is on screen. Open it to look.');
  }
  m.append(t, sub);
  const dg = draftGlance(drafts.get(keyOf(a)));   // words you typed here and did not send
  if (dg) m.append(el('div', 'sub draft', '\u270e ' + dg));
  n.append(m, el('div', 'chev', '\u203a'));
  gesture(n, { tap: () => openDetail(a.host, a.pane, ctx), long: () => openPeek(a, ctx) });
  return n;
}

gesture($('count'), { tap: () => nextWaiting() });

function render() {
  const q = S.q;
  if (!q) return;
  const totalWaiting = agents().filter((a) => a.status === 'blocked').length;
  const c = $('count');
  c.hidden = false; c.textContent = totalWaiting; c.classList.toggle('none', totalWaiting === 0);
  c.setAttribute('aria-label', totalWaiting ? 'Open what is waiting on you (' + totalWaiting + ')' : 'Nothing is waiting on you');
  const b = $('badge');
  if (b) {
    const was = b.textContent;
    b.hidden = totalWaiting === 0; b.textContent = totalWaiting;
    if (was !== String(totalWaiting) && totalWaiting) { b.classList.remove('pop'); void b.offsetWidth; b.classList.add('pop'); }
  }
  document.title = (totalWaiting ? '(' + totalWaiting + ') ' : '') + 'Shepr';
  paintBanner();
  paintHostbar();
  paintTabs();
  paintMain();
  if (S.cur) paintDetailChrome();
}

function paintHostbar() {
  const hb = $('hostbar'), hosts = S.q.hosts;
  hb.hidden = (hosts.length < 2 && hosts.every((h) => h.status === 'ok')) || (S.tab !== 'waiting' && S.tab !== 'all');
  hb.textContent = '';
  if (S.host && !hosts.some((h) => h.id === S.host)) { S.host = null; store.set('shepr.host', null); }
  const setHostTo = (id) => { S.host = id; store.set('shepr.host', id); S.shown.clear(); render(); };
  if (hosts.length > 1) {
    const all = el('button', 'chip all' + (S.host ? '' : ' on')); all.append('All hosts');
    gesture(all, { instant: true, tap: () => setHostTo(null) });
    hb.append(all);
  }
  hosts.forEach((h) => {
    const c = el('button', 'chip' + (S.host === h.id ? ' on' : '') + (h.status !== 'ok' ? ' bad' : '')); c.style.setProperty('--hc', hostColor(h.id));
    c.append(el('u'), h.id);
    const blocked = (h.counts && h.counts.blocked) || 0;
    if (h.status !== 'ok') c.append(el('i', null, h.status === 'disabled' ? 'off' : 'down'));
    else if (blocked) c.append(el('i', null, blocked));
    const setHost = setHostTo;
    gesture(c, {
      instant: true,
      tap: () => setHost(S.host === h.id ? null : h.id),
      dbl: () => setHost(null),
      long: () => { S.tab = 'waiting'; store.set('shepr.tab', S.tab); setHost(h.id); },
    });
    hb.append(c);
  });
}

function section(text, n) {
  const s = el('div', 'sec', text);
  if (n != null) s.append(' ', el('b', null, '(' + n + ')'));
  return s;
}

// No queue has ever arrived (the host was not reachable when the app opened). The polls keep trying, so the page fills in
// by itself once the phone can reach the host again.
function noDataNode() {
  const e = el('div', 'empty nodata'); e.id = 'nodata';
  const r = el('div', 'radar tiny'); r.append(el('i'), el('i'), el('i'), el('b'));
  const host = (S.hello && S.hello.name) || location.hostname.split('.')[0] || 'the host';
  e.append(r, el('b', null, online ? 'Reading ' + host + '\u2026' : "Can't reach " + host));
  if (!online) e.append(el('p', null, 'This phone cannot get to it. Check Tailscale on this phone: open the Tailscale app, switch it off, then on again.'),
                        btn('Try again', 'mini-btn', () => { checkNet(); fetchQueue(true); }));
  return e;
}
function paintMain() {
  const m = $('main');
  const keep = m.scrollTop;
  m.textContent = '';
  m.classList.toggle('radarpage', S.tab === 'all');   // the radar page is one fixed screen: the numbers on top, the radar filling the rest
  m.classList.toggle('setpage', S.tab === 'settings');   // Settings is one fixed screen too: four sections behind a bar, each fitting without scrolling
  if (S.tab !== 'subs') BUD.fresh = true;   // the Subs page animates in each time you arrive, not on every refresh
  if (!S.q && S.tab !== 'settings') { m.append(noDataNode()); return; }   // never a blank page: say the host could not be reached
  if (S.tab === 'waiting') {
    const w = waiting(), d = finished(), ds = finishedSeen(), sl = sleeping(), wk = working();
    if (S.q && S.q.writes) {   // the one big button: a brand-new project (name, folder, the start command)
      const nb = btn('', 'newbig', openNewProject); nb.id = 'newproject';
      nb.append(el('span', 'plus', '+'), el('span', 'lbl', 'Start new'), el('span', 'sub2', 'a new project'));
      m.append(nb);
      const lg = ledgerStrip(); if (lg) m.append(lg);
    }
    if (!w.length && !d.length) {
      const e = el('div', 'empty');
      const r = el('div', 'radar tiny'); r.append(el('i'), el('i'), el('i'), el('b'));
      e.append(r, el('b', null, 'Nothing needs you.'));
      m.append(e);
    }
    if (w.length) { m.append(section('Needs you \u00b7 longest first', w.length)); w.forEach((a, i) => m.append(itemNode(a, 'waiting', false, i))); }
    if (d.length) { m.append(section('Finished, not yet looked at', d.length)); d.forEach((a, i) => m.append(itemNode(a, 'waiting', false, i))); }
    if (ds.length) { m.append(section('Finished, looked at', ds.length)); ds.forEach((a, i) => m.append(itemNode(a, 'waiting', false, i))); }
    if (wk.length) { m.append(section('Working', wk.length)); wk.forEach((a, i) => m.append(itemNode(a, 'waiting', false, i))); }
    if (sl.length) {
      const h = el('div', 'sec secrow');
      const t = el('span', null, 'Sleeping '); t.append(el('b', null, '(' + sl.length + ')')); h.append(t);
      if (S.q.writes) h.append(btn('Wake\u2026', 'mini-btn', openWake));
      m.append(h);
      sl.forEach((a, i) => m.append(itemNode(a, 'waiting', false, i)));
    }
    // Projects with no agent running at all (a terminal sitting at a shell prompt). One row, because there can be
    // dozens; the picker behind it is searchable and puts the most recently used first.
    const asleep = (S.q.hosts || []).filter((h) => !S.host || h.id === S.host).reduce((n, h) => n + (h.asleep || 0), 0);
    if (asleep && S.q.writes) {
      const h = el('div', 'sec secrow'); h.id = 'asleeprow';
      const t = el('span', null, 'Asleep '); t.append(el('b', null, '(' + asleep + ')')); h.append(t);
      h.append(btn('Wake\u2026', 'mini-btn', openAsleep));
      m.append(h);
    }
  } else if (S.tab === 'all') {
    m.append(radarNode(agents().filter(inHost)));   // the Queue already lists every session by state; this shows the fleet at a glance
  } else if (S.tab === 'subs') {
    const root = el('div', 'subs' + (BUD.fresh ? ' enter' : '')); root.id = 'budget'; m.append(root);
    if (BUD.fresh) { BUD.fresh = false; setTimeout(() => root.classList.remove('enter'), 2600); }
    paintBudget(); fetchBudget(false);
  } else if (S.tab === 'hosts') {
    paintHostsView(m);
  } else {
    paintSettingsView(m);
  }
  m.scrollTop = keep;
}

$('refresh').addEventListener('click', () => { fetchQueue(true); if (S.tab === 'hosts' || S.tab === 'settings') loadHosts(); });

// Pull down from the top of any list to refresh. Reads only: it never writes, and it never navigates.
(function pullToRefresh() {
  const m = $('main'), ind = el('div', 'ptr'); ind.setAttribute('aria-hidden', 'true');
  ind.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5"/></svg>';   // constant markup, never data
  document.body.append(ind);
  const LIMIT = 70;
  let y0 = null, pull = 0;
  const paint = (p, spin) => {
    ind.style.top = (m.getBoundingClientRect().top + 6) + 'px';
    ind.style.opacity = p ? Math.min(1, p / LIMIT) : 0;
    ind.style.transform = 'translateY(' + Math.min(p, LIMIT) * 0.6 + 'px) rotate(' + (p * 4) + 'deg)';
    ind.classList.toggle('spin', !!spin);
  };
  m.addEventListener('touchstart', (e) => {
    y0 = (m.scrollTop <= 0 && !S.sheetOpen && !S.cur && e.touches.length === 1) ? e.touches[0].clientY : null; pull = 0;
  }, { passive: true });
  m.addEventListener('touchmove', (e) => {
    if (y0 == null) return;
    pull = Math.max(0, e.touches[0].clientY - y0);
    if (m.scrollTop > 0) { y0 = null; paint(0); return; }
    paint(pull);
  }, { passive: true });
  const end = () => {
    if (y0 == null) return;
    const go = pull >= LIMIT; y0 = null;
    if (!go) { paint(0); return; }
    paint(LIMIT, true);
    Promise.all([fetchQueue(true), (S.tab === 'hosts' || S.tab === 'settings') ? loadHosts() : null]).finally(() => setTimeout(() => paint(0), 400));
  };
  m.addEventListener('touchend', end, { passive: true });
  m.addEventListener('touchcancel', () => { y0 = null; paint(0); }, { passive: true });
})();

// ---- subscriptions (the Subs tab). Burn Bar measures each plan on the host; this lists all of them at once. Budget means an even spend
// across the plan's window: if you have used less than the clock has, the difference is BANKED; more, and you are BEHIND, with a
// countdown to when the clock catches up if you stop. Same rules as Burn Bar's own panel, so the two always agree.
const BUD = { data: null, at: 0, busy: false, ticks: [], err: '', fresh: true };
const serverNow = () => Date.now() + (S.skew || 0);   // the host's clock: the resets are in its time
const two = (n) => String(n).padStart(2, '0');
function clockSpan(ms) { const t = Math.max(0, Math.floor(ms / 1000)), d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600), m = Math.floor(t % 3600 / 60), sec = t % 60; return (d ? d + 'd ' + two(h) : String(h)) + ':' + two(m) + ':' + two(sec); }
function spanWords(ms) { const m = Math.round(ms / 60000), d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60); return d ? d + 'd ' + h + 'h' : h ? h + 'h ' + (m % 60) + 'm' : m + 'm'; }
// Where one window stands right now. Pure: the clock keeps moving between host readings, so elapsed comes from the reset and `now`.
function paceLive(used, resetsMs, windowMs, now) {
  if (!(used >= 0) || !(resetsMs > 0) || !(windowMs > 0) || !(now > 0) || now >= resetsMs) return null;
  const p = Math.min(1, used), left = resetsMs - now, e = Math.max(0, Math.min(1, 1 - left / windowMs));
  const o = { used: p, elapsed: e, leftMs: left, banked: 0, bankedMs: 0, behind: 0, over: false, comeBackMs: 0, spent: p >= 0.995 };
  if (o.spent) { o.behind = Math.max(0, p - e); o.over = true; o.comeBackMs = left; }
  else if (e > p) { o.banked = e - p; o.bankedMs = o.banked * windowMs; }
  else if (p > e) { o.behind = p - e; o.over = o.behind > 0.01 && (e <= 0 || p / e > 1.05); o.comeBackMs = Math.max(0, Math.min(resetsMs, resetsMs - windowMs * (1 - p)) - now); }
  return o;
}
function budgetVerdict(live, rough) {
  if (!live) return { tone: 'dim', word: 'NO PACE YET', line: 'Not enough of this window has passed to judge.' };
  const t = rough ? '~' : '';
  if (live.spent) return { tone: 'hot', word: 'SPENT', line: 'Back at the reset in ' + clockSpan(live.leftMs) };
  if (live.over) return { tone: 'hot', word: 'BEHIND', line: 'Come back in ' + clockSpan(live.comeBackMs) + ' if you stop · ' + t + Math.round(live.behind * 100) + '% over' };
  if (live.banked >= 0.01) return { tone: 'ok', word: 'BANKED ' + t + Math.round(live.banked * 100) + '%', line: clockSpan(live.bankedMs) + ' in the bank' };
  return { tone: 'accent', word: 'ON PACE', line: 'Even with the clock. Carry on.' };
}
// Which subscription to reach for, by Burn Bar's rules: only one that is AHEAD of an even spend and not blocked by a full 5-hour window; a snapshot
// (Grok) needs a bigger margin because it is not re-measured; the winner has the most plan left against the clock it has left, which compares a
// week and a month on one scale and becomes earliest-deadline-first as a reset nears. Needs two subscriptions with data, or there is nothing to choose.
function budgetGuidance(agents, now) {
  const rows = agents.map((ag) => {
    const rs = ag.rows.slice().sort((x, y) => (y.window_ms - x.window_ms) || (y.used - x.used)), prim = rs[0]; if (!prim) return null;
    const live = paceLive(prim.used, prim.resets_ms, prim.window_ms, now); if (!live) return null;
    const short = rs.find((r) => r.window_ms > 0 && r.window_ms <= 6 * 3600000 && r.used >= 0.95);
    return { ag, prim, live, rough: !ag.live, short, room: live.elapsed < 1 ? (1 - live.used) / (1 - live.elapsed) : 0 };
  }).filter(Boolean);
  if (rows.length < 2) return null;
  const resting = rows.filter((r) => r.live.over || r.live.spent || r.short).map((r) => ({ name: r.ag.name, ms: r.short ? Math.max(0, r.short.resets_ms - now) : r.live.comeBackMs, spent: r.live.spent }));
  const ok = rows.filter((r) => !r.live.spent && !r.live.over && !r.short && r.live.banked >= (r.rough ? 0.25 : 0.05)).sort((x, y) => y.room - x.room);
  if (!ok.length) return { kind: 'none', rest: resting };
  const w = ok[0];
  return { kind: w.live.leftMs <= 24 * 3600000 && w.live.banked >= 0.10 ? 'now' : 'next', id: w.ag.id, name: w.ag.name, rough: w.rough, banked: w.live.banked, bankedMs: w.live.bankedMs, leftMs: w.live.leftMs,
           span: w.prim.window_ms >= 20 * 86400000 ? 'month' : w.prim.window_ms >= 5 * 86400000 ? 'week' : 'window', rest: resting };
}
function subGuide(d) {
  const box = el('div', 'sg'), t = el('div', 'sg-t'), w = el('div', 'sg-w'); box.append(t, w);
  const paint = () => {
    const g = budgetGuidance(d.agents, serverNow()); box.hidden = !g; if (!g) return;
    const tag = g.rough ? '~' : '';
    box.dataset.tone = g.kind === 'none' ? 'hot' : g.kind === 'now' ? 'warn' : 'ok';
    box.dataset.id = g.id || '';
    t.textContent = g.kind === 'none' ? 'NOTHING HAS ROOM' : 'USE ' + g.name.toUpperCase() + (g.kind === 'now' ? ' NOW' : ' NEXT');
    w.textContent = g.kind === 'none' ? 'Every subscription is at or past an even spend.'
      : (g.kind === 'now' ? 'Use it or lose it: ' : '') + tag + Math.round(g.banked * 100) + '% of its ' + g.span + ' banked \u00b7 ' + spanWords(g.bankedMs) + ' ahead \u00b7 resets in ' + spanWords(g.leftMs);
  };
  paint(); BUD.ticks.push(paint);
  return box;
}
function budgetRowName(l) { return l.replace(/\s*\(([^)]*)\)/, ' $1').replace(/\bWeekly 7-day\b/, 'Week').replace(/\bSession 5-hour\b/, '5-hour session').replace(/\bMonthly total\b/, 'Month'); }
const GW = 260, GH = 50;
function budgetGraph(row, live, tone, gid) {
  const NS = 'http://www.w3.org/2000/svg', mk = (t, a) => { const n = document.createElementNS(NS, t); Object.keys(a || {}).forEach((k) => n.setAttribute(k, a[k])); return n; };
  const X0 = 4, X1 = GW - 4, Y0 = GH - 4, Y1 = 4, X = (e) => (X0 + Math.max(0, Math.min(1, e)) * (X1 - X0)).toFixed(1), Y = (u) => (Y0 - Math.max(0, Math.min(1, u)) * (Y0 - Y1)).toFixed(1);
  const svg = mk('svg', { viewBox: '0 0 ' + GW + ' ' + GH, class: 'bd-svg', preserveAspectRatio: 'none', role: 'img', 'aria-label': 'Burn-down: plan used against the clock' });
  const col = 'var(--' + tone + ')';
  const defs = mk('defs'), grad = mk('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(mk('stop', { offset: '0%', 'stop-color': col, 'stop-opacity': '.55' }), mk('stop', { offset: '100%', 'stop-color': col, 'stop-opacity': '0' })); defs.append(grad); svg.append(defs);
  [0.25, 0.5, 0.75].forEach((u) => svg.append(mk('line', { x1: X0, x2: X1, y1: Y(u), y2: Y(u), class: 'bd-grid' })));
  svg.append(mk('line', { x1: X0, y1: Y(0), x2: X1, y2: Y(1), class: 'bd-even' }));   // an even spend is the diagonal; above it is over
  const pts = (row.series || []).filter((q) => q[0] <= (live ? live.elapsed : 1) + 0.0001).map((q) => [q[0], q[1]]);
  if (live) pts.push([live.elapsed, live.used]);
  if (pts.length >= 2) {
    const line = pts.map((q, i) => (i ? 'L' : 'M') + X(q[0]) + ' ' + Y(q[1])).join(' ');
    svg.append(mk('path', { d: line + ' L' + X(pts[pts.length - 1][0]) + ' ' + Y(0) + ' L' + X(pts[0][0]) + ' ' + Y(0) + ' Z', fill: 'url(#' + gid + ')', class: 'bd-area' }));
    svg.append(mk('path', { d: line, class: 'bd-line', stroke: col, pathLength: '1' }));
  }
  if (live) {
    svg.append(mk('line', { x1: X(live.elapsed), x2: X(live.elapsed), y1: Y1, y2: Y0, class: 'bd-now' }));
    if (row.projected > 0 && live.elapsed < 0.995) {   // where the current rate lands at the reset
      const pj = Math.min(1.15, row.projected), over = row.projected > 1;
      svg.append(mk('line', { x1: X(live.elapsed), y1: Y(live.used), x2: X1, y2: Y(pj), class: 'bd-proj' + (over ? ' over' : '') }));
    }
    svg.append(mk('circle', { cx: X(live.elapsed), cy: Y(live.used), r: 5, fill: 'none', stroke: col, 'stroke-width': 1.5, class: 'bd-ping' }), mk('circle', { cx: X(live.elapsed), cy: Y(live.used), r: 3.4, fill: col, stroke: '#05080c', 'stroke-width': 1.2 }));
  }
  return svg;
}
// A number that climbs to its value, once, when the page arrives. Reduce Motion goes straight to the answer.
function countUp(node, to, prefix, ms) {
  if (REDUCED) { node.textContent = prefix + to; return; }
  const t0 = performance.now();
  (function step(t) { const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3); node.textContent = prefix + Math.round(to * e); if (k < 1) requestAnimationFrame(step); })(t0);
}
// One subscription, everything visible: the verdict, the burn-down, the clock and every window as a bar with a marker for the clock.
function subCard(ag, i, enter) {
  const rows = ag.rows.slice().sort((a, b) => (b.window_ms - a.window_ms) || (b.used - a.used));
  const prim = rows[0], rough = !ag.live;
  const card = el('div', 'sc'); card.dataset.id = ag.id; card.style.setProperty('--i', i);
  const head = el('div', 'sc-h'), nm = el('span', 'sc-n', ag.name), vd = el('span', 'sc-v'); head.append(nm, vd); card.append(head);
  if (!prim) {   // installed but not measurable: say NO DATA, why in plain words, then what to do as its own dim line
    card.style.setProperty('--tone', 'var(--dim)'); vd.textContent = 'NO DATA';
    const why = ag.status || 'No plan limits reported.', fix = /\s/.test(ag.help || '') ? ag.help : '';   // a help text is a sentence; a bare token is a code, not advice
    card.append(el('div', 'sc-l wrap plain', why)); if (fix) card.append(el('div', 'sc-note fix', fix));
    return card;
  }
  const mid = el('div', 'sc-m'), big = el('div', 'sc-pct'), num = el('span', 'sc-num'), gwrap = el('div', 'sc-g'), line = el('div', 'sc-l');
  big.append(num, el('small', null, '% used')); mid.append(big, gwrap); card.append(mid, line);
  const bars = rows.map((r) => { const w = el('div', 'sc-r'), nmx = el('span', 'sc-rn', budgetRowName(r.label)), tr = el('span', 'sc-bar'), fill = el('i'), tick = el('b'), tx = el('span', 'sc-rt'); tr.append(fill, tick); w.append(nmx, tr, tx); card.append(w); return { r, fill, tick, tx, w }; });
  const note = el('div', 'sc-note'); card.append(note);
  const gid = 'scg-' + ag.id, prefix = rough ? '~' : '';
  function paint(first) {
    const now = serverNow(), live = paceLive(prim.used, prim.resets_ms, prim.window_ms, now), v = budgetVerdict(live, rough);
    card.style.setProperty('--tone', 'var(--' + v.tone + ')'); card.dataset.tone = v.tone; vd.textContent = v.word; line.textContent = v.line;
    if (first) { if (enter) countUp(num, Math.round(prim.used * 100), prefix, 900); else num.textContent = prefix + Math.round(prim.used * 100); gwrap.textContent = ''; gwrap.append(budgetGraph(prim, live, v.tone, gid)); }
    bars.forEach((x) => { const lv = paceLive(x.r.used, x.r.resets_ms, x.r.window_ms, now), sv = budgetVerdict(lv, rough); x.fill.style.width = Math.round(Math.min(1, x.r.used) * 100) + '%'; x.tick.style.left = lv ? Math.round(lv.elapsed * 100) + '%' : '0%'; x.tick.hidden = !lv; x.w.style.setProperty('--tone', 'var(--' + sv.tone + ')'); x.tx.textContent = Math.round(x.r.used * 100) + '% · ' + sv.word.toLowerCase(); });
    const age = ag.measured_at ? now - ag.measured_at : 0;
    note.textContent = rough ? 'A snapshot from ' + (ag.measured_at ? new Date(ag.measured_at).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : 'when it last started') + ', so the numbers are rough.'
      : (age > 20 * 60000 ? 'Last measured ' + spanWords(age) + ' ago.' : '');
    note.hidden = !note.textContent;
  }
  paint(true); BUD.ticks.push(() => paint(false));
  return card;
}
function paintBudget() {
  const host = $('budget'); if (!host) return;
  BUD.ticks = []; host.textContent = '';
  const d = BUD.data, enter = host.classList.contains('enter');
  if (!d) { host.append(el('div', 'bd-empty', BUD.err ? 'Subscription budget unavailable: ' + BUD.err : 'Reading your subscriptions…')); return; }
  if (!d.available) { host.append(el('div', 'bd-empty', d.reason || 'No subscription data on this host.')); return; }
  host.append(subGuide(d));
  d.agents.forEach((ag, i) => host.append(subCard(ag, i + 1, enter)));
}
async function fetchBudget(force) {
  if (BUD.busy || !S.dev || (!force && Date.now() - BUD.at < (BUD.data && BUD.data.available ? 20000 : 3000))) return;
  BUD.busy = true;
  try { BUD.data = await api('api/budget'); BUD.at = Date.now(); BUD.err = ''; } catch (e) { BUD.err = e.message || 'failed'; } finally { BUD.busy = false; }
  paintBudget();
}
setInterval(() => { if (S.tab === 'subs' && !document.hidden && $('detail').hidden && !S.sheetOpen) fetchBudget(true); }, 30000);
setInterval(() => { if (S.tab === 'subs' && !document.hidden && $('detail').hidden) BUD.ticks.forEach((f) => f()); }, 1000);

// ---- the fleet radar. Centre = needs you. Rings outward: waiting, finished, working, idle. Each host owns a sector. Blocked
// dots pulse and swell the longer they wait. Tap a dot to open that session. Every part of it carries a state.
const RAD = { cx: 180, cy: 180, ring: { blocked: 62, done: 98, working: 134, idle: 166 } };
function radarNode(list) {
  list = list.map((a) => (isSeen(a) ? Object.assign({}, a, { status: 'idle' }) : a));   // looked at: no longer "to read"
  const NS = 'http://www.w3.org/2000/svg', mk = (t, a, txt) => { const n = document.createElementNS(NS, t); Object.keys(a || {}).forEach((k) => n.setAttribute(k, a[k])); if (txt != null) n.textContent = txt; return n; };
  const wrap = el('div', 'radarwrap');
  const svg = mk('svg', { viewBox: '-14 -14 388 388', class: 'radar', role: 'group', 'aria-label': 'Fleet radar' });
  Object.entries(RAD.ring).forEach(([st, r]) => { svg.append(mk('circle', { cx: RAD.cx, cy: RAD.cy, r, class: 'rring' })); svg.append(mk('text', { x: RAD.cx + 4, y: RAD.cy - r + 10, class: 'rringlbl' }, st === 'blocked' ? 'WAITING' : st.toUpperCase())); });
  for (let a = 0; a < 360; a += 30) { const t = a * Math.PI / 180; svg.append(mk('line', { x1: RAD.cx, y1: RAD.cy, x2: RAD.cx + 176 * Math.sin(t), y2: RAD.cy - 176 * Math.cos(t), class: 'rspoke' })); }
  const sweep = mk('g', { class: 'rsweep' }); sweep.append(mk('path', { d: 'M180 180 L180 4 A176 176 0 0 1 305 55 Z', class: 'rwedge' })); svg.append(sweep);
  const hosts = [...new Set(list.map((a) => a.host))].sort();
  const n = Math.max(1, hosts.length), span = 2 * Math.PI / n;
  const dots = [];
  hosts.forEach((h, hi) => {
    const a0 = hi * span;
    if (n > 1) { const t = a0; svg.append(mk('line', { x1: RAD.cx, y1: RAD.cy, x2: RAD.cx + 178 * Math.sin(t), y2: RAD.cy - 178 * Math.cos(t), class: 'rsector' })); }
    const mid = a0 + span / 2;
    svg.append(mk('text', { x: RAD.cx + 188 * Math.sin(mid), y: RAD.cy - 188 * Math.cos(mid) + 4, class: 'rhost', 'text-anchor': Math.sin(mid) > 0.3 ? 'end' : Math.sin(mid) < -0.3 ? 'start' : 'middle' }, h));
    ['blocked', 'done', 'working', 'idle'].forEach((st, ri) => {
      const grp = list.filter((a) => a.host === h && (a.status === st || (st === 'idle' && !['blocked', 'done', 'working'].includes(a.status))));
      grp.forEach((a, k) => {
        // Each ring is shifted by a different fraction of the gap between its dots, so dots on neighbouring rings do not line up on one spoke.
        const f = (k + 0.5 + ([0, 0.5, 0.25, 0.75][ri] - 0.375) * 0.6) / grp.length, ang = a0 + span * (0.12 + 0.76 * Math.min(1, Math.max(0, f))), r = RAD.ring[st];
        dots.push({ a, st, ang, x: RAD.cx + r * Math.sin(ang), y: RAD.cy - r * Math.cos(ang) });
      });
    });
  });
  // Every dot carries its name, and no two names, dots or ring captions overlap: each label tries the right, left, above, below and the four corners
  // of its dot, in that order, and takes the first free one. The loudest states choose first.
  const big = (d) => d.st === 'blocked' || d.st === 'done';
  const nameOf = (d) => { const max = big(d) ? 12 : 10, t = nm(d.a); return t.length > max ? t.slice(0, max - 1) + '…' : t; };
  const keepOut = [{ x0: 144, y0: 146, x1: 216, y1: 208 }];   // the number in the middle
  Object.values(RAD.ring).forEach((r) => keepOut.push({ x0: RAD.cx + 2, y0: RAD.cy - r + 1, x1: RAD.cx + 52, y1: RAD.cy - r + 14 }));   // IDLE / WORKING / DONE / WAITING
  const placed = [], hit = (b, c, pad) => b.x0 < c.x1 + pad && b.x1 > c.x0 - pad && b.y0 < c.y1 + pad && b.y1 > c.y0 - pad;
  const dotBox = (d) => ({ x0: d.x - 8, y0: d.y - 8, x1: d.x + 8, y1: d.y + 8 });
  const order = { blocked: 0, done: 1, working: 2, idle: 3 };
  dots.slice().sort((p, q) => order[p.st] - order[q.st]).forEach((d) => {
    const txt = nameOf(d), bg = big(d), w = txt.length * (bg ? 6.6 : 5.6), h = bg ? 12 : 11, pad = bg ? 13 : 10, right = Math.sin(d.ang) >= 0;
    const at = (ax, ay, anchor) => ({ anchor, x: ax, y: ay, box: { x0: anchor === 'start' ? ax : anchor === 'end' ? ax - w : ax - w / 2, x1: anchor === 'start' ? ax + w : anchor === 'end' ? ax : ax + w / 2, y0: ay - h + 3, y1: ay + 3 } });
    const cands = [at(d.x + pad, d.y + 4, 'start'), at(d.x - pad, d.y + 4, 'end'), at(d.x, d.y - pad + 1, 'middle'), at(d.x, d.y + pad + h - 2, 'middle'),
                   at(d.x + pad - 3, d.y - pad + 4, 'start'), at(d.x - pad + 3, d.y - pad + 4, 'end'), at(d.x + pad - 3, d.y + pad + 6, 'start'), at(d.x - pad + 3, d.y + pad + 6, 'end')];
    [-13, 13, -26, 26].forEach((dy) => { cands.push(at(d.x + pad, d.y + 4 + dy, 'start'), at(d.x - pad, d.y + 4 + dy, 'end')); });   // a row up or down, when the side is taken
    [-34, 34].forEach((dx) => { cands.push(at(d.x + dx, d.y - pad + 1, 'middle'), at(d.x + dx, d.y + pad + h - 2, 'middle')); });
    if (!right) { const t = cands[0]; cands[0] = cands[1]; cands[1] = t; }
    const score = (c) => {
      let s = 0; const b = c.box;
      if (b.x0 < -12 || b.x1 > 372 || b.y0 < -12 || b.y1 > 372) s += 1000;
      keepOut.forEach((k) => { if (hit(b, k, 1)) s += 300; });
      dots.forEach((o) => { if (o !== d && hit(b, dotBox(o), 1)) s += 300; });
      placed.forEach((p) => { if (hit(b, p, 2)) s += 400; });
      return s;
    };
    let best = cands[0], bs = Infinity;
    for (const c of cands) { const s = score(c); if (s < bs) { best = c; bs = s; if (s === 0) break; } }
    placed.push(best.box); d.label = { txt, best, bg };
  });
  dots.forEach((d) => {
    const { a, st } = d, g = mk('g', { class: 'rdot ' + st, tabindex: '0', role: 'button', 'aria-label': nm(a) + ', ' + (STATE_LABEL[a.status] || a.status) });
    g.dataset.key = keyOf(a);
    if (st === 'blocked') {
      const mins = Math.min(240, Math.round((a.for_s || 0) / 60));
      g.append(mk('circle', { cx: d.x, cy: d.y, r: 9 + Math.min(9, mins / 8), class: 'rpulse' }));
      g.append(mk('circle', { cx: d.x, cy: d.y, r: 7 + Math.min(7, mins / 12), class: 'rcore' }));
    } else g.append(mk('circle', { cx: d.x, cy: d.y, r: st === 'done' ? 6 : 4.5, class: 'rcore' }));
    g.append(mk('circle', { cx: d.x, cy: d.y, r: 17, class: 'rhit' }));
    g.append(mk('text', { x: d.label.best.x, y: d.label.best.y, class: 'rlbl' + (d.label.bg ? '' : ' sm'), 'text-anchor': d.label.best.anchor }, d.label.txt));
    gesture(g, { tap: () => openDetail(a.host, a.pane, 'all') });
    svg.append(g);
  });
  const waiting = list.filter((a) => a.status === 'blocked').length;
  const centre = mk('g', { class: 'rcentre', role: 'button', tabindex: '0', 'aria-label': 'Open what is waiting on you' });
  centre.append(mk('circle', { cx: RAD.cx, cy: RAD.cy, r: 38, class: 'rhit' }));
  gesture(centre, { tap: () => nextWaiting() });
  svg.append(centre);
  svg.append(mk('text', { x: RAD.cx, y: RAD.cy + 8, class: 'rnum' + (waiting ? ' hot' : ''), 'text-anchor': 'middle' }, String(waiting)));
  svg.append(mk('text', { x: RAD.cx, y: RAD.cy + 26, class: 'rcap', 'text-anchor': 'middle' }, waiting === 1 ? 'WAITING' : 'WAITING'));
  // The numbers above the radar: also its legend (each tile is the colour of its dots), and how long the longest wait has been.
  const by = (st) => list.filter((a) => a.status === st), longest = Math.max(0, ...by('blocked').map((a) => a.for_s || 0));
  const stats = el('div', 'rstats');
  [['blocked', 'Waiting', longest ? 'longest ' + fmtAge(longest, true).main : 'nothing'], ['done', 'Finished', 'to read'], ['working', 'Working', 'busy now'], ['idle', 'Idle', 'free']].forEach(([st, lbl, sub], k) => {
    const t = el('div', 'rstat ' + st), n = el('b', null, String(by(st).length)); t.style.setProperty('--k', k);
    t.append(n, el('span', 'rl', lbl), el('span', 'rs', sub)); stats.append(t);
  });
  wrap.append(stats);
  wrap.append(svg);
  if (!list.length) wrap.append(el('div', 'empty', 'No sessions.'));
  return wrap;
}

// ---- the waiting ledger: how long agents sat blocked on you. A slim strip under Start new, and a sheet with the detail.
const fmtDur = (s) => { s = Math.round(s); if (s < 60) return '<1m'; const m = Math.floor(s / 60); return m < 60 ? m + 'm' : Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm'; };
function loadLedger() {
  return api('api/ledger').then((l) => { S.ledger = l; S.ledgerAt = Date.now(); if (S.tab === 'waiting' && !S.sheetOpen && !S.cur) paintMain(); }).catch(() => { /* a missing ledger just hides the strip */ });
}
function ledgerBars(days, w, h, gap) {
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h); svg.setAttribute('aria-hidden', 'true');
  const max = Math.max(1, ...days.map((d) => d.secs)), bw = (w - gap * (days.length - 1)) / days.length;
  days.forEach((d, i) => {
    const bh = d.secs ? Math.max(2, Math.round((h - 2) * d.secs / max)) : 2;
    const r = document.createElementNS(NS, 'rect');
    r.setAttribute('x', String(i * (bw + gap))); r.setAttribute('y', String(h - bh)); r.setAttribute('width', String(bw)); r.setAttribute('height', String(bh)); r.setAttribute('rx', '1.5');
    r.setAttribute('class', i === days.length - 1 ? 'today' : 'past');
    svg.append(r);
  });
  return svg;
}
function ledgerStrip() {
  const L = S.ledger;
  if (!L || !(L.today.n || L.now.n || L.days.some((d) => d.n))) return null;
  const b = btn('', 'ledger', openLedger); b.id = 'ledger';
  const t = el('div', 'lt');
  t.append(el('div', 'l1', L.now.n ? 'WAITING NOW  ' + L.now.n + ' · ' + fmtDur(L.now.secs) : 'TODAY  ' + L.today.n + (L.today.n === 1 ? ' wait' : ' waits') + ' · ' + fmtDur(L.today.secs)),
           el('div', 'l2', L.now.n ? 'today so far: ' + L.today.n + (L.today.n === 1 ? ' wait' : ' waits') + ' · ' + fmtDur(L.today.secs) : (L.today.longest ? 'longest ' + fmtDur(L.today.longest.secs) + ' · ' + L.today.longest.label : 'nothing waited on you yet')));
  if (L.now.n) t.firstChild.classList.add('hot');
  b.append(t, ledgerBars(L.days, 70, 28, 4));
  return b;
}
function openLedger() {
  const L = S.ledger; if (!L) return;
  const box = el('div');
  box.append(el('h3', null, 'Waiting on you'), el('div', 'sub', 'How long agents sat blocked on a question. Only waits Shepr watched from the first moment to the last are counted.'));
  const chart = el('div', 'lchart');
  const names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const max = Math.max(1, ...L.days.map((d) => d.secs));
  L.days.forEach((d, i) => {
    const col = el('div', 'lcol' + (i === L.days.length - 1 ? ' today' : ''));
    const bar = el('div', 'lbar'); bar.style.height = (d.secs ? Math.max(4, Math.round(70 * d.secs / max)) : 2) + 'px';
    col.append(el('div', 'ln', d.n ? String(d.n) : ''), bar, el('div', 'ld', i === L.days.length - 1 ? 'today' : names[new Date(d.d + 'T12:00:00').getDay()]), el('div', 'lt2', d.secs ? fmtDur(d.secs) : ''));
    chart.append(col);
  });
  box.append(chart);
  if (L.now.n) box.append(el('div', 'warnbox', L.now.n + (L.now.n === 1 ? ' agent is' : ' agents are') + ' waiting on you right now, ' + fmtDur(L.now.secs) + ' in all.'));
  if (L.today.top.length) {
    box.append(el('div', 'sec', 'Longest waits today'));
    const list = el('div', 'devbox');
    L.today.top.forEach((w) => { const r = el('div', 'hrow'); const m = el('div', 'meta'); m.append(el('div', 'ttl', w.label), el('div', 'sub', w.host)); r.append(m, el('b', 'ldur', fmtDur(w.secs))); list.append(r); });
    box.append(list);
  }
  const row = el('div', 'btns'); row.style.marginTop = '8px'; row.append(btn('Close', '', closeSheet)); box.append(row);
  openSheet(box);
}

// ---- notifications: the host pushes "something changed", the service worker asks what and shows it
const pushOK = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
const keyBytes = (b64) => { const s = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - b64.length % 4) % 4)); return Uint8Array.from(s, (c) => c.charCodeAt(0)); };
function loadPush() {
  if (S.pushLoading) return Promise.resolve();
  S.pushLoading = true;
  return api('api/push/config').then((c) => { S.push = c; if (S.tab === 'settings') paintMain(); }).catch(() => { /* the banner says what is wrong */ })
    .finally(() => { S.pushLoading = false; });
}
async function pushOn() {
  try {
    if (!S.push) await loadPush();
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { notify('Notifications are blocked for Shepr. Turn them on in the iPhone Settings, under Notifications.', true); paintMain(); return; }
    const reg = await navigator.serviceWorker.ready;
    const old = await reg.pushManager.getSubscription();
    if (old) await old.unsubscribe();   // a subscription is tied to one host key: always start fresh
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(S.push.key) });
    S.push = await post('api/push/subscribe', { subscription: sub.toJSON(), prefs: { blocked: true, done: false } });
    notify('Notifications are on.', false);
  } catch (e) { notify('Could not turn notifications on: ' + e.message, true); }
  paintMain();
}
async function pushOff() {
  try {
    const reg = await navigator.serviceWorker.ready; const s = await reg.pushManager.getSubscription(); if (s) await s.unsubscribe();
    S.push = await post('api/push/unsubscribe', {});
    if (navigator.clearAppBadge) navigator.clearAppBadge().catch(() => {});
  } catch (e) { notify(e.message, true); }
  paintMain();
}
function pushPref(k, v) { post('api/push/prefs', Object.assign({}, S.push.prefs, { [k]: v })).then((c) => { S.push = c; paintMain(); }).catch((e) => notify(e.message, true)); }
function notifyCard() {
  const c = el('div', 'card'); c.id = 'notifycard';
  c.append(el('h2', null, 'Notifications'));
  if (!S.push && S.dev) loadPush();
  if (!pushOK()) { c.append(el('p', 'dim', 'iPhone only allows notifications for apps on the Home Screen. Tap Share, then Add to Home Screen, and open Shepr from there.')); return c; }
  if (typeof Notification !== 'undefined' && Notification.permission === 'denied') { c.append(el('p', 'dim', 'Notifications are blocked for Shepr. Turn them on in the iPhone Settings, under Notifications, then come back.')); return c; }
  if (!S.push || !S.push.subscribed) {
    c.append(el('p', 'dim', 'Get a buzz when an agent is waiting on you, so you do not find out an hour late. The host only says "something changed"; this phone then asks it what, so nothing about your sessions passes through Apple.'));
    const b = btn('Turn on notifications', 'mini-btn', pushOn); b.id = 'pushon'; c.append(b);
    return c;
  }
  const row = (label, key) => { const r = el('label', 'chk'); const cb = el('input'); cb.type = 'checkbox'; cb.checked = !!S.push.prefs[key]; cb.addEventListener('change', () => pushPref(key, cb.checked));
    const m = el('div', 'meta'); m.append(el('div', 'ttl', label)); r.append(cb, m); return r; };
  c.append(row('An agent is waiting on me', 'blocked'), row('An agent finished', 'done'));
  if (S.push.last) {   // what the push service said to the last push: "accepted" means Apple has it, so a silent phone is a phone setting
    const L = S.push.last, ok = L.code >= 200 && L.code < 300;
    c.append(el('p', 'dim', 'Last push ' + new Date(L.at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) + ' (' + L.why + '): Apple answered ' + L.code + (ok ? ', so it reached Apple. If nothing showed, check Focus (the moon or bed icon) and Settings, Notifications, Shepr.' : '. It did not go through.')));
  }
  const acts = el('div', 'btns'); acts.style.marginTop = '8px';
  acts.append(btn('Send a test', '', () => post('api/push/test', {}).then((r) => { notify(r.ok ? 'Apple accepted it. If nothing appears in a few seconds, check Focus and Settings, Notifications, Shepr.' : 'The push service answered ' + r.status + '.', !r.ok); loadPush(); }).catch((e) => notify(e.message, true))), btn('Turn off', '', pushOff));
  c.append(acts);
  return c;
}
function updateBadge() {
  if (!('setAppBadge' in navigator)) return;
  const n = waiting().length;
  (n ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => { /* needs notification permission */ });
}
if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', (e) => { if (e.data && e.data.type === 'open' && e.data.host && S.dev) openDetail(e.data.host, e.data.pane, 'all'); });

// ---------------------------------------------------------------------------
// 8. Hosts and keys views
// ---------------------------------------------------------------------------
let hostsTimer = null, hostsBusy = false;
function loadHosts() {
  if (hostsBusy) return Promise.resolve();
  hostsBusy = true;
  clearTimeout(hostsTimer);
  return api('api/hosts').then((h) => {
    S.hosts = h;
    if (S.tab === 'hosts' || S.tab === 'settings') paintMain();
    // Keep watching a running scan, but only while you are looking at it, and only ever one timer.
    if (h.scan && h.scan.running && (S.tab === 'hosts' || S.tab === 'settings')) hostsTimer = setTimeout(loadHosts, 1500);
  }).catch(() => { /* the banner already says what is wrong */ })
    .finally(() => { hostsBusy = false; });
}

function copyText(text, what) {
  const done = () => notify('Copied ' + (what || 'it') + '.', false);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const t = el('textarea'); t.value = text; t.style.cssText = 'position:fixed;opacity:0;left:-1000px';
  document.body.append(t); t.select();
  try { document.execCommand('copy'); done(); } catch (e) { notify('Could not copy: select it by hand.', true); }
  t.remove();
}

function hostRow(h) {
  const r = el('div', 'hrow');
  const m = el('div', 'meta');
  const counts = Object.keys(h.counts || {}).map((k) => h.counts[k] + ' ' + k).join(', ');
  m.append(el('div', 'ttl', h.id + (h.kind === 'local' ? ' · this machine' : '')),
           el('div', 'sub' + (h.status !== 'ok' && h.enabled ? ' bad' : ''), h.status === 'ok' ? (counts || 'no sessions') : !h.enabled ? 'saved, switched off'
             : ((h.error || 'not answering').slice(0, 90) + (h.last_ok ? '  \u00b7  last seen ' + agoText(h.last_ok) : '  \u00b7  not seen since Shepr started'))));
  r.append(m);
  const st = h.kind === 'local' ? 'ok' : !h.enabled ? 'off' : h.status === 'ok' ? 'ok' : 'bad';
  r.append(el('span', 'pill ' + (st === 'ok' ? 'ok' : st === 'bad' ? 'bad' : ''), st === 'ok' ? 'on' : st === 'off' ? 'off' : 'down'));
  if (h.kind === 'remote' && S.hosts && S.hosts.writes) {
    r.append(btn(h.enabled ? 'Turn off' : 'Turn on', 'mini-btn' + (h.enabled ? ' warn' : ''),
      () => prepare({ kind: h.enabled ? 'host_disable' : 'host_enable', id: h.id })));
  }
  return r;
}

const PEER_STATE = {
  ready: ['ready', 'ok'], nokey: ['needs key', 'warn'], hostkey: ['first connect', 'warn'],
  noherdr: ['no herdr', ''], down: ['unreachable', 'bad'],
};
function peerRow(p) {
  const r = el('div', 'hrow');
  const m = el('div', 'meta');
  m.append(el('div', 'ttl', p.name), el('div', 'sub', p.os + ' · ' + p.ip + ' · ' + p.detail));
  const ps = PEER_STATE[p.state] || [p.state, ''];
  r.append(m, el('span', 'pill ' + ps[1], ps[0]));
  if (p.state === 'ready' && S.hosts.writes) r.append(btn('Add', 'mini-btn', () => prepare({ kind: 'host_add', name: p.name })));
  if (p.state === 'nokey' && S.hosts.enroll) {
    r.append(btn('Copy key', 'mini-btn warn', () => copyText(S.hosts.enroll, 'the one-line command. Paste it on ' + p.name)));
  }
  if (p.state === 'hostkey') r.append(btn('Copy', 'mini-btn warn', () => copyText('ssh ' + p.name, 'ssh ' + p.name)));
  return r;
}

function paintHostsView(m) {
  const H = S.hosts;
  if (!H) { m.append(el('div', 'empty', 'Loading hosts…')); return; }
  m.append(section('Hosts herdr can reach', H.saved.length));
  H.saved.forEach((h) => m.append(hostRow(h)));

  m.append(section('Find hosts on your tailnet'));
  const c = el('div', 'card hscan');
  const r = el('div', 'radar sm' + (H.scan.running ? ' scan' : '')); r.append(el('i'), el('i'), el('i'), el('b'));
  const side = el('div', 'side');
  side.append(el('p', 'dim', H.scan.running ? "Looking\u2026 trying ssh with this host's key, never prompting."
    : H.scan.at ? 'Scanned ' + clock(H.scan.at) + ': ' + H.scan.peers.length + ' of your machine' + (H.scan.peers.length === 1 ? '' : 's') + ' online, not yet added.'
    : 'Only machines that belong to you are tried. Shared-in nodes are never touched.'));
  const sb = btn(H.scan.running ? 'Scanning\u2026' : 'Scan tailnet', 'bigbtn', startScan);
  sb.disabled = H.scan.running || !H.writes;
  side.append(sb);
  c.append(r, side);
  m.append(c);
  H.scan.peers.forEach((p) => m.append(peerRow(p)));
  if (H.scan.peers.some((p) => p.state === 'nokey')) {
    const k = el('div', 'card');
    k.append(el('h2', null, 'Why "needs key"'));
    k.append(el('p', null, H.hub + " cannot ssh there yet. Copy the one-line command and run it once on that machine; it adds " + H.hub + "'s public key to authorized_keys. Then Scan again."));
    m.append(k);
  }
}

function startScan() {
  post('api/hosts/scan', {}).then(() => loadHosts())
    .catch((e) => notify(e.message, true));
}

// ---- Settings: four sections behind a bar so each one fits a screen (Law 17: the page never scrolls). Display is a set of
// choices kept on this phone; Alerts and Phones are the host's lists; About holds the build, and the two links sit at the bottom.
const SET_SEGS = [['display', 'Display'], ['alerts', 'Alerts'], ['phones', 'Phones'], ['about', 'About']];
let setSeg = store.get('shepr.setseg', 'display');
const REFRESH_NOTE = { fast: 'queue every 5 s, an open session every 3 s', normal: 'queue every 10 s, an open session every 6 s', slow: 'queue every 30 s, an open session every 15 s',
                       manual: 'only when you pull down, tap refresh, or come back to the app' };
function setRow(label, hint, ctl) {
  const r = el('div', 'setrow'); r.dataset.set = label.toLowerCase().replace(/[^a-z]+/g, '-');
  r.append(el('div', 'ttl', label), ctl);
  if (hint != null) { r._hint = el('div', 'sub', hint); r.append(r._hint); }
  return r;
}
function setChoice(key, after) {   // a row of choices from PREFS, the chosen one lit
  const c = el('div', 'segc'); c.dataset.pref = key;
  PREFS[key].opts.forEach(([v, text]) => {
    const b = btn(text, 'segb' + (prefs.get(key) === v ? ' on' : ''), () => {
      prefs.set(key, v);
      c.querySelectorAll('.segb').forEach((x) => x.classList.toggle('on', x === b));
      if (after) after(v);
    });
    b.dataset.v = v; c.append(b);
  });
  return c;
}
function setStep(bump, text) {   // a minus, the value, a plus
  const c = el('div', 'stepc'), val = el('span', 'val', text());
  const go = (d) => () => { bump(d); val.textContent = text(); };
  c.append(btn('−', 'segb', go(-1)), val, btn('+', 'segb', go(1)));
  return c;
}
function setDisplay(b) {
  const refresh = setRow('Refresh', REFRESH_NOTE[prefs.get('refresh')], el('div'));
  refresh.replaceChild(setChoice('refresh', (v) => { refresh._hint.textContent = REFRESH_NOTE[v]; startPolls(); }), refresh.children[1]);
  b.append(
    setRow('Text size', null, setStep((d) => bumpTfs(d), () => String(tfs))),
    setRow('Splash screen', null, setChoice('splash')),
    setRow('Animations', null, setChoice('motion', () => { REDUCED = osReduced() || prefs.get('motion') === 'off'; applyMotion(); })),
    setRow('Open on', null, setChoice('open')),
    refresh,
    setRow('Slash commands', null, setChoice('slash', () => paintSlash())),
    setRow('Top spacing', 'The phone reports ' + reportedInset() + 'px at the top', setStep((d) => { topx = Math.max(0, Math.min(80, topx + d * 4)); applyTopx(true); }, () => topx + 'px')),
  );
}
function setPhones(b) {
  const H = S.hosts, q = S.q;
  const me = el('div', 'card');
  me.append(el('h2', null, 'This phone'),
            el('p', null, (S.dev ? S.dev.name : '?') + ' is paired with ' + ((q && q.host) || (S.hello && S.hello.name) || 'the host') + '.'));
  const mine = H && H.devices.find((d) => d.you);
  if (mine) me.append(el('div', 'code selectable', mine.fp));
  b.append(me);
  if (H) {
    b.append(section('Phones the host trusts', H.devices.length));
    const box = el('div', 'devbox');   // the one list here: it takes the spare room and scrolls in place
    H.devices.forEach((d) => {
      const r = el('div', 'hrow'); const mm = el('div', 'meta');
      mm.append(el('div', 'ttl', d.name + (d.you ? ' (this phone)' : '')),
                el('div', 'sub', d.fp.slice(0, 20) + '… · seen ' + (d.last_seen ? clock(d.last_seen) : 'never')));
      r.append(mm);
      if (H.writes) r.append(btn('Revoke', 'mini-btn bad', () => prepare({ kind: 'device_revoke', id: d.id })));
      box.append(r);
    });
    b.append(box);
    const more = () => box.classList.toggle('more', box.scrollTop + box.clientHeight < box.scrollHeight - 2);   // a soft fade at the bottom says there is more to scroll to
    box.addEventListener('scroll', more); requestAnimationFrame(more);
  }
  const add = el('div', 'hrow'), am = el('div', 'meta');
  am.append(el('div', 'ttl', 'Pair another phone'), el('div', 'sub', 'On the host run: shepr pair, then tap Scan'));
  add.append(am); b.append(add);
  const row = el('div', 'btns'); row.append(btn('Forget this phone…', 'bad', forgetThisPhone)); b.append(row);
}
function setAbout(b) {
  const q = S.q, n = Object.keys(drafts.all()).length;
  const c = el('div', 'card');
  c.append(el('h2', null, 'Shepr ' + BUILD),
           el('p', null, 'Host ' + ((q && q.host) || (S.hello && S.hello.name) || '?') + (q && q.server_build ? ' runs ' + q.server_build : '')
             + (q && q.writes ? '. Sends always preview first; Shepr never approves anything by itself.' : '. Read-only host.')),
           el('p', 'dim', 'The private key stays in this app and signs every request.' + (standalone ? '' : ' Add Shepr to the Home Screen so iOS keeps it.')),
           el('p', 'dim', 'Blocked time is watched every ' + ((q && q.poll_s) || 15) + ' s; ≥ means it was already blocked when watching began.'));
  b.append(c);
  const r1 = el('div', 'btns'); r1.append(btn('Gestures', '', openHelp), btn('Reload Shepr', '', () => location.reload()));
  const clr = btn('Clear unsent drafts' + (n ? ' (' + n + ')' : ''), '', () => { store.set('shepr.drafts', {}); notify('Unsent drafts cleared.', false); paintMain(); });
  clr.disabled = !n;
  const r2 = el('div', 'btns'); r2.append(clr);
  b.append(r1, r2);
}
function settingsFoot() {   // the two links, always at the very bottom of Settings
  const f = el('div', 'setfoot');
  const link = (href, text) => { const a = el('a', null, text); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; };
  f.append(link('https://nixfred.com', 'nixfred.com'), link('https://github.com/nixfred/shepr', 'Shepr on GitHub'));
  return f;
}
function paintSettingsView(m) {
  const seg = SET_SEGS.some((x) => x[0] === setSeg) ? setSeg : 'display';
  const bar = el('div', 'setseg'); bar.id = 'setseg';
  SET_SEGS.forEach(([id, text]) => { const bt = btn(text, 'mini-btn' + (id === seg ? ' on' : ''), () => { setSeg = id; store.set('shepr.setseg', id); paintMain(); }); bt.dataset.seg = id; bar.append(bt); });
  const body = el('div', 'setbody'); body.id = 'setbody';
  ({ display: setDisplay, alerts: (x) => x.append(notifyCard()), phones: setPhones, about: setAbout })[seg](body);
  m.append(bar, body, settingsFoot());
}

function openHelp() {
  const box = el('div');
  box.append(el('h3', null, 'Moving around'), el('div', 'sub', 'Tap, double-tap and hold. None of them ever sends anything.'));
  const kv = el('div', 'kv');
  [['Queue row', 'tap opens \u00b7 hold peeks at the last lines'],
   ['Session title', 'tap = Go To \u00b7 double-tap = next waiting \u00b7 hold = previous session'],
   ['\u25c0  \u25b6', 'tap steps \u00b7 hold jumps to first or last'],
   ['Tab', 'tap switches \u00b7 double-tap refreshes \u00b7 hold = Go To'],
   ['Host chip', 'tap filters \u00b7 double-tap clears \u00b7 hold = only that host']]
    .forEach((r) => { kv.append(el('div', 'k', r[0]), el('div', 'v', r[1])); });
  box.append(kv);
  const row = el('div', 'btns'); row.append(btn('Got it', 'go', closeSheet)); box.append(row);
  openSheet(box);
}

function forgetThisPhone() {
  const box = el('div');
  box.append(el('h3', null, 'Forget this phone?'),
             el('div', 'sub', 'Deletes the key from this app. The host keeps trusting it until you revoke it there.'));
  const row = el('div', 'btns');
  row.append(btn('Cancel', '', closeSheet), btn('Forget', 'go appr', () => {
    forgetDevice().then(() => { S.dev = null; S.q = null; closeSheet(); showPair('This phone forgot its key. Pair again to continue.'); })
      .catch((e) => { closeSheet(); notify('Could not delete the key from this phone: ' + (e && e.message || 'storage error') + '. It is still paired.', true); });
  }));
  box.append(row); openSheet(box);
}

// ---------------------------------------------------------------------------
// 9. Sheet (one at a time): confirm / peek / go to
// ---------------------------------------------------------------------------
let sheetTimer = null;
function openSheet(node) {
  const sh = $('sheet');
  sh.textContent = ''; sh.append(node);
  $('shade').hidden = false; sh.hidden = false; S.sheetOpen = true;
}
function closeSheet() {
  clearTimeout(sheetTimer);
  $('shade').hidden = true; $('sheet').hidden = true; $('sheet').textContent = ''; S.sheetOpen = false;
}
$('shade').addEventListener('click', closeSheet);

function btn(text, cls, fn) {
  const b = el('button', cls, text); b.type = 'button'; b.addEventListener('click', fn); return b;
}

// ---- peek (hold a row): the last lines without leaving the list
function openPeek(a, ctx) {
  const box = el('div');
  box.append(el('h3', null, label(a)), el('div', 'sub', [a.title, a.host, STATE_LABEL[a.status] || a.status].filter(Boolean).join(' · ')));
  const pre = el('div', 'peek selectable', 'reading…');
  box.append(pre);
  const row = el('div', 'btns');
  row.append(btn('Close', '', closeSheet),
             btn('Project', '', () => { closeSheet(); openGoTo(a.project); }),
             btn('Open', 'go', () => { closeSheet(); openDetail(a.host, a.pane, ctx); }));
  box.append(row);
  openSheet(box);
  api('api/pane?host=' + encodeURIComponent(a.host) + '&pane=' + encodeURIComponent(a.pane) + '&lines=14')
    .then((r) => { renderBlocks(pre, r.text); pre.scrollTop = pre.scrollHeight; })
    .catch((e) => { pre.textContent = sessionMissing(e) ? 'That session is gone.' : 'Could not read: ' + e.message; });
}

// ---- Go To: every session on every host, searchable
function openGoTo(prefill) {
  const box = el('div');
  box.style.cssText = 'display:flex;flex-direction:column;min-height:0';
  const inp = el('input', 'search'); inp.type = 'search'; inp.placeholder = 'Go to project, session or host';
  inp.value = prefill || ''; inp.autocomplete = 'off'; inp.autocapitalize = 'off'; inp.spellcheck = false;
  const list = el('div', 'golist');
  box.append(inp, list);
  const row = el('div', 'btns'); row.style.marginTop = '8px';
  row.append(btn('Close', '', closeSheet));
  box.append(row);
  const recent = store.get('shepr.recent', []);
  const ri = (a) => { const i = recent.indexOf(keyOf(a)); return i < 0 ? 99 : i; };
  const draw = () => {
    list.textContent = '';
    const toks = inp.value.toLowerCase().split(/\s+/).filter(Boolean);
    const rank = (a) => (a.status === 'blocked' ? 0 : a.status === 'done' ? 1 : (recent.indexOf(keyOf(a)) >= 0 ? 2 : 3));
    const hits = agents().filter((a) => {
      const hay = [a.host, a.project, a.title, a.workspace, a.agent, a.status, a.cwd].join(' ').toLowerCase();
      return toks.every((t) => hay.indexOf(t) >= 0);
    }).sort((a, b) => rank(a) - rank(b) || ri(a) - ri(b));
    if (!hits.length) list.append(el('div', 'empty', 'Nothing matches.'));
    hits.slice(0, 60).forEach((a) => {
      const n = itemNode(a, 'all', false, 0).cloneNode(true);   // clone drops the list gestures
      n.classList.remove('enter');
      gesture(n, { tap: () => { closeSheet(); openDetail(a.host, a.pane, 'all'); } });
      list.append(n);
    });
  };
  inp.addEventListener('input', draw);
  draw();
  openSheet(box);
}

// ---- Start new: a brand-new project. Ask for the name that goes on the herdr title bar and the folder under ~/Projects,
// then (after you confirm) herdr makes the project, the folder is made, and the start command is typed in it.
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
const DIR_OK = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
function openNewProject() {
  const box = el('div'); box.style.cssText = 'display:flex;flex-direction:column;min-height:0';
  let tab = store.get('shepr.newtab', 'new') === 'existing' ? 'existing' : 'new';
  const sub = el('div', 'sub'), seg = el('div', 'seg'), body = el('div');
  seg.id = 'np-tabs'; body.style.cssText = 'display:flex;flex-direction:column;min-height:0;flex:1 1 auto';
  const bNew = btn('New project', 'mini-btn', () => show('new')), bOld = btn('Existing folder', 'mini-btn', () => show('existing'));
  bNew.dataset.tab = 'new'; bOld.dataset.tab = 'existing'; seg.append(bNew, bOld);
  box.append(el('h3', null, 'Start a project'), sub, seg, body);
  const show = (t, first) => {
    tab = t; store.set('shepr.newtab', t);
    bNew.classList.toggle('on', t === 'new'); bOld.classList.toggle('on', t === 'existing');
    body.textContent = '';
    if (t === 'new') { sub.textContent = 'Makes a new herdr project, creates its folder in ~/Projects, and starts Claude in it. You confirm first.'; newProjectForm(body, first); }
    else { sub.textContent = 'Opens a folder you already have as a herdr project. Nothing in it is created or changed. You confirm first.'; existingFolderPicker(body); }
  };
  openSheet(box);
  show(tab, true);
}

function newProjectForm(box, focusIt) {
  const field = (label, input) => { const f = el('div', 'npf'); f.append(el('div', 'k', label), input); return f; };
  const mk = (ph, max) => { const i = el('input', 'search'); i.type = 'text'; i.placeholder = ph; i.maxLength = max; i.autocomplete = 'off'; i.autocapitalize = 'off'; i.spellcheck = false; return i; };
  const name = mk('shows on the herdr title bar', 60), dir = mk('folder-name', 80), first = mk('optional: what should Claude start on?', 500);
  name.id = 'np-name'; dir.id = 'np-dir'; first.id = 'np-msg';
  const hostsOk = ((S.q && S.q.hosts) || []).filter((h) => h.status === 'ok');
  let sel = null;
  if (hostsOk.length > 1) {
    sel = el('select', 'search'); sel.id = 'np-host';
    hostsOk.forEach((h) => { const o = el('option', null, h.id); o.value = h.id; if (S.hello && h.id === S.hello.name) o.selected = true; sel.append(o); });
  }
  const where = el('div', 'dim'); where.style.cssText = 'font-size:14.5px;margin:2px 2px 10px;overflow-wrap:anywhere';
  let touched = false;
  const go = btn('Review', 'go', () => prepare({ kind: 'new_project', host: sel ? sel.value : (S.hello && S.hello.name) || (hostsOk[0] && hostsOk[0].id), name: name.value.trim(), dir: dir.value.trim(), message: first.value.trim() || undefined }));
  const upd = () => {
    where.textContent = dir.value ? '~/Projects/' + dir.value : '';
    go.disabled = !(name.value.trim() && DIR_OK.test(dir.value.trim()) && !dir.value.trim().endsWith('.'));
    dir.style.borderColor = dir.value && go.disabled ? 'var(--hot)' : '';
  };
  name.addEventListener('input', () => { if (!touched) dir.value = slug(name.value); upd(); });
  dir.addEventListener('input', () => { touched = true; upd(); });
  box.append(field('Project name', name), field('Folder in ~/Projects', dir), field('First message (optional)', first), ...(sel ? [field('Host', sel)] : []), where);
  const row = el('div', 'btns');
  row.append(btn('Cancel', '', closeSheet), go);
  box.append(row);
  upd();
  if (focusIt) setTimeout(() => name.focus(), 50);
}

// ---- Existing folder: a folder you already have, opened as a herdr project. The host lists the folders in ~/Projects that herdr has no
// terminal in (newest conversation first); you pick Claude or Codex, resume or fresh, and see the exact command. Nothing is created.
function existingFolderPicker(box) {
  const local = () => (S.hello && S.hello.name) || '';
  let mode = store.get('shepr.openmode', 'continue') === 'fresh' ? 'fresh' : 'continue';
  const seg = el('div', 'seg');
  const bR = btn('Resume last', 'mini-btn', () => setMode('continue')), bN = btn('Fresh session', 'mini-btn', () => setMode('fresh'));
  seg.append(bR, bN);
  const inp = el('input', 'search'); inp.id = 'ex-search'; inp.type = 'search'; inp.placeholder = 'Search folders'; inp.autocomplete = 'off'; inp.autocapitalize = 'off'; inp.spellcheck = false;
  const count = el('div', 'dim'); count.id = 'ex-count'; count.style.cssText = 'font-size:13.5px;margin:0 2px 6px';
  const list = el('div', 'golist'); list.id = 'ex-list';
  list.append(el('div', 'empty', 'Looking…'));
  box.append(seg, inp, count, list);
  const row = el('div', 'btns'); row.style.marginTop = '8px'; row.append(btn('Close', '', closeSheet)); box.append(row);
  let all = [], info = null;
  const setMode = (m) => { mode = m; store.set('shepr.openmode', m); draw(); };
  const draw = () => {
    bR.classList.toggle('on', mode === 'continue'); bN.classList.toggle('on', mode === 'fresh');
    list.textContent = '';
    if (!info) return;
    const toks = inp.value.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = all.filter((i) => toks.every((t) => i.dir.toLowerCase().indexOf(t) >= 0));
    count.textContent = all.length + (all.length === 1 ? ' folder' : ' folders') + ' without a herdr project' + (info.open ? ', ' + info.open + ' already open' : '');
    if (!hits.length) list.append(el('div', 'empty', all.length ? 'Nothing matches.' : 'Every folder in ' + info.base + ' is already a herdr project.'));
    hits.slice(0, 80).forEach((it) => {
      const r = el('div', 'hrow wake'); r.dataset.dir = it.dir;
      const meta = el('div', 'meta'), tt = el('div', 'ttl');
      tt.append(it.dir); if (it.git) tt.append(el('span', 'exgit', 'git'));
      const seen = [it.claude ? 'Claude ' + agoText(it.claude) : '', it.codex ? 'Codex ' + agoText(it.codex) : ''].filter(Boolean).join(' · ');
      meta.append(tt, el('div', 'sub', seen || 'No conversations yet'));
      r.append(meta);
      [['claude', 'Claude'], ['codex', 'Codex']].forEach(([agent, name]) => {
        const b = btn(name, 'mini-btn', () => prepare({ kind: 'open_folder', host: local(), dir: it.dir, mode, agent }));
        if (mode === 'continue' && !it[agent]) { b.disabled = true; b.title = 'no ' + name + ' conversation here to resume'; }   // resuming needs something to resume
        r.append(b);
      });
      list.append(r);
    });
    if (hits.length > 80) { const more = el('div', 'dim', 'Showing 80 of ' + hits.length + '. Search to narrow the list.'); more.style.cssText = 'font-size:13.5px;padding:6px 2px'; list.append(more); }
  };
  inp.addEventListener('input', draw);
  draw();
  api('api/folders?host=' + encodeURIComponent(local())).then((r) => { all = r.items || []; info = r; draw(); })
    .catch((e) => { list.textContent = ''; list.append(el('div', 'empty', 'Could not list folders: ' + e.message)); });
}

// ---- wake a PROJECT: start Claude in a terminal that has no agent in it. Pick, then confirm the exact command.
const agoText = (ts) => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  for (const [n, u] of [[2592000, 'month'], [604800, 'week'], [86400, 'day'], [3600, 'hour'], [60, 'minute']]) if (s >= n) { const k = Math.floor(s / n); return k + ' ' + u + (k === 1 ? '' : 's') + ' ago'; }
  return 'just now';
};
function openAsleep() {
  const box = el('div');
  box.style.cssText = 'display:flex;flex-direction:column;min-height:0';
  box.append(el('h3', null, 'Wake a project'),
             el('div', 'sub', 'Starts Claude or Codex in that project’s terminal. You see the exact command before anything runs.'));
  let mode = store.get('shepr.wakemode', 'continue') === 'fresh' ? 'fresh' : 'continue';
  const seg = el('div', 'seg');
  const bR = btn('Resume last', 'mini-btn', () => setMode('continue')), bN = btn('Fresh session', 'mini-btn', () => setMode('fresh'));
  seg.append(bR, bN);
  const inp = el('input', 'search'); inp.type = 'search'; inp.placeholder = 'Search projects'; inp.autocomplete = 'off'; inp.autocapitalize = 'off'; inp.spellcheck = false;
  const list = el('div', 'golist');
  list.append(el('div', 'empty', 'Looking…'));
  box.append(seg, inp, list);
  const row = el('div', 'btns'); row.style.marginTop = '8px';
  row.append(btn('Close', '', closeSheet));
  box.append(row);
  let all = [];
  const multiHost = () => new Set(all.map((i) => i.host)).size > 1;
  const local = () => (S.hello && S.hello.name);
  const setMode = (m) => { mode = m; store.set('shepr.wakemode', m); draw(); };
  const draw = () => {
    bR.classList.toggle('on', mode === 'continue'); bN.classList.toggle('on', mode === 'fresh');
    list.textContent = '';
    const toks = inp.value.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = all.filter((i) => { const hay = (i.label + ' ' + i.cwd + ' ' + i.host).toLowerCase(); return toks.every((t) => hay.indexOf(t) >= 0); });
    if (!hits.length) list.append(el('div', 'empty', all.length ? 'Nothing matches.' : 'Nothing is asleep.'));
    hits.slice(0, 80).forEach((it) => {
      const r = el('div', 'hrow wake');
      const meta = el('div', 'meta');
      const seen = [it.claude ? 'Claude ' + agoText(it.claude) : '', it.codex ? 'Codex ' + agoText(it.codex) : ''].filter(Boolean).join(' · ');
      const tt = el('div', 'ttl'); if (multiHost()) tt.append(hostPill(it.host)); tt.append(it.label);
      meta.append(tt,
                  el('div', 'sub', (seen ? seen + ' · ' : '') + it.cwd.replace(/^\/home\/[^/]+/, '~')));
      r.append(meta);
      [['claude', 'Claude'], ['codex', 'Codex']].forEach(([agent, name]) => {
        const b = btn(name, 'mini-btn', () => prepare({ kind: 'start', host: it.host, pane: it.pane, mode, agent }));
        // Resuming needs something to resume. That is known for this host; for another host it is unknown, so offered.
        if (mode === 'continue' && it.host === local() && !it[agent]) { b.disabled = true; b.title = 'no ' + name + ' conversation here to resume'; }
        r.append(b);
      });
      list.append(r);
    });
  };
  inp.addEventListener('input', draw);
  openSheet(box);
  api('api/asleep').then((r) => { all = r.items || []; draw(); })
    .catch((e) => { list.textContent = ''; list.append(el('div', 'empty', 'Could not list projects: ' + e.message)); });
}

// ---- wake: one message to each sleeping session you pick, confirm first
function openWake() {
  const list = sleeping();
  if (!list.length) { notify('Nothing is sleeping.', false); return; }
  const box = el('div');
  box.append(el('h3', null, 'Wake sleeping sessions'),
             el('div', 'sub', 'Each picked session gets this one message. You see exactly what goes where before anything is sent.'));
  const rows = el('div', 'golist');
  const boxes = [];
  list.forEach((a) => {
    const r = el('label', 'chk');
    const cb = el('input'); cb.type = 'checkbox'; cb.checked = true; boxes.push([cb, a]);
    const tt = el('div', 'ttl'); if (S.q && S.q.hosts.length > 1) tt.append(hostPill(a.host)); tt.append(label(a));
    const m = el('div', 'meta'); m.append(tt, el('div', 'sub', a.title));
    r.append(cb, m);
    rows.append(r);
  });
  const msg = el('input', 'wakemsg'); msg.type = 'text'; msg.maxLength = 1000; msg.autocomplete = 'off';
  msg.value = store.get('shepr.wake', 'Continue where you left off.');
  const row = el('div', 'btns');
  const go = btn('Review', 'go', () => {
    const targets = boxes.filter((x) => x[0].checked).map((x) => ({ host: x[1].host, pane: x[1].pane }));
    if (!targets.length) { notify('Pick at least one.', true); return; }
    if (!msg.value.trim()) { notify('Write the message first.', true); return; }
    store.set('shepr.wake', msg.value.trim());
    prepare({ kind: 'wake', targets, text: msg.value.trim() });
  });
  row.append(btn('Cancel', '', closeSheet), go);
  box.append(rows, msg, row);
  openSheet(box);
}

// ---- confirm a send (or a host change)
const HOST_KINDS = ['host_enable', 'host_disable', 'host_add', 'device_revoke'];
function showPlan(plan) {
  const t = plan.target;
  const isHost = HOST_KINDS.indexOf(plan.kind) >= 0;
  const isWake = plan.kind === 'wake', isStart = plan.kind === 'start', isNew = plan.kind === 'new_project', isOpen = plan.kind === 'open_folder';
  const box = el('div');
  box.append(el('h3', null, plan.approves ? 'This approves it. Send?' : isWake ? 'Wake these sessions?' : isStart ? 'Wake this project?' : isNew ? 'Start this new project?' : isOpen ? 'Open this project?' : isHost ? 'Make this change?' : 'Send this?'),
             el('div', 'sub', [t.host, t.title, t.pane].filter(Boolean).join(' · ') + (t.status ? ' · ' + (STATE_LABEL[t.status] || t.status) : '')));
  if (plan.approves) box.append(el('div', 'warnbox', 'The option you picked reads like an approval. Check it is what you mean.'));
  const kv = el('div', 'kv selectable');
  plan.lines.forEach((l) => { kv.append(el('div', 'k', l.k), el('div', 'v', l.v)); });
  box.append(kv);
  const go = btn(isWake ? 'Wake them' : isStart ? 'Start it' : isNew ? 'Create it' : isOpen ? 'Open it' : isHost ? 'Do it' : 'Send', 'go' + (plan.approves ? ' appr' : ''), () => commit(plan, go));
  // WHY: the sheet appears under the thumb that just tapped Send in the dock. A short dead time
  // stops a double-tap or a late touch from confirming something nobody has read yet.
  go.disabled = true;
  sheetTimer = setTimeout(() => { go.disabled = false; }, 700);
  const row = el('div', 'btns');
  row.append(btn('Cancel', '', closeSheet), go);
  box.append(row);
  openSheet(box);
}

function commit(plan, goBtn) {
  goBtn.disabled = true; goBtn.textContent = 'Working…';
  // The plan's target, not the session you stand in now: a push tapped while this sheet is open can have moved you.
  const sentTo = plan.target && plan.target.host ? plan.target.host + '|' + plan.target.pane : (S.cur ? S.cur.host + '|' + S.cur.pane : null);
  post('api/send/commit', { nonce: plan.nonce }).then((r) => {
    closeSheet();
    if (plan.kind === 'new_project' || plan.kind === 'open_folder') {
      notify(plan.kind === 'open_folder' ? 'Opened ' + plan.target.title + '. It is starting there.' : 'Created ' + plan.target.title + '. Claude is starting there.', false);
      fetchQueue(true); setTimeout(() => fetchQueue(true), 3000); setTimeout(() => fetchQueue(true), 8000);
      return;
    }
    if (plan.kind === 'start') {
      notify('Starting ' + plan.target.title + '. It appears in the queue in a few seconds.', false);
      fetchQueue(true); setTimeout(() => fetchQueue(true), 3000); setTimeout(() => fetchQueue(true), 8000);
      return;
    }
    if (plan.kind === 'wake') {
      const bits = ['Woke ' + r.woke.length];
      if (r.skipped.length) bits.push(r.skipped.length + ' skipped (they had moved on)');
      if (r.failed.length) bits.push(r.failed.length + ' failed: ' + r.failed[0]);
      notify(bits.join(', ') + '.', r.failed.length > 0);
      fetchQueue(true);
      return;
    }
    if (HOST_KINDS.indexOf(plan.kind) >= 0) {
      notify('Done.', false); loadHosts(); fetchQueue(true);
      return;
    }
    S.selSig = null; S.freeText = '';   // the next tick-several question starts from what is on screen
    // The answer can arrive after you moved to another session: only clear the draft of the one it was for.
    if (plan.kind === 'text' && sentTo) drafts.put(sentTo, '');   // sent: nothing left to keep, even if you have moved on since
    if (S.cur && S.cur.host + '|' + S.cur.pane === sentTo) { S.keybuf = []; paintStage(); if (plan.kind === 'text') { $('msg').value = ''; autosize(); } }   // a tapped option must not wipe the words you have not sent
    if (plan.kind === 'text') noteSlashUse(plan.target.host, S.lastText);
    notify('Sent to ' + (plan.target.title || plan.target.pane), false);
    setTimeout(loadTail, 1400);
    fetchQueue(true);
  }).catch((e) => {
    closeSheet();
    notify(e.message, true);   // includes "may still have landed" for transport failures
    if (S.cur) loadTail();
  });
}

function prepare(body) {
  if (body.kind === 'text') S.lastText = body.text;
  const isHost = HOST_KINDS.indexOf(body.kind) >= 0 || body.kind === 'wake' || body.kind === 'start' || body.kind === 'new_project' || body.kind === 'open_folder';
  if (!isHost && !S.cur) return;
  const b = isHost ? body : Object.assign({ host: S.cur.host, pane: S.cur.pane }, body);
  post('api/send/prepare', b).then((p) => { p.kind = p.kind || body.kind; showPlan(p); })
    .catch((e) => {
      // Words a numbered question would read as keystrokes can never be typed into it. Do not just refuse: keep them, and offer the way forward.
      if (body.kind === 'text' && !body.skip && SKIPPABLE.indexOf(e.code) >= 0) { openSkipSheet(body.text); return; }
      notify(e.message, true);
    });
}
const SKIPPABLE = ['menu_text', 'blocked_text'];
function openSkipSheet(text) {
  const box = el('div', 'skipsheet'), mid = el('div', 'skipmid');   // the words scroll on a short screen; the buttons stay on screen
  mid.append(el('h3', null, 'A question is waiting'),
             el('div', 'sub', 'Your words cannot be typed into it: a numbered question reads typed characters as keystrokes. Your message stays in the box either way.'));
  const quote = el('div', 'skipmsg selectable'); quote.textContent = text;   // your own words, as text
  mid.append(quote, el('div', 'sub', 'Or skip the question. Shepr presses Esc to close it without answering anything, then sends your message as a normal prompt. You review it first.'));
  const row = el('div', 'btns');
  row.append(btn('Answer it first', '', closeSheet), btn('Skip and send\u2026', 'go', () => { closeSheet(); prepare({ kind: 'text', text, skip: true }); }));
  box.append(mid, row);
  openSheet(box);
}

// ---------------------------------------------------------------------------
// 10. Session view
// ---------------------------------------------------------------------------
function navList() {
  if (S.ctx === 'waiting') return queueList();
  return agents().filter(inHost);
}
function position() {
  const l = navList(), i = l.findIndex((a) => S.cur && keyOf(a) === S.cur.host + '|' + S.cur.pane);
  if (i >= 0) S.idx = i;
  return { l, i };
}

function openDetail(host, pane, ctx) {
  const k = host + '|' + pane;
  if (S.cur) drafts.put(S.cur.host + '|' + S.cur.pane, $('msg').value);   // what you typed here and did not send waits for you
  if (S.cur && (S.cur.host + '|' + S.cur.pane) !== k) S.prevKey = S.cur.host + '|' + S.cur.pane;
  S.cur = { host, pane }; S.ctx = ctx || S.ctx;
  S.tailText = null; S.menu = null; S.agent = null; S.keybuf = []; S.selSig = null; S.hist = null;
  const rec = store.get('shepr.recent', []).filter((x) => x !== k);
  rec.unshift(k); store.set('shepr.recent', rec.slice(0, 12));
  try { history.replaceState(null, '', '#' + encodeURIComponent(host) + '/' + encodeURIComponent(pane)); } catch (e) { /* ignore */ }
  const d = $('detail');
  if (d.hidden) { d.hidden = false; } else { d.style.animation = 'none'; void d.offsetWidth; d.style.animation = S.slideFrom === 'left' ? 'slideinl .28s cubic-bezier(.2,.9,.3,1)' : ''; }
  S.slideFrom = null;
  $('tail').textContent = 'reading…'; S.tailAt = 0; S.stick = true; paintTailAge();
  $('toast').hidden = true;
  const g = $('gtoast'); if (g) g.hidden = true;   // a toast left over from the list must not cover this session's buttons
  S.slashOpen = false; $('msg').value = drafts.get(k); autosize(); paintStage(); paintDetailChrome();
  loadCmds(host);
  loadTail(true);
}

function closeDetail() {
  if (S.cur) drafts.put(S.cur.host + '|' + S.cur.pane, $('msg').value);
  $('detail').hidden = true; S.cur = null;
  try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* ignore */ }
  fetchQueue(true);
}


// ---- model and effort. Claude Code takes "/model opus" and "/effort high" as plain arguments, so this only sends those two lines
// (after the usual confirm sheet), and only to a Claude session that is waiting for its next prompt. Claude Code also saves the
// choice as the default for new sessions on that host, and the confirm sheet says so.
const MODELS = [['fable', 'Fable'], ['opus', 'Opus'], ['sonnet', 'Sonnet'], ['haiku', 'Haiku']];
const EFFORTS = [['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['xhigh', 'xhigh'], ['max', 'max']];
// What the session says it is running, read off the screen: the status line ("Opus 5.5·high") or the banner ("Opus 5.5 with high effort").
function currentModel() {
  const t = $('tail').textContent || '', all = (re) => { let m, last = null; const g = new RegExp(re.source, 'gi'); while ((m = g.exec(t))) last = m; return last; };
  const a = all(/\b(Fable|Opus|Sonnet|Haiku)\b[^\n·]{0,8}·\s*(low|medium|high|xhigh|max)\b/), b = all(/\b(Fable|Opus|Sonnet|Haiku)\b [\d.]+ with (low|medium|high|xhigh|max) effort/), m = a || b;
  return m ? { model: m[1].toLowerCase(), effort: m[2].toLowerCase() } : null;
}
function paintModelChip(a) {
  const b = $('dmodel'); if (!b) return;
  const ok = !!(a && a.agent === 'claude' && (a.status === 'idle' || a.status === 'done') && S.q && S.q.writes);
  b.hidden = !ok; if (!ok) return;
  const cur = currentModel();
  b.textContent = cur ? cur.model.charAt(0).toUpperCase() + cur.model.slice(1) + '·' + cur.effort : 'Model';
}
function openModelSheet() {
  const cur = currentModel(), box = el('div');
  box.append(el('h3', null, 'Model and effort'), el('div', 'sub', (cur ? 'Running ' + cur.model + ' at ' + cur.effort + ' effort. ' : '') + 'You see the exact line before it is sent. Claude Code also makes it the default for new sessions.'));
  const grp = (title, list, kind, now) => {
    box.append(el('div', 'mdl-t', title));
    const r = el('div', 'mdl-row'); r.style.gridTemplateColumns = 'repeat(' + list.length + ', 1fr)';
    list.forEach(([v, t]) => { const b = btn(t, 'mdl-b' + (now === v ? ' on' : ''), () => { closeSheet(); prepare({ kind, value: v }); }); b.dataset.value = v; r.append(b); });
    box.append(r);
  };
  grp('Model', MODELS, 'model', cur && cur.model); grp('Effort', EFFORTS, 'effort', cur && cur.effort);
  const row = el('div', 'btns'); row.append(btn('Cancel', '', closeSheet)); box.append(row);
  openSheet(box);
}
$('dmodel').addEventListener('click', openModelSheet);

function paintDetailChrome() {
  if (!S.cur) return;
  const a = byKey(S.cur.host + '|' + S.cur.pane) || S.agent;
  const { l, i } = position();
  if (a) {
    $('dtitle').textContent = '';
    if (S.q && S.q.hosts.length > 1) $('dtitle').append(hostPill(a.host));
    $('dtitle').append(label(a));
    $('dsub').textContent = a.title;
    const st = $('dstate');
    let txt = STATE_LABEL[a.status] || a.status;
    if (a.for_s != null) txt += ' ' + fmtAge(a.for_s, a.exact).main + (fmtAge(a.for_s, a.exact).sub || '');
    st.textContent = txt; st.className = 'state ' + a.status;
  } else {
    $('dstate').textContent = 'GONE'; $('dstate').className = 'state';
  }
  if (a && a.status === 'done') seenStore.mark(S.cur.host + '|' + S.cur.pane, a.seq);   // you are looking at it
  paintModelChip(a);
  $('dpos').textContent = i >= 0 ? (i + 1) + '/' + l.length : '–/' + l.length;
  $('dprev').disabled = !(l.length && (i > 0 || (i < 0 && S.idx > 0)));
  $('dnext').disabled = !(l.length && (i >= 0 ? i < l.length - 1 : S.idx < l.length));
}

function stepTo(delta, edge) {
  const { l, i } = position();
  if (!l.length) return false;
  let j;
  if (edge === 'first') j = 0;
  else if (edge === 'last') j = l.length - 1;
  else j = (i >= 0 ? i : (delta > 0 ? S.idx - 1 : S.idx)) + delta;
  j = Math.max(0, Math.min(l.length - 1, j));
  if (l[j] && keyOf(l[j]) !== S.cur.host + '|' + S.cur.pane) { openDetail(l[j].host, l[j].pane); return true; }
  return false;
}
// Swipe left or right anywhere in a session to step to the next or previous open project, in the same order as the
// arrow buttons. Navigation only: it never sends anything. Starts in the dock or a text box are ignored, as are
// slow drags, mostly-vertical drags and anything while text is selected.
(function swipeBetweenSessions() {
  const d = $('detail');
  let id = null, x0 = 0, y0 = 0, t0 = 0;
  d.addEventListener('pointerdown', (e) => {
    id = null;
    if (!e.isPrimary || e.pointerType === 'mouse' || S.sheetOpen || e.target.closest('.dock, textarea, input')) return;
    id = e.pointerId; x0 = e.clientX; y0 = e.clientY; t0 = performance.now();
  });
  d.addEventListener('pointercancel', () => { id = null; });
  d.addEventListener('pointerup', (e) => {
    if (e.pointerId !== id) return;
    id = null;
    const dx = e.clientX - x0, dy = e.clientY - y0;
    if (performance.now() - t0 > 800 || Math.abs(dx) < 70 || Math.abs(dx) < 2 * Math.abs(dy)) return;
    if (window.getSelection && String(window.getSelection()).length) return;
    S.slideFrom = dx < 0 ? 'right' : 'left';   // a left swipe pulls the next one in from the right
    if (!stepTo(dx < 0 ? 1 : -1)) { S.slideFrom = null; notify(dx < 0 ? 'That is the last open project.' : 'That is the first open project.', false); }
  });
})();

function nextWaiting() {
  if (!S.cur) { const w = agents().filter((a) => a.status === 'blocked'); if (w[0]) openDetail(w[0].host, w[0].pane, 'waiting'); else notify('Nothing is waiting on you.', false); return; }
  const cur = S.cur.host + '|' + S.cur.pane;
  const w = agents().filter((a) => a.status === 'blocked' && keyOf(a) !== cur);
  if (!w.length) { notify('Nothing else is waiting on you.', false); return; }
  openDetail(w[0].host, w[0].pane, 'waiting');
}

function flipPrevious() {
  const p = S.prevKey && byKey(S.prevKey);
  if (!p) { notify('No previous session yet.', false); return; }
  openDetail(p.host, p.pane);
}

gesture($('dprev'), { tap: () => stepTo(-1), long: () => stepTo(0, 'first') });
gesture($('dnext'), { tap: () => stepTo(1), long: () => stepTo(0, 'last') });
gesture($('dhead'), { tap: () => openGoTo(''), dbl: nextWaiting, long: flipPrevious });
$('dback').addEventListener('click', closeDetail);
$('drefresh').addEventListener('click', () => { loadTail(true); if (S.hist) scheduleSync(); });

// Session text size: big by default (it is the thing you read), A- / A+ to taste, remembered.
// Top spacing: the phone's safe-area inset is the right number when iOS reports it, but a Home Screen app has
// been seen clipping the header anyway, so there is a floor (see app.css) plus this nudge, remembered.
let topx = Math.max(0, Math.min(80, store.get('shepr.topx2', 26)));   // saved only when you change it, so a better default can reach you
function applyTopx(save) { document.documentElement.style.setProperty('--topx', topx + 'px'); if (save) store.set('shepr.topx2', topx); }
applyTopx(false);
function reportedInset() {
  const p = document.createElement('div');
  p.style.cssText = 'position:fixed;left:0;top:0;width:0;height:env(safe-area-inset-top,0px);visibility:hidden';
  document.body.append(p); const h = Math.round(p.getBoundingClientRect().height); p.remove(); return h;
}
let tfs = Math.max(13, Math.min(28, store.get('shepr.tfs', 18)));
function applyTfs() { document.documentElement.style.setProperty('--tfs', tfs + 'px'); store.set('shepr.tfs', tfs); }
applyTfs();
$('fsm').addEventListener('click', () => { tfs = Math.max(13, tfs - 1); applyTfs(); });
$('fsp').addEventListener('click', () => { tfs = Math.min(28, tfs + 1); applyTfs(); });
function bumpTfs(d) { tfs = Math.max(13, Math.min(28, tfs + d)); applyTfs(); }   // Settings > Text size

// ---------------------------------------------------------------------------
// Terminal text, made readable on a phone. A terminal is 100+ columns of fixed-width cells, so at phone width the
// raw text wraps mid-line with no alignment. Instead each LINE becomes a block: prose reflows in the normal font
// with a hanging indent (bullets and numbered lines stay aligned), code / tables / box art stay fixed-width,
// rules become thin dividers, and the status-bar block under the prompt becomes chips. Text only ever goes
// through textContent: it is data, never markup.
// ---------------------------------------------------------------------------
const RULE = /^[\s─-╿\-_=|]{8,}$/;
const BOX = /[─-╿▀-▟]/;
const BULLET = /^(?:[•·▸▶►❯>*\-]\s+|\d{1,2}[.)]\s+|[☐☑✓✗✔✘⏺⎿]\s*)/;
const CODEISH = /[{}<>=`\\]|\w\/\w|^\$ |\.(?:js|ts|py|md|json|sh|css|html|toml|yaml|yml|lock)\b/;
function tidy(text) {
  return text.split('\n').map((l) => (RULE.test(l) ? '────────────' : l)).join('\n');
}
// The status bar under the prompt is a dozen segments; on a phone it should be a glance, not a page. These are the
// ones worth the room, in the order they appear: model, context, banked / weekly subscription, 5h window,
// branch, permission mode. Everything else is one tap away ("+N").
const STATUS_KEEP = [
  /\b(?:fable|opus|sonnet|haiku|gpt|gemini|kimi|grok|codex)\b|xhigh/i,
  /^ctx\b/i,
  /banked|\bweek\b/i,
  /^\d+h\s*\d+%/i,
  /⌥|\b(?:master|main)\b/,
  /bypass|permission|accept edits|plan mode/i,
];
function statusZone(segs) {
  const wrap = el('div', 'chips compact');
  const clean = (s) => s.replace(/\s*\((?:shift\+tab|esc)[^)]*\)/i, '').replace(/(?:\s*[·←])+\s*\d+ agents?$/i, '').replace(/\s*·$/, '').trim();
  const keep = segs.map((s) => STATUS_KEEP.some((re) => re.test(s)));
  if (!keep.some(Boolean)) segs.slice(0, 5).forEach((_, i) => { keep[i] = true; });
  let shown = 0;
  segs.forEach((s, i) => { if (keep[i] && ++shown > 6) keep[i] = false; });
  segs.forEach((s, i) => { const c = el('span', keep[i] ? null : 'x', clean(s)); wrap.append(c); });
  const extra = keep.filter((k) => !k).length;
  if (extra) {
    const more = el('span', 'more', '+' + extra);
    more.addEventListener('click', () => { const open = wrap.classList.toggle('compact'); more.textContent = open ? '+' + extra : 'less'; });
    wrap.append(more);
  }
  return wrap;
}

function renderBlocks(box, text) {
  box.textContent = '';
  const lines = text.replace(/\t/g, '    ').split('\n');
  // The status zone is whatever sits below the LAST rule near the bottom (Claude Code: prompt box, then status lines).
  let ruleAt = -1;
  for (let i = lines.length - 1; i >= Math.max(0, lines.length - 20); i--) { if (RULE.test(lines[i])) { ruleAt = i; break; } }
  const frag = document.createDocumentFragment();
  const segs = [];
  let blank = 0, prevRule = false, skipRule = false;
  lines.forEach((raw, i) => {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { if (++blank === 1 && !prevRule) frag.append(el('div', 'tl gap')); return; }
    blank = 0;
    if (RULE.test(line)) {
      if (skipRule) { skipRule = false; return; }      // the closing rule of an empty prompt box: one divider is enough
      frag.append(el('div', 'tl rule')); prevRule = true; return;
    }
    // An empty prompt box (rule, bare ❯, rule) is three lines of nothing: keep one divider.
    if (prevRule && /^\s*[❯>]\s*$/.test(line)) { skipRule = true; prevRule = false; return; }
    prevRule = false;
    if (ruleAt >= 0 && i > ruleAt) {                                   // status zone: collect, render once
      const parts = line.trim().split(/\s+[│|]\s+/).filter(Boolean);
      parts.forEach((p) => segs.push(p));
      return;
    }
    const m = /^(\s*)(.*)$/.exec(line);
    const ind = m[1].length, body = m[2];
    const bm = BULLET.exec(body);
    const mono = BOX.test(line) || ind >= 4 || CODEISH.test(body) || /^⎿/.test(body);   // tool output stays fixed-width, all of it
    const d = el('div', 'tl' + (mono ? ' mono' : '') + (/^❯/.test(body) ? ' sel' : ''));
    d.style.setProperty('--ind', ind);
    if (bm) {
      // The marker gets a box exactly --hang cells wide, so the text after it starts where a wrapped line will.
      // A marker with no space after it (the record dot, a checkbox) is a wide glyph: give it one more cell.
      d.style.setProperty('--hang', bm[0].length + (/[^\x00-\x7f]/.test(bm[0]) ? 1 : 0) + (/\s$/.test(bm[0]) ? 0 : 1));
      d.append(el('span', 'bm', bm[0].trimEnd()), body.slice(bm[0].length));
    } else d.append(body);
    frag.append(d);
  });
  if (segs.length) frag.append(statusZone(segs));
  box.append(frag);
}

// Stay at the bottom while the content under you reflows (the status chips, fonts, history arriving above), unless you have scrolled up to read.
// Without this the tail was pinned only at the moment it rendered, and a later reflow left the last line hidden behind the message bar.
S.stick = true;
$('tail').addEventListener('scroll', () => { const t = $('tail'); S.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 60; }, { passive: true });
const tailRO = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => { const t = $('tail'); if (S.stick && S.cur) t.scrollTop = t.scrollHeight; }) : null;
function renderTail(text) {
  const t = $('tail');
  const near = t.scrollHeight - t.scrollTop - t.clientHeight < 60;
  renderBlocks(tailDom(), text);
  return near;
}

function sessionMissing(e) {
  return e && (e.code === 'agent_not_found' || e.code === 'pane_not_found');
}
let tailBusy = false;
// How old what you are reading is: "live" while the refresh is keeping up, amber once it has stopped (a stale screen must not look current).
function paintTailAge() {
  const n = $('dtime'); if (!n || !S.cur) return;
  if (!S.tailAt) { n.textContent = ''; n.className = 'dim grow'; return; }
  const s = Math.max(0, Math.round((Date.now() - S.tailAt) / 1000));
  n.textContent = s < 9 ? 'live' : s < 60 ? s + 's ago' : Math.floor(s / 60) + 'm ago';
  n.className = 'dim grow ' + (s < 9 ? 'live' : s >= 30 ? 'stale' : '');
}
setInterval(() => { if (S.cur && !document.hidden) paintTailAge(); }, 1000);
function loadTail(force) {
  if (!S.cur || tailBusy) return;
  const at = S.cur;
  tailBusy = true;
  api('api/pane?host=' + encodeURIComponent(at.host) + '&pane=' + encodeURIComponent(at.pane) + '&lines=40')
    .then((r) => {
      if (!S.cur || S.cur.host !== at.host || S.cur.pane !== at.pane) return;
      S.agent = r.agent; S.menu = r.menu; S.queued = !!r.queued;
      const changed = r.text !== S.tailText;
      if (changed || force) {
        const first = S.tailText === null;
        const stick = renderTail(r.text);
        S.tailText = r.text;
        if (first || force || stick) $('tail').scrollTop = $('tail').scrollHeight;
        if (!S.hist) startHistory(); else scheduleSync();
      }
      S.tailAt = Date.now(); paintTailAge();
      paintDock(); paintDetailChrome();
    })
    .catch((e) => {
      if (S.cur && S.cur.pane === at.pane) {
        if (sessionMissing(e)) { closeDetail(); notify('That session is gone.', false); return; }
        $('tail').textContent = e.code === 'bad_host' ? 'That host is not reachable.' : 'Could not read this session: ' + e.message;
        S.agent = null; paintDock();
      }
    })
    .finally(() => { tailBusy = false; });
}
// (an open session's refresh is started together with the queue's: startPolls, at the end of the file)

// ---- the whole session, one scroll. The live text stays at the bottom exactly as before; ABOVE it sits the entire
// conversation. herdr keeps ~1,000 lines, the Claude transcript keeps everything, so the host serves it a page at a time
// (newest first) and each older page is added above what you are reading, without moving it.
const alnum = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const stamp = (ts) => { const d = new Date(ts); return isNaN(d) ? '' : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); };
const sigOf = (e) => e.r + '|' + (e.ts || '') + '|' + (e.x || '').slice(0, 60);
function histNode(e) {
  const n = el('div', 'hent h-' + e.r + (e.err ? ' err' : ''));
  if (e.r === 'user' || e.r === 'assistant') {
    const body = el('div', 'hbody'); body.textContent = e.x;   // DATA: text only
    n.append(el('div', 'hwho', (e.r === 'user' ? 'YOU' : 'CLAUDE') + (e.ts ? '   ' + stamp(e.ts) : '')), body);
  } else n.textContent = e.x;
  return n;
}
// The scroll box holds, top to bottom: a status line, the history, a "now" rule, and the live text.
function tailDom() {
  const t = $('tail');
  let live = $('live');
  if (!live || live.parentNode !== t) {
    t.textContent = '';
    S.hist = null;   // the box was (re)built empty, so whatever history state existed no longer matches the page
    const mark = el('div', 'hmark'); mark.id = 'hmark';
    const hist = el('div'); hist.id = 'hist';
    const now = el('div', 'hnow', 'now'); now.id = 'hnow'; now.hidden = true;
    live = el('div'); live.id = 'live';
    t.append(mark, hist, now, live);
    if (tailRO) { tailRO.disconnect(); tailRO.observe(hist); tailRO.observe(live); }
  }
  return live;
}
// The newest entries are what the live text below already shows. Find where they start so nothing appears twice.
function liveCut(entries) {
  const pane = alnum(S.tailText);
  let cut = entries.length, gap = 0, found = false;
  for (let i = entries.length - 1; i >= 0; i--) {
    const k = alnum(entries[i].x).slice(0, 40);
    if (k.length >= 20 && pane.includes(k)) { cut = i; found = true; gap = 0; } else if (++gap > (found ? 6 : 12)) break;
  }
  return cut;
}
// Add nodes above or below the history without moving what you are looking at.
function histInsert(nodes, atTop) {
  const t = $('tail'), hist = $('hist');
  const prevH = t.scrollHeight, prevTop = t.scrollTop, histBottom = hist.offsetTop + hist.offsetHeight;
  const near = prevH - prevTop - t.clientHeight < 60;
  const frag = document.createDocumentFragment(); nodes.forEach((n) => frag.append(n));
  if (atTop) hist.prepend(frag); else hist.append(frag);
  $('hnow').hidden = !hist.firstChild;
  if (atTop || prevTop >= histBottom - 5) t.scrollTop = prevTop + (t.scrollHeight - prevH);   // you were below the change: stay put
  if (!atTop && near) t.scrollTop = t.scrollHeight;
}
function startHistory() {
  if (!S.cur || S.hist) return;
  S.hist = { before: null, done: false, loading: false, mode: null, lastSig: null, syncing: false, at: S.cur };
  loadOlder(true);
}
function loadOlder(first) {
  const h = S.hist;
  if (!h || h.loading || h.done) return;
  h.loading = true;
  const at = h.at, mark = $('hmark');
  api('api/history?host=' + encodeURIComponent(at.host) + '&pane=' + encodeURIComponent(at.pane) + (h.before != null ? '&before=' + h.before : ''))
    .then((r) => {
      if (S.hist !== h) return;   // you moved to another session
      h.loading = false;
      if (r.source === 'pane') {
        h.mode = 'pane'; h.done = true;
        paintPaneHistory(r.text);
        mark.textContent = r.note || 'start of what herdr keeps';
      } else {
        h.mode = 'transcript';
        let es = r.entries;
        if (first) { es = es.slice(0, liveCut(es)); }
        if (first && es.length) h.lastSig = sigOf(es[es.length - 1]);
        histInsert(es.map(histNode), !first);
        h.before = r.before; h.done = r.before == null;
        mark.textContent = h.done ? 'start of this session' + (r.note ? ' (' + r.note + ')' : '') : '';
        if (first && !es.length && !h.done) { h.mode = 'transcript'; }
      }
      const t = $('tail');
      if (!h.done && t.scrollHeight <= t.clientHeight + 300) loadOlder(false);   // a short page: fetch the next straight away
    })
    .catch((e) => {
      if (S.hist !== h) return;
      h.loading = false;
      mark.textContent = 'Could not load older history: ' + e.message + '. Tap here to retry.';
      mark.onclick = () => { mark.onclick = null; mark.textContent = ''; loadOlder(first); };
    });
}
// No transcript (another host, or an agent that is not Claude): the ~1,000 lines herdr keeps, minus the last 40 the live text shows.
function paintPaneHistory(text) {
  const t = $('tail'), hist = $('hist');
  const prevH = t.scrollHeight, prevTop = t.scrollTop, histBottom = hist.offsetTop + hist.offsetHeight;
  const lines = text.split('\n');
  const box = el('div', 'hpane');
  renderBlocks(box, lines.slice(0, Math.max(0, lines.length - 40)).join('\n'));
  hist.textContent = ''; hist.append(box);
  $('hnow').hidden = !box.firstChild;
  if (prevTop >= histBottom - 5 || prevH - prevTop - t.clientHeight < 60) t.scrollTop = prevTop + (t.scrollHeight - prevH);
}
// The session keeps growing while you read it: pull in what is new so there is never a gap above the live text.
let syncTimer = null;
function scheduleSync() { clearTimeout(syncTimer); syncTimer = setTimeout(syncHistory, 1500); }
function syncHistory() {
  const h = S.hist;
  if (!h || !h.mode || h.syncing || h.loading) return;
  h.syncing = true;
  const at = h.at;
  api('api/history?host=' + encodeURIComponent(at.host) + '&pane=' + encodeURIComponent(at.pane))
    .then((r) => {
      if (S.hist !== h) return;
      if (r.source === 'pane') { paintPaneHistory(r.text); return; }
      const es = r.entries;
      let idx = -1;
      if (h.lastSig) for (let i = es.length - 1; i >= 0; i--) { if (sigOf(es[i]) === h.lastSig) { idx = i; break; } }
      const fresh = es.slice(idx + 1);
      const add = fresh.slice(0, liveCut(fresh));
      if (!add.length) return;
      const nodes = add.map(histNode);
      if (h.lastSig && idx < 0) nodes.unshift(el('div', 'hent h-divider', 'earlier entries are not shown here'));
      histInsert(nodes, false);
      h.lastSig = sigOf(add[add.length - 1]);
    })
    .catch(() => { /* the next change tries again */ })
    .finally(() => { h.syncing = false; });
}
$('tail').addEventListener('scroll', () => {
  const h = S.hist;
  if (h && !h.done && !h.loading && h.mode && $('tail').scrollTop < 700) loadOlder(false);
}, { passive: true });

// ---- dock: options, keys, compose
const KEYS = [['↑', 'up'], ['↓', 'down'], ['⏎', 'enter'], ['Esc', 'esc'], ['Tab', 'tab'], ['␣', 'space']];
const KEYNAME = {}; KEYS.forEach((k) => { KEYNAME[k[1]] = k[0]; });

function paintDock() {
  const st = S.agent ? S.agent.status : null;
  const opt = $('optrow'), key = $('keyrow');
  opt.textContent = ''; key.textContent = '';
  const m = S.menu;
  opt.hidden = !(st === 'blocked' && m && m.options.length);
  if (!opt.hidden) {
    if (m.multi) paintMulti(opt, m);
    else m.options.forEach((o) => {
      // "Type something" is a text field: pressing Enter on it with nothing typed DECLINES the question, so it is never a plain pick.
      const b = o.free
        ? btn('\u270e ' + o.n, (o.n === m.current ? 'cur ' : '') + 'free', () => openTypeSheet({ title: 'Type your own answer', sub: o.n + '. ' + o.label + ' (your words go into that row, then Enter)', cta: 'Review', onDone: (text) => prepare({ kind: 'choose', option: o.n, text }) }))
        : btn(String(o.n), (o.n === m.current ? 'cur ' : '') + (o.approves ? 'appr' : ''), () => prepare({ kind: 'choose', option: o.n }));
      b.setAttribute('aria-label', o.free ? 'Type your own answer' : 'Pick option ' + o.n + ': ' + o.label);
      opt.append(b);
    });
  }
  key.hidden = !(st === 'blocked' || st === 'working');
  if (!key.hidden) {
    (st === 'working' ? [KEYS[3]] : KEYS).forEach((k) => key.append(btn(k[0], '', () => stageKey(k[1]))));
  }
  // A message waiting behind running work: Claude Code's own "ctrl+enter to send now", as a button (the hint line itself is hidden).
  const q = $('qrow'); q.textContent = '';
  q.hidden = !(S.queued && S.agent && S.agent.agent === 'claude' && S.q && S.q.writes);
  if (!q.hidden) q.append(el('span', 'qtxt', 'A message is queued behind the current work.'), btn('Send now', 'qgo', () => prepare({ kind: 'sendnow' })));
  // Short on purpose: the box is narrow now (the / button), and a placeholder that wraps makes the box grow.
  const ph = st === 'blocked' ? (m && m.options.length ? (m.options.some((o) => o.free) ? 'Your own answer (goes in the \u270e row)' : 'Tap an option, or write to skip it') : 'Answer, sent with \u23ce')
           : st === 'working' ? 'Queue a message'
           : 'Message ' + (S.agent ? S.agent.agent : '');
  $('msg').placeholder = ph;
  $('send').disabled = !S.agent;
  paintSlash();
}

// A "tick all that apply" question. Tapping a number only changes the buttons on your phone; nothing is sent until
// Submit / Next, which shows exactly what will be ticked and pressed and waits for you to confirm.
function paintMulti(opt, m) {
  const tog = m.options.filter((o) => o.checked !== null && !o.free && o.n <= 9);
  const sig = JSON.stringify([S.agent && S.agent.seq, m.options.map((o) => [o.n, o.label, o.checked])]);
  if (S.selSig !== sig) { S.selSig = sig; S.freeText = ''; S.sel = new Set(tog.filter((o) => o.checked).map((o) => o.n)); }
  tog.forEach((o) => {
    const on = S.sel.has(o.n);
    const b = btn((on ? '\u2713 ' : '') + o.n, 'tog' + (on ? ' on' : ''), () => {
      if (S.sel.has(o.n)) S.sel.delete(o.n); else S.sel.add(o.n);
      paintDock();
    });
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute('aria-label', (on ? 'Ticked' : 'Not ticked') + ', option ' + o.n + ': ' + o.label);
    opt.append(b);
  });
  const free = m.options.find((o) => o.free);
  if (free) {
    const fb = btn(S.freeText ? '\u270e \u2713' : '\u270e', 'tog free' + (S.freeText ? ' on' : ''), () => openTypeSheet({ title: 'Type your own answer', sub: 'It is typed into the "' + free.label + '" row, which ticks it. Nothing is sent until you press ' + m.action.label + '.', initial: S.freeText, cta: 'Done',
      onDone: (text) => { S.freeText = text; closeSheet(); paintDock(); notify('Your words will be typed when you press ' + m.action.label + '.', false); } }));
    fb.setAttribute('aria-label', 'Type your own answer' + (S.freeText ? ' (written)' : ''));
    opt.append(fb);
  }
  const go = btn(m.action.label + '\u2026', 'tog-go', () => prepare({ kind: 'multi', picks: Array.from(S.sel).sort((a, b) => a - b), advance: true, text: S.freeText || undefined }));
  go.setAttribute('aria-label', m.action.label + ' with ' + S.sel.size + ' ticked');
  opt.append(go);
}

// ---- your own words for a "Type something" row: one line, written here, sent only after the confirm sheet
function openTypeSheet(o) {
  const box = el('div');
  box.append(el('h3', null, o.title), el('div', 'sub', o.sub));
  const ta = el('textarea', 'typebox'); ta.id = 'typebox'; ta.rows = 4; ta.maxLength = 1000; ta.placeholder = 'Your own words'; ta.value = o.initial || '';
  ta.autocomplete = 'off'; ta.spellcheck = true;
  const one = () => ta.value.replace(/\s*[\r\n\t]+\s*/g, ' ').trim();   // a line break would send it early, a tab would move the cursor
  const go = btn(o.cta, 'go', () => { const t = one(); if (t) o.onDone(t); });
  const upd = () => { go.disabled = !one(); };
  ta.addEventListener('input', upd);
  const row = el('div', 'btns'); row.append(btn('Cancel', '', closeSheet), go);
  box.append(ta, el('div', 'dim', 'One line. It is shown to you again before anything is typed.'), row);
  openSheet(box); upd();
  setTimeout(() => ta.focus(), 60);
}

// ---- photos and files: resize on the phone, upload to the host, and put the saved path in the message box.
// Uploading sends nothing to an agent: you add a note and Send, and the confirm sheet shows the exact text.
async function prepareUpload(file) {
  const isImg = /^image\//.test(file.type) || /\.(heic|heif|jpe?g|png|gif|webp)$/i.test(file.name);
  if (isImg) {
    if (file.type === 'image/png' && file.size <= 4 * 1024 * 1024) return { bytes: new Uint8Array(await file.arrayBuffer()), ext: 'png' };   // keep screenshots crisp
    const bmp = await createImageBitmap(file);   // Safari decodes HEIC here, so a camera photo becomes an ordinary JPEG
    const scale = Math.min(1, 2000 / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas'); c.width = Math.max(1, Math.round(bmp.width * scale)); c.height = Math.max(1, Math.round(bmp.height * scale));
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise((res) => c.toBlob(res, 'image/jpeg', 0.85));
    return { bytes: new Uint8Array(await blob.arrayBuffer()), ext: 'jpg' };
  }
  return { bytes: new Uint8Array(await file.arrayBuffer()), ext: (file.name.split('.').pop() || '').toLowerCase() };
}
// ---- attach: pick one or several photos or files, add a comment to each, and they go into the message box as paths. Nothing
// reaches the agent until you press Send and confirm. Claude Code turns a path on its own line into an image and keeps the
// lines after it, so each photo's comment stays next to it (checked on Claude Code 2.1.293).
const ATT_MAX = 10;
const ATT = { items: [], to: null, busy: false, urls: [] };
$('filepick').multiple = true;
$('attachbtn').addEventListener('click', () => { if (S.cur) { ATT.add = false; $('filepick').click(); } });
$('filepick').addEventListener('change', () => {
  const files = Array.from($('filepick').files || []); $('filepick').value = '';
  if (!files.length || !S.cur) return;
  if (!ATT.add || !ATT.to) { attDrop(); ATT.to = { host: S.cur.host, pane: S.cur.pane }; }
  files.forEach((f) => { if (ATT.items.length < ATT_MAX) ATT.items.push({ file: f, comment: '', path: null, url: null }); });
  if (files.length + (ATT.items.length - files.length) > ATT_MAX) notify('Up to ' + ATT_MAX + ' at a time.', true);
  openAttachSheet();
});
// A thumbnail holds the whole photo in memory until its object URL is revoked, so every way the photos go (cancelled, replaced,
// attached) revokes them; a few cancelled camera photos would otherwise stay in memory until the page reloads.
function attDrop() { ATT.items = []; ATT.urls.forEach((u) => { try { URL.revokeObjectURL(u); } catch (e) { /* ignore */ } }); ATT.urls = []; }
function attName(f) { return (f.name || 'photo').replace(/^image\./, 'photo.'); }
function attThumb(it) {
  const box = el('span', 'at-th');
  const isImg = /^image\//.test(it.file.type) || /\.(jpe?g|png|gif|webp)$/i.test(it.file.name || '');
  if (isImg) {
    try { if (!it.url) { it.url = URL.createObjectURL(it.file); ATT.urls.push(it.url); } } catch (e) { it.url = null; }
    if (it.url) { const im = el('img'); im.alt = ''; im.src = it.url; im.addEventListener('error', () => { im.remove(); box.textContent = 'IMG'; }); box.append(im); return box; }
  }
  box.textContent = (/\.pdf$/i.test(it.file.name || '') ? 'PDF' : 'FILE');
  return box;
}
function openAttachSheet() {
  const box = el('div', 'attsheet'), n = ATT.items.length;
  box.append(el('h3', null, n === 1 ? 'Attach 1 photo' : 'Attach ' + n + ' photos'), el('div', 'sub', 'Add a comment to each if you like. They go into the message box; nothing is sent until you press Send.'));
  const list = el('div', 'at-list'), grows = [], cap = n === 1 ? 6 : 4;   // a lone photo gets a taller box
  ATT.items.forEach((it, i) => {
    const row = el('div', 'at-row'), ci = el('textarea', 'at-c'), rm = btn('×', 'at-x', () => { const [gone] = ATT.items.splice(i, 1); if (gone && gone.url) { try { URL.revokeObjectURL(gone.url); } catch (e) { /* ignore */ } ATT.urls = ATT.urls.filter((u) => u !== gone.url); } if (!ATT.items.length) { closeSheet(); return; } openAttachSheet(); });
    ci.rows = 1; ci.maxLength = 300; ci.placeholder = 'Comment on this photo'; ci.value = it.comment; ci.autocomplete = 'off'; ci.enterKeyHint = 'done';
    ci.addEventListener('input', () => { it.comment = ci.value; fitBox(ci, cap); });   // grows to show everything you type, up to four lines
    ci.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); ci.blur(); } });   // one paragraph per photo; Done closes the keyboard
    grows.push(ci);
    rm.setAttribute('aria-label', 'Remove ' + attName(it.file)); rm.disabled = ATT.busy;
    const mid = el('span', 'at-mid'); mid.append(el('span', 'at-n', (n > 1 ? (i + 1) + '. ' : '') + attName(it.file) + (it.path ? '  ✓' : '')), ci);
    row.append(attThumb(it), mid, rm); list.append(row);
  });
  const more = btn('+ Add more', 'at-more', () => { ATT.add = true; $('filepick').click(); }); more.disabled = ATT.busy || n >= ATT_MAX;
  const status = el('div', 'at-status'); status.id = 'at-status';
  const go = btn(n === 1 ? 'Attach' : 'Attach ' + n, 'go', async () => { await attachAll(go, status); }); go.id = 'at-go'; go.disabled = ATT.busy;
  const row = el('div', 'btns'); row.append(btn('Cancel', '', () => { attDrop(); closeSheet(); }), go);
  box.append(list, more, status, row);
  openSheet(box);
  grows.forEach((t) => fitBox(t, cap)); setTimeout(() => grows.forEach((t) => fitBox(t, cap)), 90);   // once it is on screen to measure, and again after it has slid up
}
// Shrink and upload each file in turn, then write the paths and comments into that session's message box.
async function attachAll(go, status) {
  if (ATT.busy || !ATT.items.length || !ATT.to) return;
  const to = ATT.to; ATT.busy = true; go.disabled = true; $('attachbtn').classList.add('on');
  try {
    for (let i = 0; i < ATT.items.length; i++) {
      const it = ATT.items[i]; if (it.path) continue;
      status.textContent = 'Uploading ' + (i + 1) + ' of ' + ATT.items.length + '…';
      const { bytes, ext } = await prepareUpload(it.file);
      if (bytes.length > 8 * 1024 * 1024) throw new Error(attName(it.file) + ' is over 8 MB even after shrinking it.');
      const r = await api('api/upload?host=' + encodeURIComponent(to.host) + '&ext=' + encodeURIComponent(ext),
        { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Shepr': '1' }, body: bytes });
      it.path = r.path;
    }
    const many = ATT.items.length > 1, lines = [];
    ATT.items.forEach((it, i) => { lines.push(it.path); const c = it.comment.replace(/\s+/g, ' ').trim(); if (c) lines.push((many ? 'Photo ' + (i + 1) + ': ' : '') + c); });
    const block = lines.join('\n'), key = to.host + '|' + to.pane, count = ATT.items.length;
    if (S.cur && S.cur.host === to.host && S.cur.pane === to.pane) {
      const m = $('msg'); m.value = (m.value.trim() ? m.value.replace(/\s*$/, '\n') : '') + block + '\n'; autosize(); drafts.put(key, m.value);
      notify('Attached ' + count + (count === 1 ? ' file' : ' files') + '. Add a note if you like, then Send.', false);
    } else {   // you moved to another session while it uploaded: the draft of the one it was for gets it
      const d = drafts.get(key); drafts.put(key, (d.trim() ? d.replace(/\s*$/, '\n') : '') + block + '\n');
      notify('Attached ' + count + ' to the message waiting in ' + to.pane + '.', false);
    }
    attDrop();
    closeSheet();
  } catch (e) { status.textContent = e.message; status.classList.add('err'); go.disabled = false; notify(e.message, true); }
  finally { ATT.busy = false; $('attachbtn').classList.remove('on'); }
}

function stageKey(k) {
  if (S.keybuf.length >= 8) return;
  S.keybuf.push(k); paintStage();
}
function paintStage() {
  const st = $('stage');
  st.textContent = ''; st.hidden = S.keybuf.length === 0;
  if (st.hidden) return;
  const lab = el('div', 'dim', 'Keys: ' + S.keybuf.map((k) => KEYNAME[k] || k).join(' '));
  lab.style.cssText = 'flex:1 1 auto;align-self:center;font-weight:700;color:#fff';
  const clear = btn('Clear', '', () => { S.keybuf = []; paintStage(); });
  const send = btn('Send keys', '', () => prepare({ kind: 'keys', keys: S.keybuf.slice() }));
  clear.style.flex = '0 0 auto';
  send.style.cssText = 'flex:0 0 auto;padding:0 14px;color:#04141a;background:var(--accent);border-color:var(--accent)';
  st.append(lab, clear, send);
}

// Grow a text box to what is typed, up to maxLines WHOLE lines. A box that ends part-way through a line is how you lose sight of what you
// are typing. Collapsing to "auto" to measure resets the scroll on iOS, so the position is put back (or kept at the end while you type there).
function fitBox(t, maxLines) {
  const cs = getComputedStyle(t), lh = parseFloat(cs.lineHeight) || 22;
  const padV = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom), edge = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
  const keep = t.scrollTop, atEnd = t.selectionStart === t.value.length;
  t.style.height = 'auto';
  const lines = Math.max(1, Math.min(maxLines, Math.round((t.scrollHeight - padV) / lh)));
  t.style.height = Math.ceil(lines * lh + padV + edge) + 'px';
  t.scrollTop = atEnd ? t.scrollHeight : keep;
}
function autosize() { fitBox($('msg'), 5); }
$('msg').addEventListener('input', autosize);
$('msg').addEventListener('input', () => { if (S.cur) drafts.put(S.cur.host + '|' + S.cur.pane, $('msg').value); });
$('compose').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = $('msg').value.trim();
  if (text) prepare({ kind: 'text', text });
});

// ---- slash row: the last 6 skills/commands you used, tappable; MORE opens a searchable list of every one.
// Tapping only FILLS the box with "/name ". Nothing is sent: it still goes through the preview sheet like any prompt.
S.cmds = {};          // host -> {at, data}
S.slashOpen = false;
const cmdKey = (h) => 'shepr.slash.' + h;
function loadCmds(host) {
  const c = S.cmds[host];
  if (c && Date.now() - c.at < 5 * 60 * 1000) return Promise.resolve(c.data);
  return api('api/commands?host=' + encodeURIComponent(host)).then((d) => { S.cmds[host] = { at: Date.now(), data: d }; paintSlash(); return d; })
    .catch(() => null);
}
// What to show: this phone's own recent picks first (they are instant), then the host's history, no duplicates.
function recentCmds(host, data) {
  const mine = store.get(cmdKey(host), []);
  const byName = {}; ((data && data.all) || []).forEach((c) => { byName[c.name] = c; });
  const names = [];
  mine.concat(((data && data.recent) || []).map((c) => c.name)).forEach((n) => { if (n && names.indexOf(n) < 0) names.push(n); });
  return names.slice(0, 6).map((n) => byName[n] || { name: n, desc: '', kind: 'used' });
}
function noteSlashUse(host, text) {
  const m = /^\/([A-Za-z][\w:.-]*)(?:\s|$)/.exec(text || '');
  if (!m) return;
  const l = store.get(cmdKey(host), []).filter((x) => x !== m[1]); l.unshift(m[1]); store.set(cmdKey(host), l.slice(0, 12));
}
function fillCmd(name) {
  const t = $('msg'); t.value = '/' + name + ' '; autosize(); S.slashOpen = false; paintSlash();
  t.focus(); try { t.setSelectionRange(t.value.length, t.value.length); } catch (e) { /* ignore */ }
}
function paintSlash() {
  const row = $('slashrow'), btn_ = $('slashbtn');
  const st = S.agent ? S.agent.status : null;
  // A session sitting at a dialog takes typed text INTO the dialog: a slash command makes no sense there.
  const allowed = !!S.cur && !!S.agent && st !== 'blocked' && prefs.get('slash') === 'show';   // Settings > Slash commands
  btn_.hidden = !allowed;
  const v = $('msg').value;
  const typing = /^\/\S*$/.test(v);
  const show = allowed && (S.slashOpen || typing);
  btn_.classList.toggle('on', show);
  row.hidden = !show; row.textContent = '';
  if (!show) return;
  const data = (S.cmds[S.cur.host] || {}).data;
  let list;
  if (typing && v.length > 1) {
    const q = v.slice(1).toLowerCase();
    const all = (data && data.all) || [];
    list = all.filter((c) => c.name.toLowerCase().startsWith(q)).concat(all.filter((c) => !c.name.toLowerCase().startsWith(q) && c.name.toLowerCase().includes(q))).slice(0, 6);
  } else list = recentCmds(S.cur.host, data);
  list.forEach((c) => { const b = el('button', null, '/' + c.name); b.type = 'button'; b.addEventListener('click', () => fillCmd(c.name)); row.append(b); });
  if (!list.length) row.append(el('span', 'none', data ? 'No match' : 'Loading…'));
  const more = el('button', 'more', 'MORE'); more.type = 'button';
  more.addEventListener('click', () => openSlashSheet(typing ? v.slice(1) : ''));
  row.append(more);
}
function openSlashSheet(prefill) {
  const host = S.cur ? S.cur.host : null;
  if (!host) return;
  const box = el('div');
  box.style.cssText = 'display:flex;flex-direction:column;min-height:0';
  const inp = el('input', 'search'); inp.type = 'search'; inp.placeholder = 'Search skills and commands';
  inp.value = prefill || ''; inp.autocomplete = 'off'; inp.autocapitalize = 'off'; inp.spellcheck = false;
  const list = el('div', 'golist');
  const closeRow = el('div', 'btns'); closeRow.style.marginTop = '8px'; closeRow.append(btn('Close', '', closeSheet));
  box.append(inp, list, closeRow);
  const draw = (data) => {
    list.textContent = '';
    if (!data) { list.append(el('div', 'empty', 'Could not read the list from this host.')); return; }
    const recent = recentCmds(host, data).map((c) => c.name);
    const toks = inp.value.toLowerCase().replace(/^\//, '').split(/\s+/).filter(Boolean);
    const hits = data.all.filter((c) => toks.every((t) => (c.name + ' ' + c.desc).toLowerCase().includes(t)))
      .sort((a, b) => (recent.indexOf(a.name) < 0 ? 99 : recent.indexOf(a.name)) - (recent.indexOf(b.name) < 0 ? 99 : recent.indexOf(b.name)) || a.name.localeCompare(b.name));
    if (!hits.length) list.append(el('div', 'empty', 'Nothing matches.'));
    hits.slice(0, 80).forEach((c) => {
      const b = el('button', 'cmd' + (recent.indexOf(c.name) >= 0 ? ' rec' : '')); b.type = 'button';
      b.append(el('span', 'k', c.kind), el('span', 'n', '/' + c.name), el('span', 'd', c.desc || ' '));
      b.addEventListener('click', () => { closeSheet(); fillCmd(c.name); });
      list.append(b);
    });
    if (data.note) list.append(el('div', 'dim fine', data.note));
  };
  inp.addEventListener('input', () => draw((S.cmds[host] || {}).data));
  openSheet(box);
  list.append(el('div', 'empty', 'Loading…'));
  loadCmds(host).then(draw);
}
$('slashbtn').addEventListener('click', () => {
  S.slashOpen = !S.slashOpen; paintSlash();
  if (S.cur) loadCmds(S.cur.host);
});
$('msg').addEventListener('input', () => { paintSlash(); if (S.cur && /^\//.test($('msg').value)) loadCmds(S.cur.host); });

// ---- toasts: inside the session view, or floating above the tab bar elsewhere
let toastT = null;
function notify(msg, isErr, action, sticky) {
  const inDetail = !$('detail').hidden;
  let t = inDetail ? $('toast') : $('gtoast');
  if (!t) { t = el('div', 'toast gtoast'); t.id = 'gtoast'; $('app').append(t); }
  t.textContent = ''; t.className = 'toast' + (inDetail ? '' : ' gtoast') + (isErr ? ' err' : ''); t.hidden = false;
  t.append(el('span', 'grow', msg));
  if (inDetail && !isErr && S.cur) {
    const nxt = agents().filter((a) => a.status === 'blocked' && keyOf(a) !== S.cur.host + '|' + S.cur.pane);
    if (nxt.length) t.append(btn('Next waiting (' + nxt.length + ')', '', nextWaiting));
  }
  if (action) t.append(btn(action[0], '', action[1]));
  clearTimeout(toastT);
  if (!sticky) toastT = setTimeout(() => { t.hidden = true; }, isErr ? 12000 : 7000);
  if (isErr) t.append(btn('✕', '', () => { t.hidden = true; }));
}

// ---------------------------------------------------------------------------
// 11. Pairing: scan the QR the host shows, or type the code
// ---------------------------------------------------------------------------
const pairState = { stream: null, raf: 0, scanning: false };
function pairErr(msg) { const e = $('perr'); e.hidden = !msg; e.textContent = msg || ''; }
function defaultName() {
  const ua = navigator.userAgent || '';
  return store.get('shepr.name', /iPad/.test(ua) ? 'iPad' : /iPhone/.test(ua) ? 'iPhone' : 'Browser');
}
function showPair(reason, code) {
  $('pair').hidden = false;
  stopScanner();
  const host = (S.hello && S.hello.name) || location.hostname;
  $('ptitle').textContent = 'Pair with ' + host;
  pairErr(reason);
  $('pbtns').hidden = false; $('pform').hidden = true; $('pdone').hidden = true;
  if (code) showForm(code);
}
function showForm(code) {
  $('pbtns').hidden = true; $('pform').hidden = false;
  $('pcodein').value = code || '';
  $('pname').value = defaultName();
  setTimeout(() => (code ? $('pname') : $('pcodein')).focus(), 50);
}
$('pcmd').addEventListener('click', () => {
  const c = $('pcmd'); copyText('shepr pair', 'shepr pair');
  c.classList.add('copied'); setTimeout(() => c.classList.remove('copied'), 1200);
});
$('pcode').addEventListener('click', () => showForm(''));
$('pscan').addEventListener('click', startScanner);
$('pstop').addEventListener('click', () => { stopScanner(); $('pbtns').hidden = false; });
$('pcodein').addEventListener('input', (e) => {
  const raw = e.target.value.toUpperCase().replace(/[^0-9A-Z]/g, '').slice(0, 12);
  e.target.value = raw.replace(/(.{4})(?=.)/g, '$1-');
});

function parsePair(t) {
  t = String(t).trim();
  try {
    const u = new URL(t);
    const m = /shepr-pair=([A-Za-z0-9-]+)/.exec(u.hash || '');
    if (!m) return { err: 'That QR is not a Shepr pairing code.' };
    if (u.origin !== location.origin) return { err: 'That QR is for ' + u.host + ' but this app is served from ' + location.host + '.' };
    return { code: m[1] };
  } catch (e) { /* not a URL: maybe a bare code */ }
  const c = t.replace(/[^0-9A-Za-z]/g, '').toUpperCase();
  return c.length === 12 ? { code: c.replace(/(.{4})(?=.)/g, '$1-') } : { err: 'That does not look like a pairing code.' };
}

function loadScript(src) {
  return new Promise((res, rej) => {
    if (window.jsQR) return res();
    const s = document.createElement('script'); s.src = src; s.onload = res; s.onerror = () => rej(new Error('scanner failed to load'));
    document.head.append(s);
  });
}

async function startScanner() {
  pairErr(null);
  $('pbtns').hidden = true; $('pscanner').hidden = false; $('pscanner').classList.remove('hit');
  $('pscanmsg').textContent = 'Starting the camera…';
  try {
    pairState.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
  } catch (e) {
    stopScanner();
    pairErr('The camera is not available (' + (e.name || 'error') + '). Allow it in Settings, or type the code.');
    showForm('');
    return;
  }
  const v = $('pvideo');
  v.srcObject = pairState.stream;
  try { await v.play(); } catch (e) { /* autoplay quirks: the stream still decodes */ }
  const det = 'BarcodeDetector' in window ? new BarcodeDetector({ formats: ['qr_code'] }) : null;
  if (!det) {
    try { await loadScript('vendor/jsQR.js'); } catch (e) { stopScanner(); pairErr(e.message); showForm(''); return; }
  }
  $('pscanmsg').textContent = 'Point at the QR on your host';
  const cv = document.createElement('canvas'), cx = cv.getContext('2d', { willReadFrequently: true });
  pairState.scanning = true;
  const tick = async () => {
    if (!pairState.scanning) return;
    if (v.readyState >= 2 && v.videoWidth) {
      const w = Math.min(640, v.videoWidth), h = Math.round(w * v.videoHeight / v.videoWidth);
      cv.width = w; cv.height = h; cx.drawImage(v, 0, 0, w, h);
      let text = null;
      try {
        if (det) { const r = await det.detect(cv); text = r[0] && r[0].rawValue; }
        else { const r = window.jsQR(cx.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: 'dontInvert' }); text = r && r.data; }
      } catch (e) { /* a bad frame: try the next */ }
      if (text) {
        const p = parsePair(text);
        if (p.err) { $('pscanmsg').textContent = p.err; }
        else {
          pairState.scanning = false;
          $('pscanner').classList.add('hit'); $('pscanmsg').textContent = 'Got it';
          setTimeout(() => { stopScanner(); showForm(p.code); }, 450);
          return;
        }
      }
    }
    pairState.raf = setTimeout(tick, 110);
  };
  tick();
}
function stopScanner() {
  pairState.scanning = false; clearTimeout(pairState.raf);
  if (pairState.stream) { pairState.stream.getTracks().forEach((t) => t.stop()); pairState.stream = null; }
  $('pvideo').srcObject = null; $('pscanner').hidden = true;
}
document.addEventListener('visibilitychange', () => { if (document.hidden && pairState.scanning) { stopScanner(); $('pbtns').hidden = false; } });

$('pform').addEventListener('submit', async (e) => {
  e.preventDefault();
  pairErr(null);
  const code = $('pcodein').value, name = $('pname').value.trim() || 'iPhone';
  if (code.replace(/-/g, '').length !== 12) { pairErr('The code is 12 characters: XXXX-XXXX-XXXX.'); return; }
  $('pgo').disabled = true; $('pgo').textContent = 'Pairing…';
  try {
    if (!(window.crypto && crypto.subtle)) throw new Error('This browser has no secure crypto: open the HTTPS address, not http.');
    const k = await newKey();
    const res = await api('api/pair', { method: 'POST', public: true, headers: { 'Content-Type': 'application/json', 'X-Shepr': '1' },
                                        body: JSON.stringify({ code, jwk: k.jwk, name }) });
    S.dev = { id: res.device, key: k.key, jwk: k.jwk, name, host: res.host };
    await saveDevice(S.dev);
    store.set('shepr.name', name);
    try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (x) { /* optional */ }
    $('pdonemsg').textContent = 'Paired with ' + res.host;
    $('pdone').hidden = false;
    setTimeout(() => { $('pair').hidden = true; $('pdone').hidden = true; fetchQueue(true); switchTab('waiting'); }, REDUCED ? 300 : 1700);
  } catch (err) {
    pairErr(err.message);
  } finally {
    $('pgo').disabled = false; $('pgo').textContent = 'Pair';
  }
});

// ---------------------------------------------------------------------------
// 12. Keyboard: the on-screen keyboard shrinks the VISUAL viewport on iOS but not the layout
//     viewport, so the dock would sit underneath it. Follow visualViewport instead.
// ---------------------------------------------------------------------------
if (window.visualViewport) {
  const vv = window.visualViewport;
  const fit = () => {
    document.documentElement.style.setProperty('--vvh', vv.height + 'px');
    document.documentElement.style.setProperty('--vvt', vv.offsetTop + 'px');
    window.scrollTo(0, 0);   // WHY: iOS pans the locked page to reveal a focused field; pan it back.
  };
  vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); fit();
}

// ---------------------------------------------------------------------------
// 13. Splash and boot
// ---------------------------------------------------------------------------
const splash = $('splash');
const t0 = performance.now();
const lastSplash = store.get('shepr.splash', 0);
const splashPref = prefs.get('splash');   // Settings > Splash screen: Full, Short or Off
const quick = splashPref === 'short' || REDUCED || Date.now() - lastSplash < 20 * 60 * 1000;   // seen it recently: keep it short
if (quick) splash.classList.add('quick');
store.set('shepr.splash', Date.now());
const sver = $('sver'); if (sver) sver.textContent = BUILD;
let splashGone = false;
if (splashPref === 'off') { splashGone = true; splash.remove(); }
function hideSplash(force) {
  if (splashGone) return;
  const min = force ? 0 : quick ? 650 : 4000;   // the full one finishes typing its tagline at about 3.1 s: hold a little past that, not long
  const wait = Math.max(0, min - (performance.now() - t0));
  splashGone = true;
  setTimeout(() => { splash.classList.add('out'); setTimeout(() => splash.remove(), 600); }, wait);
}
splash.addEventListener('pointerdown', () => { splashGone = false; hideSplash(true); });

document.addEventListener('visibilitychange', () => { if (!document.hidden && S.dev) { checkNet(); fetchQueue(true); loadTail(); } });
let queueTimer = null, tailTimer = null;
function startPolls() {   // Settings > Refresh sets how often the queue and an open session are re-read
  clearInterval(queueTimer); clearInterval(tailTimer);
  const [qms, tms] = REFRESH_MS[prefs.get('refresh')];
  if (qms) queueTimer = setInterval(() => { if (!document.hidden && !S.sheetOpen && S.dev && $('pair').hidden) fetchQueue(false); }, qms);
  if (tms) tailTimer = setInterval(() => { if (S.cur && !S.sheetOpen && !document.hidden) loadTail(); }, tms);
}
startPolls();

async function boot() {
  buildTabs();
  try { await syncClock(); } catch (e) { setOnline(false); }
  try { S.dev = await loadDevice(); } catch (e) { S.dev = null; }
  const hash = decodeURIComponent(location.hash.slice(1));
  const pm = /^shepr-pair=([A-Za-z0-9-]+)$/.exec(hash);
  if (pm) {
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* ignore */ }
    showPair(null, pm[1]);
  } else if (!S.dev) {
    showPair(S.hello ? null : 'Cannot reach the host. Is Tailscale on?');
  } else {
    if (S.hello) await fetchQueue(true); else { fetchQueue(true); paintMain(); }   // host unreachable: show that now, keep trying behind it
    // A link like #gus/w4E:p1 opens that session directly.
    const m = hash.match(/^([^/]+)\/(w[0-9A-Za-z]+:p[0-9A-Za-z]+)$/);
    if (m) openDetail(m[1], m[2], 'all');
  }
  hideSplash(false);
}
boot();
window.shepr = { S, ping, fetchQueue, standalone, signHeaders, renderBlocks, appIsStale, BUILD, liveCut, syncHistory, prepareUpload, loadLedger, fmtDur, paceLive, budgetVerdict, drafts, prefs, PREFS, currentModel, seenStore, isSeen, finished, finishedSeen, paintMain, paintHostbar, budgetGuidance, hostRow, paintTailAge, subCard };
