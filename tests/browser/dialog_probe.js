/* §9.4's two-step dialog, on §7.13's two danger buttons.
 *
 * It stops before the last confirm: the point is the gates, not the deletion.
 *
 * This probe is why `[hidden] { display: none !important }` exists. `hidden`
 * is a statement of fact and any explicit `display` silently beats it — so
 * `.btn[hidden]` did nothing, and at step 1 both `Continue` and the final
 * `Delete account` were on screen at once, the second merely disabled.
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8112';
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 950 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);
  await page.goto(BASE + '/next/profile', { waitUntil: 'networkidle' });
  await page.waitForSelector('[data-wipe]');

  out.buttons = await page.locator('[data-wipe]').allTextContents();
  await page.click('[data-wipe="account"]');
  await page.waitForSelector('.scrim');

  out.step1 = {
    title: (await page.locator('.dialog-title').textContent()).trim(),
    body: (await page.locator('.dialog-body').textContent()).trim().slice(0, 60) + '…',
    dot1: await page.getAttribute('[data-step="1"]', 'data-state'),
    dot2: await page.getAttribute('[data-step="2"]', 'data-state'),
    dangerWord: (await page.locator('.danger-word').textContent()).trim(),
    wordColour: await page.$eval('.danger-word', n => getComputedStyle(n).color),
    dangerToken: await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--danger').trim()),
    continueDisabled: await page.locator('[data-act="next"]').isDisabled(),
    continueVariant: await page.getAttribute('[data-act="next"]', 'data-variant'),
    finalHidden: await page.locator('[data-act="go"]').isHidden(),
  };

  // The wrong word keeps it shut.
  await page.fill('#confirm-word', 'delete');
  out.wrongWordStillDisabled = await page.locator('[data-act="next"]').isDisabled();
  await page.fill('#confirm-word', 'DELETE');
  out.rightWordEnables = !(await page.locator('[data-act="next"]').isDisabled());

  await page.click('[data-act="next"]');
  await page.waitForTimeout(150);
  out.step2 = {
    dot1: await page.getAttribute('[data-step="1"]', 'data-state'),
    dot2: await page.getAttribute('[data-step="2"]', 'data-state'),
    wordPaneHidden: await page.locator('[data-pane="1"]').isHidden(),
    identityShown: await page.locator('#confirm-identity').isVisible(),
    continueGone: await page.locator('[data-act="next"]').isHidden(),
    finalVariant: await page.getAttribute('[data-act="go"]', 'data-variant'),
    finalDisabled: await page.locator('[data-act="go"]').isDisabled(),
  };
  await page.fill('#confirm-identity', 'x');
  out.step2.enabledByIdentity = !(await page.locator('[data-act="go"]').isDisabled());

  // Cancel: the account survives.
  await page.click('[data-act="cancel"]');
  await page.waitForTimeout(200);
  out.closed = (await page.locator('.scrim').count()) === 0;
  out.stillSignedIn = (await page.evaluate((u) =>
    fetch(u + '/api/counts').then(r => r.status), BASE)) === 200;

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
