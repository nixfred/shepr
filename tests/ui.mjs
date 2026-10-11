// Shepr end-to-end test: iPhone-sized Chromium against a server running on the ISOLATED hrtest herdr session.
//   tests/fake-session.sh up ; eval "$(tests/fake-session.sh env)"; SHEPR_URL=http://localhost:8743 python3 server.py 8743 &
//   node tests/ui.mjs
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
import fs from 'fs';
const require = createRequire(process.env.PW_PATH || '/home/pi/.local/share/mise/installs/npm-playwright/latest/node_modules/');
const { chromium, devices } = require('playwright');

const OUT = '/tmp/claude-1000/shots'; fs.mkdirSync(OUT, { recursive: true });
const BASE = 'http://localhost:8743';
const SERVER = new URL('../server.py', import.meta.url).pathname;
const ENV = { ...process.env, SHEPR_STATE: process.env.SHEPR_STATE || '/tmp/claude-1000/shepr-state', SHEPR_URL: BASE,
              HERDR_SOCKET_PATH: process.env.HERDR_SOCKET_PATH || '/home/pi/.config/herdr/sessions/hrtest/herdr.sock' };
const cli = (...a) => execFileSync('python3', [SERVER, ...a], { env: ENV, encoding: 'utf8' });
const newCode = (png) => JSON.parse(cli('pair', '--no-wait', '--json', ...(png ? ['--png', png] : '')));

const ok = [];
const check = (name, cond) => { ok.push([name, !!cond]); console.log((cond ? 'PASS ' : 'FAIL ') + name); };
const errs = [];

// pointer helpers: dispatch the sequences a finger produces
async function hold(page, sel, ms) {
  await page.evaluate(({ sel, ms }) => new Promise((res) => {
    const n = document.querySelector(sel); const r = n.getBoundingClientRect();
    const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
    n.dispatchEvent(new PointerEvent('pointerdown', o));
    setTimeout(() => { n.dispatchEvent(new PointerEvent('pointerup', o)); res(); }, ms);
  }), { sel, ms });
}
async function tap(page, sel, n = 1, gap = 90) {
  await page.evaluate(({ sel, n, gap }) => new Promise((res) => {
    const node = document.querySelector(sel); const r = node.getBoundingClientRect();
    const o = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2, pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0 };
    let i = 0; const go = () => {
      node.dispatchEvent(new PointerEvent('pointerdown', o)); node.dispatchEvent(new PointerEvent('pointerup', o));
      if (++i < n) setTimeout(go, gap); else res();
    }; go();
  }), { sel, n, gap });
}
const watch = (page) => {
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'warning' && /queue fetch failed/.test(m.text())) console.log('   WARN', m.text().slice(0, 300)); if (m.type() === 'error' && !/400|401|403|404|409|410|429|502/.test(m.text())) errs.push('console: ' + m.text()); });
};

const dev = devices['iPhone 14'];
const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });

