/* R-18 rendered: the Activity meters + spend list, the Profile switch, and
 * the source page's "Delete the file now" with its dialog. */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8149';
const SHOT = '/tmp/';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  let b;
  const out = { errors: [] };
  try {
  b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));

  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);

  // Ingest a URL and an upload so there is real spend to show.
  const csrf = await page.evaluate(() =>
    document.cookie.split('; ').find(c => c.startsWith('brain_csrf='))?.split('=')[1]);
  await page.evaluate(async ([u, t]) => {
    await fetch(u + '/api/ingest', { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Brain-Request': '1', 'X-Brain-CSRF': t },
      body: JSON.stringify({ url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' }) });
    const fd = new FormData();
    fd.append('file', new File(
      ['We always measure the baseline before changing anything at all. '.repeat(30)],
      'quarterly plan.txt', { type: 'text/plain' }));
    await fetch(u + '/api/upload', { method: 'POST',
      headers: { 'X-Brain-Request': '1', 'X-Brain-CSRF': t }, body: fd });
  }, [BASE, csrf]);
  await sleep(6000);

  // --- Activity ---
  await page.goto(BASE + '/next/activity', { waitUntil: 'networkidle' });
  await page.waitForSelector('.meter-card');
  out.meters = await page.locator('.meter-label').allTextContents();
  out.figures = await page.locator('.meter-figure').allTextContents();
  out.notes = await page.locator('.meter-note').allTextContents();
  out.spendHeading = await page.locator('.section-title').allTextContents();
  out.spend = await page.locator('.section:has-text("Where your credits went") .setting-row')
    .evaluateAll(ns => ns.map(n => n.innerText.replace(/\n/g, ' | ')));
  await page.screenshot({ path: SHOT + 'credits_activity.png', fullPage: false });

  // --- Profile ---
  await page.goto(BASE + '/next/profile', { waitUntil: 'networkidle' });
  await page.waitForSelector('.setting-row');
  out.keepRow = await page.locator('.setting-row:has-text("Keep the file")').innerText()
    .catch(() => 'MISSING');
  out.dangerButtons = await page.locator('[data-variant="danger"]').count();
  const yourData = page.locator('.section:has-text("Your data")');
  await yourData.scrollIntoViewIfNeeded();
  await page.screenshot({ path: SHOT + 'credits_profile.png',
    clip: await yourData.evaluate(n => { const r = n.getBoundingClientRect();
      return { x: r.x, y: Math.max(0, r.y), width: r.width, height: Math.min(r.height, 420) }; }) });

  // --- a source page with a held file ---
  await page.goto(BASE + '/next/library/sources', { waitUntil: 'networkidle' });
  const link = page.locator('a:has-text("quarterly plan")').first();
  out.foundUpload = await link.count() > 0;
  if (out.foundUpload) {
    await link.click();
    await page.waitForSelector('.card-menu-trigger, [data-menu]', { timeout: 5000 }).catch(()=>{});
    await sleep(800);
    await page.locator('[data-menu-for]').first().click();
    await sleep(400);
    out.menu = await page.locator('.menu:not([hidden])').first().innerText().catch(() => 'NO MENU');
    const del = page.locator('text=Delete the file now').first();
    out.hasDeleteFile = await del.count() > 0;
    if (out.hasDeleteFile) {
      await del.click();
      await page.waitForSelector('.dialog', { timeout: 4000 });
      out.dialog = await page.locator('.dialog').innerText();
      await page.screenshot({ path: SHOT + 'credits_dialog.png' });
      await page.locator('[data-act="go"]').click();
      await sleep(1500);
      out.afterDelete = await page.locator('text=Delete the file now').count();
    }
  }
  } catch (e) { out.probeError = String(e).split('\n')[0]; }
  console.log(JSON.stringify(out, null, 2));
  if (b) await b.close();
})();
