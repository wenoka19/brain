/* §4.1's hover actions on §7.4's cards.
 *
 * The slot and §4.7's menu were both built, styled, and instantiated by no
 * caller: every card was a bare row, so the only thing a transcript could do
 * was open.
 *
 * It also pins the bug that made the menus unusable: the trigger is a
 * `<button>` inside the card's own `<a>`, and `wireMenus` stopped the bubble
 * without cancelling the link — so opening the menu navigated to the
 * transcript and drew the menu onto a page already being replaced.
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8114';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({
    viewport: { width: 1280, height: 950 },
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
  await page.goto(BASE + '/next/library/transcripts', { waitUntil: 'networkidle' });
  await page.waitForSelector('.item-card');

  const card = page.locator('.item-card').first();
  out.restOpacity = await card.locator('.item-actions').evaluate(n => getComputedStyle(n).opacity);
  await card.hover();
  await page.waitForTimeout(250);
  out.hoverOpacity = await card.locator('.item-actions').evaluate(n => getComputedStyle(n).opacity);

  // §7.4's three controls, and the four menu items.
  out.inlineActions = await card.locator('.item-actions > [data-act]').evaluateAll(
    ns => ns.map(n => n.dataset.act));
  await card.locator('[data-menu-for]').click();
  await page.waitForTimeout(150);
  out.menuItems = (await card.locator('.menu-item').allTextContents()).map(t => t.trim());
  await page.keyboard.press('Escape');

  // Copy must not navigate, and must reach the clipboard.
  const urlBefore = page.url();
  await card.hover();
  await card.locator('[data-act="copy"]').click();
  await page.waitForTimeout(250);
  out.stayedOnTheList = page.url() === urlBefore;
  out.clipboard = (await page.evaluate(() => navigator.clipboard.readText())).slice(0, 36);

  // Rename, in place.
  await card.hover();
  await card.locator('[data-menu-for]').click();
  await page.waitForTimeout(120);
  await card.locator('[data-act="rename"]').click();
  await page.waitForTimeout(150);
  out.renameFieldShown = await card.locator('.item-rename').count() === 1;
  await card.locator('.item-rename').fill('Renamed from the card');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(400);
  out.titleAfterRename = (await card.locator('.item-title').textContent()).trim();
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.item-card');
  out.titlePersisted = (await page.locator('.item-card .item-title').first().textContent()).trim();

  // Sort flips both the groups and the rows.
  out.sortLabelBefore = (await page.locator('[data-sort]').textContent()).trim();
  const titlesBefore = await page.locator('.item-title').allTextContents();
  await page.click('[data-sort]');
  await page.waitForTimeout(200);
  out.sortLabelAfter = (await page.locator('[data-sort]').textContent()).trim();
  const titlesAfter = await page.locator('.item-title').allTextContents();
  out.sortReversed = JSON.stringify(titlesAfter) === JSON.stringify([...titlesBefore].reverse());

  // Archive removes the row and offers Undo.
  const countBefore = await page.locator('.item-card').count();
  const first = page.locator('.item-card').first();
  await first.hover();
  await first.locator('[data-menu-for]').click();
  await page.waitForTimeout(120);
  await first.locator('[data-act="archive"]').click();
  await page.waitForSelector('.toast');
  out.archiveToast = (await page.locator('.toast').textContent()).trim().split('\n')[0];
  out.rowsAfterArchive = await page.locator('.item-card').count();
  out.archivedOne = out.rowsAfterArchive === countBefore - 1;

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