// =============================================================== 1. splash + first run (unpaired)
{
  const ctx = await browser.newContext({ ...dev, serviceWorkers: 'block' });
  const page = await ctx.newPage(); watch(page);
  await page.goto(BASE + '/');
  await page.waitForSelector('#splash');
  check('splash shows the wordmark', (await page.textContent('.sword')).replace(/\s/g, '') === 'SHEPR');
  check('splash says who made it: MADE BY @nixfred', (await page.textContent('.smade')).replace(/\s/g, '') === 'MADEBY@nixfred');
  await page.waitForTimeout(300);  await page.screenshot({ path: `${OUT}/s1-splash-early.png` });
  await page.waitForTimeout(700);  await page.screenshot({ path: `${OUT}/s2-splash-mid.png` });
  // the finished picture, as soon as it is finished (the splash holds for 5 s in all)
  const late = await page.waitForFunction(() => { const b = document.querySelector('.smade'); if (!b) return null;
    const r = { badge: +getComputedStyle(b).opacity, letters: [...document.querySelectorAll('.sword span')].map((s) => +getComputedStyle(s).opacity),
      typed: document.querySelector('.stag .ty').getBoundingClientRect().width, rule: document.querySelector('.srule').getBoundingClientRect().width, ver: document.getElementById('sver').textContent };
    return r.badge > 0.99 && r.letters.every((o) => o > 0.99) && r.typed > 100 && r.rule > 150 ? r : null; }, null, { timeout: 4000, polling: 50 }).then((h) => h.jsonValue()).catch(() => null);
  await page.screenshot({ path: `${OUT}/s3-splash-late.png` }).catch(() => {});
  check('splash: the badge, all five letters, the typed tagline and the rule all land before it leaves', !!late);
  check('splash: it shows the build number', !!late && /^v\d+$/.test(late.ver));
  await page.waitForSelector('#splash', { state: 'detached', timeout: 9000 });
  check('splash removes itself', true);
  check('unpaired phone lands on the pairing screen', await page.isVisible('#pair'));
  check('no API data is shown while unpaired', (await page.$$('.item')).length === 0);
  await page.screenshot({ path: `${OUT}/p1-pair.png` });

  // ---- pair by typing the code
  const { code } = newCode();
  await page.click('#pcode');
  await page.fill('#pcodein', code.toLowerCase().replace(/-/g, ''));
  check('code field formats as XXXX-XXXX-XXXX', (await page.inputValue('#pcodein')) === code);
  await page.fill('#pname', 'Test iPhone');
  await page.screenshot({ path: `${OUT}/p2-form.png` });
  await page.click('#pgo');
  await page.waitForSelector('#pdone:not([hidden])');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/p3-success.png` });
  await page.waitForSelector('.item', { timeout: 8000 });
  check('pairing succeeds and the queue loads with signed requests', (await page.$$('.item')).length >= 3);
  check('private key is stored non-extractable', await page.evaluate(async () => { const d = window.shepr.S.dev; return d.key.extractable === false && d.key.type === 'private'; }));
  const devJson = JSON.parse(fs.readFileSync(ENV.SHEPR_STATE + '/devices.json', 'utf8'));
  const myId = await page.evaluate(() => window.shepr.S.dev.id);
  check('host recorded this phone as a public key only (no private part)', !!devJson[myId] && !('d' in devJson[myId]) && Object.values(devJson).every((d) => !('d' in d)));
  const reuse = await page.evaluate(async (code) => {
    const kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign']);
    const j = await crypto.subtle.exportKey('jwk', kp.publicKey);
    const r = await fetch('api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Shepr': '1' },
      body: JSON.stringify({ code, jwk: { kty: j.kty, crv: j.crv, x: j.x, y: j.y }, name: 'attacker' }) });
    return r.status;
  }, code);
  check('a used pairing code cannot be used again', reuse === 403);

  // ---- attacks against the signature scheme
  const r = await page.evaluate(async () => {
    const S = window.shepr, out = {};
    const h = await S.signHeaders('GET', 'api/queue', '');
    out.first = (await fetch('api/queue', { headers: h })).status;
    out.replay = (await fetch('api/queue', { headers: h })).status;
    const h2 = await S.signHeaders('GET', 'api/queue', '');
    out.otherPath = (await fetch('api/pane?host=gus&pane=w1:p1', { headers: h2 })).status;   // signed for another path
    // Same path, same (empty) body, ONLY the method differs: a valid POST signature replayed as a GET.
    const h3 = await S.signHeaders('POST', 'api/queue', '');
    out.methodSwap = (await fetch('api/queue', { headers: h3 })).status;
    out.noSig = (await fetch('api/queue')).status;
    // A CORRECTLY signed request that is ten minutes old (the clock offset is part of what gets signed).
    const st = S.S, skew = st.skew; st.skew = skew - 10 * 60 * 1000;
    const h4 = await S.signHeaders('GET', 'api/queue', '');
    st.skew = skew;
    out.stale = (await fetch('api/queue', { headers: h4 })).status;
    return out;
  });
  console.log('   attacks:', JSON.stringify(r));
  check('valid signature accepted once', r.first === 200);
  check('replayed request refused', r.replay === 401);
  check('signature bound to the path', r.otherPath === 401);
  check('signature bound to the method', r.methodSwap === 401);
  check('unsigned request refused', r.noSig === 401);
  check('old timestamp refused', r.stale === 401);

  // ---- main screen
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/q1-queue.png` });
  const ages = await page.$$eval('.item .age', (n) => n.map((x) => x.textContent));
  check('queue shows blocked rows', ages.length >= 3);

  // ---- the host announces the current build; a stale app reloads itself, but only at a safe moment
  const t0 = await page.evaluate(() => performance.timeOrigin);
  await page.evaluate(() => window.shepr.appIsStale('v999'));      // on the list, nothing typed, no sheet: safe
  await page.waitForTimeout(1800);
  await page.waitForSelector('.item', { timeout: 8000 });
  check('a stale app reloads itself on the list screen', (await page.evaluate(() => performance.timeOrigin)) !== t0);
  check('reload keeps this phone paired (key survives)', (await page.$$('.item')).length >= 3);
  check('page does not scroll (Law 17)', await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1));

  // ---- queue order: needs you, then working, then sleeping last
  const secs = await page.$$eval('#main .sec', (n) => n.map((x) => x.textContent.trim().split(' ')[0]));
  console.log('   sections:', secs.join(' > '));
  const tail = secs.filter((x) => x !== 'Asleep');   // the Asleep row (projects with no agent) always comes last of all
  check('queue order: Needs you before Working before Sleeping, then Asleep', secs.indexOf('Needs') === 0 && tail.indexOf('Sleeping') === tail.length - 1 && (tail.indexOf('Working') === -1 || tail.indexOf('Working') < tail.indexOf('Sleeping')) && (secs.indexOf('Asleep') === -1 || secs.indexOf('Asleep') === secs.length - 1));
  const statuses = await page.$$eval('#main .item', (n) => n.map((x) => x.classList.contains('blocked') ? 'b' : x.classList.contains('working') ? 'w' : 'i').join(''));
  check('rows are ordered blocked, then working, then sleeping', /^b+w*i*$/.test(statuses));

  // ---- pull down from the top refreshes (a read: it asks the host for a fresh queue, nothing else)
  {
    let fresh = 0; const onReq = (r) => { if (/api\/queue\?fresh=1/.test(r.url())) fresh++; };
    page.on('request', onReq);
    await page.evaluate(() => {
      const m = document.getElementById('main'); m.scrollTop = 0;
      const t = (type, y) => m.dispatchEvent(new TouchEvent(type, { bubbles: true, cancelable: true,
        touches: type === 'touchend' ? [] : [new Touch({ identifier: 1, target: m, clientX: 190, clientY: y })],
        changedTouches: [new Touch({ identifier: 1, target: m, clientX: 190, clientY: y })] }));
      t('touchstart', 200); t('touchmove', 230); t('touchmove', 300); t('touchend', 300);
    });
    await page.waitForTimeout(700);
    check('pull down from the top refreshes the queue', fresh >= 1);
    const before = fresh;
    await page.evaluate(() => {
      const m = document.getElementById('main'); m.scrollTop = 0;
      const t = (type, y) => m.dispatchEvent(new TouchEvent(type, { bubbles: true, cancelable: true,
        touches: type === 'touchend' ? [] : [new Touch({ identifier: 1, target: m, clientX: 190, clientY: y })],
        changedTouches: [new Touch({ identifier: 1, target: m, clientX: 190, clientY: y })] }));
      t('touchstart', 200); t('touchmove', 220); t('touchend', 220);
    });
    await page.waitForTimeout(500);
    check('a short pull does not refresh', fresh === before);
    page.off('request', onReq);
  }

  // ---- wake sleeping sessions: confirm first, never a blocked one
  const blockedPane = await page.evaluate(() => window.shepr.S.q.agents.find((a) => a.status === 'blocked'));
  const refuse = await page.evaluate(async (a) => {
    const body = JSON.stringify({ kind: 'wake', targets: [{ host: a.host, pane: a.pane }], text: 'go' });
    const h = await window.shepr.signHeaders('POST', 'api/send/prepare', body);
    const r = await fetch('api/send/prepare', { method: 'POST', headers: { ...h, 'Content-Type': 'application/json', 'X-Shepr': '1' }, body });
    return [r.status, (await r.json()).error];
  }, blockedPane);
  check('wake refuses a BLOCKED session (cannot approve anything)', refuse[0] === 400 && refuse[1] === 'not_sleeping');
  await page.click('#main .secrow .mini-btn');
  await page.waitForSelector('#sheet:not([hidden]) .chk');
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/w1-wake.png` });
  check('wake sheet lists the sleeping sessions, all ticked', (await page.$$eval('#sheet .chk input', (n) => n.length && n.every((x) => x.checked))));
  await page.fill('#sheet .wakemsg', 'Pick this back up');
  await page.click('#sheet .btns .go');
  await page.waitForSelector('#sheet .kv');
  const prev = await page.textContent('#sheet .kv');
  await page.screenshot({ path: `${OUT}/w2-wake-confirm.png` });
  check('preview names every target and the exact message', prev.includes('Wakes') && prev.includes('Pick this back up'));
  check('wake preview says nothing is approved', prev.includes('Nothing is approved'));
  await page.waitForTimeout(900);
  await page.click('#sheet .btns .go');                      // Wake them
  await page.waitForSelector('#gtoast:not([hidden])', { timeout: 15000 });
  const wres = await page.textContent('#gtoast');
  console.log('   wake result:', wres.slice(0, 160));
  check('wake reports honestly (the fake agent refuses a prompt, and it says so)', /failed|Woke/.test(wres));

  // ---- slash row: last 6 used, tap fills (never sends), MORE searches all, none on a blocked session
  const idleA = await page.evaluate(() => window.shepr.S.q.agents.find((a) => a.status === 'idle'));
  await tap(page, `.item[data-key="${idleA.host}|${idleA.pane}"]`);
  await page.waitForSelector('#detail:not([hidden])'); await page.waitForSelector('#slashbtn:not([hidden])', { timeout: 8000 });
  await page.click('#slashbtn'); await page.waitForSelector('#slashrow:not([hidden]) button:not(.more)');
  const chips = await page.$$eval('#slashrow button:not(.more)', (n) => n.map((x) => x.textContent));
  console.log('   slash chips:', chips.join(' '));
  check('slash row: last 6 used, newest first, no /exit, no paths', JSON.stringify(chips) === JSON.stringify(['/Beta', '/effort', '/plug:delta', '/Alpha', '/model', '/Gamma']));
  await page.screenshot({ path: `${OUT}/sl1-row.png` });
  await page.click('#slashrow button:first-child');
  check('tapping a chip fills the box with /name and sends nothing', (await page.inputValue('#msg')) === '/Beta ' && (await page.$eval('#sheet', (n) => n.hidden)));
  await page.fill('#msg', '/al');
  check('typing / filters the row to matches', (await page.$$eval('#slashrow button:not(.more)', (n) => n.map((x) => x.textContent))).includes('/Alpha'));
  await page.fill('#msg', '');
  await page.click('#slashbtn');                                                     // open the row again
  await page.click('#slashrow .more'); await page.waitForSelector('#sheet:not([hidden]) .cmd');
  await page.fill('#sheet .search', 'delta'); await page.waitForTimeout(200);
  const found = await page.$$eval('#sheet .cmd .n', (n) => n.map((x) => x.textContent));
  check('MORE searches every skill, including plugin skills', found.length === 1 && found[0] === '/plug:delta');
  await page.fill('#sheet .search', 'fixture'); await page.waitForTimeout(200);
  await page.screenshot({ path: `${OUT}/sl2-more.png` });
  check('MORE search matches descriptions too (3 fixture skills)', (await page.$$('#sheet .cmd')).length === 3);
  await page.click('#sheet .cmd:first-child');
  check('picking from MORE fills the box', /^\/[A-Za-z]/.test(await page.inputValue('#msg')) && (await page.$eval('#sheet', (n) => n.hidden)));
  await page.fill('#msg', '/Alpha please'); await page.click('#send'); await page.waitForSelector('#sheet:not([hidden]) .kv');
  check('a slash command still goes through the preview sheet', (await page.textContent('#sheet .kv')).includes('/Alpha please'));
  await page.click('#sheet .btns button:first-child'); await page.fill('#msg', '');
  await page.click('#dback'); await page.waitForTimeout(300);
  const blk = await page.evaluate(() => window.shepr.S.q.agents.find((a) => a.status === 'blocked'));
  await tap(page, `.item[data-key="${blk.host}|${blk.pane}"]`);
  await page.waitForSelector('#detail:not([hidden])'); await page.waitForSelector('#optrow button'); await page.waitForTimeout(400);
  check('a session at a dialog offers no slash command', await page.$eval('#slashbtn', (n) => n.hidden));
  await page.click('#dback'); await page.waitForTimeout(300);

  // ---- session text is big and adjustable
  await tap(page, '.item:nth-of-type(2)');
  await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(600);
  const fs1 = await page.$eval('#tail', (n) => parseFloat(getComputedStyle(n).fontSize));
  check('session text is at least 17px', fs1 >= 17);
  await page.click('#fsp'); await page.click('#fsp');
  const fs2 = await page.$eval('#tail', (n) => parseFloat(getComputedStyle(n).fontSize));
  check('A+ makes the session text bigger', fs2 === fs1 + 2);
  await page.click('#fsm'); await page.click('#fsm'); await page.click('#fsm');
  check('A- makes it smaller', (await page.$eval('#tail', (n) => parseFloat(getComputedStyle(n).fontSize))) === fs1 - 1);
  await page.click('#fsp');
  await page.screenshot({ path: `${OUT}/d3-bigtext.png` });
  // inside a session a stale app must NOT reload under your thumb: it offers an Update button instead
  const t1 = await page.evaluate(() => performance.timeOrigin);
  await page.evaluate(() => window.shepr.appIsStale('v999'));
  await page.waitForTimeout(800);
  check('in a session: no reload, an Update button appears', (await page.evaluate(() => performance.timeOrigin)) === t1 && (await page.textContent('#toast')).includes('Update'));
  await page.click('#dback'); await page.waitForTimeout(300);

  // ---- icon tab bar
  const tabs = await page.$$eval('.tab', (n) => n.map((x) => [x.dataset.view, !!x.querySelector('svg'), x.textContent.trim()]));
  console.log('   tabs:', JSON.stringify(tabs));
  check('five icon tabs with labels', tabs.length === 5 && tabs.every((t) => t[1]));
  const x0 = await page.$eval('#tabind', (n) => getComputedStyle(n).transform);
  await tap(page, '.tab[data-view=all]'); await page.waitForTimeout(500);
  const x1 = await page.$eval('#tabind', (n) => getComputedStyle(n).transform);
  check('tab indicator slides', x0 !== x1);
  await page.screenshot({ path: `${OUT}/t1-herd.png` });
  await hold(page, '.tab[data-view=waiting]', 900); await page.waitForSelector('#sheet:not([hidden]) .search');
  check('Holding a tab opens Go To (the Go tab is gone)', true);
  await page.waitForTimeout(600);   // let the sheet finish sliding up before measuring it
  check('Go To sheet keeps its Close button on screen', await page.evaluate(() => { const b = document.querySelector('#sheet .btns button'); return b && b.getBoundingClientRect().bottom <= innerHeight; }));
  await page.screenshot({ path: `${OUT}/t2-goto.png` });
  await page.evaluate(() => document.getElementById('shade').click());
  await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);

  // ---- gestures still work
  await hold(page, '.item:nth-of-type(2)', 650);
  await page.waitForSelector('#sheet:not([hidden]) .peek'); await page.waitForTimeout(400);
  check('long-press row opens peek', (await page.textContent('.peek')).length > 10);
  await page.click('#sheet .btns button:first-child');
  await tap(page, '.item:nth-of-type(2)');
  await page.waitForSelector('#detail:not([hidden])');
  await page.waitForFunction(() => document.querySelector('#tail').textContent.length > 20);
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/d1-detail.png` });
  check('detail shows option buttons', (await page.$$('#optrow button')).length >= 3);
  const before = await page.textContent('#dsub');
  await tap(page, '#dhead', 2, 80); await page.waitForTimeout(900);
  check('double-tap title jumps to next waiting', before !== (await page.textContent('#dsub')));
  await hold(page, '#dhead', 650); await page.waitForTimeout(900);
  check('hold title flips back', (await page.textContent('#dsub')) === before);

  // ---- confirm sheet; nothing sent until Send
  await page.click('#optrow button:nth-child(2)');
  await page.waitForSelector('#sheet:not([hidden]) .kv');
  await page.screenshot({ path: `${OUT}/d2-confirm.png` });
  check('Send disabled at first (ghost-tap guard)', await page.locator('#sheet .btns .go').isDisabled());
  await page.click('#sheet .btns button:first-child');
  await page.click('#dback');

  // ---- hosts + keys
  await tap(page, '.tab[data-view=hosts]'); await page.waitForTimeout(900);
  await page.screenshot({ path: `${OUT}/h1-hosts.png` });
  check('Hosts view lists the local host and vic (disabled)', (await page.textContent('#main')).includes('vic'));
  await tap(page, '.tab[data-view=settings]'); await page.waitForTimeout(900);
  await page.click('#setseg [data-seg=phones]'); await page.waitForTimeout(500);
  await page.screenshot({ path: `${OUT}/k1-settings-phones.png` });
  check('Settings > Phones shows the device fingerprint', (await page.textContent('#main')).includes('SHA256:'));

  // ---- tick-several questions: tapping only ticks on the phone, Submit shows exactly what will be pressed
  {
    const fs_ = new URL('./fake-session.sh', import.meta.url).pathname;
    execFileSync('bash', [fs_, 'multi'], { env: ENV, encoding: 'utf8' });
    await page.waitForFunction(() => window.shepr.S.q && window.shepr.S.q.agents.some((a) => a.wlabel === 'multi'), null, { timeout: 15000 }).catch(async () => {
      await page.click('#refresh'); await page.waitForTimeout(4000);
    });
    const key = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.wlabel === 'multi'); return a && (a.host + '|' + a.pane); });
    check('the tick-several fixture session is visible, named by its workspace', !!key);
    if (key) {
      await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(400);
      await tap(page, `.item[data-key="${key}"]`);
      await page.waitForSelector('#detail:not([hidden])'); await page.waitForSelector('#optrow .tog', { timeout: 8000 });
      const labels = await page.$$eval('#optrow button', (n) => n.map((x) => x.textContent.trim()));
      console.log('   multi buttons:', JSON.stringify(labels));
      check('multi: four tick buttons, a pencil for your own words, and a Submit button', labels.length === 6 && labels[4] === '\u270e' && /^Submit/.test(labels[5]));
      check('multi: the option already ticked on screen starts ticked', (await page.$$eval('#optrow .tog.on', (n) => n.map((x) => x.textContent.trim()))).join() === '\u2713 2');
      await page.click('#optrow .tog:nth-child(1)'); await page.click('#optrow .tog:nth-child(3)');
      check('multi: tapping ticks on the phone and sends nothing', (await page.$$eval('#optrow .tog.on', (n) => n.length)) === 3 && (await page.locator('#sheet:not([hidden])').count()) === 0);
      await page.screenshot({ path: `${OUT}/m1-multi.png` });
      await page.click('#optrow .tog-go');
      await page.waitForSelector('#sheet:not([hidden]) .kv');
      const kv = await page.textContent('#sheet .kv');
      console.log('   multi plan:', kv.replace(/\s+/g, ' ').trim());
      check('multi: the sheet lists what gets ticked and pressed', /Ticks/.test(kv) && /1\. Alpha/.test(kv) && /3\. Charlie/.test(kv) && /Submit/.test(kv) && /1 3 \u2193 \u2193 \u2193 \u2193 \u2193 \u23ce/.test(kv));
      await page.screenshot({ path: `${OUT}/m2-multi-confirm.png` });
      await page.click('#sheet .btns button:first-child');
      const r = await page.evaluate(async (k) => { const [host, pane] = k.split('|'); const body = JSON.stringify({ host, pane, kind: 'choose', option: 1 });
        const res = await fetch('api/send/prepare', { method: 'POST', body, headers: Object.assign({ 'Content-Type': 'application/json', 'X-Shepr': '1' }, await window.shepr.signHeaders('POST', 'api/send/prepare', body)) });
        return res.status + ' ' + (await res.text()); }, key);
      console.log('   choose on multi:', r.slice(0, 160));
      check('multi: choosing a single option on a tick-several question is refused', /multi_select|tick/i.test(String(r)));
    }
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(500);
    execFileSync('bash', [fs_, 'multi-rm'], { env: ENV, encoding: 'utf8' });
  }

  // ---- the whole session in ONE scroll (the host's /api/history is stubbed: this tests the PAGE)
  {
    const mk = (n, count = 40) => Array.from({ length: count }, (_, i) => ({ ts: '2026-10-09T10:00:00Z', r: ['user', 'assistant', 'tool'][i % 3], x: `p${n} entry ${i}` }));
    // the last two entries are what the live text already shows, so the page must hide them
    const seen = [{ ts: '2026-10-09T10:01:00Z', r: 'assistant', x: 'Working on burnbar. Do you want to proceed?' }, { ts: '2026-10-09T10:01:01Z', r: 'assistant', x: 'Do you want to proceed? 1. Yes 2. Yes, and do not ask again' }];
    let newest = [...mk(3, 38), ...seen];
    const calls = [];
    await page.route('**/api/history*', (route) => {
      const b = new URL(route.request().url()).searchParams.get('before'); calls.push(b);
      const n = b === null ? 3 : b === '300' ? 2 : 1;
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ source: 'transcript',
        entries: b === null ? newest : mk(n), before: n === 1 ? null : n === 3 ? 300 : 100, size: 999, note: null, at: Date.now() / 1000 }) });
    });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(400);
    const bk = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'blocked'); return a.host + '|' + a.pane; });
    await tap(page, `.item[data-key="${bk}"]`);
    await page.waitForSelector('#detail:not([hidden])');
    await page.waitForSelector('#hist .hent', { timeout: 8000 });
    await page.waitForTimeout(300);
    const t1 = await page.evaluate(() => { const t = document.getElementById('tail'); return { n: t.querySelectorAll('#hist .hent').length,
      nearBottom: t.scrollHeight - t.scrollTop - t.clientHeight < 40, hist: document.getElementById('hist').textContent, live: document.querySelectorAll('#live .tl').length,
      button: !!t.querySelector('.earlier, .hlive, .hbar') }; });
    check('history: no button, it is just there above the live text', !t1.button && t1.n > 0);
    check('history: the live text stays at the bottom, in view', t1.nearBottom && t1.live > 3);
    check('history: entries the live text already shows are hidden (38 of 40 shown)', t1.n === 38 && !t1.hist.includes('Working on burnbar'));
    check('history: the page itself does not scroll', await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1));
    await page.screenshot({ path: `${OUT}/h1-history.png` });
    await page.evaluate(() => { const t = document.getElementById('tail'); t.scrollTop = t.scrollHeight - t.clientHeight - 900; t.dispatchEvent(new Event('scroll')); });
    await page.waitForTimeout(300);
    check('history: scrolling up from the live text walks into the history, no jump', await page.evaluate(() => { const t = document.getElementById('tail'); return t.scrollTop > 0 && t.querySelectorAll('#hist .hent').length === 38; }));
    await page.evaluate(() => { const t = document.getElementById('tail'); t.scrollTop = 0; t.dispatchEvent(new Event('scroll')); });
    await page.waitForFunction(() => document.querySelectorAll('#hist .hent').length === 78, null, { timeout: 5000 });
    const t2 = await page.evaluate(() => { const t = document.getElementById('tail'); const ents = [...t.querySelectorAll('#hist .hent')];
      return { first: ents[0].textContent, joint: ents[40].textContent, top: t.scrollTop }; });
    check('history: reaching the top loads the next older page ABOVE, in order', t2.first.includes('p2 entry 0') && t2.joint.includes('p3 entry 0'));
    check('history: your place is kept when older text lands above it', t2.top > 200);
    await page.evaluate(() => { const t = document.getElementById('tail'); t.scrollTop = 0; t.dispatchEvent(new Event('scroll')); });
    await page.waitForFunction(() => document.getElementById('hmark').textContent.includes('start of this session'), null, { timeout: 5000 });
    check('history: keeps going until the very start, then says so', JSON.stringify(calls.slice(0, 3)) === JSON.stringify([null, '300', '100']) && (await page.$$eval('#hist .hent', (n) => n.length)) === 118);
    await page.screenshot({ path: `${OUT}/h2-history-start.png` });
    // the session grows while it is open: the new entries appear at the bottom of the history, once each, in order
    newest = [...mk(3, 38), ...[1, 2, 3].map((i) => ({ ts: `2026-10-09T10:0${i + 1}:00Z`, r: 'assistant', x: `fresh entry ${i}` }))];
    await page.evaluate(() => window.shepr.syncHistory());
    await page.waitForFunction(() => document.getElementById('hist').textContent.includes('fresh entry 3'), null, { timeout: 5000 });
    const t3 = await page.evaluate(() => { const e = [...document.querySelectorAll('#hist .hent')].map((n) => n.textContent); return { n: e.length, tail: e.slice(-3) }; });
    check('history: new entries are appended once, in order, with no gap', t3.n === 121 && t3.tail.every((x, i) => x.includes('fresh entry ' + (i + 1))));
    check('history: the matching rule is sound (short text never matches, long text does)', await page.evaluate(() => {
      window.shepr.S.tailText = 'Alpha beta gamma delta epsilon zeta eta theta';
      const hit = window.shepr.liveCut([{ x: 'unrelated old message here' }, { x: 'alpha, beta! gamma delta epsilon zeta' }, { x: 'ok' }]);
      const none = window.shepr.liveCut([{ x: 'yes' }, { x: 'no' }]);
      return hit === 1 && none === 2; }));
    await page.unroute('**/api/history*');
    // no stub: the fixture agents have no transcript, so the host falls back to what herdr keeps
    await page.click('#dback'); await page.waitForTimeout(500);
    await tap(page, `.item[data-key="${bk}"]`);
    await page.waitForSelector('#detail:not([hidden])');
    await page.waitForFunction(() => document.getElementById('hmark') && document.getElementById('hmark').textContent.includes('herdr keeps'), null, { timeout: 8000 });
    check('history: an agent with no transcript falls back to what herdr keeps, and says so', (await page.$$('#live .tl')).length > 3);
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(500);
  }

  // ---- no pinch zoom and no sideways scrolling, in every view, with hostile (very long, unbroken) content
  {
    const LONG = 'W'.repeat(500), PATHY = 'some/long/path/'.repeat(40), URL = 'https://example.com/' + 'x'.repeat(300);
    await page.route('**/api/pane*', async (route) => {
      const res = await route.fetch(); const j = await res.json();
      j.text = [LONG, PATHY, URL, j.text].join('\n');
      route.fulfill({ response: res, json: j });
    });
    await page.route('**/api/history*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ source: 'transcript',
      entries: [{ ts: '2026-10-09T10:00:00Z', r: 'user', x: 'H'.repeat(500) }, { ts: '2026-10-09T10:00:01Z', r: 'assistant', x: 'other/dir/'.repeat(60) + 'https://history.example/' + 'q'.repeat(300) },
                { r: 'tool', x: 'Bash(' + 'T'.repeat(400) + ')' }, { r: 'result', x: 'R'.repeat(450) }], before: null, size: 9, note: null, at: Date.now() / 1000 }) }));
    const audit = () => page.evaluate(async () => {
      const off = [], de = document.documentElement;
      if (de.scrollWidth > innerWidth + 1) off.push('document ' + de.scrollWidth + '>' + innerWidth);
      if (document.body.scrollWidth > innerWidth + 1) off.push('body ' + document.body.scrollWidth);
      const scrollers = [];
      for (const n of document.querySelectorAll('body, body *')) {
        const cs = getComputedStyle(n);
        if (cs.display === 'none' || n.closest('[hidden]')) continue;
        const name = n.id ? '#' + n.id : n.className && typeof n.className === 'string' ? '.' + n.className.split(' ')[0] : n.tagName;
        if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && cs.overflowX !== 'hidden' && cs.overflowX !== 'clip') off.push(name + ' could scroll sideways (overflow-x ' + cs.overflowX + ')');
        if (cs.overflowY === 'auto' || cs.overflowY === 'scroll') scrollers.push(n);
        const r = n.getBoundingClientRect();
        if (r.width > 0 && (r.right > innerWidth + 1 || r.left < -1) && cs.position !== 'fixed') off.push(name + ' sticks out of the screen (' + Math.round(r.left) + '..' + Math.round(r.right) + ')');
      }
      for (const n of [de, ...scrollers]) n.scrollLeft = 80;                // try to push everything sideways
      await new Promise((r) => setTimeout(r, 150));
      for (const n of [de, ...scrollers]) if (n.scrollLeft !== 0) off.push((n.id || n.className || 'document') + ' moved sideways to ' + n.scrollLeft);
      return off;
    });
    const noSide = async (label) => { const off = await audit(); if (off.length) console.log('   sideways (' + label + '):', JSON.stringify(off.slice(0, 5))); check('no sideways scroll: ' + label, off.length === 0); };

    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(500);
    await noSide('queue');
    await hold(page, '.item:nth-of-type(2)', 650);
    await page.waitForSelector('#sheet:not([hidden]) .peek'); await page.waitForTimeout(400);
    await noSide('peek sheet (long pane text)');
    await page.click('#sheet .btns button:first-child'); await page.waitForTimeout(300);
    await page.click('#main .secrow .mini-btn'); await page.waitForSelector('#sheet:not([hidden]) .chk'); await page.waitForTimeout(300);
    await page.fill('#sheet textarea, #sheet input[type=text]', LONG).catch(() => {});
    await noSide('wake sheet');
    await page.click('#sheet .btns button:first-child'); await page.waitForTimeout(300);

    const bk2 = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'blocked'); return a.host + '|' + a.pane; });
    await tap(page, `.item[data-key="${bk2}"]`);
    await page.waitForSelector('#detail:not([hidden])'); await page.waitForSelector('#hist .hent', { timeout: 8000 }); await page.waitForTimeout(400);
    await noSide('session view (long pane text and long history)');
    await page.screenshot({ path: `${OUT}/z1-long-content.png` });
    await page.click('#dback'); await page.waitForTimeout(300);   // a blocked numbered question refuses typed words now, so the long message goes to an idle session
    const idl = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'idle' && x.agent === 'claude'); return a.host + '|' + a.pane; });
    await tap(page, `.item[data-key="${idl}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(400);
    await page.fill('#msg', LONG + ' ' + URL); await page.click('#send'); await page.waitForSelector('#sheet:not([hidden]) .kv'); await page.waitForTimeout(300);
    await noSide('confirm sheet (long message)');
    await page.click('#sheet .btns button:first-child'); await page.waitForTimeout(300);
    await page.fill('#msg', '');
    await page.click('#dback'); await page.waitForTimeout(400);
    await hold(page, '.tab[data-view=waiting]', 900); await page.waitForSelector('#sheet:not([hidden]) .search'); await page.fill('#sheet .search', LONG); await page.waitForTimeout(300);
    await noSide('go-to (long search)');
    await page.evaluate(() => document.getElementById('shade').click()); await page.waitForTimeout(300);
    for (const v of ['all', 'hosts', 'settings']) { await tap(page, `.tab[data-view=${v}]`); await page.waitForTimeout(500); await noSide('tab ' + v); }
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await page.unroute('**/api/pane*'); await page.unroute('**/api/history*');

    // pinch zoom: the page must refuse it
    const meta = await page.getAttribute('meta[name=viewport]', 'content');
    check('zoom: the viewport forbids scaling (user-scalable=no, minimum and maximum scale 1)', /user-scalable=no/.test(meta) && /maximum-scale=1\b/.test(meta) && /minimum-scale=1\b/.test(meta));
    const ta = await page.evaluate(() => ['body', '#main', '#tail', '#send', '.item', '.tab', '#msg', '.sheet', 'html'].map((q) => { const n = document.querySelector(q); return n ? [q, getComputedStyle(n).touchAction] : null; }).filter(Boolean));
    check('zoom: touch-action is pan-y on every kind of element (iOS ignores user-scalable)', ta.length >= 8 && ta.every(([, v]) => v === 'pan-y'));
    check('zoom: Safari gesture events (pinch) are cancelled', await page.evaluate(() => ['gesturestart', 'gesturechange', 'gestureend'].every((t) => { const e = new Event(t, { cancelable: true, bubbles: true }); document.body.dispatchEvent(e); return e.defaultPrevented; })));
    check('zoom: a two-finger move is cancelled', await page.evaluate(() => { const mkT = (id, x) => new Touch({ identifier: id, target: document.body, clientX: x, clientY: 200 });
      const e = new TouchEvent('touchmove', { cancelable: true, bubbles: true, touches: [mkT(1, 100), mkT(2, 200)], changedTouches: [mkT(1, 100)] }); document.body.dispatchEvent(e); return e.defaultPrevented; }));
    check('zoom: a one-finger move is NOT cancelled (scrolling still works)', await page.evaluate(() => { const t = new Touch({ identifier: 1, target: document.body, clientX: 100, clientY: 200 });
      const e = new TouchEvent('touchmove', { cancelable: true, bubbles: true, touches: [t], changedTouches: [t] }); document.body.dispatchEvent(e); return !e.defaultPrevented; }));
    check('zoom: ctrl+wheel (trackpad pinch) is cancelled', await page.evaluate(() => { const e = new WheelEvent('wheel', { ctrlKey: true, deltaY: -50, cancelable: true, bubbles: true }); document.body.dispatchEvent(e); return e.defaultPrevented; }));
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.synthesizePinchGesture', { x: 195, y: 300, scaleFactor: 2.5, relativeSpeed: 600, gestureSourceType: 'touch' }).catch(() => {});
    await page.waitForTimeout(300);
    check('zoom: a real synthesized pinch leaves the page at scale 1', (await page.evaluate(() => window.visualViewport.scale)) === 1);
  }

  // ---- wake a PROJECT: a terminal with no agent in it (needs the server started with SHEPR_CLAUDE_CMD="echo STARTED")
  {
    const fsh = new URL('./fake-session.sh', import.meta.url).pathname;
    execFileSync('bash', [fsh, 'asleep'], { env: ENV, encoding: 'utf8' });
    const hr = (...a) => execFileSync('herdr', a, { env: ENV, encoding: 'utf8', input: '' });
    const paneOf = (cwd) => JSON.parse(hr('pane', 'list')).result.panes.find((p) => p.cwd === cwd).pane_id;
    const herdrRead = (pane) => hr('pane', 'read', pane, '--lines', '30');
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await page.waitForFunction(() => (window.shepr.S.q.hosts || []).some((h) => h.asleep >= 2), null, { timeout: 15000 }).catch(async () => { await page.click('#refresh'); await page.waitForTimeout(4000); });
    await page.waitForSelector('#asleeprow .mini-btn', { timeout: 8000 });
    check('asleep: the queue has one "Asleep (N)" row with a Wake button, not a row per project', /Asleep/.test(await page.textContent('#asleeprow')) && (await page.$$('#main .item')).length < 12);
    await page.screenshot({ path: `${OUT}/a1-asleep-row.png` });
    await page.click('#asleeprow .mini-btn');
    await page.waitForSelector('#sheet:not([hidden]) .hrow', { timeout: 8000 }).catch(async (e) => { console.log('   picker sheet text:', JSON.stringify((await page.textContent('#sheet')).slice(0, 200))); throw e; });
    const rows = await page.$$eval('#sheet .hrow', (n) => n.map((x) => x.textContent));
    console.log('   asleep list:', JSON.stringify(rows.map((r) => r.slice(0, 60))));
    const iD = rows.findIndex((r) => r.includes('dormant')), iB = rows.findIndex((r) => r.includes('busy'));
    check('asleep: lists the projects with no agent, the one with a conversation before the one without', iD >= 0 && iB >= 0 && iD < iB);
    await page.screenshot({ path: `${OUT}/a2-asleep-picker.png` });
    await page.fill('#sheet .search', 'dorm'); await page.waitForTimeout(250);
    check('asleep: search narrows the list', (await page.$$('#sheet .hrow')).length === 1);
    check('asleep: every project can be woken with Claude or Codex', (await page.$$eval('#sheet .hrow button', (b) => b.map((x) => x.textContent))).join() === 'Claude,Codex');
    check('asleep: Resume last is the default', (await page.$$eval('#sheet .seg .on', (b) => b.map((x) => x.textContent))).join() === 'Resume last');
    await page.click('#sheet .hrow button:first-of-type');
    await page.waitForSelector('#sheet:not([hidden]) .kv', { timeout: 8000 });
    const kv = await page.textContent('#sheet .kv');
    console.log('   start plan:', kv.replace(/\s+/g, ' ').trim().slice(0, 200));
    check('asleep: the confirm sheet shows the exact command, the directory and what it resumes', /Claude: echo STARTED --continue/.test(kv) && /\/tmp\/shepr-dormant/.test(kv) && /Resumes/.test(kv));
    await page.screenshot({ path: `${OUT}/a3-asleep-confirm.png` });
    check('asleep: nothing has run yet', !herdrRead(paneOf('/tmp/shepr-dormant')).includes('STARTED'));
    await page.waitForTimeout(800);
    await page.click('#sheet .btns .go');
    await page.waitForFunction(() => document.querySelector('#toast, .toast') && /Starting dormant/.test(document.body.textContent), null, { timeout: 8000 });
    const paneId = paneOf('/tmp/shepr-dormant');
    let seen = '';
    for (let i = 0; i < 10 && !seen.includes('STARTED --continue'); i++) { await page.waitForTimeout(500); seen = herdrRead(paneId); }
    check('asleep: after confirming, the command ran in that terminal', seen.includes('STARTED --continue'));
    // Codex: resume the last Codex session, or start a new one, from the same picker
    await page.waitForTimeout(600);
    await page.click('#asleeprow .mini-btn'); await page.waitForSelector('#sheet:not([hidden]) .hrow'); await page.fill('#sheet .search', 'busy'); await page.waitForTimeout(250);
    check('asleep: Resume is disabled where there is nothing to resume (and Start new enables it)', (await page.$$eval('#sheet .hrow button', (b) => b.map((x) => x.disabled))).every(Boolean));
    await page.click('#sheet .seg .mini-btn:last-child'); await page.waitForTimeout(200);
    check('asleep: Start new enables both agents', (await page.$$eval('#sheet .hrow button', (b) => b.map((x) => x.disabled))).every((d) => !d));
    await page.fill('#sheet .search', 'dorm'); await page.click('#sheet .seg .mini-btn:first-child'); await page.waitForTimeout(200);
    await page.click('#sheet .hrow button:nth-of-type(2)');
    await page.waitForSelector('#sheet:not([hidden]) .kv');
    const kv2 = await page.textContent('#sheet .kv');
    check('asleep: Codex resumes the last Codex session in that directory', /Codex: echo CODEX resume --last/.test(kv2) && /Resumes/.test(kv2) && /Codex conversation/.test(kv2));
    await page.waitForTimeout(800); await page.click('#sheet .btns .go');
    let seen2 = '';
    for (let i = 0; i < 10 && !seen2.includes('CODEX resume --last'); i++) { await page.waitForTimeout(500); seen2 = herdrRead(paneId); }
    check('asleep: after confirming, the Codex command ran in that terminal', seen2.includes('CODEX resume --last'));
    // a terminal that is busy (not a shell prompt) must be refused, even though it has no agent
    const busy = await page.evaluate(async () => {
      const sign = (m, p, b) => window.shepr.signHeaders(m, p, b);
      const a = await (await fetch('api/asleep', { headers: await sign('GET', 'api/asleep', '') })).json();
      const it = a.items.find((x) => x.label === 'busy');
      const body = JSON.stringify({ host: it.host, pane: it.pane, kind: 'start', mode: 'fresh', agent: 'codex' });
      const r = await fetch('api/send/prepare', { method: 'POST', body, headers: Object.assign({ 'Content-Type': 'application/json', 'X-Shepr': '1' }, await sign('POST', 'api/send/prepare', body)) });
      return r.status + ' ' + (await r.text());
    });
    console.log('   busy terminal:', busy.slice(0, 170));
    check('asleep: a terminal running something else is refused, with the reason', /not_idle_shell/.test(busy) && /sleep/.test(busy) && !/^2/.test(busy));
    check('asleep: and so is a pane that is not asleep at all (an agent is in it)', await page.evaluate(async () => {
      const sign = (m, p, b) => window.shepr.signHeaders(m, p, b);
      const ag = window.shepr.S.q.agents[0];
      const body = JSON.stringify({ host: ag.host, pane: ag.pane, kind: 'start', mode: 'fresh' });
      const r = await fetch('api/send/prepare', { method: 'POST', body, headers: Object.assign({ 'Content-Type': 'application/json', 'X-Shepr': '1' }, await sign('POST', 'api/send/prepare', body)) });
      return !r.ok && /not_asleep|already running/.test(await r.text()); }));
    execFileSync('bash', [fsh, 'asleep-rm'], { env: ENV, encoding: 'utf8' });
  }

  // ---- Start new: a brand-new project (name on the title bar, folder under ~/Projects, ccc started). The server runs with
  // SHEPR_NEW_CMD="echo CCC" and SHEPR_PROJECTS=/tmp/shepr-projects so nothing touches the real ~/Projects.
  {
    fs.mkdirSync('/tmp/shepr-projects/exists', { recursive: true });
    const hr = (...a) => execFileSync('herdr', a, { env: ENV, encoding: 'utf8', input: '' });
    const wsOf = (label) => JSON.parse(hr('workspace', 'list')).result.workspaces.find((w) => w.label === label);
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(500);
    check('new: a big "Start new" button is the first thing in the queue', await page.evaluate(() => { const b = document.getElementById('newproject');
      return !!b && b === document.getElementById('main').firstElementChild && b.getBoundingClientRect().height >= 56 && /Start new/.test(b.textContent); }));
    await page.screenshot({ path: `${OUT}/a4-start-new-button.png` });
    await page.click('#newproject'); await page.waitForSelector('#np-name'); await page.waitForTimeout(250);   // the sheet focuses the name field after 50 ms; filling during that races it
    await page.screenshot({ path: `${OUT}/a5-start-new-empty.png` });
    check('new: Review is off until there is a name and a folder', await page.$eval('#sheet .btns .go', (b) => b.disabled));
    await page.fill('#np-name', 'Test Project X');
    check('new: the folder name is suggested from the project name', (await page.inputValue('#np-dir')) === 'test-project-x' && !(await page.$eval('#sheet .btns .go', (b) => b.disabled)));
    for (const bad of ['../evil', 'a/b', '.hidden', '-x', 'bad name', 'x.']) {
      await page.fill('#np-dir', bad);
      if (!(await page.$eval('#sheet .btns .go', (b) => b.disabled))) { check('new: folder ' + JSON.stringify(bad) + ' is refused', false); }
    }
    check('new: folders with a slash, a leading dot or dash, a space, or a trailing dot are refused', true);
    await page.fill('#np-dir', 'exists'); await page.click('#sheet .btns .go'); await page.waitForSelector('#sheet:not([hidden]) .kv');
    check('new: an existing folder is flagged, and will be reused', /already exists/.test(await page.textContent('#sheet .kv')));
    await page.click('#sheet .btns button:first-child'); await page.waitForTimeout(300);
    await page.click('#newproject'); await page.waitForSelector('#np-name'); await page.waitForTimeout(250);   // the sheet focuses the name field after 50 ms; filling during that races it
    const NASTY = `it's "quoted" $HOME \`whoami\` ; touch /tmp/shepr-pwned #done`;
    await page.fill('#np-name', 'Test Project X'); await page.fill('#np-msg', NASTY); await page.click('#sheet .btns .go'); await page.waitForSelector('#sheet:not([hidden]) .kv');
    const kvn = await page.textContent('#sheet .kv');
    check('new: the first message is shown on the confirm sheet', kvn.includes('First message') && kvn.includes(NASTY));
    console.log('   new project plan:', kvn.replace(/\s+/g, ' ').trim().slice(0, 260));
    check('new: the confirm sheet shows the name, the folder, and the exact command', /Test Project X/.test(kvn) && /\/tmp\/shepr-projects\/test-project-x/.test(kvn) && /will be created/.test(kvn) &&
      /mkdir -p \/tmp\/shepr-projects\/test-project-x && cd \/tmp\/shepr-projects\/test-project-x && echo CCC/.test(kvn));
    await page.screenshot({ path: `${OUT}/a6-start-new-confirm.png` });
    check('new: nothing exists yet', !wsOf('Test Project X') && !fs.existsSync('/tmp/shepr-projects/test-project-x'));
    await page.waitForTimeout(800); await page.click('#sheet .btns .go');
    await page.waitForFunction(() => /Created Test Project X/.test(document.body.textContent), null, { timeout: 8000 });
    let ws = null, pane = '', out = '';
    for (let i = 0; i < 12 && !(ws && out.includes('CCC')); i++) { await page.waitForTimeout(500); ws = wsOf('Test Project X'); if (ws) { pane = ws.workspace_id + ':p1'; out = hr('pane', 'read', pane, '--lines', '30'); } }
    check('new: the herdr project exists with the name on its title bar', !!ws);
    check('new: the folder was created', fs.existsSync('/tmp/shepr-projects/test-project-x'));
    check('new: the command ran in that terminal, in the new folder', out.includes('CCC') && /shepr-projects\/test-project-x/.test(out));
    check('new: the first message reached the command EXACTLY, with nothing in it expanded or run', out.includes('CCC ' + NASTY) && !fs.existsSync('/tmp/shepr-pwned') && !out.includes('CCC it\'s "quoted" /home'));
    const prep = (body) => page.evaluate(async (b) => { const body = JSON.stringify(Object.assign({ host: window.shepr.S.hello.name, kind: 'new_project' }, b));
      const r = await fetch('api/send/prepare', { method: 'POST', body, headers: Object.assign({ 'Content-Type': 'application/json', 'X-Shepr': '1' }, await window.shepr.signHeaders('POST', 'api/send/prepare', body)) });
      return r.status + ' ' + (await r.text()); }, body);
    const badMsgs = [await prep({ name: 'ok', dir: 'fine', message: 'two\nlines' }), await prep({ name: 'ok', dir: 'fine', message: 'x'.repeat(501) }), await prep({ name: 'ok', dir: 'fine', message: 'bell\u0007' }), await prep({ name: 'ok', dir: 'fine', message: 5 })];
    check('new: the server refuses a first message with a newline, a control character, a non-string or over 500 characters', badMsgs.every((b) => /^400 /.test(b) && /bad_message/.test(b)));
    const bads = [await prep({ name: 'ok', dir: '../etc' }), await prep({ name: 'ok', dir: 'a/b' }), await prep({ name: 'ok', dir: '.x' }), await prep({ name: 'ok', dir: 'a;rm -rf' }),
                  await prep({ name: '', dir: 'fine' }), await prep({ name: 'x'.repeat(61), dir: 'fine' }), await prep({ name: 'bad\nname', dir: 'fine' }), await prep({ name: 'ok', dir: '' })];
    console.log('   refused:', JSON.stringify(bads.map((b) => b.slice(0, 18))));
    check('new: the server refuses path tricks, empty or oversized names, and newlines on its own', bads.every((b) => /^400 /.test(b) && /bad_dir|bad_name/.test(b)));
    if (ws) hr('workspace', 'close', ws.workspace_id);
    fs.rmSync('/tmp/shepr-projects', { recursive: true, force: true });
  }

  // ---- photos and files: resize on the phone, store on the host, put the path in the message box, send nothing
  {
    const TINY_PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(400);
    const key = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'idle' && x.agent === 'claude'); return a.host + '|' + a.pane; });   // idle: typed words are refused at a numbered question
    await tap(page, `.item[data-key="${key}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(500);
    check('photo: the message bar has an attach button', !!(await page.$('#attachbtn')) && (await page.$eval('#attachbtn', (b) => b.getBoundingClientRect().height)) >= 44);
    await page.screenshot({ path: `${OUT}/p1-attach-button.png` });
    await page.setInputFiles('#filepick', { name: 'shot.png', mimeType: 'image/png', buffer: TINY_PNG });
    await page.waitForSelector('#sheet:not([hidden]) .at-row'); await page.click('#at-go');
    await page.waitForFunction(() => /\/uploads\/.+\.png/.test(document.getElementById('msg').value), null, { timeout: 8000 });
    const m1 = await page.inputValue('#msg');
    const p1 = m1.trim().split(/\s+/).pop();
    check('photo: the saved path lands in the message box, ready for a note', /\/uploads\/\d{4}-\d{2}-\d{2}\/[0-9a-f]{16}\.png$/.test(p1));
    check('photo: the file on the host is the PNG, private (0600)', fs.existsSync(p1) && Buffer.compare(fs.readFileSync(p1), TINY_PNG) === 0 && (fs.statSync(p1).mode & 0o777) === 0o600);
    await page.fill('#msg', '');
    await page.setInputFiles('#filepick', { name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('line one\nline two\n') });
    await page.waitForSelector('#sheet:not([hidden]) .at-row'); await page.click('#at-go');
    await page.waitForFunction(() => /\.txt/.test(document.getElementById('msg').value), null, { timeout: 8000 });
    const p2 = (await page.inputValue('#msg')).trim().split(/\s+/).pop();
    check('photo: a plain text file is stored as text', fs.readFileSync(p2, 'utf8') === 'line one\nline two\n');
    await page.fill('#msg', 'keep this');
    await page.setInputFiles('#filepick', { name: 'evil.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('MZ\x90\x00 not allowed') });
    await page.waitForSelector('#sheet:not([hidden]) .at-row'); await page.click('#at-go');
    await page.waitForFunction(() => /not a photo|will accept/.test(document.body.textContent), null, { timeout: 8000 });
    await page.click('#sheet .btns button:not(.go)'); await page.waitForTimeout(300);
    check('photo: a program is refused with a reason, and the message box is untouched', (await page.inputValue('#msg')) === 'keep this');
    // several photos at once, each with its own comment
    await page.fill('#msg', 'Please look at these.');
    const PNG2 = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNgYAAAAAMAASsJTYQAAAAASUVORK5CYII=', 'base64');
    await page.setInputFiles('#filepick', [{ name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'b.png', mimeType: 'image/png', buffer: PNG2 }, { name: 'c.png', mimeType: 'image/png', buffer: TINY_PNG }]);
    await page.waitForSelector('#sheet:not([hidden]) .at-row');
    const rows = await page.$$eval('.at-row', (n) => n.length);
    check('photos: choosing three shows three rows, each with a thumbnail and a comment box', rows === 3 && (await page.$$eval('.at-row .at-th', (n) => n.length)) === 3 && (await page.$$eval('.at-row .at-c', (n) => n.length)) === 3);
    check('photos: the sheet says how many', /Attach 3 photos/.test(await page.textContent('#sheet h3')));
    await page.waitForTimeout(400);
    await page.fill('.at-row:nth-child(1) .at-c', 'Then this happened. Fix this edge case but do not make the user lose what they typed. It should keep every word visible while I type it.'); await page.waitForTimeout(300);
    const ac = await page.$eval('.at-row:nth-child(1) .at-c', (t) => { const cs = getComputedStyle(t), lh = parseFloat(cs.lineHeight); return { tag: t.tagName, h: t.clientHeight, atEnd: t.scrollTop + t.clientHeight >= t.scrollHeight - 1, lines: (t.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom)) / lh }; });
    check('photos: the comment box grows as you type, up to four lines when photos share the sheet, and the end of what you typed stays in view', ac.tag === 'TEXTAREA' && ac.h > 44 && ac.atEnd && ac.lines <= 4.1);
    await page.fill('.at-row:nth-child(1) .at-c', '');
    await page.waitForTimeout(600);   // let the sheet finish sliding up
    check('photos: the Cancel and Attach buttons are on screen with three photos listed', await page.evaluate(() => { const b = document.querySelector('#sheet .btns'); return b.getBoundingClientRect().bottom <= window.innerHeight + 1; }));
    await page.screenshot({ path: `${OUT}/p2-attach-sheet.png` });
    const cs = await page.$$('.at-c'); await cs[0].fill('the login screen'); await cs[2].fill('the error, in red');
    await page.click('.at-row:nth-child(2) .at-x'); await page.waitForTimeout(200);
    check('photos: a photo can be removed before it is uploaded, and the comments of the others stay', (await page.$$eval('.at-row', (n) => n.length)) === 2 && (await page.$$eval('.at-c', (n) => n.map((x) => x.value).join('|'))) === 'the login screen|the error, in red');
    await page.click('#at-go'); await page.waitForFunction(() => /uploads\/.+\.png/.test(document.getElementById('msg').value) && document.getElementById('sheet').hidden, null, { timeout: 12000 });
    const mm = (await page.inputValue('#msg')).split('\n');
    console.log('   multi message:', JSON.stringify(mm));
    check('photos: the box keeps what you had typed, then each path on its own line with its comment under it, in order', mm[0] === 'Please look at these.' && /\/uploads\/.+\.png$/.test(mm[1]) && mm[2] === 'Photo 1: the login screen' && /\/uploads\/.+\.png$/.test(mm[3]) && mm[4] === 'Photo 2: the error, in red');
    check('photos: both files are on the host, private', [mm[1], mm[3]].every((f) => fs.existsSync(f) && (fs.statSync(f).mode & 0o777) === 0o600) && mm[1] !== mm[3]);
    check('photos: nothing was sent to the agent, only put in the box (and kept as the draft)', (await page.evaluate(() => Object.values(JSON.parse(localStorage.getItem('shepr.drafts') || '{}')).some((d) => /Photo 2: the error/.test(d.t)))));
    await page.setInputFiles('#filepick', [{ name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }, { name: 'a.png', mimeType: 'image/png', buffer: TINY_PNG }]);
    await page.waitForSelector('#sheet:not([hidden]) .at-row');
    check('photos: at most ten at a time', (await page.$$eval('.at-row', (n) => n.length)) === 10 && await page.$eval('.at-more', (b) => b.disabled));
    await page.click('#sheet .btns button:not(.go)'); await page.waitForTimeout(300);
    await page.fill('#msg', 'keep this');
    const big = await page.evaluate(async () => { const c = document.createElement('canvas'); c.width = 3200; c.height = 2400; const x = c.getContext('2d');
      for (let i = 0; i < 40; i++) { x.fillStyle = `hsl(${i * 9},80%,50%)`; x.fillRect(i * 80, (i % 7) * 340, 300, 600); }
      const blob = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.95)); const f = new File([blob], 'IMG_0001.JPG', { type: 'image/jpeg' });
      const out = await window.shepr.prepareUpload(f); const bmp = await createImageBitmap(new Blob([out.bytes]));
      return { w: bmp.width, h: bmp.height, ext: out.ext, magic: Array.from(out.bytes.slice(0, 3)), orig: blob.size, now: out.bytes.length }; });
    check('photo: a big camera photo is shrunk to 2000 px and sent as JPEG', Math.max(big.w, big.h) === 2000 && big.ext === 'jpg' && big.magic.join() === '255,216,255' && big.now < big.orig);
    // the endpoint itself, with hostile requests
    const tryUp = (opts) => page.evaluate(async (o) => {
      const path = 'api/upload?host=' + encodeURIComponent(o.host || window.shepr.S.hello.name) + '&ext=' + (o.ext || 'png');
      const bytes = o.bytes ? new Uint8Array(o.bytes) : new Uint8Array(10);
      const h = { 'Content-Type': o.ctype || 'application/octet-stream' }; if (o.xs !== false) h['X-Shepr'] = '1';
      if (o.sign !== false) Object.assign(h, await window.shepr.signHeaders('POST', path, o.signedBody || bytes));
      const r = await fetch(path, { method: 'POST', headers: h, body: bytes }); return r.status + ' ' + (await r.text()).slice(0, 120); },
      opts);
    const png = [...TINY_PNG];
    const res = { unsigned: await tryUp({ sign: false, bytes: png }), json: await tryUp({ ctype: 'application/json', bytes: png }), noHeader: await tryUp({ xs: false, bytes: png }),
                  badMagic: await tryUp({ bytes: [77, 90, 0, 0, 1, 2, 3] }), badHost: await tryUp({ host: 'nowhere', bytes: png }), tampered: await tryUp({ bytes: png, signedBody: new Uint8Array(3) }),
                  tooBig: await page.evaluate(async () => { const path = 'api/upload?host=' + window.shepr.S.hello.name + '&ext=png'; const b = new Uint8Array(9 * 1024 * 1024);
                    const r = await fetch(path, { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/octet-stream', 'X-Shepr': '1' }, await window.shepr.signHeaders('POST', path, b)), body: b }); return r.status + ' ' + (await r.text()).slice(0, 90); }) };
    console.log('   upload attacks:', JSON.stringify(Object.fromEntries(Object.entries(res).map(([k, v]) => [k, v.slice(0, 22)]))));
    check('photo: an unsigned upload is refused (401)', /^401 /.test(res.unsigned));
    check('photo: a wrong content type, a missing header, a tampered body and a bad host are refused (400/401)', /^400 /.test(res.json) && /^400 /.test(res.noHeader) && /^401 /.test(res.tampered) && /^40[04] /.test(res.badHost));
    check('photo: bytes that are not an allowed type are refused with a reason', /^400 /.test(res.badMagic) && /not a photo/.test(res.badMagic));
    check('photo: a 9 MB upload is refused before it is read', /^400 /.test(res.tooBig) && /too large/.test(res.tooBig));
    await page.fill('#msg', 'what is this? ' + p1); await page.click('#send'); await page.waitForSelector('#sheet:not([hidden]) .kv');
    check('photo: Send still shows the exact text on the confirm sheet, with the path in it', (await page.textContent('#sheet .kv')).includes(p1));
    await page.click('#sheet .btns button:first-child'); await page.fill('#msg', '');
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
  }

  // ---- the Notifications card is on the Keys tab
  {
    await tap(page, '.tab[data-view=settings]'); await page.waitForTimeout(600);
    await page.click('#setseg [data-seg=alerts]'); await page.waitForTimeout(400);
    const c = await page.textContent('#notifycard').catch(() => '');
    check('notifications: Settings > Alerts has a Notifications card that says what it does and how to enable it', /Notifications/.test(c) && /(Turn on notifications|Home Screen|blocked)/.test(c));
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
  }

  // ---- the waiting ledger: a strip under Start new, a sheet behind it, hidden when there is nothing to show
  {
    const days = ['2026-10-04', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-10'].map((d, i) => ({ d, n: i % 3, secs: [0, 900, 0, 2700, 600, 1620, 4320][i] }));
    const data = (now) => ({ today: { n: 4, secs: 4320, longest: { label: 'burnbar', secs: 2280 }, top: [{ label: 'burnbar', secs: 2280, host: 'gus' }, { label: 'clarity', secs: 1200, host: 'gus' }, { label: 'blip', secs: 840, host: 'vic' }] },
      days, now: now || { n: 0, secs: 0 }, at: Date.now() / 1000 });
    let L = data();
    await page.route('**/api/ledger*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(L) }));
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await page.evaluate(() => window.shepr.loadLedger()); await page.waitForSelector('#ledger', { timeout: 6000 });
    const txt = await page.textContent('#ledger');
    check('ledger: a strip under Start new says how many waits today and for how long', /TODAY/.test(txt) && /4 waits/.test(txt) && /1h 12m/.test(txt) && /longest 38m/.test(txt) && /burnbar/.test(txt));
    check('ledger: it sits right under the Start new button, with a 7-day mini chart', await page.evaluate(() => { const m = document.getElementById('main'); return m.children[1].id === 'ledger' && document.querySelectorAll('#ledger svg rect').length === 7 && document.querySelectorAll('#ledger svg rect.today').length === 1; }));
    await page.screenshot({ path: `${OUT}/l1-ledger-strip.png` });
    await page.click('#ledger'); await page.waitForSelector('#sheet:not([hidden]) .lchart');
    const sh = await page.textContent('#sheet');
    check('ledger: tapping it shows a 7-day chart and the longest waits today, by name and host', (await page.$$('#sheet .lcol')).length === 7 && /Longest waits today/.test(sh) && /burnbar/.test(sh) && /38m/.test(sh) && /vic/.test(sh));
    await page.screenshot({ path: `${OUT}/l2-ledger-sheet.png` });
    await page.evaluate(() => document.getElementById('shade').click()); await page.waitForTimeout(300);
    L = data({ n: 2, secs: 780 }); await page.evaluate(() => window.shepr.loadLedger()); await page.waitForTimeout(500);
    check('ledger: when agents are waiting right now the strip leads with that, in amber', /WAITING NOW\s+2/.test(await page.textContent('#ledger')) && /13m/.test(await page.textContent('#ledger')) && (await page.$('#ledger .l1.hot')) !== null);
    L = { today: { n: 0, secs: 0, longest: null, top: [] }, days: days.map((d) => ({ ...d, n: 0, secs: 0 })), now: { n: 0, secs: 0 }, at: 1 };
    await page.evaluate(() => window.shepr.loadLedger()); await page.waitForTimeout(500);
    check('ledger: with nothing to report the strip is not shown at all', (await page.$('#ledger')) === null);
    check('ledger: durations read well (under a minute, minutes, hours)', await page.evaluate(() => [20, 60, 2280, 3600, 4320, 7260].map(window.shepr.fmtDur).join('|') === '<1m|1m|38m|1h 00m|1h 12m|2h 01m'));
    await page.unroute('**/api/ledger*');
  }

  // ---- the Radar tab (it replaced the Herd list, which was only the Queue again in alphabetical order)
  {
    await tap(page, '.tab[data-view=all]'); await page.waitForSelector('svg.radar'); await page.waitForTimeout(400);
    check('radar: the tab is called Radar and shows the radar straight away, with no list behind it', (await page.textContent('.tab[data-view=all]')).trim() === 'Radar' && (await page.$$('#main .item')).length === 0 && (await page.$('#herdseg')) === null);
    const want = await page.evaluate(() => ({ all: window.shepr.S.q.agents.length, blocked: window.shepr.S.q.agents.filter((a) => a.status === 'blocked').length, working: window.shepr.S.q.agents.filter((a) => a.status === 'working').length }));
    const got = await page.evaluate(() => ({ dots: document.querySelectorAll('.radar .rdot').length, blocked: document.querySelectorAll('.radar .rdot.blocked').length, pulses: document.querySelectorAll('.radar .rdot.blocked .rpulse').length,
      working: document.querySelectorAll('.radar .rdot.working').length, centre: document.querySelector('.radar .rnum').textContent, hosts: [...document.querySelectorAll('.radar .rhost')].map((t) => t.textContent) }));
    console.log('   radar:', JSON.stringify({ want, got }));
    check('radar: one dot per session, in the right state, blocked ones pulsing', got.dots === want.all && got.blocked === want.blocked && got.pulses === want.blocked && got.working === want.working);
    check('radar: the centre counts what is waiting on you, and the host is named on its sector', got.centre === String(want.blocked) && got.hosts.length >= 1);
    check('radar: a waiting session is labelled by name on the radar', (await page.$$eval('.radar .rdot.blocked .rlbl', (t) => t.map((x) => x.textContent))).length === want.blocked);
    check('radar: the page and the radar do not scroll sideways, and it fits without scrolling the page', await page.evaluate(() => { const r = document.querySelector('.radarwrap').getBoundingClientRect(); return document.documentElement.scrollWidth <= innerWidth + 1 && r.right <= innerWidth + 1 && r.left >= -1; }));
    await page.screenshot({ path: `${OUT}/r1-radar.png` });
    await tap(page, '.radar .rdot.blocked'); await page.waitForSelector('#detail:not([hidden])', { timeout: 5000 });
    check('radar: tapping a dot opens that session', (await page.textContent('#dtitle')).length > 0);
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
    await page.emulateMedia({ reducedMotion: 'reduce' }); await page.waitForTimeout(200);
    check('radar: with reduced motion on, the sweep and the pulses are still', await page.evaluate(() => getComputedStyle(document.querySelector('.radar .rsweep')).animationName === 'none' && getComputedStyle(document.querySelector('.radar .rpulse')).animationName === 'none'));
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    check('radar: the tab bar has five tabs and the radar tab has its own icon', (await page.$$('.tab')).length === 5 && (await page.$$eval('.tab[data-view=all] svg circle', (c) => c.length)) >= 2);
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
  }

  // ---- what a row is waiting on, and the count that takes you to it
  {
    const fsh = new URL('./fake-session.sh', import.meta.url).pathname;
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(500);
    const subs = await page.$$eval('.item.blocked .sub', (n) => n.map((x) => ({ t: x.textContent, c: x.className })));
    console.log('   waiting rows say:', JSON.stringify(subs.map((s) => s.t)));
    check('waiting: each blocked row says WHAT it is waiting on (the question on screen), not the session title', subs.length >= 3 && subs.every((s) => /ask/.test(s.c) && /Do you want to proceed\?/.test(s.t)));
    execFileSync('bash', [fsh, 'nomenu'], { env: ENV, encoding: 'utf8' });
    await page.waitForFunction(() => window.shepr.S.q.agents.some((a) => a.wlabel === 'nomenu' && a.ask), null, { timeout: 15000 }).catch(async () => { await page.click('#refresh'); await page.waitForTimeout(4500); });
    await page.waitForTimeout(500);
    const nm = await page.$$eval('.item.blocked .sub.nomenu', (n) => n.map((x) => x.textContent));
    check('waiting: a blocked agent with NO question on screen says so plainly (a false alarm is visible at a glance)', nm.length === 1 && /no question is on screen/.test(nm[0]));
    await page.screenshot({ path: `${OUT}/w1-waiting-rows.png` });
    check('waiting: the header count takes you to what is waiting', await (async () => { await page.click('#count'); await page.waitForSelector('#detail:not([hidden])', { timeout: 4000 }); const t = await page.textContent('#dtitle'); await page.click('#dback'); await page.waitForTimeout(400); return t.length > 0; })());
    await page.click('#count'); await page.waitForSelector('#detail:not([hidden])'); const first = await page.textContent('#dtitle');
    await page.dblclick('#dhead'); await page.waitForTimeout(700); const second = await page.textContent('#dtitle');   // the open session covers the header count, so the title's double-tap is how you go on
    check('waiting: double-tapping the title inside a session goes on to the next waiting one, not the same one', first.length > 0 && first !== second);
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
    await tap(page, '.tab[data-view=all]'); await page.waitForSelector('svg.radar'); await tap(page, '.radar .rcentre'); await page.waitForSelector('#detail:not([hidden])', { timeout: 4000 });
    check('waiting: the number in the middle of the radar takes you there too', true);
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
    execFileSync('bash', [fsh, 'nomenu-rm'], { env: ENV, encoding: 'utf8' });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
  }

  // ---- subscription budget (the Subs page, from a usage fixture) and unsent drafts that survive moving between sessions
  {
    const now = Date.now(), H = 3600000, D = 24 * H;
    const row = (label, used, win, left) => ({ label, used, resets_ms: now + left, window_ms: win, projected: used / (1 - left / win), dry_at: 0,
      series: Array.from({ length: 20 }, (_, i) => { const e = (1 - left / win) * (i + 1) / 20; return [e, used * (i + 1) / 20 * (0.8 + 0.2 * Math.sin(i))]; }) });
    const ag = (id, name, live, rows) => ({ id, name, live, measured_at: now - (live ? 60000 : 5 * H), status: '', help: '', tier: '', rows });
    const fx = { available: true, measuring: false, reason: '', generated_at: now, now, agents: [   // the view usage.py produces, as a fixture (SHEPR_USAGE_FIXTURE)
      ag('claude', 'Claude', true, [row('Session (5-hour)', 0.02, 5 * H, 3.5 * H), row('Weekly (7-day)', 0.91, 7 * D, 1 * D), row('Fable Weekly', 0.34, 7 * D, 1 * D)]),
      ag('codex', 'Codex', true, [row('Weekly (7-day)', 0.17, 7 * D, 3.6 * D)]),
      ag('kimi', 'Kimi', true, [row('Session (5-hour)', 0.0, 5 * H, 4 * H), row('Monthly (total)', 0.5, 30 * D, 15 * D)]),
      ag('grok', 'Grok', false, [row('Weekly (7-day)', 0.02, 7 * D, 4.7 * D)])] };
    fs.writeFileSync(process.env.SHEPR_USAGE_FIXTURE || '/tmp/claude-1000/shepr-usage.json', JSON.stringify(fx));
    // the radar page: the four counts on top (also its legend), the radar filling everything below, nothing to scroll
    await tap(page, '.tab[data-view=all]'); await page.waitForSelector('svg.radar'); await page.waitForTimeout(700);
    const rg = await page.evaluate(() => { const m = document.getElementById('main'), mr = m.getBoundingClientRect(), r = document.querySelector('svg.radar').getBoundingClientRect(), s = document.querySelector('.rstats').getBoundingClientRect();
      return { tiles: [...document.querySelectorAll('.rstat')].map((t) => t.querySelector('b').textContent + ' ' + t.querySelector('.rl').textContent), radarShare: r.height / mr.height, scrolls: m.scrollHeight > m.clientHeight + 1, side: document.documentElement.scrollWidth > window.innerWidth, above: s.bottom <= r.top + 2, noBudget: !document.getElementById('budget') }; });
    console.log('   radar page:', JSON.stringify(rg));
    check('radar: four counts on top, Waiting Finished Working Idle, which are also the legend', rg.tiles.length === 4 && /Waiting/.test(rg.tiles[0]) && /Finished/.test(rg.tiles[1]) && /Working/.test(rg.tiles[2]) && /Idle/.test(rg.tiles[3]) && (await page.$('.rleg')) === null);
    check('radar: the radar is back to the full page: it fills the screen under the counts, with nothing else under it, and nothing scrolls', rg.radarShare > 0.7 && rg.above && rg.noBudget && !rg.scrolls && !rg.side);
    await page.screenshot({ path: `${OUT}/r1-radar-page.png` });
    check('there is no Go tab any more, and Keys is now Settings: Queue, Radar, Subs, Hosts, Settings', (await page.$$eval('.tab', (n) => n.map((x) => x.dataset.view + ':' + x.querySelector('span:not(.badge)').textContent).join())) === 'waiting:Queue,all:Radar,subs:Subs,hosts:Hosts,settings:Settings');
    // the Subs page: every subscription listed with its numbers, no tap needed, and it animates in
    await tap(page, '.tab[data-view=subs]'); await page.waitForSelector('.sc', { timeout: 10000 });
    const anim = await page.evaluate(() => ({ enter: document.getElementById('budget').classList.contains('enter'), card: getComputedStyle(document.querySelector('.sc')).animationName, draw: getComputedStyle(document.querySelector('.sc .bd-line')).animationName, ping: getComputedStyle(document.querySelector('.sc .bd-ping')).animationName }));
    check('subs: it arrives animated: the cards slide in, the curves draw themselves, and the current point pings', anim.enter && anim.card === 'scin' && anim.draw === 'scdraw' && anim.ping === 'scping');
    await page.waitForTimeout(1400);
    const cards = await page.$$eval('.sc', (n) => n.map((c) => ({ name: c.querySelector('.sc-n').textContent, verdict: c.querySelector('.sc-v').textContent, pct: c.querySelector('.sc-pct').textContent, line: c.querySelector('.sc-l').textContent, bars: c.querySelectorAll('.sc-r').length, svg: !!c.querySelector('svg.bd-svg path.bd-line'), note: (c.querySelector('.sc-note') || {}).textContent || '' })));
    console.log('   subs cards:', JSON.stringify(cards.map((c) => [c.name, c.verdict, c.pct, c.bars])));
    check('subs: Claude, Codex, Kimi and Grok each get a card, in that order', cards.map((c) => c.name).join() === 'Claude,Codex,Kimi,Grok');
    check('subs: Claude is 91% through its week with the clock at 86%: BEHIND, with the come-back time already on the card', cards[0].verdict === 'BEHIND' && /91/.test(cards[0].pct) && /^Come back in/.test(cards[0].line));
    check('subs: Codex shows what is banked and the clock of it without a tap', /^BANKED \d+%$/.test(cards[1].verdict) && /in the bank$/.test(cards[1].line));
    check('subs: Kimi on an even spend says ON PACE', cards[2].verdict === 'ON PACE');
    check('subs: Grok is a snapshot: ~ on its numbers and a note saying so', /^BANKED ~/.test(cards[3].verdict) && /^~/.test(cards[3].pct) && /snapshot/i.test(cards[3].note));
    check('subs: every window is listed as a bar: Claude 3, Codex 1, Kimi 2 (its duplicate meter is not shown twice), Grok 1', cards.map((c) => c.bars).join() === '3,1,2,1');
    check('subs: every card draws its burn-down graph', cards.every((c) => c.svg));
    const l0 = await page.textContent('.sc[data-id=codex] .sc-l'); await page.waitForTimeout(2300); const l1 = await page.textContent('.sc[data-id=codex] .sc-l');
    check('subs: the banked clock ticks up on its own', l0 !== l1);
    await page.waitForTimeout(1500);
    check('subs: the entrance plays once: after it, the animation class is gone, and a refresh does not replay it', await (async () => { const gone = await page.evaluate(() => !document.getElementById('budget').classList.contains('enter')); await page.click('#refresh'); await page.waitForTimeout(900); return gone && (await page.evaluate(() => !document.getElementById('budget').classList.contains('enter'))); })());
    await page.screenshot({ path: `${OUT}/b1-budget.png` });
    await page.setViewportSize({ width: 390, height: 844 }); await page.waitForTimeout(500);
    const fit = await page.evaluate(() => { const m = document.getElementById('main'); return { scrolls: m.scrollHeight > m.clientHeight + 1, over: m.scrollHeight - m.clientHeight, side: m.scrollWidth > m.clientWidth + 1 || document.documentElement.scrollWidth > window.innerWidth, clipped: [...document.querySelectorAll('.sc-n,.sc-v,.sc-rt')].filter((n) => n.scrollWidth > n.clientWidth + 1).length }; });
    console.log('   subs fit at 390x844:', JSON.stringify(fit));
    check('subs: on a full-size iPhone all four cards fit on the screen with nothing scrolling, and no word is cut off', !fit.scrolls && !fit.side && fit.clipped === 0);
    await page.screenshot({ path: `${OUT}/b2-subs-tall.png` });
    await page.setViewportSize({ width: 390, height: 664 }); await page.waitForTimeout(300);
    check('budget: pace maths agrees with Burn Bar (ahead banks, over 5% is behind, spent waits for the reset)', await page.evaluate(() => { const P = window.shepr.paceLive, n = 1000; const a = P(0.2, 2000, 1000, 1500), b = P(0.6, 2000, 1000, 1500), c = P(0.52, 2000, 1000, 1500), d = P(0.996, 2000, 1000, 1500); return Math.abs(a.banked - 0.3) < 1e-9 && b.over && b.comeBackMs === 100 && !c.over && c.behind > 0.01 && d.spent && d.comeBackMs === 500 && P(0.1, 2000, 1000, 2500) === null; }));
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
  }

  {
    const keys = await page.evaluate(() => window.shepr.S.q.agents.slice(0, 2).map((a) => a.host + '|' + a.pane));
    const openK = async (k) => { await tap(page, `.item[data-key="${k}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(300); };
    const backK = async () => { await page.click('#dback'); await page.waitForTimeout(400); };
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await page.evaluate(() => localStorage.removeItem('shepr.drafts'));
    await openK(keys[0]);
    await page.click('#msg'); await page.keyboard.type('word '.repeat(80), { delay: 0 });
    const sc = await page.evaluate(() => { const m = document.getElementById('msg'); return { atBottom: m.scrollTop + m.clientHeight >= m.scrollHeight - 3, h: m.getBoundingClientRect().height, tall: m.scrollHeight > m.clientHeight }; });
    check('message box: typing a long message keeps what you are typing in view (the box scrolls to the end, up to five whole lines)', sc.tall && sc.atBottom && sc.h > 100 && sc.h <= 142);
    await page.fill('#msg', 'half a thought for the first one');
    await backK(); await openK(keys[1]);
    check('drafts: another session starts with an empty box, not the first one\'s words', (await page.inputValue('#msg')) === '');
    await page.fill('#msg', 'second session words');
    await backK(); await openK(keys[0]);
    check('drafts: coming back to the first session finds what you typed and did not send', (await page.inputValue('#msg')) === 'half a thought for the first one');
    check('drafts: and the second still has its own', await (async () => { await backK(); await openK(keys[1]); return (await page.inputValue('#msg')) === 'second session words'; })());
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('shepr.drafts') || '{}'));
    check('drafts: they are kept on the phone, so a reload does not lose them', Object.keys(stored).length === 2 && stored[keys[0]].t === 'half a thought for the first one');
    await page.fill('#msg', ''); await backK(); await openK(keys[0]); await backK();
    // the Queue marks sessions that hold unsent words
    await page.evaluate((k) => { window.shepr.drafts.put(k, 'half a thought\n/tmp/x/uploads/2026-10-10/ab12.png\nPhoto 1: a comment'); }, keys[0]);
    await page.evaluate((k) => { window.shepr.drafts.put(k, 'second session words'); }, keys[1]);   // (the box of the second one was emptied above)
    await page.click('.tab[data-view=all]'); await page.waitForTimeout(200); await page.click('.tab[data-view=waiting]'); await page.waitForSelector(`.item[data-key="${keys[0]}"]`);
    const dtxt = await page.textContent(`.item[data-key="${keys[0]}"] .sub.draft`).catch(() => '');
    check('drafts: a Queue row holding unsent words shows them with a pencil, and how many files are with them', dtxt === '✎ half a thought (+1 file)');
    const d2 = await page.textContent(`.item[data-key="${keys[1]}"] .sub.draft`).catch(() => '');
    check('drafts: each session shows its own unsent words, not another one\'s', d2 === '\u270e second session words');
    await page.evaluate(([a, b]) => { window.shepr.drafts.put(a, ''); window.shepr.drafts.put(b, ''); }, keys);
    check('drafts: clearing the box forgets the draft', !(await page.evaluate(() => JSON.parse(localStorage.getItem('shepr.drafts') || '{}'))[keys[1]]));
    await page.fill('#msg', '').catch(() => {});
    await openK(keys[0]); await page.fill('#msg', ''); await backK();
  }

  // ---- all hosts together: no host has to be picked, and every row says which machine it is on
  {
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(400);
    // the test server reads only the isolated session, so a second machine is simulated: it is added to what the app holds, and removed again
    await page.evaluate(() => { const S = window.shepr, a = S.S.q.agents; S.keepHosts = S.S.q.hosts.slice(); S.keepAgents = a.slice();
      S.S.q.hosts.push({ id: 'vic', kind: 'remote', status: 'ok', counts: { blocked: 1 }, asleep: 0 });
      a.push(Object.assign({}, a.find((x) => x.status === 'blocked'), { host: 'vic', pane: 'wV1:p1', wlabel: 'vic-session', seq: 4000 })); S.S.host = null; S.paintMain(); window.shepr.paintHostbar && window.shepr.paintHostbar(); });
    const hh = await page.evaluate(() => ({ host: window.shepr.S.host, chips: [...document.querySelectorAll('#hostbar .chip')].map((c) => c.textContent.trim() + (c.classList.contains('on') ? '*' : '')),
      pills: [...document.querySelectorAll('#main .item')].map((r) => (r.querySelector('.hpill') || {}).textContent || '-'), items: document.querySelectorAll('#main .item').length }));
    console.log('   hosts view:', JSON.stringify(hh));
    check('hosts: the Queue shows every host at once by default (no filter), with an "All hosts" chip lit', hh.host === null && /^All hosts\*/.test(hh.chips[0]) && hh.chips.length === 3);
    check('hosts: every session row carries a coloured pill naming its machine, and the second machine\'s session is in the same list', hh.items > 1 && hh.pills.every((p) => p === 'gus' || p === 'vic') && hh.pills.includes('vic') && hh.pills.includes('gus'));
    check('hosts: the two machines get different colours', await page.evaluate(() => { const c = (t) => getComputedStyle([...document.querySelectorAll('#main .hpill')].find((p) => p.textContent === t)).color; return c('gus') !== c('vic'); }));
    await page.screenshot({ path: `${OUT}/h1-queue-all-hosts.png` });
    await page.evaluate(() => { const S = window.shepr; S.S.q.hosts.splice(0, S.S.q.hosts.length, ...S.keepHosts); S.S.q.agents.splice(0, S.S.q.agents.length, ...S.keepAgents); S.paintMain(); });
    // a phone that had the old per-host filter saved starts combined, once
    await page.evaluate(() => { localStorage.removeItem('shepr.all2'); localStorage.setItem('shepr.host', JSON.stringify('gus')); });
    await page.reload(); await page.waitForSelector('#main .item', { timeout: 20000 });
    check('hosts: a saved per-host filter from before is dropped once, so the Queue starts combined', await page.evaluate(() => window.shepr.S.host === null && JSON.parse(localStorage.getItem('shepr.host')) === null));
  }

  // ---- radar: every dot carries its name
  {
    await tap(page, '.tab[data-view=all]'); await page.waitForSelector('svg.radar .rdot'); await page.waitForTimeout(600);
    const lab = await page.evaluate(() => { const dots = [...document.querySelectorAll('svg.radar .rdot')]; return { dots: dots.length, labelled: dots.filter((d) => d.querySelector('text.rlbl')).length, inside: dots.every((d) => { const t = d.querySelector('text.rlbl'); if (!t) return false; const b = t.getBoundingClientRect(), r = document.querySelector('svg.radar').getBoundingClientRect(); return b.left >= r.left - 2 && b.right <= r.right + 2; }) }; });
    console.log('   radar labels:', JSON.stringify(lab));
    check('radar: every dot, whatever its state, carries its name, and no name runs off the radar', lab.dots >= 4 && lab.labelled === lab.dots && lab.inside);
    // crowd it: twelve more sessions across the states, and no name may sit on another name, a dot, or a ring caption
    const crowd = await page.evaluate(() => { const S = window.shepr, base = S.S.q.agents[0], names = ['Hourglass', 'gliff', 'mobs', 'Shepr', 'whats-next', 'omarchy.stories', 'clarity', 'burnbar', 'nixfredOS', 'lamplight', 'infomarchy', 'LightShow'];
      const fakes = names.map((n, i) => Object.assign({}, base, { pane: 'wQ' + i + ':p1', status: ['blocked', 'done', 'working', 'idle'][i % 4], wlabel: n, for_s: 60 * (i + 1), seq: 900 + i }));
      S.S.q.agents.push(...fakes); S.S.tab = 'all'; S.paintMain(); S.S.q.agents.splice(S.S.q.agents.length - fakes.length, fakes.length);
      const box = (n) => n.getBoundingClientRect(), ov = (a, b) => a.left < b.right - 1 && a.right > b.left + 1 && a.top < b.bottom - 1 && a.bottom > b.top + 1;
      const labels = [...document.querySelectorAll('svg.radar text.rlbl')], cores = [...document.querySelectorAll('svg.radar .rcore')], caps = [...document.querySelectorAll('svg.radar .rringlbl, svg.radar .rnum')];
      let ll = 0, ld = 0, lc = 0;
      labels.forEach((x, i) => { labels.forEach((y, j) => { if (j > i && ov(box(x), box(y))) ll++; }); cores.forEach((c) => { if (ov(box(x), box(c))) ld++; }); caps.forEach((c) => { if (ov(box(x), box(c))) lc++; }); });
      return { dots: document.querySelectorAll('svg.radar .rdot').length, labels: labels.length, ll, ld, lc }; });
    console.log('   crowded radar:', JSON.stringify(crowd));
    check('radar: with seventeen dots every one still has a name, and no name overlaps another name, a dot or a ring caption', crowd.dots >= 16 && crowd.labels === crowd.dots && crowd.ll === 0 && crowd.ld === 0 && crowd.lc === 0);
    await page.screenshot({ path: `${OUT}/r3-radar-crowded.png` });
    await page.evaluate(() => window.shepr.paintMain());
    await page.screenshot({ path: `${OUT}/r2-radar-names.png` });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
  }

  // ---- finished sessions you have opened are "looked at", per finish
  {
    const r = await page.evaluate(() => { const S = window.shepr, a0 = S.S.q.agents[0], fake = Object.assign({}, a0, { pane: 'wZZ:p9', status: 'done', seq: 777, wlabel: 'finisher' }), key = fake.host + '|' + fake.pane;
      S.S.q.agents.push(fake); localStorage.removeItem('shepr.seen');
      const before = [S.finished().length, S.finishedSeen().length]; S.seenStore.mark(key, 777); const after = [S.finished().length, S.finishedSeen().length];
      S.paintMain(); const heads = [...document.querySelectorAll('#main .sec')].map((n) => n.textContent);
      fake.seq = 778; const again = [S.finished().length, S.finishedSeen().length];
      S.S.q.agents.pop(); localStorage.removeItem('shepr.seen'); S.paintMain(); return { before, after, heads, again }; });
    check('looked at: a finished session starts as not yet looked at, and once opened moves to "Finished, looked at"', r.before.join() === '1,0' && r.after.join() === '0,1' && r.heads.some((h) => /Finished, looked at/.test(h)) && !r.heads.some((h) => /not yet looked at/.test(h)));
    check('looked at: when it finishes again it is unread again', r.again.join() === '1,0');
  }

  // ---- a message queued behind running work: Send now, and Claude Code's hints that do nothing here are hidden
  {
    const fsh = new URL('./fake-session.sh', import.meta.url).pathname;
    execFileSync('bash', [fsh, 'queued'], { env: ENV, encoding: 'utf8' });
    await page.waitForFunction(() => window.shepr.S.q.agents.some((a) => a.wlabel === 'queued'), null, { timeout: 15000 }).catch(async () => { await page.click('#refresh'); await page.waitForTimeout(4500); });
    const key = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.wlabel === 'queued'); return a.host + '|' + a.pane; });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await tap(page, `.item[data-key="${key}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForSelector('#qrow:not([hidden])', { timeout: 8000 }).catch(() => {});
    const tail = await page.textContent('#tail');
    check('queued: "Press up to edit queued messages" and the ctrl hints, which do nothing in this app, are not shown', !/Press up to edit/i.test(tail) && !/ctrl\+enter to send now/i.test(tail) && !/ctrl\+o to expand/i.test(tail) && /QUEUED-SECOND/.test(tail));
    check('queued: a Send now button appears for the queued message', !(await page.$eval('#qrow', (n) => n.hidden)) && /Send now/.test(await page.textContent('#qrow')));
    await page.screenshot({ path: `${OUT}/q1-send-now.png` });
    await page.click('#qrow .qgo'); await page.waitForSelector('#sheet:not([hidden]) .go', { timeout: 6000 });
    const qt = await page.textContent('#sheet');
    check('queued: Send now shows Ctrl+Enter and what it does before anything is pressed', /Ctrl\+Enter/.test(qt) && /queued message now/.test(qt));
    await page.click('#sheet .btns button:not(.go)'); await page.waitForTimeout(300);
    await page.click('#dback'); await page.waitForTimeout(300);
    execFileSync('bash', [fsh, 'queued-rm'], { env: ENV, encoding: 'utf8' });
  }

  // ---- a session herdr no longer has: the phone leaves it and says so, instead of asking again every 6 seconds
  {
    const fsh = new URL('./fake-session.sh', import.meta.url).pathname;
    execFileSync('bash', [fsh, 'gone'], { env: ENV, encoding: 'utf8' });
    await page.waitForFunction(() => window.shepr.S.q.agents.some((a) => a.wlabel === 'gone'), null, { timeout: 15000 }).catch(async () => { await page.click('#refresh'); await page.waitForTimeout(4500); });
    const key = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.wlabel === 'gone'); return a.host + '|' + a.pane; });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await tap(page, `.item[data-key="${key}"]`); await page.waitForSelector('#detail:not([hidden])');
    await page.waitForFunction(() => document.querySelector('#tail').textContent.length > 20);
    execFileSync('bash', [fsh, 'gone-rm'], { env: ENV, encoding: 'utf8' });
    await page.waitForSelector('#detail', { state: 'hidden', timeout: 14000 }).catch(() => {});
    const g = await page.evaluate(() => ({ open: !document.getElementById('detail').hidden, cur: window.shepr.S.cur, toast: (document.getElementById('gtoast') || {}).textContent || '', tail: document.getElementById('tail').textContent }));
    check('gone session: the phone leaves a session herdr no longer has', !g.open && g.cur === null);
    check('gone session: it says so, and never shows "Could not read this session"', /That session is gone/.test(g.toast) && !/Could not read/.test(g.toast + g.tail));
  }

  // ---- words typed at a numbered question are never sent as keystrokes
  {
    const bl = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'blocked' && x.wlabel === 'burnbar'); return a.host + '|' + a.pane; });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await tap(page, `.item[data-key="${bl}"]`); await page.waitForSelector('#optrow button', { timeout: 8000 }); await page.waitForTimeout(400);
    check('typing: at a numbered question with no typing row the box says to tap an option', /Tap an option/.test(await page.getAttribute('#msg', 'placeholder')));
    const WORDS = 'I should be able to multi select here. I cant do 1 2 3';
    await page.fill('#msg', WORDS); await page.click('#send');
    await page.waitForSelector('.skipsheet', { timeout: 6000 }).catch(() => {});
    check('typing: words sent at that question are not typed into it; a sheet says why and shows them', (await page.$('.skipsheet')) !== null && /A question is waiting/.test(await page.textContent('#sheet')) && (await page.textContent('.skipmsg')) === WORDS && (await page.$('#sheet .btns .go')) !== null);
    check('typing: what you typed is still in the box and kept as the draft', (await page.inputValue('#msg')) === WORDS && await page.evaluate((w) => Object.values(JSON.parse(localStorage.getItem('shepr.drafts') || '{}')).some((d) => d.t === w), WORDS));
    await page.click('#sheet .btns button:not(.go)'); await page.waitForTimeout(300);
    check('typing: "Answer it first" closes the sheet and keeps the words', (await page.$('.skipsheet')) === null && (await page.inputValue('#msg')) === WORDS);
    await page.click('#send'); await page.waitForSelector('.skipsheet'); await page.click('#sheet .btns .go');
    await page.waitForFunction(() => /Closes the question/.test(document.getElementById('sheet').textContent), null, { timeout: 6000 }).catch(() => {});
    const skipPlan = await page.textContent('#sheet');
    check('typing: skipping shows exactly what will happen first (Esc, then your words as a prompt) and approves nothing', /Closes the question/.test(skipPlan) && /Esc\. Nothing is answered or approved/.test(skipPlan) && /Then sends as a prompt/.test(skipPlan) && skipPlan.includes(WORDS) && (await page.$('#sheet .go.appr')) === null);
    await page.waitForTimeout(900); await page.click('#sheet .btns .go');
    await page.waitForFunction(() => /did not close/.test(document.body.textContent), null, { timeout: 14000 }).catch(() => {});
    check('typing: if the question will not close it says so, nothing was sent, and your words are still there', /did not close/.test(await page.textContent('body')) && (await page.inputValue('#msg')) === WORDS);
    await page.fill('#msg', Array.from({ length: 12 }, (_, i) => 'line ' + (i + 1) + ' of my message').join('\n')); await page.waitForTimeout(250);
    const mb = await page.$eval('#msg', (t) => { const cs = getComputedStyle(t), lh = parseFloat(cs.lineHeight), pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom); return { atEnd: t.scrollTop + t.clientHeight >= t.scrollHeight - 1, lines: (t.clientHeight - pad) / lh, scrolls: t.scrollHeight > t.clientHeight }; });
    check('typing: a long message keeps the line you are typing in full view, in a box of whole lines (no half-cut line at the bottom, five lines at most)', mb.atEnd && mb.scrolls && Math.abs(mb.lines - Math.round(mb.lines)) < 0.06 && mb.lines <= 5.06);
    await page.fill('#msg', ''); await page.click('#dback'); await page.waitForTimeout(300);
  }

  // ---- Settings: four sections that each fit one screen, the two links at the very bottom, and every choice does something
  {
    const sections = ['display', 'alerts', 'phones', 'about'], fits = {};
    for (const h of [844, 664]) {
      await page.setViewportSize({ width: 390, height: h }); await page.waitForTimeout(300);
      await tap(page, '.tab[data-view=settings]'); await page.waitForTimeout(600);
      for (const seg of sections) {
        await page.click(`#setseg [data-seg=${seg}]`); await page.waitForTimeout(300);
        fits[h + seg] = await page.evaluate(() => { const m = document.getElementById('main'), s = document.getElementById('setbody'), f = document.querySelector('.setfoot').getBoundingClientRect();
          return { fits: s.scrollHeight <= s.clientHeight + 1 && m.scrollHeight <= m.clientHeight + 1, foot: f.bottom <= innerHeight, side: m.scrollWidth - m.clientWidth }; });
        if (h === 844) await page.screenshot({ path: `${OUT}/s-${seg}.png` });
      }
    }
    console.log('   settings fit:', JSON.stringify(fits));
    check('settings: every section fits one screen at 390x844 with nothing to scroll and nothing sticking out sideways', sections.every((s) => fits['844' + s].fits && fits['844' + s].foot && fits['844' + s].side === 0));
    check('settings: and on a short phone, 390x664, too', sections.every((s) => fits['664' + s].fits && fits['664' + s].foot && fits['664' + s].side === 0));
    await page.click('#setseg [data-seg=about]'); await page.waitForTimeout(300);
    const links = await page.$$eval('.setfoot a', (a) => a.map((x) => ({ href: x.getAttribute('href'), target: x.target, rel: x.rel, h: x.getBoundingClientRect().height })));
    check('settings: at the very bottom are two links, nixfred.com and the Shepr repository on GitHub, each a big tap target that opens outside the app',
      links.length === 2 && links[0].href === 'https://nixfred.com' && links[1].href === 'https://github.com/nixfred/shepr' && links.every((l) => l.target === '_blank' && /noopener/.test(l.rel) && l.h >= 44));
    check('settings: the links are the last thing on the page, under every section', await page.evaluate(() => { const m = document.getElementById('main'); return m.lastElementChild.classList.contains('setfoot') && m.lastElementChild.getBoundingClientRect().top >= document.getElementById('setbody').getBoundingClientRect().bottom - 1; }));
    await page.click('#setseg [data-seg=display]'); await page.waitForTimeout(300);
    const pick = (k, v) => page.click(`.segc[data-pref=${k}] [data-v=${v}]`);
    const stored = () => page.evaluate(() => JSON.parse(localStorage.getItem('shepr.prefs') || '{}'));
    await pick('motion', 'off'); await page.waitForTimeout(150);
    check('settings: Animations Off stops every animation at once, and is remembered', await page.evaluate(() => document.documentElement.dataset.motion === 'off' && parseFloat(getComputedStyle(document.querySelector('.tab svg')).transitionDuration) < 0.001) && (await stored()).motion === 'off');
    await pick('motion', 'on'); await page.waitForTimeout(150);
    check('settings: Animations On brings them back', await page.evaluate(() => document.documentElement.dataset.motion === 'on' && parseFloat(getComputedStyle(document.querySelector('.tab svg')).transitionDuration) > 0.1));
    const px = () => page.evaluate(() => ({ tfs: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--tfs')), topx: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--topx')) }));
    const p0 = await px();
    await page.click('[data-set=text-size] .segb:last-child'); await page.click('[data-set=top-spacing] .segb:last-child');
    const p1 = await px(), shown = await page.textContent('[data-set=text-size] .val');
    check('settings: Text size and Top spacing steps change the page at once and show the new value', p1.tfs === p0.tfs + 1 && p1.topx === p0.topx + 4 && shown === String(p1.tfs));
    await page.click('[data-set=text-size] .segb:first-child'); await page.click('[data-set=top-spacing] .segb:first-child');
    let queueCalls = 0; const onReq = (r) => { if (/\/api\/queue/.test(r.url())) queueCalls++; }; page.on('request', onReq);
    await pick('refresh', 'fast'); await page.waitForTimeout(6500); const fastCalls = queueCalls; queueCalls = 0;
    await pick('refresh', 'manual'); await page.waitForTimeout(6500); const manualCalls = queueCalls; page.removeListener('request', onReq);
    console.log('   refresh: fast', fastCalls, 'calls in 6.5 s, manual', manualCalls);
    check('settings: Refresh really sets the pace: Fast re-reads the queue by itself within 6 seconds, Manual never does', fastCalls >= 1 && manualCalls === 0);
    check('settings: the Refresh line says what the chosen speed means', /only when you pull down/.test(await page.textContent('[data-set=refresh] .sub')));
    await pick('refresh', 'normal');
    check('settings: a damaged or unknown saved choice falls back to the default instead of breaking', await page.evaluate(() => { const P = window.shepr.prefs; localStorage.setItem('shepr.prefs', 'not json'); const a = P.get('refresh'); localStorage.setItem('shepr.prefs', JSON.stringify({ refresh: 'zzz', slash: 5 })); const b = P.get('refresh'), c = P.get('slash'); localStorage.removeItem('shepr.prefs'); return a === 'normal' && b === 'normal' && c === 'show'; }));
    // Slash commands Hide removes the / button in a session
    const idle = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'idle'); return a.host + '|' + a.pane; });
    const slashShown = async () => { await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300); await tap(page, `.item[data-key="${idle}"]`); await page.waitForSelector('#detail:not([hidden])');
      await page.waitForFunction(() => window.shepr.S.agent, null, { timeout: 8000 }).catch(() => {}); await page.waitForTimeout(500); const v = !(await page.$eval('#slashbtn', (b) => b.hidden)); await page.click('#dback'); await page.waitForTimeout(300); return v; };
    await pick('slash', 'hide'); const hid = !(await slashShown());
    await tap(page, '.tab[data-view=settings]'); await page.waitForTimeout(400); await pick('slash', 'show'); const back = await slashShown();
    check('settings: Slash commands Hide takes the / button away in a session, and Show brings it back', hid && back);
    // Clear unsent drafts
    await page.evaluate(() => { window.shepr.drafts.put('gus|wX:p1', 'a draft I never sent'); });
    await tap(page, '.tab[data-view=settings]'); await page.waitForTimeout(400); await page.click('#setseg [data-seg=about]'); await page.waitForTimeout(300);
    check('settings: About offers to clear unsent drafts and says how many there are', /Clear unsent drafts \(\d+\)/.test(await page.textContent('#setbody')));
    await page.click('text=Clear unsent drafts'); await page.waitForTimeout(400);
    check('settings: clearing them empties the saved drafts and the button goes quiet', (await page.evaluate(() => localStorage.getItem('shepr.drafts'))) === '{}' && await page.$eval('#setbody .btns:last-child button', (b) => b.disabled));
    // the choices read at launch: Open on, and the splash screen
    await page.click('#setseg [data-seg=display]'); await page.waitForTimeout(300);
    await pick('open', 'subs'); await pick('splash', 'off'); await page.reload(); await page.waitForTimeout(150);
    const rl = await page.evaluate(() => ({ splash: !!document.getElementById('splash'), tab: window.shepr.S.tab }));
    check('settings: Splash screen Off removes the opening animation, and Open on Subs lands on Subs', rl.splash === false && rl.tab === 'subs');
    await page.evaluate(() => { window.shepr.prefs.set('splash', 'short'); window.shepr.prefs.set('open', 'last'); localStorage.removeItem('shepr.splash'); });
    await page.reload(); await page.waitForTimeout(150);
    check('settings: Splash screen Short plays the quick version', await page.evaluate(() => { const s = document.getElementById('splash'); return !!s && s.classList.contains('quick'); }));
    await page.evaluate(() => { window.shepr.prefs.set('splash', 'full'); localStorage.removeItem('shepr.splash'); });
    const tFull = Date.now(); await page.reload(); await page.waitForSelector('#splash', { state: 'detached', timeout: 9000 }); const full = Date.now() - tFull;
    console.log('   full splash lasted', full, 'ms');
    check('settings: the full splash is quicker than before: gone within about 5.3 seconds, where it used to hold 5 seconds plus the fade', full >= 3900 && full <= 5300);
    await page.evaluate(() => { localStorage.removeItem('shepr.prefs'); });
    await page.waitForFunction(() => window.shepr && window.shepr.S.q, null, { timeout: 15000 });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(500);
  }

  // ---- looking around: guidance on Subs, a down host says why, a Codex tag, the age of what you read, and the tail stays at the bottom
  {
    await tap(page, '.tab[data-view=subs]'); await page.waitForSelector('.sg', { timeout: 10000 }); await page.waitForTimeout(1500);
    const sg = await page.evaluate(() => ({ t: document.querySelector('.sg-t').textContent, w: document.querySelector('.sg-w').textContent, hidden: document.querySelector('.sg').hidden, first: document.getElementById('budget').firstElementChild.className }));
    console.log('   subs guidance:', JSON.stringify(sg));
    check('subs: a banner at the top says which subscription to reach for, and why (Codex has the most room in the fixture)', sg.first === 'sg' && !sg.hidden && sg.t === 'USE CODEX NEXT' && /^\d+% of its week banked . .* ahead . resets in/.test(sg.w));
    check('subs: the banner is two lines (advice and why): who is resting is already on each card, so it is not said twice', (await page.$('.sg-r')) === null && (await page.$eval('.sg', (n) => n.getBoundingClientRect().height)) < 72);
    const G = await page.evaluate(() => { const now = 1e12, D = 86400000, W = 7 * D, ag = (id, name, used, el, live = true, more = []) => ({ id, name, live, rows: [{ label: 'Weekly (7-day)', used, resets_ms: now + (1 - el) * W, window_ms: W, projected: 0, series: [] }, ...more] }), g = window.shepr.budgetGuidance;
      const five = (u) => ({ label: 'Session (5-hour)', used: u, resets_ms: now + 3600000, window_ms: 5 * 3600000, projected: 0, series: [] });
      return { a: g([ag('claude', 'Claude', 0.9, 0.5), ag('codex', 'Codex', 0.1, 0.5)], now), b: g([ag('codex', 'Codex', 0.1, 0.5)], now), c: g([ag('claude', 'Claude', 0.9, 0.5), ag('kimi', 'Kimi', 0.8, 0.5)], now),
        d: g([ag('codex', 'Codex', 0.1, 0.5, true, [five(0.97)]), ag('kimi', 'Kimi', 0.3, 0.5)], now), e: g([ag('grok', 'Grok', 0.4, 0.55, false), ag('kimi', 'Kimi', 0.4, 0.55)], now),
        f: g([ag('codex', 'Codex', 0.5, 0.95), ag('kimi', 'Kimi', 0.2, 0.5)], now), h: g([ag('a', 'A', 0.1, 0.5), ag('b', 'B', 0.2, 0.5)], now) }; });
    check('guidance: of two, the one that is ahead is picked', G.a && G.a.kind === 'next' && G.a.id === 'codex');
    check('guidance: one subscription means nothing to choose between, so no advice', G.b === null);
    check('guidance: when everything is behind it says nothing has room, and who is resting', G.c && G.c.kind === 'none' && G.c.rest.length === 2);
    check('guidance: a subscription with a full 5-hour window is never suggested, even with its week wide open', G.d && G.d.id === 'kimi' && G.d.rest.some((x) => x.name === 'Codex'));
    check('guidance: a snapshot (Grok) needs a bigger margin than a live reading: 5% banked is not enough', G.e === null || G.e.id !== 'grok');
    check('guidance: a reset within a day with budget unspent says use it now, or lose it', G.f && G.f.kind === 'now' && G.f.id === 'codex');
    check('guidance: the one with the most plan left against the clock left wins', G.h && G.h.id === 'a');
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    const tags = await page.evaluate(() => [...document.querySelectorAll('#main .item')].map((r) => { const a = window.shepr.S.q.agents.find((x) => x.host + '|' + x.pane === r.dataset.key); return [a.agent, (r.querySelector('.atag') || {}).textContent || '']; }));
    check('rows: a session that is not Claude carries its agent (codex) as a tag, and Claude rows carry none', tags.some((x) => x[0] === 'codex' && x[1] === 'codex') && tags.filter((x) => x[0] === 'claude').every((x) => x[1] === ''));
    const hr = await page.evaluate(() => { const n = (o) => window.shepr.hostRow(Object.assign({ id: 'vic', kind: 'remote', enabled: true, status: 'down', counts: {} }, o)).querySelector('.sub').textContent;
      return { a: n({ error: 'timeout: ssh took longer than 9s', last_ok: Date.now() / 1000 - 720 }), b: n({ error: '', last_ok: null }) }; });
    check('hosts: a down machine says why and when it was last seen', /timeout: ssh took longer than 9s/.test(hr.a) && /last seen 12 minutes ago/.test(hr.a));
    check('hosts: a machine never reached since Shepr started says that instead of a time', /not answering/.test(hr.b) && /not seen since Shepr started/.test(hr.b));

    const wrapOk = await page.evaluate(() => { const m = document.getElementById('main'), box = document.createElement('div'); m.append(box);
      const r = window.shepr.hostRow({ id: 'vic', kind: 'remote', enabled: true, status: 'down', counts: {}, error: 'timeout: ssh took longer than 9s', last_ok: Date.now() / 1000 - 720 }); box.append(r);
      const s = r.querySelector('.sub'), out = { clipped: s.scrollWidth > s.clientWidth + 1, tall: s.getBoundingClientRect().height > parseFloat(getComputedStyle(s).lineHeight) * 1.5, inside: r.getBoundingClientRect().right <= window.innerWidth + 1 }; box.remove(); return out; });
    check('hosts: the reason a machine is down is shown whole, wrapped onto a second line, not cut off with an ellipsis', !wrapOk.clipped && wrapOk.tall && wrapOk.inside);
    const nd = await page.evaluate(() => { const c = (help) => window.shepr.subCard({ id: 'codex', name: 'Codex', live: true, rows: [], status: 'Codex limits unavailable', help }, 0, false).textContent;
      return { code: c('account/read'), advice: c('Run `codex login` to authenticate.') }; });
    check('subs: a card with no data shows why and a help sentence, but never a bare code like "account/read"', /Codex limits unavailable/.test(nd.code) && !/account\/read/.test(nd.code) && /Run `codex login` to authenticate\./.test(nd.advice));
    const nd2 = await page.evaluate(() => { const c = window.shepr.subCard({ id: 'claude', name: 'Claude', live: true, rows: [], status: 'Not signed in here', help: 'Run `claude auth login` on this host and Shepr can read your plan usage.' }, 0, false);
      return { verdict: c.querySelector('.sc-v').textContent, why: c.querySelector('.sc-l').textContent, fix: (c.querySelector('.sc-note.fix') || {}).textContent }; });
    check('subs: a card Shepr cannot measure says NO DATA, then why in plain words, then what to do as its own dim line', nd2.verdict === 'NO DATA' && nd2.why === 'Not signed in here' && /^Run `claude auth login` on this host/.test(nd2.fix));
    const idl = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.status === 'idle' && x.agent === 'claude'); return a.host + '|' + a.pane; });
    await tap(page, `.item[data-key="${idl}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForSelector('#tail .tl', { timeout: 8000 }); await page.waitForTimeout(1800);
    const age = await page.evaluate(() => { const n = document.getElementById('dtime'); return { t: n.textContent, c: n.className }; });
    check('reading: the toolbar says "live" while the refresh keeps up, instead of a clipped clock', age.t === 'live' && /live/.test(age.c));
    const old = await page.evaluate(() => { window.shepr.S.tailAt = Date.now() - 45000; window.shepr.paintTailAge(); const n = document.getElementById('dtime'); return { t: n.textContent, c: n.className }; });
    check('reading: once the refresh has stopped it says how old the screen is, in amber, so a stale screen does not look current', old.t === '45s ago' && /stale/.test(old.c));
    const gap = await page.evaluate(() => { const t = document.getElementById('tail'); return Math.round(t.scrollHeight - t.scrollTop - t.clientHeight); });
    check('reading: a session opens scrolled to the very bottom, so its last line is not hidden behind the message bar', gap <= 3);
    const rs = await page.evaluate(async () => { const t = document.getElementById('tail'), live = document.getElementById('live'), wait = () => new Promise((r) => setTimeout(r, 160)), grow = () => { const d = document.createElement('div'); d.style.height = '90px'; d.className = 'growprobe'; live.append(d); };
      grow(); await wait(); const stuck = Math.round(t.scrollHeight - t.scrollTop - t.clientHeight);
      t.scrollTop = 0; await wait(); grow(); await wait(); const left = t.scrollTop;
      document.querySelectorAll('.growprobe').forEach((n) => n.remove()); t.scrollTop = t.scrollHeight; await wait(); return { stuck, left }; });
    check('reading: content that grows under you keeps you at the bottom, but never pulls you down while you have scrolled up to read', rs.stuck <= 3 && rs.left === 0);
    await page.click('#dback'); await page.waitForTimeout(300);
  }

  // ---- model and effort: a chip in the session header opens a sheet of buttons; the confirm sheet shows the exact line
  {
    const idle = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.agent === 'claude' && x.status === 'idle'); return a ? a.host + '|' + a.pane : null; });
    const notIdle = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.agent === 'claude' && x.status === 'blocked'); return a ? a.host + '|' + a.pane : null; });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    if (notIdle) {
      await tap(page, `.item[data-key="${notIdle}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(400);
      check('model: no model chip on a session that is waiting on a question', await page.$eval('#dmodel', (b) => b.hidden));
      await page.click('#dback'); await page.waitForTimeout(300);
    }
    if (idle) {
      await tap(page, `.item[data-key="${idle}"]`); await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(500);
      check('model: an idle Claude session shows the Model chip', !(await page.$eval('#dmodel', (b) => b.hidden)));
      await page.click('#dmodel'); await page.waitForSelector('.mdl-b', { timeout: 4000 });
      const bs = await page.$$eval('.mdl-b', (n) => n.map((x) => x.dataset.value));
      check('model: the sheet offers the four models and the five effort levels', bs.join() === 'fable,opus,sonnet,haiku,low,medium,high,xhigh,max');
      await page.screenshot({ path: `${OUT}/m1-model-sheet.png` });
      await page.click('.mdl-b[data-value=opus]'); await page.waitForSelector('#sheet:not([hidden]) .go', { timeout: 6000 });
      const txt = await page.textContent('#sheet');
      check('model: choosing Opus shows the exact line "/model opus" and says it is saved as the default', /\/model opus/.test(txt) && /default for new sessions/.test(txt));
      await page.screenshot({ path: `${OUT}/m2-model-confirm.png` });
      await page.click('#sheet .btns button:not(.go)').catch(() => {}); await page.waitForTimeout(300);
      await page.click('#dmodel'); await page.waitForSelector('.mdl-b'); await page.click('.mdl-b[data-value=high]'); await page.waitForSelector('#sheet:not([hidden]) .go', { timeout: 6000 });
      check('model: choosing an effort shows "/effort high"', /\/effort high/.test(await page.textContent('#sheet')));
      await page.click('#sheet .btns button:not(.go)').catch(() => {}); await page.waitForTimeout(300);
      await page.click('#dback').catch(() => {}); await page.waitForTimeout(300);
    }
    check('model: the running model and effort are read off the screen (status line and banner)', await page.evaluate(() => { const t = document.getElementById('tail'), keep = t.textContent, c = window.shepr.currentModel; t.textContent = 'x Opus 5.5·xhigh y'; const a = c(); t.textContent = ' Haiku 5.5 with low effort · Claude Max'; const b = c(); t.textContent = 'nothing here'; const n = c(); t.textContent = keep; return a.model === 'opus' && a.effort === 'xhigh' && b.model === 'haiku' && b.effort === 'low' && n === null; }));
  }

  // ---- "Type something": your own words (the fixture pane is static, so the CURSOR SAFETY CHECK is what is exercised here)
  {
    const fsh = new URL('./fake-session.sh', import.meta.url).pathname;
    const hr = (...a) => execFileSync('herdr', a, { env: ENV, encoding: 'utf8', input: '' });
    execFileSync('bash', [fsh, 'free'], { env: ENV, encoding: 'utf8' });
    await page.waitForFunction(() => window.shepr.S.q.agents.some((a) => a.wlabel === 'freetext'), null, { timeout: 15000 }).catch(async () => { await page.click('#refresh'); await page.waitForTimeout(4500); });
    const key = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.wlabel === 'freetext'); return a.host + '|' + a.pane; });
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await tap(page, `.item[data-key="${key}"]`); await page.waitForSelector('#optrow button.free', { timeout: 8000 });
    const labels = await page.$$eval('#optrow button', (b) => b.map((x) => x.textContent.trim()));
    console.log('   typing-row buttons:', JSON.stringify(labels));
    check('type: the typing row is a pencil button, not a number that would press Enter and decline the question', labels.length === 4 && /^✎ 3$/.test(labels[2]) && labels[0] === '1' && labels[1] === '2');
    await page.screenshot({ path: `${OUT}/y1-type-button.png` });
    await page.click('#optrow button.free'); await page.waitForSelector('#sheet:not([hidden]) #typebox');
    check('type: it opens a sheet for your words, and Review stays off until there are some', await page.$eval('#sheet .btns .go', (b) => b.disabled));
    const WORDS = "What about 42, it's \"ok\" $HOME -- and `ls`?";
    await page.fill('#typebox', 'line one\nline two'); 
    check('type: a line break is flattened to a space (it would send the answer early)', (await page.evaluate(() => { const t = document.getElementById('typebox'); return t.value; })).includes('\n') && !(await page.$eval('#sheet .btns .go', (b) => b.disabled)));
    await page.fill('#typebox', WORDS);
    await page.screenshot({ path: `${OUT}/y2-type-sheet.png` });
    await page.click('#sheet .btns .go'); await page.waitForSelector('#sheet:not([hidden]) .kv');
    const kv = await page.textContent('#sheet .kv');
    console.log('   typed plan:', kv.replace(/\s+/g, ' ').trim().slice(0, 190));
    check('type: the confirm sheet shows the option, YOUR words exactly, and the keys', /3\. Type something\./.test(kv) && kv.includes(WORDS) && /↓ ↓ then your words, then ⏎/.test(kv));
    await page.screenshot({ path: `${OUT}/y3-type-confirm.png` });
    await page.waitForTimeout(800); await page.click('#sheet .btns .go');
    await page.waitForFunction(() => /did not land on the row|nothing was typed/.test(document.body.textContent), null, { timeout: 8000 });
    const pane = (JSON.parse(hr('pane', 'list')).result.panes.find((p) => p.cwd === (process.env.HOME + '/Projects/whats-next') && p.terminal_title_stripped !== undefined) || {}).pane_id;
    const shown = hr('pane', 'read', JSON.parse(hr('workspace', 'list')).result.workspaces.find((w) => w.label === 'freetext').workspace_id + ':p1', '--lines', '30');
    check('type: the pane never moved, so the host REFUSED to type: nothing was typed and no Enter was pressed', !shown.includes('What about 42'));
    // the multi-select fixture has a typing row too: a pencil that rides along with Submit
    execFileSync('bash', [fsh, 'free-rm'], { env: ENV, encoding: 'utf8' });
    execFileSync('bash', [fsh, 'multi'], { env: ENV, encoding: 'utf8' });
    await page.waitForFunction(() => window.shepr.S.q.agents.some((a) => a.wlabel === 'multi'), null, { timeout: 15000 }).catch(async () => { await page.click('#refresh'); await page.waitForTimeout(4500); });
    const mk2 = await page.evaluate(() => { const a = window.shepr.S.q.agents.find((x) => x.wlabel === 'multi'); return a.host + '|' + a.pane; });
    await page.click('#dback').catch(() => {}); await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(300);
    await tap(page, `.item[data-key="${mk2}"]`); await page.waitForSelector('#optrow .tog');
    const ml = await page.$$eval('#optrow button', (b) => b.map((x) => x.textContent.trim()));
    check('type: a tick-several question gets a pencil beside the ticks, before Submit', ml.length === 6 && ml[4] === '✎' && /^Submit/.test(ml[5]));
    await page.click('#optrow button.free'); await page.waitForSelector('#typebox'); await page.fill('#typebox', 'my own words'); await page.click('#sheet .btns .go');
    await page.waitForFunction(() => /✎ ✓/.test(document.getElementById('optrow').textContent), null, { timeout: 4000 });
    check('type: once written, the pencil shows a tick and nothing is sent yet', true);
    await page.click('#optrow .tog-go'); await page.waitForSelector('#sheet:not([hidden]) .kv');
    const mkv = await page.textContent('#sheet .kv');
    check('type: Submit shows your words on the confirm sheet along with the ticks and the keys', /Types/.test(mkv) && /my own words/.test(mkv) && /Submit/.test(mkv));
    await page.click('#sheet .btns button:first-child');
    execFileSync('bash', [fsh, 'multi-rm'], { env: ENV, encoding: 'utf8' });
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
  }

  // ---- an app newer than its server: say so plainly instead of a confusing pane error
  {
    const prep = (kind) => page.evaluate(async (k) => { const body = JSON.stringify({ host: window.shepr.S.hello.name, kind: k });
      const r = await fetch('api/send/prepare', { method: 'POST', body, headers: Object.assign({ 'Content-Type': 'application/json', 'X-Shepr': '1' }, await window.shepr.signHeaders('POST', 'api/send/prepare', body)) });
      return r.status + ' ' + (await r.text()); }, kind);
    const bogus = await prep('teleport');
    console.log('   unknown action:', bogus.slice(0, 150));
    check('skew: an action the server does not know says the server is older, not "not a pane id"', /^400 /.test(bogus) && /older than the app/.test(bogus) && !/pane id/.test(bogus));
    check('skew: the queue carries the server build, equal to the app build when they match', await page.evaluate(async () => { const q = await window.shepr.S.q; return !!q.server_build && q.server_build === q.build; }));
    await page.route('**/api/queue*', async (route) => { const res = await route.fetch(); const j = await res.json(); j.server_build = 'v0'; route.fulfill({ response: res, json: j }); });
    await page.evaluate(() => { window.shepr.S.skewShown = false; });
    await page.click('#refresh');
    check('skew: when the host runs an older Shepr than the app, the phone says so, once', await page.waitForFunction(() => /Restart Shepr on the host/.test(document.body.textContent), null, { timeout: 8000 }).then(() => true, () => false));
    await page.unroute('**/api/queue*');
  }

  // ---- swipe left / right in a session to move to the next / previous open project
  {
    // The fixtures removed just above can linger in the host's last poll for a few seconds. A swipe onto one is, correctly, a session
    // that is gone and ends the walk early, so wait until the list holds only real sessions.
    await page.click('#refresh');
    await page.waitForFunction(() => !window.shepr.S.q.agents.some((a) => ['multi', 'freetext', 'nomenu', 'queued', 'gone'].indexOf(a.wlabel) >= 0), null, { timeout: 15000 }).catch(() => {});
    await tap(page, '.tab[data-view=waiting]'); await page.waitForTimeout(400);
    const first = await page.evaluate(() => { const l = [...document.querySelectorAll('#main .item')][0]; return l.dataset.key; });
    await tap(page, `.item[data-key="${first}"]`);
    await page.waitForSelector('#detail:not([hidden])'); await page.waitForTimeout(600);
    const who = () => page.textContent('#dtitle');
    const swipe = (x0, y0, x1, y1, ms = 160, from = null) => page.evaluate(({ x0, y0, x1, y1, ms, from }) => new Promise((res) => {
      const tgt = from ? document.querySelector(from) : document.elementFromPoint(x0, y0);
      const ev = (type, x, y) => tgt.dispatchEvent(new PointerEvent(type, { pointerId: 7, pointerType: 'touch', isPrimary: true, clientX: x, clientY: y, bubbles: true, cancelable: true }));
      ev('pointerdown', x0, y0); setTimeout(() => { ev('pointermove', (x0 + x1) / 2, (y0 + y1) / 2); ev('pointerup', x1, y1); res(); }, ms);
    }), { x0, y0, x1, y1, ms, from });
    const t0 = await who();
    await swipe(300, 300, 80, 305);
    await page.waitForTimeout(500);
    const t1 = await who();
    check('swipe: a left swipe in a session goes to the next open project', t1 !== t0);
    await swipe(80, 300, 300, 295); await page.waitForTimeout(500);
    check('swipe: a right swipe comes back to the previous one', (await who()) === t0);
    await swipe(300, 300, 250, 302); await page.waitForTimeout(400);
    check('swipe: a short swipe does nothing', (await who()) === t0);
    await swipe(200, 200, 150, 480); await page.waitForTimeout(400);
    check('swipe: a mostly-vertical drag does nothing (it is scrolling)', (await who()) === t0);
    await swipe(300, 300, 80, 305, 1100); await page.waitForTimeout(400);
    check('swipe: a slow drag does nothing', (await who()) === t0);
    await swipe(300, 1500, 80, 1500, 160, '#msg'); await page.waitForTimeout(400);
    check('swipe: a swipe that starts in the message box does nothing', (await who()) === t0);
    await swipe(300, 300, 80, 305); await swipe(300, 300, 80, 305); await swipe(300, 300, 80, 305);
    for (let i = 0; i < 8; i++) { await swipe(300, 300, 80, 305); await page.waitForTimeout(250); }
    check('swipe: at the last project it stops and says so', /last open project/.test(await page.textContent('body')));
    await page.click('#dback').catch(() => {}); await page.waitForTimeout(400);
  }

  // ---- revocation signs the phone out
  cli('revoke', myId);
  await page.evaluate(() => window.shepr.S.q && fetch('api/queue').catch(() => 0));
  await page.evaluate(async () => { try { await window.fetch('api/queue', { headers: await window.shepr.signHeaders('GET', 'api/queue', '') }); } catch (e) {} });
  await tap(page, '#refresh').catch(() => {}); await page.click('#refresh');
  // A refresh is ignored while a poll is already in flight, and the next poll is up to 10 s away: keep asking until the phone notices.
  for (let k = 0; k < 14 && (await page.$eval('#pair', (n) => n.hidden)); k++) { await page.click('#refresh').catch(() => {}); await page.waitForTimeout(1000); }
  await page.waitForSelector('#pair:not([hidden])', { timeout: 3000 });
  check('revoked phone is thrown back to the pairing screen', true);
  const rv = await page.evaluate(async () => (await fetch('api/queue', { headers: await window.shepr.signHeaders('GET', 'api/queue', '') })).status);
  check('revoked key is refused by the host', rv === 401);
  await ctx.close();
}

