// Push, end to end in a real browser: pair, subscribe through the Notifications card (the browser's push API is stubbed, the host
// side is real), watch the host send a signed empty push to a stand-in push service, then deliver a push event to the REAL service
// worker and read the notification it shows. Needs the test server on :8743 started with SHEPR_PUSH_ALLOW=127.0.0.1:9988.
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
import http from 'http';
const require = createRequire(process.env.PW_PATH || '/home/pi/.local/share/mise/installs/npm-playwright/latest/node_modules/');
const { chromium, devices } = require('playwright');
const BASE = 'http://localhost:8743', PUSH_PORT = 9988;
const SERVER = new URL('../server.py', import.meta.url).pathname;
const ENV = { ...process.env, SHEPR_STATE: process.env.SHEPR_STATE || '/tmp/claude-1000/shepr-state', SHEPR_URL: BASE,
              HERDR_SOCKET_PATH: process.env.HERDR_SOCKET_PATH || '/home/pi/.config/herdr/sessions/hrtest/herdr.sock' };
const res = []; const check = (n, c) => { res.push(!!c); console.log((c ? 'PASS ' : 'FAIL ') + n); };
const got = [];
const pushSvc = http.createServer((rq, rs) => { let n = 0; rq.on('data', (d) => { n += d.length; }); rq.on('end', () => { got.push({ path: rq.url, auth: rq.headers.authorization || '', ttl: rq.headers.ttl, bytes: n }); rs.writeHead(201); rs.end(); }); });
await new Promise((r) => pushSvc.listen(PUSH_PORT, '127.0.0.1', r));

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
check('the card then shows the two choices, "waiting on me" on and "finished" off', (await p.$$eval('#notifycard .chk input', (n) => n.map((x) => x.checked))).join() === 'true,false');
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

// a phone the host no longer trusts: the push must still show SOMETHING (iOS requires it) but never data
const me = await p.evaluate(() => window.shepr.S.dev.id);
execFileSync('python3', [SERVER, 'revoke', me], { env: ENV, encoding: 'utf8' });
await p.waitForTimeout(500);
await cdp.send('ServiceWorker.deliverPushMessage', { origin: BASE, registrationId: regId, data: '' });
await p.waitForTimeout(2500);
const n2 = await shown();
check('after the phone is revoked, a push shows a plain generic notice, with no names', n2.length === 1 && /needs you|Shepr/.test(n2[0].title + n2[0].body) && !/clarity|burnbar/.test(JSON.stringify(n2)));
check('revoking removed its push subscription from the host', !JSON.parse(require('fs').readFileSync((ENV.SHEPR_STATE) + '/push.json', 'utf8'))[me]);
check('no page errors', errs.length === 0);
if (errs.length) console.log(errs.join('\n'));
pushSvc.close(); await b.close();
process.exit(res.every(Boolean) ? 0 : 1);
