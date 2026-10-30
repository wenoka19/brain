/* §7.9's card and §7.8's two actions.
 *
 * A proposed skill had no `Review` button, which §7.9 calls the queue's entry
 * point; the `⋯` had no handler, so Activate, Deactivate, Export .md and
 * Delete were all unreachable; §7.8's Edit and Archive were plain buttons
 * with no handler; and `?create=1` was a parameter no frame read.
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8117';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/401/.test(m.text())) out.errors.push(m.text()); });
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);

  // ---- §7.9 -------------------------------------------------------------
  await page.goto(BASE + '/next/skills', { waitUntil: 'networkidle' });
  await page.waitForSelector('.exp-card');
  out.sections = await page.locator('.group-title').allTextContents();
  out.notes = await page.locator('.group-note').allTextContents();

  const proposed = page.locator('.exp-card[data-state="proposed"]');
  // §7.9: the Review button is visible on the COLLAPSED card.
  out.reviewVisibleClosed = await proposed.locator('[data-act="review"]').isVisible();
  out.proposedOpenAtRest = await proposed.getAttribute('data-open');

  const approved = page.locator('.exp-card:not([data-state])').first();
  out.approvedHeadActions = await approved.locator('.exp-actions').evaluate(
    n => n.innerHTML.includes('data-menu-for'));
  out.approvedHasPencil = await approved.locator('.exp-actions [data-act="edit"]').count();
  // R-7: ⋯ alone, and it only shows once open.
  out.approvedActionsHiddenClosed = !(await approved.locator('.exp-actions').isVisible());

  // Review opens the card and lands on Approve.
  await proposed.locator('[data-act="review"]').click();
  await page.waitForTimeout(200);
  out.reviewOpened = await proposed.getAttribute('data-open');
  out.reviewButtons = (await proposed.locator('.btn-row .btn').allTextContents()).map(t => t.trim());
  out.provenanceChips = (await proposed.locator('.chip').allTextContents()).map(t => t.trim());

  // The approved card's menu.
  await approved.locator('.exp-head').click();
  await page.waitForTimeout(150);
  await approved.locator('[data-menu-for]').click();
  await page.waitForTimeout(150);
  out.menuItems = (await approved.locator('.menu-item').allTextContents()).map(t => t.trim());
  out.stillOnSkills = page.url().endsWith('/skills');

  // Export .md downloads a real file.
  const dl = page.waitForEvent('download');
  await approved.locator('[data-act="export"]').click();
  const file = await dl;
  out.exportName = file.suggestedFilename();
  const fs = require('fs');
  const path = await file.path();
  out.exportHead = fs.readFileSync(path, 'utf8').split('\n').slice(0, 7);

  // Deactivate moves it. The card stays OPEN — a collapsed card shows the
  // caret alone (§18.6), so its menu is deliberately out of reach.
  const name = (await approved.locator('.item-title').textContent()).trim();
  await approved.locator('[data-menu-for]').click();
  await page.waitForTimeout(120);
  await approved.locator('[data-act="deactivate"]').click();
  await page.waitForTimeout(900);
  out.afterDeactivate = await page.evaluate((n) => {
    const card = [...document.querySelectorAll('.exp-card')]
      .find(c => c.querySelector('.item-title').textContent.trim() === n);
    return { state: card?.dataset.state, section: card?.closest('.section')?.querySelector('.group-title')?.textContent };
  }, name);

  // ---- §7.8 -------------------------------------------------------------
  await page.goto(BASE + '/next/rules', { waitUntil: 'networkidle' });
  await page.waitForSelector('.exp-card');
  const rule = page.locator('.exp-card').first();
  await rule.locator('.exp-head').click();
  await page.waitForTimeout(150);
  await rule.locator('[data-act="edit"]').click();
  await page.waitForTimeout(150);
  out.ruleEditShown = await rule.locator('.rule-edit').count() === 1;
  await rule.locator('.rule-edit textarea').fill('Never use em dashes, ever.');
  await rule.locator('.rule-edit [data-save]').click();
  await page.waitForTimeout(500);
  out.ruleTitleAfterEdit = (await rule.locator('.item-title').textContent()).trim();
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.exp-card');
  out.rulePersisted = (await page.locator('.item-title').first().textContent()).trim();

  // ---- ?create=1 --------------------------------------------------------
  await page.goto(BASE + '/next/rules?create=1', { waitUntil: 'networkidle' });
  await page.waitForTimeout(300);
  out.createPanel = await page.locator('.create-panel .group-title').textContent().catch(() => null);
  out.createFields = await page.locator('.create-field .field-label').allTextContents();

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
