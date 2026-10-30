/* §7.12 and §9.4 in a browser.
 *
 *     BRAIN_ENV=test BRAIN_DATA_DIR=/tmp/r BRAIN_NEXT_UI=1 \
 *       python3 -m uvicorn app.main:app --port 8112
 *     node tests/browser/archive_probe.js
 *
 * Archive had no `Restore` at all — the page whose whole job is undoing an
 * archive could not undo one — and not one destructive control in the app
 * confirmed, although §9.4's dialog molecule was fully built in CSS.
 *
 * It checks: the caret is the last thing in the row (§18.6), a collapsed card
 * shows it alone, `Restore` removes the row and offers Undo, Undo puts it
 * back, the menu lists Restore and Delete permanently, the dialog opens,
 * Escape means no and deletes nothing, and going through it removes exactly
 * one row.
 */
const { chromium } = require('playwright');
const BASE = process.env.BRAIN_URL || 'http://127.0.0.1:8112';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
  page.on('console', m => { if (m.type() === 'error') out.errors.push(m.text()); });
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);

  await page.goto(BASE + '/next/archive', { waitUntil: 'networkidle' });
  await page.waitForSelector('.exp-card');
  out.rows = await page.locator('.exp-card').count();
  out.kinds = await page.locator('[data-kind]').allTextContents();

  // §18.6: the caret is the last thing in the row.
  out.caretIsLast = await page.$eval('.exp-top', (n) =>
    n.lastElementChild.classList.contains('exp-caret-btn'));
  out.caretOnRight = await page.evaluate(() => {
    const top = document.querySelector('.exp-top').getBoundingClientRect();
    const caret = document.querySelector('.exp-caret-btn').getBoundingClientRect();
    return caret.left > top.left + top.width / 2;
  });
  // Collapsed shows the caret alone.
  out.actionsHiddenClosed = !(await page.locator('.exp-card .exp-actions').first().isVisible());

  // Open one, and Restore it.
  const first = page.locator('.exp-card').first();
  await first.locator('.exp-head').click();
  await page.waitForTimeout(150);
  out.actionsShownOpen = await first.locator('.exp-actions').isVisible();
  out.restoreLabel = (await first.locator('[data-restore]').first().textContent()).trim();

  const before = out.rows;
  await first.locator('[data-restore]').first().click();
  await page.waitForSelector('.toast');
  out.toast = (await page.locator('.toast').textContent()).trim();
  out.rowsAfterRestore = await page.locator('.exp-card').count();
  out.restoredOne = out.rowsAfterRestore === before - 1;

  // Undo puts it back.
  await page.locator('.toast button').click();
  await page.waitForTimeout(700);
  await page.waitForSelector('.exp-card');
  out.rowsAfterUndo = await page.locator('.exp-card').count();

  // Delete permanently: the menu, then the dialog.
  const card = page.locator('.exp-card').first();
  await card.locator('.exp-head').click();
  await page.waitForTimeout(120);
  await card.locator('[data-menu-for]').click();
  await page.waitForTimeout(120);
  out.menuItems = (await card.locator('.menu-item').allTextContents()).map(t => t.trim());
  await card.locator('[data-delete]').click();
  await page.waitForSelector('.scrim');
  out.dialog = {
    title: (await page.locator('.dialog-title').textContent()).trim(),
    hasSteps: await page.locator('.scrim .steps').count(),
    confirmVariant: await page.getAttribute('.scrim [data-act="go"]', 'data-variant'),
  };
  // Escape means no, and nothing is deleted.
  await page.keyboard.press('Escape');
  await page.waitForTimeout(150);
  out.dialogClosedOnEscape = await page.locator('.scrim').count() === 0;
  out.rowsAfterCancel = await page.locator('.exp-card').count();

  // Now go through with it.
  await card.locator('[data-menu-for]').click();
  await page.waitForTimeout(100);
  await card.locator('[data-delete]').click();
  await page.waitForSelector('.scrim');
  await page.click('.scrim [data-act="go"]');
  await page.waitForTimeout(400);
  out.rowsAfterDelete = await page.locator('.exp-card').count();
  out.deleteToast = await page.locator('.toast').count()
    ? (await page.locator('.toast').textContent()).trim() : '';

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
