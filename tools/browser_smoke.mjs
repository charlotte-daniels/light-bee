// Headless browser smoke test of the whole page against tools/dev_server.py.
// Never shipped. Usage: node tools/browser_smoke.mjs http://127.0.0.1:8770 [out-dir]
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const base = process.argv[2] || 'http://127.0.0.1:8770';
const out = process.argv[3] || '.';
mkdirSync(out, { recursive: true });
const failures = [];
const check = (ok, what) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`); if (!ok) failures.push(what); };

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, acceptDownloads: true });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text().slice(0, 160)); });
await page.addInitScript(() => {
  window.__violations = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__violations.push(e.violatedDirective));
});

await page.goto(base + '/');
await page.getByLabel('UDL username').fill('analyst.one');
await page.getByLabel('UDL password').fill('wrong');
await page.getByRole('button', { name: 'Sign in' }).click();
await page.waitForFunction(() => document.getElementById('sign-in-error').textContent.length > 0);
check((await page.textContent('#sign-in-error')).includes('did not accept'), 'wrong password is refused with a message');
await page.getByLabel('UDL password').fill('not-real-distinctive-pw-7');
await page.getByRole('button', { name: 'Sign in' }).click();
await page.waitForSelector('#app-panel:not([hidden])');
check(true, 'sign-in shows the run form');

check(await page.inputValue('#change-date') === '2026-11-10', 'the change date defaults to the server\u2019s today');
await page.getByLabel('NORAD ID').fill('0');
await page.getByRole('button', { name: 'Run' }).click();
check(await page.getAttribute('#sat-no', 'aria-invalid') === 'true', 'a bad NORAD ID marks its field and says why');
await page.getByLabel('NORAD ID').fill('43874');
const started = Date.now();
await page.getByRole('button', { name: 'Run' }).click();
await page.waitForFunction(() => document.getElementById('progress').textContent.startsWith('Done'), null, { timeout: 120000 });
console.log(`      run finished in ${((Date.now() - started) / 1000).toFixed(1)} s (fake UDL)`);
await page.waitForSelector('#flag:not([hidden])');
check((await page.textContent('#flag-headline')).length > 0, 'the seasonal flag is shown with a headline');
check(await page.locator('#plot-seasonal .heatmaplayer image, #plot-seasonal .hm image').count() > 0, 'the heatmap is drawn');
const label = await page.getAttribute('#plot-seasonal', 'aria-label');
check(label.startsWith('Seasonal Behaviour? for NORAD 43874'), 'the seasonal figure has a text alternative');
const marking = await page.textContent('#marking-top');
check(marking.includes('PR-MRKT'), `the page marking follows the data (${marking})`);
await page.screenshot({ path: `${out}/seasonal.png`, fullPage: true });

const seasonalDownload = page.waitForEvent('download');
await page.getByRole('button', { name: 'Download PNG' }).first().click();
const seasonalFile = await seasonalDownload;
check(seasonalFile.suggestedFilename() === 'lightbee-43874-seasonal-2026-11-10.png', `seasonal export named ${seasonalFile.suggestedFilename()}`);
await seasonalFile.saveAs(`${out}/export-seasonal.png`);

await page.getByRole('tab', { name: 'Other Causes' }).focus();
await page.keyboard.press('ArrowRight');
check(await page.getByRole('tab', { name: 'Seasonal Behaviour?' }).getAttribute('aria-selected') === 'true', 'arrow keys move between tabs');
await page.getByRole('tab', { name: 'Other Causes' }).click();
await page.waitForSelector('#plot-causes .main-svg');
await page.screenshot({ path: `${out}/causes.png`, fullPage: true });

// Ghost years: off by default, switched on from the keyboard, named in the title and summary.
const ghostLegend = async () => page.locator('#plot-causes .legendtext', { hasText: 'year ago' }).count()
  + await page.locator('#plot-causes .legendtext', { hasText: 'years ago' }).count();
check(!(await page.isChecked('#ghost-years')), 'ghost years are off by default');
check(await ghostLegend() === 0, 'no ghost-year traces are drawn while off');
check((await page.textContent('#plot-causes')).includes('Light curve over SEPA'), 'the light curve panel is titled without ghost years');
await page.focus('#ghost-years');
await page.keyboard.press('Space');
await page.waitForFunction(() => document.querySelector('#plot-causes').textContent.includes('ago beneath'));
check(await page.isChecked('#ghost-years'), 'the ghost years checkbox works from the keyboard');
check(await ghostLegend() === 2, 'both ghost years appear in the legend ("1 year ago", "2 years ago")');
check((await page.textContent('#causes-summary')).includes('1 year ago'), 'the text summary names the ghost years');
await page.screenshot({ path: `${out}/causes-ghosts.png`, fullPage: true });
const ghostFile = page.waitForEvent('download');
await page.click('#export-causes');
await (await ghostFile).saveAs(`${out}/export-causes-ghosts.png`);
check(true, 'the Other Causes export runs with ghost years on');

// Drag a box across the VisMag v Time panel (the second subplot).
const target = page.locator('#plot-causes .nsewdrag[data-subplot="x2y2"]');
await target.scrollIntoViewIfNeeded();
const box = await target.boundingBox();
await page.mouse.move(box.x + box.width * 0.70, box.y + 5);
await page.mouse.down();
await page.mouse.move(box.x + box.width * 0.86, box.y + box.height - 5, { steps: 12 });
await page.mouse.up();
await page.waitForSelector('#scan-dialog[open]', { timeout: 30000 }).catch(async () => {
  console.log('      progress:', await page.textContent('#progress'));
  console.log('      selected:', await page.evaluate(() => document.querySelectorAll('#plot-causes .selected').length));
  throw new Error('the scan dialog did not open');
});
const rows = await page.locator('#scan-rows tr').count();
check(rows > 0, `the scan lists neighbours (${rows})`);
check((await page.locator('#scan-rows tr td').first().textContent()) === '40001', 'the 100 km neighbour is first');
await page.screenshot({ path: `${out}/scan.png` });
const csv = page.waitForEvent('download');
await page.getByRole('button', { name: 'Download CSV' }).click();
check((await csv).suggestedFilename().endsWith('.csv'), 'the scan downloads as CSV');
await page.getByRole('button', { name: 'Close' }).click();

// The keyboard scan's preset range finds observations on first use.
await page.getByRole('button', { name: 'Scan this range' }).focus();
await page.keyboard.press('Enter');
await page.waitForSelector('#scan-dialog[open]', { timeout: 30000 });
check(true, 'the preset keyboard scan range contains observations');
await page.keyboard.press('Escape');

// A reversed range is refused against its field.
await page.getByLabel('Scan to (UTC)').fill('2026-11-01T00:00');
await page.getByRole('button', { name: 'Scan this range' }).click();
check((await page.getAttribute('#scan-to', 'aria-invalid')) === 'true', 'a scan range error marks its field');

// The same scan from the keyboard alone.
await page.getByLabel('Scan from (UTC)').fill('2026-11-09T00:00');
await page.getByLabel('Scan to (UTC)').fill('2026-11-09T23:59');
await page.getByRole('button', { name: 'Scan this range' }).focus();
await page.keyboard.press('Enter');
await page.waitForSelector('#scan-dialog[open]', { timeout: 30000 });
check((await page.locator('#scan-rows tr td').first().textContent()) === '40001', 'the keyboard range scan finds the neighbour');
await page.keyboard.press('Escape');
check((await page.textContent('#causes-summary')).includes('NORAD 40001'), 'Other Causes has a text summary naming the nearest object');

await page.getByLabel('Absolute').check();
await page.waitForTimeout(1500);
check((await page.textContent('#plot-causes')).includes('raw magnitude'), 'the absolute toggle redraws Other Causes with raw magnitude');
check(!(await page.textContent('#plot-seasonal')).includes('range-normalised'), 'and redraws Seasonal Behaviour? on raw magnitude');
check(await ghostLegend() === 2, 'ghost years stay on across the magnitude toggle');
await page.uncheck('#ghost-years');
await page.waitForFunction(() => !document.querySelector('#plot-causes').textContent.includes('ago beneath'));
check(await ghostLegend() === 0, 'switching ghost years off removes them');

const cdp = await context.newCDPSession(page);
await cdp.send('Performance.enable');
const memory = await cdp.send('Performance.getMetrics');
const heap = memory.metrics.find((m) => m.name === 'JSHeapUsedSize').value / 1e6;
check(heap < 500, `browser JS heap ${heap.toFixed(0)} MB (target under 500 MB)`);
const violations = await page.evaluate(() => window.__violations);
const scripts = violations.filter((v) => v.startsWith('script'));
check(scripts.length === 0, `no script blocked by the content security policy (${violations.length} style attributes blocked)`);
const unexpected = errors.filter((e) => !e.includes('Content Security Policy') && !e.includes('status of 401'));
check(unexpected.length === 0, `no page errors beyond the deliberate wrong password ${JSON.stringify(unexpected.slice(0, 3))}`);

await page.getByRole('button', { name: 'Sign out' }).click();
await page.waitForSelector('#sign-in-panel:not([hidden])');
check(true, 'sign-out returns to the sign-in form');
await browser.close();
console.log(failures.length ? `${failures.length} FAILED` : 'SMOKE: PASS');
process.exit(failures.length ? 1 : 0);
