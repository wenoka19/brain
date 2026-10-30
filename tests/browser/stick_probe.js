/* The sticky read bar, on a page tall enough to have a top to leave.
 *
 * §7.5: "The bar gains its bottom border only once stuck." The fixture
 * transcript fits one viewport, so the first probe could not tell a working
 * observer from a broken one — it padded nothing and scrolled nowhere. This
 * one gives the page room, then watches the border arrive and leave.
 */
const { chromium } = require('playwright');
const BASE = process.env.BRAIN_URL || 'http://127.0.0.1:8111';
(async () => {
  const out = {};
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 600 } })).newPage();
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);

  await page.goto(BASE + '/next/library/transcripts', { waitUntil: 'networkidle' });
  await page.click('.item-card');
  await page.waitForSelector('.read-bar');
  // Give the page room to scroll without touching the frame's own markup.
  await page.evaluate(() => {
    const pad = document.createElement('div');
    pad.style.height = '2000px';
    document.querySelector('.transcript-body').after(pad);
  });
  await page.waitForTimeout(100);
  out.room = await page.evaluate(() => document.documentElement.scrollHeight - innerHeight);
  out.atTop = await page.getAttribute('.read-bar', 'data-stuck');
  out.borderAtTop = await page.$eval('.read-bar', n => getComputedStyle(n).borderBottomWidth);
  await page.evaluate(() => window.scrollTo(0, 800));
  await page.waitForTimeout(300);
  out.scrolled = await page.getAttribute('.read-bar', 'data-stuck');
  out.borderScrolled = await page.$eval('.read-bar', n => getComputedStyle(n).borderBottomWidth);
  out.barStillAtTop = await page.$eval('.read-bar', n => Math.round(n.getBoundingClientRect().top));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(300);
  out.backAtTop = await page.getAttribute('.read-bar', 'data-stuck');

  // The summary's read-time arithmetic, with the real numbers behind it.
  await page.goto(BASE + '/next/library/summaries', { waitUntil: 'networkidle' });
  await page.click('.item-card');
  await page.waitForSelector('.segmented');
  out.words = await page.evaluate(() => {
    const count = (s) => s.trim().split(/\s+/).filter(Boolean).length;
    return {
      gist: count(document.querySelector('.gist').textContent),
      points: [...document.querySelectorAll('.point-text')].reduce((n, p) => n + count(p.textContent), 0),
      chapters: [...document.querySelectorAll('.chapter-text')].reduce((n, p) => n + count(p.textContent), 0),
    };
  });
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
