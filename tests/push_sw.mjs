// Push, end to end in a real browser: pair, subscribe through the Notifications card (the browser's push API is stubbed, the host
// side is real), watch the host send a signed empty push to a stand-in push service, then deliver a push event to the REAL service
// worker and read the notification it shows. Needs the test server on :8743 started with SHEPR_PUSH_ALLOW=127.0.0.1:9988.
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
import fs from 'fs';
import http from 'http';
import vm from 'vm';
// Playwright is a global install, not an npm dependency (nixweb rule). Look where a stranger would have it.
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;
const BASE = 'http://localhost:8743', PUSH_PORT = 9988;
const SERVER = new URL('../server.py', import.meta.url).pathname;
const ENV = { ...process.env, SHEPR_STATE: process.env.SHEPR_STATE || '/tmp/claude-1000/shepr-state', SHEPR_URL: BASE,
              HERDR_SOCKET_PATH: process.env.HERDR_SOCKET_PATH || '/home/pi/.config/herdr/sessions/hrtest/herdr.sock' };
const res = []; const check = (n, c) => { res.push(!!c); console.log((c ? 'PASS ' : 'FAIL ') + n); };
const got = [];
const pushSvc = http.createServer((rq, rs) => { let n = 0; rq.on('data', (d) => { n += d.length; }); rq.on('end', () => { got.push({ path: rq.url, auth: rq.headers.authorization || '', ttl: rq.headers.ttl, bytes: n }); rs.writeHead(201); rs.end(); }); });
await new Promise((r) => pushSvc.listen(PUSH_PORT, '127.0.0.1', r));

// The lock screen must agree with the app's Finished list: a finished session you already opened is not announced again.
// The worker cannot read localStorage, so the app mirrors what you opened into IndexedDB; the worker's wording is tested directly here.
const swCtx = { self: { addEventListener() {} }, caches: {}, clients: {}, indexedDB: {}, crypto: {}, location: { origin: 'http://localhost' }, fetch() {}, btoa, TextEncoder, URL, Response };
vm.createContext(swCtx); vm.runInContext(fs.readFileSync(process.env.SW_UNDER_TEST || new URL('../sw.js', import.meta.url), 'utf8'), swCtx);
const say = swCtx.swDescribe;
const ag = (pane, status, seq, w) => ({ host: 'gus', pane, status, seq, wlabel: w, project: w });
const two = { agents: [ag('w1:p1', 'done', 3, 'alpha'), ag('w2:p1', 'done', 9, 'bravo'), ag('w3:p1', 'working', 1, 'charlie')] };
const d1 = say(two, { 'gus|w1:p1': { seq: 3 } });
check('lock screen: a finished session you already opened is not announced again', d1.title === 'bravo finished' && d1.body === 'bravo');
const d2 = say(two, { 'gus|w1:p1': { seq: 3 }, 'gus|w2:p1': { seq: 9 } });
check('lock screen: when every finished session was opened, it says nothing is waiting, with no names', d2.title === 'Shepr' && /Nothing is waiting/.test(d2.body) && !/alpha|bravo/.test(JSON.stringify(d2)));
const d3 = say(two, { 'gus|w1:p1': { seq: 2 } });
check('lock screen: a session that finished AGAIN after you opened it (new seq) is announced', d3.title === '2 agents finished' && d3.body === 'alpha, bravo');
check('lock screen: no usable record of what you opened (missing, null, a list, a string) behaves as before', [undefined, null, [], 'x'].every((x) => say(two, x).title === '2 agents finished'));
const d5 = say({ agents: [ag('w1:p1', 'blocked', 4, 'alpha'), ag('w2:p1', 'done', 9, 'bravo')] }, { 'gus|w1:p1': { seq: 4 } });
check('lock screen: a waiting session is always announced, opened or not', d5.title === 'alpha is waiting on you' && d5.count === 1);
// v49: the push is empty, so the host says (in the queue the worker fetches) what it pushed for, and the wording follows that. Still names and
// Shepr's own labels only: never a command, never a conversation title.
const w = (why, agents) => say({ agents, pushed: { why }, quiet_min: 15 }, {});
const risky = Object.assign(ag('w1:p1', 'working', 2, 'alpha'), { risk: { level: 'danger', what: 'discarded local changes (git reset --hard)', t: 1 } });
const r1 = w('risk', [risky, ag('w2:p1', 'blocked', 4, 'bravo')]);
check('lock screen: a risky command is announced by project and by Shepr\'s own label, never by the command', r1.title === 'alpha ran something risky' && r1.body === 'discarded local changes' && r1.count === 1 && !/reset --hard/.test(JSON.stringify([r1.title, r1.body])));
check('lock screen: a risky command from a session that is no longer open still says where to look', w('risk', [ag('w2:p1', 'done', 4, 'bravo')]).title === 'An agent ran something risky');
const s1 = w('blocked+stopped', [ag('w1:p1', 'blocked', 4, 'alpha'), ag('w2:p1', 'done', 9, 'bravo'), ag('w3:p1', 'idle', 1, 'charlie')]);
check('lock screen: everything stopping says so, with how many wait and how many finished', s1.title === 'Everything has stopped' && s1.body === '1 waiting on you, 1 finished' && s1.target.pane === 'w1:p1');
check('lock screen: ...but not when something is working again by the time the phone asks', w('stopped', [ag('w1:p1', 'blocked', 4, 'alpha'), ag('w3:p1', 'working', 1, 'charlie')]).title === 'alpha is waiting on you');
const q1 = w('quiet', [Object.assign(ag('w1:p1', 'working', 2, 'alpha'), { quiet_s: 17 * 60 }), Object.assign(ag('w2:p1', 'working', 2, 'bravo'), { quiet_s: 60 })]);
check('lock screen: an agent gone quiet is named, with for how long', q1.title === 'alpha has gone quiet' && q1.body === 'No output for 17 minutes.' && q1.target.pane === 'w1:p1');
check('lock screen: a push for the usual reasons reads as it always did', w('blocked', [ag('w1:p1', 'blocked', 4, 'alpha')]).title === 'alpha is waiting on you' && say({ agents: [ag('w1:p1', 'blocked', 4, 'alpha')] }, {}).title === 'alpha is waiting on you');
if (process.env.SW_ONLY) { process.exit(res.every(Boolean) ? 0 : 1); }

