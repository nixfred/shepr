// Regenerates the README screenshots from the RUNNING app against the isolated fake herdr session. Never a mock:
// every image is a capture. Needs: tests/fake-session.sh up, a test server on :8743 (see ui.mjs header).
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
import fs from 'fs';
// Playwright is a global install, not an npm dependency (nixweb rule). Look where a stranger would have it.
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;
const BASE = 'http://localhost:8743';
const SERVER = new URL('../server.py', import.meta.url).pathname;
const OUT = new URL('../docs/img/', import.meta.url).pathname;
const ENV = { ...process.env, SHEPR_STATE: process.env.SHEPR_STATE || '/tmp/claude-1000/shepr-state', SHEPR_URL: BASE,
  HERDR_SOCKET_PATH: process.env.HERDR_SOCKET_PATH || '/home/pi/.config/herdr/sessions/hrtest/herdr.sock' };
const code = JSON.parse(execFileSync('python3', [SERVER, 'pair', '--no-wait', '--json'], { env: ENV, encoding: 'utf8' })).code;
const dev = { ...devices['iPhone 14'], deviceScaleFactor: 2 };
const b = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const ctx = await b.newContext({ ...dev, serviceWorkers: 'block' });
const p = await ctx.newPage();
fs.mkdirSync(OUT + 'shots', { recursive: true });
const tap = (sel) => p.evaluate((sel) => { const n = document.querySelector(sel); const r = n.getBoundingClientRect();
  const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
  n.dispatchEvent(new PointerEvent('pointerdown', o)); n.dispatchEvent(new PointerEvent('pointerup', o)); }, sel);
const shot = (n) => p.screenshot({ path: `${OUT}shots/${n}.png` });

await p.goto(BASE + '/');
await p.waitForSelector('#pair:not([hidden])'); await p.waitForSelector('#splash', { state: 'detached', timeout: 8000 });
await p.waitForTimeout(500); await shot('pair');
await p.click('#pcode'); await p.fill('#pcodein', code); await p.fill('#pname', 'Fred’s iPhone'); await p.click('#pgo');
await p.waitForSelector('.item', { timeout: 10000 }); await p.waitForTimeout(2500);
await shot('queue');
// a blocked session with a dialog
const blocked = await p.evaluate(() => window.shepr.S.q.agents.find((a) => a.status === 'blocked' && a.project === 'burnbar'));
await tap(`.item[data-key="${blocked.host}|${blocked.pane}"]`);
await p.waitForSelector('#optrow button'); await p.waitForTimeout(900); await shot('session-dialog');
await p.click('#dback'); await p.waitForTimeout(400);
// a sleeping session with readable text
const idle = await p.evaluate(() => window.shepr.S.q.agents.find((a) => a.status === 'idle'));
await tap(`.item[data-key="${idle.host}|${idle.pane}"]`);
await p.waitForSelector('#tail .tl'); await p.waitForTimeout(900);
await p.evaluate(() => { const t = document.getElementById('tail'); t.scrollTop = t.scrollHeight; });   // the bottom: where the status chips are
await p.waitForTimeout(300);
await shot('session-text');
await p.click('#slashbtn'); await p.waitForSelector('#slashrow button:not(.more)'); await p.waitForTimeout(500);
await shot('slash');
await p.click('#slashbtn');
await p.click('#dback'); await p.waitForTimeout(400);
// wake: review sheet (nothing is sent)
await p.click('#main .secrow .mini-btn'); await p.waitForSelector('#sheet .chk'); await p.waitForTimeout(500);
await p.click('#sheet .btns .go'); await p.waitForSelector('#sheet .kv'); await p.waitForTimeout(900);
await shot('wake-confirm');
await p.click('#sheet .btns button:first-child');
await tap('.tab[data-view=settings]'); await p.waitForTimeout(1200); await shot('settings');
await b.close();

// ---- the montage: five captures in phone frames
const names = [['queue', 'QUEUE', 'needs you first, then sleeping'], ['session-dialog', 'ANSWER A DIALOG', 'options become buttons'], ['session-text', 'READABLE TEXT', 'reflowed, status as chips'], ['slash', 'SKILLS', 'last 6 used + MORE'], ['wake-confirm', 'WAKE · CONFIRM FIRST', 'every target, the exact text']];
const img = (n) => 'data:image/png;base64,' + fs.readFileSync(`${OUT}shots/${n}.png`).toString('base64');
const html = `<html><body style="margin:0;background:#070a0f;font-family:JetBrains Mono,ui-monospace,monospace"><div id="c" style="width:1400px;padding:34px 40px 26px;box-sizing:border-box;position:relative;
background:radial-gradient(90% 70% at 50% 40%,#0d2233,#070a0f 70%);background-image:linear-gradient(rgba(0,240,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(0,240,255,.035) 1px,transparent 1px);background-size:40px 40px">
<div style="display:flex;gap:24px;justify-content:center">${names.map(([n, t, s]) => `<div style="width:240px"><div style="border-radius:34px;padding:7px;background:#0b1018;border:2px solid #1a2635;box-shadow:0 0 30px rgba(0,240,255,.12)"><img src="${img(n)}" style="width:100%;display:block;border-radius:27px"></div>
<div style="margin-top:14px;text-align:center;color:#00f0ff;font-weight:800;letter-spacing:3px;font-size:14px">${t}</div><div style="text-align:center;color:#8694a5;font-size:12.5px;margin-top:4px">${s}</div></div>`).join('')}</div>
<div style="text-align:center;color:#8694a5;font-size:12px;letter-spacing:1.5px;margin-top:22px">CAPTURED FROM THE RUNNING APP AGAINST AN ISOLATED TEST HERDR SESSION · THE AGENTS AND THEIR TITLES ARE MADE UP</div></div></body></html>`;
const b2 = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const pg = await b2.newPage({ viewport: { width: 1400, height: 800 }, deviceScaleFactor: 1 });
await pg.setContent(html); await pg.waitForTimeout(500);
const el = await pg.$('#c'); await el.screenshot({ path: OUT + 'screens.png' });
await b2.close();
console.log('wrote', OUT + 'screens.png');
