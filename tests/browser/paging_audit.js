/* `R-20`: the capped list, in a real browser.
 *
 * This probe PRINTS and the pytest wrapper decides, the same split as
 * `app_audit.js` — the measurements belong here, the expectations belong in
 * Python, and that is what lets a number like "found 0 cards" be reported
 * without anyone encoding the verdict in JavaScript.
 *
 * It answers three questions the Python tests cannot:
 *
 *   1. Does the footer actually appear, and does it say the real total?
 *   2. Does "Show 200 more" load more, in place, without losing the reader's
 *      scroll position?
 *   3. The bug itself: search for a word that exists ONLY beyond the cap.
 *      Before this change the page answered "Nothing here matches" about a
 *      rule the library demonstrably held. That is the regression this file
 *      exists to catch, so the token is chosen by diffing the capped list
 *      against the whole one rather than by assuming an ordering.
 */
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8000';
const EMAIL = process.env.EMAIL || 'founder@example.com';
const PHONE = { width: 390, height: 844 };

const cards = (page) => page.locator('.exp-card').count();

(async () => {
  const out = {};
  let browser;
  try {
    browser = await chromium.launch({ args: ['--no-sandbox'] });
    const ctx = await browser.newContext({ viewport: PHONE });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e.message || e)));

    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.evaluate(
      async ([base, email]) => {
        await fetch(base + '/api/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email }),
        });
      },
      [BASE, EMAIL]
    );

    // --- pick a token that the capped list genuinely does not contain ------
    const probe = await page.evaluate(async (base) => {
      const grab = async (url) => {
        const r = await fetch(url, { headers: { 'X-Brain-Request': '1' } });
        const j = await r.json();
        return [...(j.mine || []), ...(j.from_your_sources || [])];
      };
      const capped = await grab(base + '/api/rules');
      const whole = await grab(base + '/api/rules?limit=100000');
      const seen = new Set(capped.map((r) => r.id));
      const beyond = whole.filter((r) => !seen.has(r.id));
      const token = (beyond[0]?.claim || '').match(/zqx\d+/)?.[0] || '';
      return { capped: capped.length, whole: whole.length, beyond: beyond.length, token };
    }, BASE);
    Object.assign(out, probe);

    // --- the footer -------------------------------------------------------
    await page.goto(BASE + '/rules', { waitUntil: 'networkidle' });
    await page.waitForTimeout(300);
    out.firstPaintCards = await cards(page);
    out.footText = ((await page.locator('.list-foot').first().textContent()) || '').trim();
    out.hasButton = (await page.locator('.list-foot [data-act="more"]').count()) > 0;
    out.buttonText = out.hasButton
      ? ((await page.locator('.list-foot [data-act="more"]').first().textContent()) || '').trim()
      : '';

    // --- show more, twice, because the button is meant to repeat ----------
    //
    // Scroll is read AFTER `scrollIntoViewIfNeeded` and not before: Playwright
    // scrolls an element into view as part of clicking it, so a reading taken
    // earlier measures Playwright's scroll and not the page's. Getting that
    // wrong the first time reported a 1200 -> 14452 jump against code that
    // holds position exactly.
    if (out.hasButton) {
      const more = () => page.locator('.list-foot [data-act="more"]').first();
      await more().scrollIntoViewIfNeeded();
      await page.waitForTimeout(150);
      out.scrollBefore = await page.evaluate(() => window.scrollY);
      await more().click();
      await page.waitForTimeout(1500);
      out.afterMoreCards = await cards(page);
      out.scrollAfter = await page.evaluate(() => window.scrollY);
      out.footAfterOne = ((await page.locator('.list-foot').first().textContent().catch(() => '')) || '').trim();

      // Second press. The footer is gone once the list is whole, which is
      // the point — a page that says "Showing 460 of 460" is noise.
      if ((await page.locator('.list-foot [data-act="more"]').count()) > 0) {
        await more().scrollIntoViewIfNeeded();
        await more().click();
        await page.waitForTimeout(1500);
        out.afterTwoCards = await cards(page);
      }
      out.footsWhenWhole = await page.locator('.list-foot').count();
    }

    // --- THE BUG: search for something only beyond the cap ----------------
    if (probe.token) {
      await page.goto(BASE + '/rules', { waitUntil: 'networkidle' });
      await page.waitForTimeout(300);
      const field = page.locator('.search .input').first();
      await field.fill(probe.token);
      // The frame may have to finish loading the list before it can answer.
      await page.waitForTimeout(2500);
      out.searchVisibleCards = await page.evaluate(
        () => [...document.querySelectorAll('.exp-card')].filter((c) => !c.hidden).length
      );
      out.searchNote = (
        (await page.locator('.search-note').first().textContent().catch(() => '')) || ''
      ).trim();
    }

    out.errors = errors;
    await ctx.close();
  } catch (error) {
    out.fatal = String(error && error.stack ? error.stack : error);
  } finally {
    if (browser) await browser.close();
  }
  console.log(JSON.stringify(out, null, 2));
})();