const b = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const ctx = await b.newContext({ ...devices['iPhone 14'], permissions: ['notifications'] });
const p = await ctx.newPage();
const errs = []; p.on('pageerror', (e) => errs.push(e.message));
const code = JSON.parse(execFileSync('python3', [SERVER, 'pair', '--no-wait', '--json'], { env: ENV, encoding: 'utf8' })).code;
await p.goto(BASE + '/'); await p.waitForSelector('#pair:not([hidden])', { timeout: 10000 });
await p.click('#pcode'); await p.fill('#pcodein', code); await p.fill('#pname', 'push test phone'); await p.click('#pgo');
await p.waitForSelector('.item', { timeout: 12000 });
await p.evaluate(() => navigator.serviceWorker.ready.then(() => true)); await p.reload(); await p.waitForSelector('.item'); await p.waitForTimeout(800);
check('the worker controls the page', await p.evaluate(() => !!navigator.serviceWorker.controller));

// the browser's push service cannot run here, so subscribing is stubbed; everything on the host is real
await p.evaluate((port) => {
  const sub = { endpoint: `http://127.0.0.1:${port}/push/phone1`, toJSON() { return { endpoint: this.endpoint, keys: { p256dh: 'BP', auth: 'au' } }; }, unsubscribe: async () => true };
  window.__subs = [];
  PushManager.prototype.getSubscription = async () => window.__subs[0] || null;
  PushManager.prototype.subscribe = async (o) => { window.__subKey = Array.from(new Uint8Array(o.applicationServerKey)); window.__subs = [sub]; return sub; };
}, PUSH_PORT);
await p.click('.tab[data-view=settings]'); await p.click('#setseg [data-seg=alerts]'); await p.waitForSelector('#notifycard');
check('keys: the Notifications card offers to turn them on', !!(await p.$('#pushon')));
await p.screenshot({ path: '/tmp/claude-1000/shots/n1-notify-card.png' });
await p.click('#pushon'); await p.waitForSelector('#notifycard .chk', { timeout: 8000 });
const cfg = await p.evaluate(async () => { const S = window.shepr.S; return { c: S.push, key: window.__subKey }; });
check('subscribing sends the host\'s VAPID key to the browser (65 bytes, uncompressed) and the host stores the subscription', cfg.key.length === 65 && cfg.key[0] === 4 && cfg.c.subscribed);
check('the card then shows the five choices: "waiting on me" on, and finished, everything stopped, gone quiet and ran something risky off until asked for', (await p.$$eval('#notifycard .chk input', (n) => n.map((x) => x.checked))).join() === 'true,false,false,false,false'
  && /Everything has stopped/.test(await p.textContent('#notifycard')) && /gone quiet \(15 min/.test(await p.textContent('#notifycard')) && /ran something risky/.test(await p.textContent('#notifycard')));
await p.screenshot({ path: '/tmp/claude-1000/shots/n2-notify-on.png' });
await p.click('#notifycard .chk:nth-of-type(2) input'); await p.waitForTimeout(500);
check('turning on "finished" is saved on the host', (await p.evaluate(() => window.shepr.S.push.prefs)).done === true);
got.length = 0;
await p.click('#notifycard .btns button:first-child'); await p.waitForTimeout(1200);
console.log('   test push got:', JSON.stringify(got.map((g) => ({ path: g.path, bytes: g.bytes, ttl: g.ttl, auth: g.auth.slice(0, 40) }))));
check('"Send a test" makes the host send ONE empty push to the push service, signed with its VAPID key', got.length === 1 && got[0].path === '/push/phone1' && got[0].bytes === 0 && /^vapid t=[\w-]+\.[\w-]+\.[\w-]+, k=[\w-]{87}$/.test(got[0].auth) && got[0].ttl === '3600');

// deliver a push to the REAL service worker and read what it shows
const cdp = await ctx.newCDPSession(p); let regId = null;
cdp.on('ServiceWorker.workerRegistrationUpdated', (e) => { if (e.registrations && e.registrations[0]) regId = e.registrations[0].registrationId; });
await cdp.send('ServiceWorker.enable'); await p.waitForTimeout(800);
check('the service worker registration is visible to the test', !!regId);
const shown = async () => p.evaluate(async () => (await (await navigator.serviceWorker.ready).getNotifications()).map((n) => ({ title: n.title, body: n.body, tag: n.tag, data: n.data })));
await cdp.send('ServiceWorker.deliverPushMessage', { origin: BASE, registrationId: regId, data: '' });
await p.waitForTimeout(2500);
const n1 = await shown();
console.log('   notification:', JSON.stringify(n1));
check('an EMPTY push makes the worker fetch the queue (signed) and show who is waiting, by name', n1.length === 1 && /agents are waiting on you/.test(n1[0].title) && /clarity/.test(n1[0].body) && n1[0].tag === 'shepr-queue');
check('the notification carries NO conversation text, only project names', !/Do you want|proceed|Bug fixes/.test(JSON.stringify(n1)));
check('it knows which session to open when tapped', !!(n1[0].data && n1[0].data.host && /^w[0-9A-Za-z]+:p/.test(n1[0].data.pane)));
await cdp.send('ServiceWorker.deliverPushMessage', { origin: BASE, registrationId: regId, data: '' });
await p.waitForTimeout(2000);
check('a second push replaces the first (same tag), it does not stack', (await shown()).length === 1);

// tapping it: the worker messages the open page, which opens that session
await p.click('.tab[data-view=waiting]'); await p.waitForTimeout(300);
await p.evaluate((d) => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { type: 'open', host: d.host, pane: d.pane } })), n1[0].data);
await p.waitForSelector('#detail:not([hidden])', { timeout: 5000 });
check('tapping the notification opens that session in the app', (await p.textContent('#dtitle')).length > 0);
await p.click('#dback').catch(() => {});

