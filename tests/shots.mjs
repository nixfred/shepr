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

// ---- the watch (v49). Real captures as well: the burn and the risky commands come from the fixture the server was started with
// (SHEPR_WATCH_FIXTURE, SHEPR_WATCH_S=5), the events from the server's own log, the git project from `fake-session.sh changes`.
{
  const WF = process.env.SHEPR_WATCH_FIXTURE || '/tmp/claude-1000/shepr-watch.json', nowS = Date.now() / 1000, rid = 'shot-' + Date.now();
  const fsh = new URL('./fake-session.sh', import.meta.url).pathname;
  const brow = (sid, share, out, ctx, replies, model, helpers, cwd) => ({ sid, share, out, ctx, think: 0, replies, model, models: [model], helpers, last: nowS - 60, cwd, title: '' });
  fs.writeFileSync(WF, JSON.stringify({
    burn: { rows: [brow('shot-1', 0.46, 412000, 88200000, 212, 'opus', 0.22, '/home/u/Projects/burnbar'), brow('shot-2', 0.27, 268000, 51000000, 140, 'sonnet', 0, '/home/u/Projects/clarity'),
      brow('shot-3', 0.15, 97000, 30500000, 77, 'fable', 0, '/home/u/Projects/blip'), brow('shot-4', 0.07, 61000, 9800000, 41, 'sonnet', 0, '/home/u/Projects/infomarchy'), brow('shot-5', 0.03, 22000, 2100000, 12, 'haiku', 0, '/home/u/Projects/whats-next')],
      others: { n: 4, share: 0.02 }, sessions: 9, replies: 498, out: 871000, ctx: 184000000, series: [0.05, 0.1, 0.08, 0.2, 0.35, 0.3, 0.5, 0.42, 0.6, 0.55, 0.7, 0.62, 0.4, 0.3, 0.45, 0.8, 1, 0.9, 0.75, 0.6, 0.5, 0.66, 0.7, 0.58], models: [], now: nowS },
    risks: [{ t: nowS - 480, sid: 'shot-1', level: 'danger', what: 'force push', cmd: 'git push --force origin main', id: rid + '-1', cwd: '/home/u/Projects/burnbar', sub: false },
      { t: nowS - 5400, sid: 'shot-3', level: 'note', what: 'recursive delete outside a scratch folder', cmd: 'rm -rf ~/Projects/blip/old-build', id: rid + '-2', cwd: '/home/u/Projects/blip', sub: true }] }));
  const q = await p.evaluate(() => window.shepr.S.q.agents.map((a) => ({ host: a.host, pane: a.pane, status: a.status, name: a.wlabel || a.project })));
  const bl = q.find((a) => a.status === 'blocked'), me = bl.host;
  fs.appendFileSync(ENV.SHEPR_STATE + '/events.jsonl', [
    { t: nowS - 7000, ev: 'blocked', host: bl.host, pane: bl.pane, label: bl.name, q: 'Ship the release to production?' },
    { t: nowS - 6400, ev: 'answered', host: bl.host, pane: bl.pane, label: bl.name, wait: 600 },
    { t: nowS - 5200, ev: 'done', host: me, pane: 'w99:p1', label: 'clarity' },
    { t: nowS - 3000, ev: 'quiet', host: me, pane: 'w98:p1', label: 'blip', mins: 17 },
    { t: nowS - 900, ev: 'stopped', blocked: 3, done: 1 }].map((e) => JSON.stringify(e)).join('\n') + '\n');
  const logged = () => p.evaluate(async () => { const u = 'api/activity?limit=300'; const d = await (await fetch(u, { headers: await window.shepr.signHeaders('GET', u, '') })).json(); return (d.items || []).some((e) => e.ev === 'risk' && e.label === 'burnbar'); });   // the one this script's own fixture names
  for (let i = 0; i < 30 && !(await logged()); i++) await p.waitForTimeout(700);
  const rowTap = (sel, pick) => p.evaluate(({ sel, pick }) => { const r = [...document.querySelectorAll(sel)].find((x) => !pick || x.dataset.path === pick || x.dataset.key === pick); const b = r.getBoundingClientRect(), o = { bubbles: true, clientX: b.x + 30, clientY: b.y + 12, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
    r.dispatchEvent(new PointerEvent('pointerdown', o)); r.dispatchEvent(new PointerEvent('pointerup', o)); }, { sel, pick });
  const sheetBtn = (text) => p.evaluate((text) => [...document.querySelectorAll('#sheet .btns button')].find((b) => b.textContent === text).click(), text);
  await tap('.tab[data-view=waiting]'); await p.waitForTimeout(300);
  await p.evaluate(() => { window.shepr.S.catchUp = null; localStorage.setItem('shepr.lastLook', JSON.stringify(Date.now() - 133 * 60000)); window.shepr.checkAway(); return window.shepr.loadActivity(true); });
  await p.waitForSelector('#catchup', { timeout: 8000 }); await p.waitForSelector('#activity'); await p.waitForTimeout(500); await shot('watch-queue');
  await p.click('#activity'); await p.waitForSelector('#actlist .arow'); await p.waitForTimeout(600); await shot('watch-activity');
  await sheetBtn('Close'); await p.waitForTimeout(300);
  await tap('.tab[data-view=subs]'); await p.waitForSelector('#burnbtn', { timeout: 10000 }); await p.waitForTimeout(3000);
  await p.click('#burnbtn'); await p.waitForSelector('#burncard .bn-r', { timeout: 10000 }); await p.waitForTimeout(700); await shot('watch-burn');
  await sheetBtn('Close'); await p.waitForTimeout(300);
  execFileSync('bash', [fsh, 'changes'], { env: ENV, encoding: 'utf8' });
  await tap('.tab[data-view=waiting]');
  let ship = null;
  for (let i = 0; i < 20 && !ship; i++) { await p.evaluate(() => window.shepr.fetchQueue(true)); await p.waitForTimeout(600); ship = await p.evaluate(() => window.shepr.S.q.agents.find((a) => (a.wlabel || a.project) === 'shipyard') || null); }
  await rowTap('.item', ship.host + '|' + ship.pane); await p.waitForSelector('#detail:not([hidden])'); await p.waitForTimeout(900);
  await p.click('#dchanges'); await p.waitForSelector('#chgfiles .crow', { timeout: 8000 }); await p.waitForTimeout(500); await shot('watch-changes');
  await rowTap('#chgfiles .crow', 'loader.py'); await p.waitForSelector('#dview .dl', { timeout: 8000 }); await p.waitForTimeout(500); await shot('watch-diff');
  await sheetBtn('Close'); await p.click('#dback'); await p.waitForTimeout(300);
  execFileSync('bash', [fsh, 'changes-rm'], { env: ENV, encoding: 'utf8' });
}
await b.close();

// ---- the montages: five captures in phone frames, twice (the app, then the watch)
const img = (n) => 'data:image/png;base64,' + fs.readFileSync(`${OUT}shots/${n}.png`).toString('base64');
const montage = (names, note) => `<html><body style="margin:0;background:#070a0f;font-family:JetBrains Mono,ui-monospace,monospace"><div id="c" style="width:1400px;padding:34px 40px 26px;box-sizing:border-box;position:relative;
background:radial-gradient(90% 70% at 50% 40%,#0d2233,#070a0f 70%);background-image:linear-gradient(rgba(0,240,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(0,240,255,.035) 1px,transparent 1px);background-size:40px 40px">
<div style="display:flex;gap:24px;justify-content:center">${names.map(([n, t, s]) => `<div style="width:240px"><div style="border-radius:34px;padding:7px;background:#0b1018;border:2px solid #1a2635;box-shadow:0 0 30px rgba(0,240,255,.12)"><img src="${img(n)}" style="width:100%;display:block;border-radius:27px"></div>
<div style="margin-top:14px;text-align:center;color:#00f0ff;font-weight:800;letter-spacing:3px;font-size:14px">${t}</div><div style="text-align:center;color:#8694a5;font-size:12.5px;margin-top:4px">${s}</div></div>`).join('')}</div>
<div style="text-align:center;color:#8694a5;font-size:12px;letter-spacing:1.5px;margin-top:22px">${note}</div></div></body></html>`;
const b2 = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const pg = await b2.newPage({ viewport: { width: 1400, height: 800 }, deviceScaleFactor: 1 });
for (const [file, names, note] of [
  ['screens.png', [['queue', 'QUEUE', 'needs you first, then sleeping'], ['session-dialog', 'ANSWER A DIALOG', 'options become buttons'], ['session-text', 'READABLE TEXT', 'reflowed, status as chips'], ['slash', 'SKILLS', 'last 6 used + MORE'], ['wake-confirm', 'WAKE · CONFIRM FIRST', 'every target, the exact text']],
    'CAPTURED FROM THE RUNNING APP AGAINST AN ISOLATED TEST HERDR SESSION · THE AGENTS AND THEIR TITLES ARE MADE UP'],
  ['watch.png', [['watch-queue', 'WHILE YOU WERE AWAY', 'what changed since you looked'], ['watch-activity', 'ACTIVITY', 'asked, answered, finished, risky'], ['watch-burn', 'WHO IS BURNING IT', 'each session’s share of the plan'], ['watch-changes', 'CHANGES', 'what git says about the project'], ['watch-diff', 'A FILE’S DIFF', 'read it before you answer']],
    'CAPTURED FROM THE RUNNING APP AGAINST AN ISOLATED TEST HERDR SESSION · THE SESSIONS, COMMANDS AND NUMBERS ARE MADE UP'],
]) {
  await pg.setContent(montage(names, note)); await pg.waitForTimeout(500);
  await (await pg.$('#c')).screenshot({ path: OUT + file });
  console.log('wrote', OUT + file);
}
await b2.close();
