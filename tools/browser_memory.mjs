// Browser memory and timing on a dense two-year object, against tools/dev_server.py --dense-history (two years at 1,500 a night).
// Never shipped. Usage: node tools/browser_memory.mjs http://127.0.0.1:8798
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const base = process.argv[2] || 'http://127.0.0.1:8798';
const LIMIT_MB = 500;

// The largest resident memory of any Chromium renderer process, in MB.
function rendererMb() {
  const ps = execFileSync('ps', ['-eo', 'rss=,args='], { encoding: 'utf8' });
  const sizes = ps.split('\n').filter((line) => line.includes('--type=renderer'))
    .map((line) => Number(line.trim().split(/\s+/)[0]) / 1024);
  return sizes.length ? Math.max(...sizes) : NaN;
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await context.newPage();
const cdp = await context.newCDPSession(page);
await cdp.send('Performance.enable');
const heapMb = async () => (await cdp.send('Performance.getMetrics')).metrics
  .find((m) => m.name === 'JSHeapUsedSize').value / 1e6;

await page.goto(base + '/');
await page.getByLabel('UDL username').fill('analyst.one');
await page.getByLabel('UDL password').fill('not-real-distinctive-pw-7');
await page.getByRole('button', { name: 'Sign in' }).click();
await page.waitForSelector('#app-panel:not([hidden])');

const results = {};
for (const [label, days] of [['cold', '7'], ['warm', '3']]) {
  await page.getByLabel('NORAD ID').fill('43874');
  await page.getByLabel('Window (days)').fill(days);
  const started = Date.now();
  await page.getByRole('button', { name: 'Run' }).click();
  await page.waitForSelector('#flag:not([hidden])', { timeout: 600000 });
  const flagAt = (Date.now() - started) / 1000;
  await page.waitForFunction(() => document.getElementById('progress').textContent.startsWith('Done'), null, { timeout: 600000 });
  const progress = await page.textContent('#progress');
  results[label] = { flag_shown_s: flagAt, all_drawn_s: (Date.now() - started) / 1000, progress };
  await page.getByRole('tab', { name: 'Other Causes' }).click();
  await page.waitForSelector('#plot-causes .main-svg');
  await page.getByRole('tab', { name: 'Seasonal Behaviour?' }).click();
}
await page.getByLabel('Absolute').check();
await page.waitForTimeout(3000);
await page.getByRole('tab', { name: 'Other Causes' }).click();
await page.waitForTimeout(2000);
// Before garbage collection: the transient high-water mark, reported only.
results.renderer_before_gc_mb = Math.round(rendererMb());
// After collection: what the page settles at, judged against the target.
await cdp.send('HeapProfiler.collectGarbage');
await page.waitForTimeout(1000);
results.heap_mb = Math.round(await heapMb());
results.renderer_mb = Math.round(rendererMb());
results.svg_nodes = (await cdp.send('Performance.getMetrics')).metrics.find((m) => m.name === 'Nodes').value;
console.log(JSON.stringify(results, null, 2));
await browser.close();
const ok = results.renderer_mb < LIMIT_MB && results.heap_mb < LIMIT_MB;
console.log(ok ? 'MEMORY: PASS' : 'MEMORY: FAIL');
process.exit(ok ? 0 : 1);
