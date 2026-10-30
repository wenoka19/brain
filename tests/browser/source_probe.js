/* §7.3: the Summary card's two controls, the header menu, and the rename.
 *
 * Three of the five actions §7.3 names did not exist: the Summary card drew
 * the gist with no controls at all, and the frame had no overflow menu.
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8118';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({
    viewport: { width: 1280, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await ctx.newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/401/.test(m.text())) out.errors.push(m.text()); });
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);

  const id = await page.evaluate((u) =>
    fetch(u + '/api/sources').then(r => r.json()).then(d => d.sources[0].id), BASE);
  await page.goto(`${BASE}/next/sources/${id}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.source-head');

  out.pill = (await page.locator('.source-head .pill').textContent()).trim();
  out.elapsed = (await page.locator('.work-head span').first().textContent()).trim();
  out.steps = await page.locator('.work-steps li').count();
  out.hint = (await page.locator('.work-hint').textContent()).trim().slice(0, 48);

  // The summary card's two controls, hidden at rest.
  const summaryCard = page.locator('.yield-card').first();
  out.actionsAtRest = await summaryCard.locator('.yield-actions').evaluate(
    n => getComputedStyle(n).opacity);
  await summaryCard.hover();
  await page.waitForTimeout(250);
  out.actionsOnHover = await summaryCard.locator('.yield-actions').evaluate(
    n => getComputedStyle(n).opacity);

  await summaryCard.locator('[data-act="copy-summary"]').click();
  await page.waitForTimeout(250);
  out.clipboard = (await page.evaluate(() => navigator.clipboard.readText())).slice(0, 50);

  const dl = page.waitForEvent('download');
  await summaryCard.locator('[data-act="download-summary"]').click();
  const file = await dl;
  out.downloadName = file.suggestedFilename();
  out.downloadHead = require('fs').readFileSync(await file.path(), 'utf8').split('\n').slice(0, 5);

  // §7.3's overflow menu.
  await page.locator('.source-head [data-menu-for]').click();
  await page.waitForTimeout(150);
  out.menuItems = (await page.locator('.source-head .menu-item').allTextContents()).map(t => t.trim());
  out.menuOnRight = await page.evaluate(() => {
    const head = document.querySelector('.source-head').getBoundingClientRect();
    const menu = document.querySelector('.source-head .menu-wrap').getBoundingClientRect();
    return menu.left > head.left + head.width / 2;
  });
  await page.keyboard.press('Escape');

  // Rename from the header.
  await page.locator('.source-head [data-menu-for]').click();
  await page.waitForTimeout(120);
  await page.locator('[data-act="rename"]').click();
  await page.waitForTimeout(150);
  out.renameField = await page.locator('.title-rename').count();
  await page.locator('.title-rename').fill('Renamed from the header');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(500);
  out.titleAfter = (await page.locator('.read-title').textContent()).trim();

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
