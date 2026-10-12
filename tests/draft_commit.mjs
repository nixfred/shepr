// A push notification tapped while the confirm sheet is open must not make a send wipe ANOTHER session's draft.
// commit() cleared the draft of wherever you stand NOW (S.cur), not of the session the plan is for (plan.target).
// Needs: tests/fake-session.sh up, a test server on :8743 (see ui.mjs header).
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
// Playwright is a global install, not an npm dependency (nixweb rule). Look where a stranger would have it.
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;
const BASE = 'http://localhost:8743';
const SERVER = new URL('../server.py', import.meta.url).pathname;
const ENV = { ...process.env, SHEPR_STATE: process.env.SHEPR_STATE || '/tmp/claude-1000/shepr-state', SHEPR_URL: BASE,
              HERDR_SOCKET_PATH: process.env.HERDR_SOCKET_PATH || (process.env.HOME || '') + '/.config/herdr/sessions/hrtest/herdr.sock' };
const res = []; const check = (n, c) => { res.push(!!c); console.log((c ? 'PASS ' : 'FAIL ') + n); };
const code = JSON.parse(execFileSync('python3', [SERVER, 'pair', '--no-wait', '--json'], { env: ENV, encoding: 'utf8' })).code;
const b = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const ctx = await b.newContext({ ...devices['iPhone 14'] });
const p = await ctx.newPage();
await p.goto(BASE + '/');
await p.waitForSelector('#pair:not([hidden])'); await p.waitForSelector('#splash', { state: 'detached', timeout: 8000 }).catch(() => {});
await p.click('#pcode'); await p.fill('#pcodein', code); await p.fill('#pname', 'draft test phone'); await p.click('#pgo');
await p.waitForSelector('.item', { timeout: 12000 });
await p.evaluate(() => navigator.serviceWorker.ready.then(() => true)); await p.reload(); await p.waitForSelector('.item'); await p.waitForTimeout(800);

const A = await p.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'idle'); return a.host + '|' + a.pane; });   // infomarchy
const B = await p.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'working'); return a.host + '|' + a.pane; }); // blip
const tapRow = async (k) => { await p.evaluate((sel) => { const n = document.querySelector(sel); const r = n.getBoundingClientRect();
  const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
  n.dispatchEvent(new PointerEvent('pointerdown', o)); n.dispatchEvent(new PointerEvent('pointerup', o)); }, `.item[data-key="${k}"]`);
  await p.waitForSelector('#detail:not([hidden])'); await p.waitForTimeout(400); };

// 1. session B gets unsent words, left behind as a draft
await tapRow(B); await p.fill('#msg', 'unsent words for B'); await p.click('#dback'); await p.waitForTimeout(400);
// 2. session A: type a message, tap Send, the confirm sheet opens
await tapRow(A); await p.fill('#msg', 'message for A'); await p.click('#send'); await p.waitForSelector('#sheet:not([hidden]) .kv');
// 3. a push arrives and is tapped: the worker tells the page to open B (sheet stays open, as on a real phone)
await p.evaluate((k) => { const [host, pane] = k.split('|'); navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { type: 'open', host, pane } })); }, B);
await p.waitForTimeout(500);
check('the push opened session B behind the open confirm sheet', await p.evaluate((k) => { const c = window.shepr.S.cur; return c && c.host + '|' + c.pane === k; }, B));
check('the box now shows B\'s draft, the sheet still offers A\'s send', (await p.inputValue('#msg')) === 'unsent words for B' && /message for A/.test(await p.textContent('#sheet .kv')));
// 4. confirm anyway. The fake fixture's agents are `sleep`, so a real write never lands; stub the commit answer as a
// success (the client behaviour on a successful send is what is tested here, like ui.mjs stubs api/history).
let commits = 0;
await p.route('**/api/send/commit', (route) => { commits++; route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) }); });
await p.waitForTimeout(600);   // the ghost-tap dead time
await p.click('#sheet .btns .go');
await p.waitForFunction(() => /Sent to/.test(document.body.textContent), null, { timeout: 10000 });
const drafts = await p.evaluate(() => JSON.parse(localStorage.getItem('shepr.drafts') || '{}'));
console.log('   drafts after send:', JSON.stringify(drafts));
check('the send itself went to the plan\'s target (one commit, for A)', commits === 1);
check('B\'s unsent words survive a send that was for A', drafts[B] && drafts[B].t === 'unsent words for B');
check('the open box (showing B) was not emptied by A\'s send', (await p.inputValue('#msg')) === 'unsent words for B');
check('A\'s draft is the one that was cleared', !drafts[A] || !drafts[A].t);
await ctx.close(); await b.close();
process.exit(res.every(Boolean) ? 0 : 1);
