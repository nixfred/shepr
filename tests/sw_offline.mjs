// Service worker: network-first shell with a cache fallback. Needs a running test server on :8743 (see ui.mjs header).
import { createRequire } from 'module';
// Playwright is a global install, not an npm dependency (nixweb rule). Look where a stranger would have it.
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;
const b = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const ctx = await b.newContext({ ...devices['iPhone 14'] });
const p = await ctx.newPage();
const res = [];
const check = (n, c) => { res.push(!!c); console.log((c ? 'PASS ' : 'FAIL ') + n); };
await p.goto('http://localhost:8743/');
await p.evaluate(() => navigator.serviceWorker.ready.then(() => true));
await p.reload(); await p.waitForTimeout(800);                       // now controlled by the worker
check('service worker controls the page', await p.evaluate(() => !!navigator.serviceWorker.controller));
const cached = await p.evaluate(async () => (await caches.keys()).length && (await (await caches.open((await caches.keys())[0])).keys()).map((r) => new URL(r.url).pathname));
check('shell is cached (index, js, css)', ['/index.html', '/app.js', '/app.css'].every((x) => cached.includes(x) || (x === '/index.html' && cached.includes('/'))));
await ctx.setOffline(true);
await p.reload({ waitUntil: 'domcontentloaded' });
check('offline: the app still opens from cache', (await p.title()).includes('Shepr') && !!(await p.$('#app')));
check('offline: the cached app.js runs (tabs built)', (await p.$$('.tab')).length === 5);
await b.close();
process.exit(res.every(Boolean) ? 0 : 1);