// =============================================================== 2. pair by SCANNING (fake camera -> jsQR)
{
  const { url } = newCode('/tmp/claude-1000/qr-live.png');
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-loop', '1', '-i', '/tmp/claude-1000/qr-live.png', '-t', '3', '-r', '15',
    '-vf', 'scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2:white', '-pix_fmt', 'yuv420p', '/tmp/claude-1000/qr-live.y4m']);
  const b2 = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream', '--use-file-for-fake-video-capture=/tmp/claude-1000/qr-live.y4m'] });
  const ctx = await b2.newContext({ ...dev, serviceWorkers: 'block', permissions: ['camera'] });
  const page = await ctx.newPage(); watch(page);
  await page.goto(BASE + '/');
  await page.waitForSelector('#pair:not([hidden])', { timeout: 8000 });
  await page.click('#pscan');
  await page.waitForSelector('#pscanner:not([hidden])');
  await page.waitForTimeout(700); await page.screenshot({ path: `${OUT}/sc1-scanner.png` });
  await page.waitForSelector('#pform:not([hidden])', { timeout: 12000 });
  const filled = await page.inputValue('#pcodein');
  check('scanner decoded the QR and filled the code', url.endsWith(filled));
  await page.screenshot({ path: `${OUT}/sc2-scanned.png` });
  await page.click('#pgo');
  await page.waitForSelector('.item', { timeout: 10000 });
  check('paired by scanning, queue loaded', true);
  await ctx.close(); await b2.close();
}

