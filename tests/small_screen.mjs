// The smallest phones (320x568, an iPhone SE 1st gen) and the keyboard: the page never scrolls (Law 17), so every button
// must be on screen or inside a box that scrolls in place. Two screens failed that: the pair form (its Pair button was
// below the fold) and the "Skip and send" sheet with the keyboard up (both buttons below the visible area).
//   tests/fake-session.sh up ; eval "$(tests/fake-session.sh env)"; SHEPR_URL=http://localhost:8743 python3 server.py 8743 &
//   node tests/small_screen.mjs
import { createRequire } from 'module';
import { execFileSync } from 'child_process';
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium } = pw;

const BASE = 'http://localhost:8743';
const SERVER = new URL('../server.py', import.meta.url).pathname;
const ENV = { ...process.env, SHEPR_STATE: process.env.SHEPR_STATE || '/tmp/claude-1000/shepr-state', SHEPR_URL: BASE };
const newCode = () => JSON.parse(execFileSync('python3', [SERVER, 'pair', '--no-wait', '--json'], { env: ENV, encoding: 'utf8' })).code;
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const ok = [];
const check = (name, cond) => { ok.push([name, !!cond]); console.log((cond ? 'PASS ' : 'FAIL ') + name); };
const onScreen = (page, sel) => page.$eval(sel, (n) => { const r = n.getBoundingClientRect();
  return r.height > 0 && r.top >= -1 && r.bottom <= innerHeight + 1 && document.scrollingElement.scrollTop === 0 && document.documentElement.scrollHeight <= innerHeight + 1; });

const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
try {
  // ---- 1. the pair form, by typed code, at every phone size we support
  for (const [w, h] of [[320, 568], [375, 667], [390, 844]]) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, userAgent: UA, serviceWorkers: 'block' });
    const page = await ctx.newPage();
    await page.goto(BASE + '/');
    await page.waitForSelector('#splash', { state: 'detached', timeout: 9000 });
    await page.click('#pcode');
    await page.waitForSelector('#pform:not([hidden])');
    check(`${w}x${h}: the name field and the Pair button are on screen, and the page does not scroll`, await onScreen(page, '#pname') && await onScreen(page, '#pgo'));
    await page.$eval('#perr', (e) => { e.hidden = false; e.textContent = 'That code did not work. Check it and try again, or make a new one on the host.'; });
    await page.$eval('#pair', (p) => { p.scrollTop = p.scrollHeight; });
    check(`${w}x${h}: with an error line too, the Pair button can be scrolled into view inside the pair box`, await onScreen(page, '#pgo'));
    await ctx.close();
  }

  // ---- 2. "Skip and send" with the keyboard up: what iOS leaves of a 320x568 screen above its keyboard is about 315px
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, userAgent: UA, serviceWorkers: 'block' });
  const page = await ctx.newPage();
  await page.goto(BASE + '/#shepr-pair=' + newCode());
  await page.waitForSelector('#pform:not([hidden])', { timeout: 9000 });
  await page.click('#pgo');
  await page.waitForSelector('.item', { timeout: 9000 });
  await page.click('.item');                                   // the queue puts a blocked numbered question first
  await page.waitForSelector('#msg');
  await page.fill('#msg', 'use staging for this one, not prod, and keep the cache warm while you are at it please');
  await page.setViewportSize({ width: 320, height: 315 });
  await page.waitForTimeout(250);
  // Known and separate: below ~383px the session view (header 132 + nav 45 + tail 24 + dock 182) pushes the compose bar
  // itself under the bottom edge, so a finger cannot reach Send here. Press it from script; this test is about the sheet.
  await page.$eval('#send', (b) => b.click());
  await page.waitForSelector('.skipsheet', { timeout: 6000 });
  await page.waitForTimeout(400);                              // the sheet slides up
  check('keyboard up on a 320x568 phone: "Answer it first" is on screen without scrolling', await onScreen(page, '#sheet .btns button:not(.go)'));
  check('keyboard up on a 320x568 phone: "Skip and send…" is on screen without scrolling', await onScreen(page, '#sheet .btns .go'));
  check('the words in that sheet still all reach the screen: the middle scrolls in its own box', await page.$eval('.skipmid', (m) => m.scrollHeight >= m.clientHeight && getComputedStyle(m).overflowY === 'auto'));
  await ctx.close();
} finally { await browser.close(); }
const bad = ok.filter((x) => !x[1]);
console.log(bad.length ? bad.length + ' FAILED' : 'ALL PASS');
process.exit(bad.length ? 1 : 0);
