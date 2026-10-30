/* §7.7's three states, the `Fix this` tooltip, and the retry.
 *
 * §7.7's second and third states were not rendered at all: the data was in
 * the payload and the frame threw it away, so a queued source and a blocked
 * one looked identical to one sitting happily in memory. `Fix this` did not
 * exist, and once it did it could only state the remedy — there was no
 * operation behind it until `op_reprocess_source`.
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8116';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 950 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/401/.test(m.text())) out.errors.push(m.text()); });
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);
  await page.goto(BASE + '/next/library/sources', { waitUntil: 'networkidle' });
  await page.waitForSelector('.item-card');

  out.rows = await page.locator('.item-card').count();
  out.pills = await page.locator('.item-card .pill').allTextContents();
  out.metaOfFirst = (await page.locator('.item-meta').first().innerText()).replace(/\n/g, ' · ');

  const blocked = page.locator('.item-card', { has: page.locator('.pill', { hasText: 'Needs you' }) });
  out.blockedFound = await blocked.count();
  out.fixTooltip = await blocked.locator('[data-act="fix"]').getAttribute('title');
  out.fixLabel = (await blocked.locator('[data-act="fix"]').textContent()).trim();

  // The menu on a healthy row.
  const healthy = page.locator('.item-card').filter({ hasNot: page.locator('.pill') }).first();
  await healthy.hover();
  await healthy.locator('[data-menu-for]').click();
  await page.waitForTimeout(150);
  out.menuItems = (await healthy.locator('.menu-item').allTextContents()).map(t => t.trim());
  out.stillOnList = page.url().endsWith('/library/sources');
  await page.keyboard.press('Escape');

  // Fix this retries: the pill turns to Processing and the button goes.
  // The row is pinned by element handle first — the retry changes the very
  // pill text the filter matched on, so a re-evaluated locator finds nothing.
  const row = await blocked.elementHandle();
  await blocked.hover();
  await blocked.locator('[data-act="fix"]').click();
  await page.waitForSelector('.toast');
  out.retryToast = (await page.locator('.toast').textContent()).trim().split('\n')[0];
  out.pillAfterRetry = await row.$eval('.pill', n => n.textContent.trim());
  out.fixGone = (await row.$$('[data-act="fix"]')).length === 0;

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