// the app keeps that record where the worker can read it
const mirrored = await p.evaluate(async () => {
  window.shepr.seenStore.mark('gus|w9:p9', 42);
  for (let i = 0; i < 20; i++) {
    const v = await new Promise((res) => { const r = indexedDB.open('shepr', 1); r.onsuccess = () => { const g = r.result.transaction('kv', 'readonly').objectStore('kv').get('seen'); g.onsuccess = () => res(g.result); g.onerror = () => res(null); }; r.onerror = () => res(null); });
    if (v && v['gus|w9:p9']) return v['gus|w9:p9'].seq;
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
});
check('opening a finished session writes it to IndexedDB, where the notification worker reads it', mirrored === 42);
await p.evaluate(({ host, pane }) => { const a = window.shepr.S.q.agents.find((x) => x.host === host && x.pane === pane); window.shepr.seenStore.mark(host + '|' + pane, a.seq); }, n1[0].data);
await p.waitForTimeout(400);
await cdp.send('ServiceWorker.deliverPushMessage', { origin: BASE, registrationId: regId, data: '' });
await p.waitForTimeout(2500);
const n3 = await shown();
check('the real worker still announces a waiting session that was marked opened (only finished ones are filtered)', n3.length === 1 && /waiting on you/.test(n3[0].title));

// a phone the host no longer trusts: the push must still show SOMETHING (iOS requires it) but never data
const me = await p.evaluate(() => window.shepr.S.dev.id);
execFileSync('python3', [SERVER, 'revoke', me], { env: ENV, encoding: 'utf8' });
await p.waitForTimeout(500);
await cdp.send('ServiceWorker.deliverPushMessage', { origin: BASE, registrationId: regId, data: '' });
await p.waitForTimeout(2500);
const n2 = await shown();
check('after the phone is revoked, a push shows a plain generic notice, with no names', n2.length === 1 && /needs you|Shepr/.test(n2[0].title + n2[0].body) && !/clarity|burnbar/.test(JSON.stringify(n2)));
check('revoking removed its push subscription from the host', !JSON.parse(fs.readFileSync((ENV.SHEPR_STATE) + '/push.json', 'utf8'))[me]);
check('no page errors', errs.length === 0);
if (errs.length) console.log(errs.join('\n'));
pushSvc.close(); await b.close();
process.exit(res.every(Boolean) ? 0 : 1);