// =============================================================== 3. wrong code is refused
{
  const ctx = await browser.newContext({ ...dev, serviceWorkers: 'block' });
  const page = await ctx.newPage(); watch(page);
  await page.goto(BASE + '/'); await page.waitForSelector('#pair:not([hidden])', { timeout: 8000 });
  await page.click('#pcode'); await page.fill('#pcodein', 'ZZZZZZZZZZZZ'); await page.click('#pgo');
  await page.waitForSelector('#perr:not([hidden])');
  check('wrong code is refused with a clear message', /wrong|expired|used/i.test(await page.textContent('#perr')));
  await ctx.close();
}

// =============================================================== 4. a second launch within 20 minutes keeps the splash short
{
  const ctx = await browser.newContext({ ...dev, serviceWorkers: 'block' });
  await ctx.addInitScript(() => { try { localStorage.setItem('shepr.splash', String(Date.now())); } catch (e) { /* ignore */ } });
  const page = await ctx.newPage(); watch(page);
  const t0 = Date.now();
  await page.goto(BASE + '/'); await page.waitForSelector('#splash');
  check('quick splash: it still has the badge', (await page.textContent('.smade')).replace(/\s/g, '') === 'MADEBY@nixfred');
  await page.waitForSelector('#splash', { state: 'detached', timeout: 9000 });
  const took = Date.now() - t0;
  console.log('   quick splash took', took, 'ms');
  check('quick splash: a second launch within 20 minutes is gone in under 2.5 s', took < 2500);
  await ctx.close();
}

await browser.close();
check('no page errors', errs.length === 0);
if (errs.length) console.log(errs.join('\n'));
const failed = ok.filter((x) => !x[1]);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASSED');
process.exit(failed.length ? 1 : 0);
