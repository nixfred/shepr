// A tmux host on the phone, in a real browser: its pane opens read only (the text is there, nothing that sends is drawn), the Hosts tab
// says so, and the server refuses a send even when one is forced past the screen. Starts its OWN server on port 8746 with a fake `ssh`
// (tests/fake_tmux_ssh.py) and an empty herdr socket, so no real machine and no real agent is asked.   node tests/tmux_ui.mjs
import { createRequire } from 'module';
import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;

const PORT = 8746, BASE = `http://localhost:${PORT}`;
const here = new URL('.', import.meta.url).pathname;
const SERVER = path.join(here, '..', 'server.py');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'shepr-tmux-ui-'));
const FAKE = path.join(TMP, 'fake'), STATE = path.join(TMP, 'state');
fs.mkdirSync(FAKE); fs.mkdirSync(STATE, { mode: 0o700 });
const NOW = Math.floor(Date.now() / 1000);
fs.writeFileSync(path.join(FAKE, 'state.json'), JSON.stringify({ now: NOW, fail: null, sessions: { work: { windows: [
  { id: 3, name: 'main', activity: NOW - 3, panes: [{ id: 7, path: '/srv/app', cmd: 'node', text: Array.from({ length: 120 }, (_, i) => `build step ${i + 1}`).join('\n') }] }] } } }));
fs.writeFileSync(path.join(STATE, 'tmux_hosts.json'), JSON.stringify({ hosts: [{ id: 'lab', target: 'labbox', sessions: ['work'] }] }), { mode: 0o600 });
const ENV = { ...process.env, SHEPR_STATE: STATE, SHEPR_URL: BASE, SHEPR_SSH: path.join(here, 'fake_tmux_ssh.py'), FAKE_TMUX_DIR: FAKE, SHEPR_LOCAL_ONLY: '1',
              SHEPR_LABEL: 'hub', SHEPR_POLL: '3', SHEPR_WATCH: '0', SHEPR_USAGE: '0', SHEPR_AUTOADD: '', SHEPR_SCAN_NETS: '',
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

// the poll has read the fake machine: its pane is in the list, flagged read only
await page.waitForFunction(() => window.shepr.S.q && window.shepr.S.q.agents.some((a) => a.host === 'lab'), null, { timeout: 30000 });
const row = await page.evaluate(() => window.shepr.S.q.agents.find((a) => a.host === 'lab'));
check('the tmux pane is in the list as a read-only "tmux" row named for its session', row.pane === 'w3:p7' && row.agent === 'tmux' && row.readonly === true && row.wlabel === 'work');
check('it is working (its window printed 3 s ago) and never blocked, so it is not a question for the Queue', row.status === 'working');

// open it with a link, the way a notification does
await page.goto(BASE + '/#lab/w3:p7'); await page.reload();
await page.waitForSelector('#detail:not([hidden])', { timeout: 20000 });
await page.waitForFunction(() => /build step 120/.test(document.getElementById('tail').textContent), null, { timeout: 20000 });
check('the pane text is on the screen, and it is the last of it', /build step 120/.test(await page.textContent('#tail')));
const vis = await page.evaluate(() => ['compose', 'keyrow', 'optrow', 'qrow', 'slashbtn', 'send', 'dmodel', 'dchanges'].map((id) => { const n = document.getElementById(id); return [id, !n || n.hidden || n.offsetParent === null]; }));
console.log('   hidden controls:', JSON.stringify(vis));
check('nothing that sends is drawn: no message box, no Send, no keys, no options, no slash chips', vis.every(([, hidden]) => hidden));
await page.waitForSelector('#splash', { state: 'detached', timeout: 12000 }).catch(() => {});
await page.waitForTimeout(400);
await page.screenshot({ path: path.join(os.tmpdir(), 'shepr-tmux-pane.png') });

// the Hosts tab names it, with no switch to flip
const lbl = await page.evaluate(async () => {
  const S = window.shepr; const h = await (await fetch('api/hosts', { headers: await S.signHeaders('GET', 'api/hosts', '') })).json();
  const t = h.saved.find((x) => x.id === 'lab'); const r = S.hostRow(t);
  return { text: r.textContent, buttons: r.querySelectorAll('button').length, labels: [...r.querySelectorAll('button')].map((x) => x.textContent), kind: t.kind, sessions: t.sessions };
});
check('the Hosts tab says tmux over ssh, read only, and has no Turn off button (only the display-only Hide)', /tmux over ssh, read only/.test(lbl.text) && lbl.buttons === 1 && /^Hide$/.test(lbl.labels[0]) && lbl.kind === 'tmux' && lbl.sessions[0] === 'work');

// a send forced past the screen is refused by the server, before any sheet
const res = await page.evaluate(async () => {
  const S = window.shepr, body = JSON.stringify({ kind: 'text', host: 'lab', pane: 'w3:p7', text: 'rm -rf ~' });
  const r = await fetch('api/send/prepare', { method: 'POST', headers: { ...(await S.signHeaders('POST', 'api/send/prepare', body)), 'Content-Type': 'application/json', 'X-Shepr': '1' }, body });
  return { status: r.status, body: await r.json().catch(() => null) };
});
console.log('   forced send:', JSON.stringify(res));
check('a send forced past the screen is refused at prepare, naming the reason', res.status >= 400 && /tmux host/.test(JSON.stringify(res.body)));
const calls = fs.readFileSync(path.join(FAKE, 'calls.log'), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
check('everything Shepr ran on that machine was a read: only tmux list-panes and capture-pane', calls.length > 0 && calls.every((c) => /^tmux (list-panes|capture-pane) /.test(c[c.length - 1])));
check('and nothing came out of the server to say it broke', !/Traceback/.test(srvErr));
await finish(browser);
