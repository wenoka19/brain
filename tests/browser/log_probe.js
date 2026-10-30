/* §7.10's call log, with real MCP traffic behind it.
 *
 * Make the calls first, with a key in the URL:
 *
 *     curl -X POST "http://127.0.0.1:8141/mcp?key=sk-brain-…" …
 *
 * It checks the row §7.10's capture draws — time · connection · tool ·
 * target · latency · status, 40px, the tool in mono on the canvas — the
 * four-part expansion, the link from a row to the connection that made it,
 * and §12's "720–959 | log rows drop the connection and latency columns".
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8141';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);
  await page.goto(BASE + '/next/activity', { waitUntil: 'networkidle' });
  await page.waitForSelector('.call');

  out.meters = await page.locator('.meter-figure').allTextContents();
  out.calls = await page.locator('.call').count();
  out.rows = await page.locator('.call-head').evaluateAll(
    ns => ns.map(n => n.innerText.replace(/\n/g, ' | ')));
  out.rowHeight = await page.locator('.call-head').first().evaluate(
    n => Math.round(n.getBoundingClientRect().height));
  out.toolIsMono = await page.locator('.call-tool').first().evaluate(
    n => getComputedStyle(n).fontFamily.toLowerCase().includes('mono'));
  out.toolOnCanvas = await page.evaluate(() => {
    const bg = getComputedStyle(document.querySelector('.call-tool')).backgroundColor;
    const canvas = getComputedStyle(document.documentElement).getPropertyValue('--bg-canvas').trim();
    return { bg, canvas };
  });

  // A failed row, and what it tells the person to do.
  const failed = page.locator('.call', { has: page.locator('.pill', { hasText: 'Failed' }) }).first();
  out.failedCount = await page.locator('.pill', { hasText: 'Failed' }).count();
  if (out.failedCount) {
    await failed.locator('.call-head').click();
    await page.waitForTimeout(150);
    out.failedExpanded = {
      labels: await failed.locator('.field-label').allTextContents(),
      askedFor: (await failed.locator('.call-pre').textContent()).trim(),
      returned: (await failed.locator('.call-returned').textContent()).trim(),
      chips: (await failed.locator('.chip').allTextContents()).map(t => t.trim()),
    };
  }

  // A good row expands to the four things §7.10 names.
  const ok = page.locator('.call', { has: page.locator('.pill', { hasText: 'OK' }) }).first();
  await ok.locator('.call-head').click();
  await page.waitForTimeout(150);
  out.okExpanded = {
    labels: await ok.locator('.field-label').allTextContents(),
    returned: (await ok.locator('.call-returned').textContent()).trim(),
    chips: (await ok.locator('.chip').allTextContents()).map(t => t.trim()),
    connectionChipHref: await ok.locator('.chip[href]').getAttribute('href'),
  };

  // §12: at 900px the connection and latency columns go.
  await page.setViewportSize({ width: 900, height: 900 });
  await page.waitForTimeout(200);
  out.narrow = {
    connHidden: !(await page.locator('.call-conn').first().isVisible()),
    latencyHidden: !(await page.locator('.call-latency').first().isVisible()),
    metersOneColumn: await page.locator('.meter-row').evaluate(
      n => getComputedStyle(n).gridTemplateColumns.split(' ').length === 1),
  };
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
