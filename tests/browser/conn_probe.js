/* §7.10 Activity and §7.11 Connections — the two frames of §9.2's last rows.
 *
 *     BRAIN_ENV=test BRAIN_DATA_DIR=/tmp/r BRAIN_NEXT_UI=1 \
 *       python3 -m uvicorn app.main:app --port 8140
 *     node tests/browser/conn_probe.js
 *
 * Expects an EMPTY workspace: §7.11's empty state is the thing being checked,
 * and it is the one §7.11 calls "the most important empty state in the app".
 *
 * This probe is why `.meter { display: block }` exists. The atom was written
 * before anything used it, and a `<span>` with no display ignores width and
 * height — so every meter rendered as a tall thin bar down its card.
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8140';
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

  // ---- §7.11, the empty state --------------------------------------------
  await page.goto(BASE + '/next/connections', { waitUntil: 'networkidle' });
  await page.waitForSelector('.connect-card');
  out.empty = {
    state: await page.getAttribute('.connect-card', 'data-state'),
    pill: (await page.locator('.connect-card .pill').textContent()).trim(),
    title: (await page.locator('.connect-title').textContent()).trim(),
    accentBorder: await page.$eval('.connect-card', n => getComputedStyle(n).boxShadow),
    accentToken: await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()),
    connectedSectionRendered: (await page.locator('.conn-list').count()) > 0,
    placeholder: await page.getAttribute('#link', 'placeholder'),
    tiles: await page.locator('.how .item-title').allTextContents(),
    markPlaceholder: (await page.locator('#agent-block mark').textContent()).trim(),
  };

  // §6 rule 11, verbatim from §7.11.
  await page.click('#copy-link');
  await page.waitForTimeout(150);
  out.copyBeforeGenerate = (await page.locator('#copy-link').textContent()).trim();
  await page.waitForTimeout(2100);
  out.copyRestores = (await page.locator('#copy-link').textContent()).trim();

  // Generate.
  await page.click('#generate');
  await page.waitForSelector('.toast');
  out.generated = {
    link: (await page.inputValue('#link')).slice(0, 34) + '…',
    hasKey: (await page.inputValue('#link')).includes('/mcp?key=sk-brain-'),
    buttonNow: (await page.locator('#generate').textContent()).trim(),
    markGone: (await page.locator('#agent-block mark').count()) === 0,
    agentHasLink: (await page.locator('#agent-block').textContent()).includes('/mcp?key='),
  };
  await page.click('#copy-link');
  await page.waitForTimeout(200);
  out.generated.clipboard = (await page.evaluate(() => navigator.clipboard.readText())).slice(0, 30) + '…';

  // The tiles open.
  await page.locator('.how .exp-head').first().click();
  await page.waitForTimeout(200);
  out.tileOpens = await page.locator('.how').first().getAttribute('data-open');
  out.aiSteps = await page.locator('.how .step-title').allTextContents();

  // ---- §7.11, the connected list -----------------------------------------
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.conn');
  out.connected = {
    rows: await page.locator('.conn').count(),
    name: (await page.locator('.conn-name').first().textContent()).trim(),
    meta: (await page.locator('.conn .item-meta').first().innerText()).replace(/\n/g, ' · '),
    pill: (await page.locator('.conn .pill').first().textContent()).trim(),
    cardState: await page.getAttribute('.connect-card', 'data-state'),
    // The key is gone after a reload — it existed once, in one response.
    linkAfterReload: await page.inputValue('#link'),
  };
  await page.locator('.conn [data-menu-for]').first().click();
  await page.waitForTimeout(150);
  out.connected.menu = (await page.locator('.conn .menu-item').allTextContents()).map(t => t.trim());
  await page.keyboard.press('Escape');

  // ---- §7.10 --------------------------------------------------------------
  await page.goto(BASE + '/next/activity', { waitUntil: 'networkidle' });
  await page.waitForSelector('.meter-row');
  out.activity = {
    meters: await page.locator('.meter-label').allTextContents(),
    figures: await page.locator('.meter-figure').allTextContents(),
    bars: await page.locator('.meter').count(),
    storageNote: (await page.locator('.meter-card').last().locator('.meter-note').textContent()).trim(),
    calls: await page.locator('.call').count(),
  };
  if (out.activity.calls) {
    const first = page.locator('.call').first();
    out.activity.row = (await first.locator('.call-head').innerText()).replace(/\n/g, ' | ');
    out.activity.rowHeight = await first.locator('.call-head').evaluate(
      n => Math.round(n.getBoundingClientRect().height));
    await first.locator('.call-head').click();
    await page.waitForTimeout(150);
    out.activity.expanded = await first.getAttribute('data-open');
    out.activity.expandedLabels = await first.locator('.field-label').allTextContents();
  }
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
