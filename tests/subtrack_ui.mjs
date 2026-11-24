// v55, in a real browser: the Subs tab tracks Claude and Grok by default; the buttons on it and in Settings > Subs choose which, and the host keeps the choice.
// Starts its OWN server on port 8748 with an empty HOME (no Claude, Codex, Grok or Kimi to read, so nothing is asked of any provider).   node tests/subtrack_ui.mjs
import { createRequire } from 'module';
import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;

const PORT = 8748, BASE = `http://localhost:${PORT}`;
const here = new URL('.', import.meta.url).pathname;
const SERVER = path.join(here, '..', 'server.py');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'shepr-subtrack-ui-'));
const STATE = path.join(TMP, 'state');
fs.mkdirSync(STATE, { mode: 0o700 });
const ENV = { ...process.env, SHEPR_STATE: STATE, SHEPR_URL: BASE, SHEPR_LOCAL_ONLY: '1',
              SHEPR_LABEL: 'hub', SHEPR_POLL: '3', SHEPR_WATCH: '0', HOME: TMP, CLAUDE_HOME: path.join(TMP, 'nope'), SHEPR_AUTOADD: '', SHEPR_SCAN_NETS: '',
              HERDR_SOCKET_PATH: path.join(TMP, 'no-herdr.sock') };
const srv = spawn('python3', [SERVER, String(PORT)], { env: ENV, stdio: ['ignore', 'ignore', 'pipe'] });
let srvErr = ''; srv.stderr.on('data', (d) => { srvErr += d; });
const ok = []; const errs = [];
const check = (name, cond) => { ok.push([name, !!cond]); console.log((cond ? 'PASS ' : 'FAIL ') + name); };
const finish = async (browser) => {
  if (browser) await browser.close().catch(() => {});
  srv.kill();
  fs.rmSync(TMP, { recursive: true, force: true });
  const failed = ok.filter((x) => !x[1]);
  if (errs.length) console.log(errs.join('\n'));
  console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASSED');
  process.exit(failed.length ? 1 : 0);
};
for (let i = 0; ; i++) {
  try { if ((await fetch(BASE + '/ping')).ok) break; } catch { /* not up yet */ }
  if (i > 60) { console.log('server did not start:\n' + srvErr); await finish(); }
  await new Promise((r) => setTimeout(r, 250));
}

const code = JSON.parse(execFileSync('python3', [SERVER, 'pair', '--no-wait', '--json'], { env: ENV, encoding: 'utf8' })).code;
const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const ctx = await browser.newContext({ ...devices['iPhone 14'], serviceWorkers: 'block' });
await ctx.addInitScript(() => { try { localStorage.setItem('shepr.splash', String(Date.now())); } catch (e) { /* ignore */ } });
const page = await ctx.newPage();
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
await page.goto(BASE + '/');
await page.waitForSelector('#pair:not([hidden])', { timeout: 15000 });
await page.click('#pcode'); await page.fill('#pcodein', code.toLowerCase().replace(/-/g, '')); await page.fill('#pname', 'Test iPhone'); await page.click('#pgo');
await page.waitForSelector('#pdone:not([hidden])', { timeout: 15000 });


await page.waitForFunction(() => window.shepr.S.q && window.shepr.S.hello, null, { timeout: 30000 });
await page.waitForSelector('#splash', { state: 'detached', timeout: 12000 }).catch(() => {});

const read = () => page.evaluate(() => ({ tab: window.shepr.S.tab, subs: [...document.querySelectorAll('#budget .trackrow .mini-btn')].map((b) => [b.dataset.provider, b.getAttribute('aria-pressed')]),
  set: [...document.querySelectorAll('#setbody .setrow .mini-btn[data-provider]')].map((b) => [b.dataset.provider, b.getAttribute('aria-pressed')]) }));
const tap = (sel) => page.evaluate((sel) => { const n = document.querySelector(sel); const r = n.getBoundingClientRect();
  const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
  n.dispatchEvent(new PointerEvent('pointerdown', o)); n.dispatchEvent(new PointerEvent('pointerup', o)); n.click(); }, sel);
