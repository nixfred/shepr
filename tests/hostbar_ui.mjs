// v54, in a real browser: the top host bar lists only hosts that are answering and not hidden; an x on the picked chip hides one (kept on the
// phone, never sent to the host); the Hosts tab still lists every host, hidden and offline ones too, with Show/Hide. Starts its OWN server on
// port 8747 with an empty herdr socket, so no real machine and no real agent is asked.   node tests/hostbar_ui.mjs
import { createRequire } from 'module';
import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;

const PORT = 8747, BASE = `http://localhost:${PORT}`;
const here = new URL('.', import.meta.url).pathname;
const SERVER = path.join(here, '..', 'server.py');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'shepr-hostbar-ui-'));
const STATE = path.join(TMP, 'state');
fs.mkdirSync(STATE, { mode: 0o700 });
const ENV = { ...process.env, SHEPR_STATE: STATE, SHEPR_URL: BASE, SHEPR_LOCAL_ONLY: '1',
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


await page.waitForFunction(() => window.shepr.S.q && window.shepr.S.hello, null, { timeout: 30000 });
await page.waitForSelector('#splash', { state: 'detached', timeout: 12000 }).catch(() => {});
await page.evaluate(() => { window.shepr.prefs.set('refresh', 'manual'); localStorage.removeItem('shepr.hiddenhosts'); });   // the poll must not repaint under the checks

const posts = []; page.on('request', (r) => { if (r.method() === 'POST') posts.push(r.url()); });
const H = (id, status, extra) => Object.assign({ id, kind: 'remote', status, counts: {}, asleep: 0, enabled: status !== 'disabled' }, extra || {});
const GUS = { id: 'gus', kind: 'local', status: 'ok', counts: {}, asleep: 0, enabled: true };
const READ = () => ({ hidden: document.getElementById('hostbar').hidden, chips: [...document.querySelectorAll('#hostbar .chip')].map((c) => c.dataset.host || 'ALL'),
  on: [...document.querySelectorAll('#hostbar .chip.on')].map((c) => c.dataset.host || 'ALL'), xs: [...document.querySelectorAll('#hostbar .chip s.hx')].map((x) => x.parentNode.dataset.host),
  bad: document.querySelectorAll('#hostbar .chip.bad').length, text: document.getElementById('hostbar').textContent, host: window.shepr.S.host, store: localStorage.getItem('shepr.hiddenhosts') });
const bar = () => page.evaluate(READ);
const setBar = async (list, host = null) => { await page.evaluate(([list, host]) => { const S = window.shepr; S.S.q.hosts.splice(0, S.S.q.hosts.length, ...list); S.S.tab = 'waiting'; S.S.host = host; S.paintHostbar(); }, [list, host]); return bar(); };
const click = (sel, n = 1, gap = 90) => page.evaluate(({ sel, n, gap }) => new Promise((res) => {
  const node = document.querySelector(sel), r = node.getBoundingClientRect();
  const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
  let i = 0; const go = () => { node.dispatchEvent(new PointerEvent('pointerdown', o)); node.dispatchEvent(new PointerEvent('pointerup', o)); if (++i < n) setTimeout(go, gap); else res(); }; go(); }), { sel, n, gap });
const hold = (sel, ms) => page.evaluate(({ sel, ms }) => new Promise((res) => {
  const node = document.querySelector(sel), r = node.getBoundingClientRect();
  const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
  node.dispatchEvent(new PointerEvent('pointerdown', o)); setTimeout(() => { node.dispatchEvent(new PointerEvent('pointerup', o)); res(); }, ms); }), { sel, ms });
const tabOf = () => page.evaluate(() => window.shepr.S.tab);
const OUTD = path.join(os.tmpdir(), 'shepr-hostbar-shots'); fs.mkdirSync(OUTD, { recursive: true });

// ---- only hosts that are answering and not hidden are in the bar
let b = await setBar([GUS, H('vic', 'ok'), H('lab', 'down'), H('old', 'disabled')]);
console.log('   bar:', JSON.stringify(b));
check('a host that is down and a host that is switched off are NOT in the top bar', JSON.stringify(b.chips) === JSON.stringify(['ALL', 'gus', 'vic']) && b.bad === 0);
check('no "down" or "off" chip text, and no x on any chip until one is picked', !/down|off/.test(b.text) && b.xs.length === 0 && b.hidden === false);
await page.screenshot({ path: path.join(OUTD, 'bar-online-only.png') });
b = await setBar([GUS, H('vic', 'ok'), H('lab', 'down')], 'lab'); const keptHost = await page.evaluate(() => localStorage.getItem('shepr.host'));
check('a filter left on a host that is not in the bar is cleared, here and in storage (no stale filter)', b.host === null && JSON.parse(keptHost) === null);
b = await setBar([GUS, H('vic', 'ok'), H('lab', 'ok'), H('old', 'disabled')]);
check('a host that comes back online reappears on the next paint', JSON.stringify(b.chips) === JSON.stringify(['ALL', 'gus', 'vic', 'lab']));

// ---- tap, double-tap and hold keep their meanings
await click('#hostbar .chip[data-host="vic"]'); b = await bar();
check('tap on a chip still filters to that host and lights it', b.host === 'vic' && JSON.stringify(b.on) === JSON.stringify(['vic']));
check('the picked chip, and only it, carries the x', JSON.stringify(b.xs) === JSON.stringify(['vic']));
await page.screenshot({ path: path.join(OUTD, 'bar-x-on-picked.png') });
await click('#hostbar .chip[data-host="vic"]', 2, 90); b = await bar();
check('double-tap still clears the filter (and the x goes with it)', b.host === null && b.xs.length === 0);
await click('#hostbar .chip[data-host="vic"]'); await click('#hostbar .chip[data-host="vic"]'); b = await bar();
check('a second tap on the picked chip still un-picks it', b.host === null);
await hold('#hostbar .chip[data-host="vic"]', 700); b = await bar();
check('hold still means Waiting + only this host', b.host === 'vic' && (await tabOf()) === 'waiting');

// ---- the x takes it off the bar, on this phone only
const before = posts.length;
await click('#hostbar .chip[data-host="vic"] s.hx'); b = await bar();
check('the x takes the host off the bar, clears the filter and remembers it on the phone', !b.chips.includes('vic') && b.host === null && JSON.parse(b.store).includes('vic'));
check('hiding is a display choice only: nothing was sent to the host (no prepare, no host_disable)', posts.length === before);
await page.screenshot({ path: path.join(OUTD, 'bar-after-hide.png') });

// ---- the Hosts tab still lists every host and the scan's peers; the hidden one says so and has Show
const saved = [GUS, H('vic', 'ok'), H('lab', 'down', { error: 'no route' }), H('old', 'disabled')];
const rowsNow = () => page.evaluate(([hs]) => { const S = window.shepr; S.S.hosts = { saved: hs, scan: { running: false, at: Date.now() / 1000, peers: [{ name: 'newpeer', os: 'linux', ip: '100.64.0.9', state: 'ready', detail: 'herdr is running' }] }, writes: true, hub: 'gus', enroll: null };
  S.S.tab = 'hosts'; S.paintMain(); S.paintHostbar();
  return [...document.querySelectorAll('#main .hrow')].map((r) => ({ id: r.dataset.host || null, text: r.textContent, btns: [...r.querySelectorAll('button')].map((x) => x.textContent), hidden: r.classList.contains('hidden-host') })); }, [saved]);
let rs = await rowsNow(); const row = (id) => rs.find((r) => r.id === id);
console.log('   hosts tab:', JSON.stringify(rs.map((r) => [r.id, r.hidden, r.btns])));
check('the Hosts tab still lists every host: online, hidden, offline and switched off', ['gus', 'vic', 'lab', 'old'].every((id) => !!row(id)));
check('...and the scan\'s not-yet-added peer, with its Add button', rs.some((r) => /newpeer/.test(r.text) && r.btns.includes('Add')));
check('the hidden host is marked HIDDEN with a Show button; the others offer Hide', !!row('vic') && row('vic').hidden && /vic · hidden from bar/.test(row('vic').text) && row('vic').btns.includes('Show')
  && ['gus', 'lab', 'old'].every((id) => !row(id).hidden && row(id).btns.includes('Hide')));
check('offline and off hosts are still described there: down with the reason, and switched off', /no route/.test(row('lab').text) && /switched off/.test(row('old').text));
check('Turn on / Turn off is still there for a remote host, next to Hide', row('old').btns.includes('Turn on') && row('vic').btns.includes('Show'));
await page.screenshot({ path: path.join(OUTD, 'hosts-tab.png') });

// ---- the hide survives a reload
await page.reload(); await page.waitForFunction(() => window.shepr && window.shepr.S.q, null, { timeout: 20000 });
await page.evaluate(() => window.shepr.prefs.set('refresh', 'manual'));
b = await setBar([GUS, H('vic', 'ok'), H('lab', 'ok')]);
check('the hide survives a reload: vic stays off the bar while online, lab is on it', !b.chips.includes('vic') && b.chips.includes('lab') && JSON.parse(b.store).includes('vic'));

// ---- Show brings it back
rs = await rowsNow();
await page.evaluate(() => { document.querySelector('#main .hrow[data-host="vic"] button.hidebtn').click(); });
const afterShow = await page.evaluate(() => ({ store: localStorage.getItem('shepr.hiddenhosts'), marked: !!document.querySelector('#main .hrow[data-host="vic"].hidden-host') }));
check('Show on the Hosts tab un-hides it: the row loses its mark and the phone forgets the hide', JSON.parse(afterShow.store).length === 0 && !afterShow.marked);
b = await setBar([GUS, H('vic', 'ok'), H('lab', 'ok')]);
check('...and it is back on the bar', b.chips.includes('vic'));
rs = await rowsNow();
await page.evaluate(() => { document.querySelector('#main .hrow[data-host="lab"] button.hidebtn').click(); });
check('Hide on the Hosts tab hides a host too', JSON.parse(await page.evaluate(() => localStorage.getItem('shepr.hiddenhosts'))).includes('lab'));
await page.evaluate(() => localStorage.removeItem('shepr.hiddenhosts'));

// ---- the bar is not drawn when it would only be noise; the "ONLY X IS SHOWN" notice still works with a visible chip
b = await setBar([GUS, H('vic', 'down')]);
check('one answering host left (the other down): no bar at all', b.hidden === true);
await page.evaluate(() => localStorage.setItem('shepr.hiddenhosts', JSON.stringify(['vic']))); b = await setBar([GUS, H('vic', 'ok')]);
check('two online hosts but one hidden: no bar', b.hidden === true);
await page.evaluate(() => localStorage.removeItem('shepr.hiddenhosts')); b = await setBar([GUS, H('vic', 'ok')]);
check('two online hosts, none hidden: the bar is there with All hosts + both', b.hidden === false && b.chips.length === 3);
const strip = await page.evaluate(() => { const S = window.shepr; S.S.q.agents.splice(0, S.S.q.agents.length, { host: 'vic', pane: 'w1:p1', status: 'blocked', wlabel: 'vic-session', seq: 1, agent: 'claude', cwd: '/x' }, { host: 'gus', pane: 'w2:p1', status: 'blocked', wlabel: 'gus-session', seq: 1, agent: 'claude', cwd: '/y' });
  S.S.host = 'gus'; S.S.tab = 'waiting'; S.paintMain(); const s = document.getElementById('filterstrip'); return s ? s.textContent : null; });
check('a visible chip that hides waiting sessions still says "ONLY GUS IS SHOWN, 1 more needs you on vic"', !!strip && /ONLY GUS IS SHOWN/.test(strip) && /1 more needs you on vic/.test(strip));

// ---- a host name is data, never markup (Law 12)
b = await setBar([GUS, H('<i id=pwn2>x</i>', 'ok')], '<i id=pwn2>x</i>');
await page.evaluate(() => { const S = window.shepr; S.S.hosts = { saved: [{ id: '<i id=pwn2>x</i>', kind: 'remote', status: 'ok', counts: {}, enabled: true }], scan: { running: false, peers: [] }, writes: true, hub: 'gus' }; });
check('a host name with markup is text in the bar, with its x, never markup', await page.evaluate(() => !document.getElementById('pwn2') && [...document.querySelectorAll('#hostbar .chip')].some((c) => c.textContent.includes('<i id=pwn2>')) && !!document.querySelector('#hostbar .chip.on s.hx')));
rs = await rowsNow().catch(() => []);
check('...and in the Hosts tab', await page.evaluate(() => { window.shepr.S.tab = 'hosts'; window.shepr.paintMain(); return !document.getElementById('pwn2'); }));
check('and nothing came out of the server to say it broke, nor out of the page', !/Traceback/.test(srvErr) && errs.length === 0);
await finish(browser);
