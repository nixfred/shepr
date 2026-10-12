// Text rendering check: feeds the renderer sample terminal output and screenshots it. Needs a server on :8743.
import { createRequire } from 'module';
// Playwright is a global install, not an npm dependency (nixweb rule). Look where a stranger would have it.
const pw = [process.env.PW_PATH, (process.env.HOME || '') + '/.local/share/mise/installs/npm-playwright/latest/node_modules/', '/usr/local/lib/node_modules/', '/usr/lib/node_modules/']
  .filter(Boolean).map((d) => { try { return createRequire(d)('playwright'); } catch { return null; } }).find(Boolean);
if (!pw) { console.error('playwright not found: npm i -g playwright, or set PW_PATH to its node_modules directory'); process.exit(1); }
const { chromium, devices } = pw;
const SAMPLE = `● I checked the three open PRs and merged the two that were green.

  Findings:
  - #34 passed on both Pythons (1488 tests), merged at 4aef18b with a clean squash commit message
  - #35 had one failing check in the packaging job, so I left it open and commented with the log excerpt
  1. Read the failing job output and found the missing build dependency in the recipe
  2. Re-ran after pinning it; green on both architectures

⏺ Bash(git -C ~/Projects/burnbar log --oneline -3)
  ⎿  a3ae047 feat: Shepr, the iPhone app that herds your herdr agents
     83630c1 fix: pairing QR carried https://127.0.0.1:8742 instead of the tailnet name
     30e1b22 test: make the pairing-URL regression check standalone

  const url = new URL(path, location.href);   // signs the exact path

Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for git commands in /home/pi/Projects/burnbar
  3. No, and tell Claude what to do differently (esc)
────────────────────────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────────────────────────
  nixfredOS | LARRY:gus | Fable 5.1 · xhigh | ctx ▪▪▪▪▪▪▪▪▫▫▫▫ 37% 371k/1M | ⏱ 1d | cache 95% 372761 tokens
  ▸ ~/Projects/burnbar | ⌥ master | +0 −0 | ⛨ hooks 41/41
  currentVersion: 2.1.293
  ◎ 3,938 sessions · 6,086 decisions · 1,305 learnings | ⌕ recalled 19m ago | ✿ 5.9
  Update available! Run: mise upgrade claude
  ⏳ week 66% · +1% banked ≈1h ahead | ↻Sun | 5h 16%
  /rc
  ATL 22:49 ☁ 68° · tmrw ↑73° ↓60° ☂ 98% | ▣ ollama gemma3 8.8/12G | load 6.75 | 🔇
  ▸▸ bypass permissions on (shift+tab to cycle) · ← 1 agent`;
const b = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] });
const ctx = await b.newContext({ ...devices['iPhone 14'], serviceWorkers: 'block' });
const p = await ctx.newPage();
await p.goto('http://localhost:8743/');
await p.waitForFunction(() => window.shepr && window.shepr.renderBlocks);
await p.evaluate((txt) => {
  document.getElementById('splash')?.remove(); document.getElementById('pair').hidden = true;
  const d = document.getElementById('detail'); d.hidden = false; d.style.animation = 'none';
  document.getElementById('dtitle').textContent = 'burnbar'; document.getElementById('dsub').textContent = 'PR Hunter burnbar';
  document.getElementById('dstate').textContent = 'IDLE'; document.getElementById('dstate').className = 'state idle';
  document.getElementById('dpos').textContent = '12/13';
  window.shepr.renderBlocks(document.getElementById('tail'), txt);
  document.getElementById('tail').scrollTop = 0;
}, SAMPLE);
await p.waitForTimeout(400);
await p.screenshot({ path: '/tmp/claude-1000/shots/f1-format-top.png' });
await p.evaluate(() => { const t = document.getElementById('tail'); t.scrollTop = t.scrollHeight; });
await p.waitForTimeout(200);
await p.screenshot({ path: '/tmp/claude-1000/shots/f2-format-bottom.png' });
const vis = () => p.evaluate(() => [...document.querySelectorAll('#tail .chips span')].filter((n) => getComputedStyle(n).display !== 'none' && !n.classList.contains('more')).map((n) => n.textContent));
const compact = await vis();
console.log('compact status:', JSON.stringify(compact));
const stats = await p.evaluate(() => ({ blocks: document.querySelectorAll('#tail .tl').length, rules: document.querySelectorAll('#tail .tl.rule').length,
  noHorizontalScroll: document.getElementById('tail').scrollWidth <= document.getElementById('tail').clientWidth + 1 }));
console.log(JSON.stringify(stats));
const has = (re) => compact.some((c) => re.test(c));
await p.screenshot({ path: '/tmp/claude-1000/shots/f3-compact.png' });
await p.click('#tail .chips .more'); await p.waitForTimeout(200);
const all = await vis();
await p.evaluate(() => { const t = document.getElementById('tail'); t.scrollTop = t.scrollHeight; });
await p.screenshot({ path: '/tmp/claude-1000/shots/f4-expanded.png' });
const ok = has(/fable|xhigh/i) && has(/banked/i) && has(/^ctx/i) && compact.length >= 4 && compact.length <= 7 && all.length >= 12 && stats.rules === 1 && stats.noHorizontalScroll;
console.log(ok ? `PASS compact strip (${compact.length} chips: model, context, banked...) expands to all ${all.length}; empty prompt box is one rule; no sideways scroll` : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