const OUTD = path.join(os.tmpdir(), 'shepr-subtrack-shots'); fs.mkdirSync(OUTD, { recursive: true });
await page.evaluate(() => { window.shepr.prefs.set('refresh', 'manual'); window.shepr.switchTab('subs'); });
await page.waitForSelector('#budget .trackrow .mini-btn', { timeout: 15000 }).catch(async (e) => { console.log('budget html:', (await page.evaluate(() => (document.getElementById('budget') || {}).innerHTML || 'NO #budget, tab=' + window.shepr.S.tab)).slice(0, 600)); console.log(errs.join('\n')); throw e; });
let r = await read();
check('Subs tab: four buttons, Claude and Grok on, Codex and Kimi off by default', JSON.stringify(r.subs) === JSON.stringify([['claude', 'true'], ['codex', 'false'], ['kimi', 'false'], ['grok', 'true']]));
await page.screenshot({ path: path.join(OUTD, 'subs-default.png') });
const wide = await page.evaluate(() => { const vw = window.innerWidth; const out = []; document.querySelectorAll('#budget, #budget > *, #budget .trackrow .mini-btn').forEach((n) => { const r = n.getBoundingClientRect(); if (r.right > vw + 1 || r.left < -1) out.push((n.id || n.className || n.tagName) + ' ' + Math.round(r.left) + '..' + Math.round(r.right) + ' of ' + vw); }); return { vw, doc: document.documentElement.scrollWidth, out }; });
console.log('   widths:', JSON.stringify(wide));
check('Subs tab: nothing runs off the right edge of the screen', wide.out.length === 0 && wide.doc <= wide.vw);
await tap('#budget .trackrow .mini-btn[data-provider="codex"]');
await page.waitForFunction(() => document.querySelector('#budget .trackrow .mini-btn[data-provider="codex"]')?.getAttribute('aria-pressed') === 'true', null, { timeout: 8000 });
r = await read();
check('tapping Codex turns it on', r.subs.find((x) => x[0] === 'codex')[1] === 'true' && r.subs.find((x) => x[0] === 'claude')[1] === 'true');
const hv = await page.evaluate(() => window.shepr.api('api/budget'));
check('the host kept it: its own view lists Codex as tracked', hv.providers.find((p) => p.id === 'codex').tracked === true);
check('the choice is on disk in the host state folder, in the tab order', JSON.stringify(JSON.parse(fs.readFileSync(path.join(STATE, 'usage-tracked.json'), 'utf8'))) === JSON.stringify(['claude', 'codex', 'grok']));
await page.evaluate(() => window.shepr.switchTab('settings'));
await tap('#setseg [data-seg="subs"]');
await page.waitForSelector('#setbody .setrow .mini-btn[data-provider]', { timeout: 15000 });
r = await read();
check('Settings > Subs has one row each, in the same state', JSON.stringify(r.set) === JSON.stringify([['claude', 'true'], ['codex', 'true'], ['kimi', 'false'], ['grok', 'true']]));
const fits = await page.evaluate(() => { const b = document.getElementById('setbody'); return { sh: b.scrollHeight, ch: b.clientHeight, seg: document.getElementById('setseg').scrollWidth <= document.getElementById('setseg').clientWidth + 1 }; });
check('Settings > Subs: every row fits on the screen with no scrolling, and the five page buttons fit in their bar', fits.sh <= fits.ch + 1 && fits.seg);
await page.setViewportSize({ width: 390, height: 664 }); await page.waitForTimeout(400);
const fitsShort = await page.evaluate(() => { const b = document.getElementById('setbody'), m = document.getElementById('main'); return b.scrollHeight <= b.clientHeight + 1 && m.scrollHeight <= m.clientHeight + 1 && document.querySelector('.setfoot').getBoundingClientRect().bottom <= innerHeight; });
check('Settings > Subs: and on a short phone, 390x664, too', fitsShort);
await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(300);
await page.screenshot({ path: path.join(OUTD, 'settings.png') });
await tap('#setbody .setrow .mini-btn[data-provider="claude"]');
await page.waitForFunction(() => document.querySelector('#setbody .setrow .mini-btn[data-provider="claude"]')?.getAttribute('aria-pressed') === 'false', null, { timeout: 8000 });
await page.evaluate(() => window.shepr.switchTab('subs'));
await page.waitForSelector('#budget .trackrow .mini-btn', { timeout: 15000 });
r = await read();
check('turning Claude off in Settings shows on the Subs tab too', r.subs.find((x) => x[0] === 'claude')[1] === 'false');
for (const id of ['codex', 'grok']) { await tap('#budget .trackrow .mini-btn[data-provider="' + id + '"]'); await page.waitForFunction((id) => document.querySelector('#budget .trackrow .mini-btn[data-provider="' + id + '"]')?.getAttribute('aria-pressed') === 'false', id, { timeout: 8000 }); }
const msg = await page.evaluate(() => document.querySelector('#budget .bd-empty')?.textContent || '');
check('with none tracked the tab says so and points at Settings, and the buttons stay to turn one back on', /Choose which ones in Settings/.test(msg) && (await read()).subs.length === 4);
await page.screenshot({ path: path.join(OUTD, 'subs-none.png') });
await finish(browser);
