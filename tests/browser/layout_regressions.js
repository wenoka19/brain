/* The five layout defects the interface audit found, measured so they cannot
 * come back. Same contract as `app_audit.js`: this PRINTS and the pytest
 * wrapper decides.
 *
 * Each number here is one that was wrong in the shipped app:
 *
 *   1. `.steps-list > li` is a `26px 1fr` grid whose counter is a `::before`,
 *      so auto-placement put every second real child in the 26px track. On
 *      `/connections` the paths, the notes and the `pre` rendered one word
 *      per line with the Copy button stretched above them.
 *   2. `.shell` is `min-height: 100svh` with two auto rows, and the default
 *      stretching align-content split the slack between them: the phone's
 *      top bar measured 261px on `/rules` against the 49px it takes on a
 *      page long enough to fill the screen.
 *   3. `.conn-list` carried `overflow: hidden` for its rounded corners and
 *      clipped the row's own menu by 35px — exactly where `Revoke` sits.
 *   4. The 132px source thumbnail never shrinks, so on a phone the title had
 *      162px and four lines.
 *   5. With the meta on its own row the status pill landed alone in a `1fr`
 *      track and stretched to a 270px bar instead of reading `Live`.
 */
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8000';
const PHONE = { width: 390, height: 844 };
const DESK = { width: 1280, height: 900 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function signIn(page) {
  await page.goto(BASE + '/signin', { waitUntil: 'networkidle' });
  await page.locator('input[type=email]').first().fill('layout@example.com');
  await page.keyboard.press('Enter');
  await sleep(1500);
  await page.evaluate(async (base) => {
    const csrf = () =>
      decodeURIComponent(
        document.cookie.split('; ').find((c) => c.startsWith('brain_csrf='))?.split('=')[1] || ''
      );
    const headers = {
      'Content-Type': 'application/json',
      'X-Brain-Request': '1',
      'X-Brain-CSRF': csrf(),
    };
    await fetch(base + '/api/demo/seed', { method: 'POST', headers, body: '{}' });
    await fetch(base + '/api/connections', {
      method: 'POST',
      headers,
      body: JSON.stringify({ name: 'My AI' }),
    });
  }, BASE);
  await sleep(600);
}

(async () => {
  const out = {};
  let browser;
  try {
    browser = await chromium.launch({ args: ['--no-sandbox'] });

    // ---- the sign-in page, with no session at all ------------------------
    // The shell asked for the theme and the counts before anyone had signed
    // in, so the one page a signed-out person ever sees logged three 401s on
    // every load — enough noise to bury a real error.
    const coldCtx = await browser.newContext({ viewport: DESK });
    const cold = await coldCtx.newPage();
    const refused = [];
    cold.on('response', (r) => {
      if (r.status() >= 400) refused.push(r.status() + ' ' + r.url().replace(BASE, ''));
    });
    await cold.goto(BASE + '/signin', { waitUntil: 'networkidle' });
    await sleep(900);
    out.signinRefused = refused;
    out.signinRenders = await cold.evaluate(
      () => document.querySelectorAll('input[type=email]').length > 0
    );
    await coldCtx.close();

    // ---- desktop ---------------------------------------------------------
    const deskCtx = await browser.newContext({ viewport: DESK });
    const desk = await deskCtx.newPage();
    await signIn(desk);
    const storage = await deskCtx.storageState();

    await desk.goto(BASE + '/connections', { waitUntil: 'networkidle' });
    await sleep(500);
    for (const which of ['ai', 'agent']) {
      await desk.locator(`[data-how="${which}"]`).first().click().catch(() => {});
      await sleep(300);
    }
    out.steps = await desk.evaluate(() =>
      Array.from(document.querySelectorAll('.steps-list > li')).map((li) => ({
        kids: Array.from(li.children).map((kid) => ({
          cls: String(kid.className || kid.tagName),
          width: Math.round(kid.getBoundingClientRect().width),
          column: getComputedStyle(kid).gridColumnStart,
        })),
      }))
    );

    await desk.goto(BASE + '/connections', { waitUntil: 'networkidle' });
    await sleep(600);
    await desk.locator('.conn .menu-wrap button').first().click().catch(() => {});
    await sleep(400);
    out.connMenu = await desk.evaluate(() => {
      const menu = document.querySelector('.conn .menu:not([hidden])');
      if (!menu) return { found: false };
      const box = menu.getBoundingClientRect();
      let node = menu.parentElement;
      while (node && node !== document.body) {
        const style = getComputedStyle(node);
        if (style.overflow !== 'visible' || style.overflowY !== 'visible') {
          const outer = node.getBoundingClientRect();
          if (box.bottom > outer.bottom + 1) {
            return {
              found: true,
              clippedBy: String(node.className),
              hiddenPx: Math.round(box.bottom - outer.bottom),
            };
          }
        }
        node = node.parentElement;
      }
      return {
        found: true,
        clippedBy: null,
        items: Array.from(menu.querySelectorAll('.menu-item')).map((i) => i.innerText.trim()),
      };
    });
    await deskCtx.close();

    // ---- phone -----------------------------------------------------------
    const phoneCtx = await browser.newContext({ viewport: PHONE, storageState: storage });
    const phone = await phoneCtx.newPage();

    out.topbar = {};
    for (const path of ['/', '/rules', '/profile', '/connections', '/library/transcripts',
                        '/activity', '/archive']) {
      await phone.goto(BASE + path, { waitUntil: 'networkidle' });
      await sleep(400);
      out.topbar[path] = await phone.evaluate(() => {
        const bar = document.querySelector('.topbar');
        return bar ? Math.round(bar.getBoundingClientRect().height) : null;
      });
    }

    const source = await phone.evaluate(async (base) => {
      const body = await (
        await fetch(base + '/api/sources', { headers: { 'X-Brain-Request': '1' } })
      ).json();
      return body.sources && body.sources[0] ? '/sources/' + body.sources[0].id : '';
    }, BASE);
    if (source) {
      await phone.goto(BASE + source, { waitUntil: 'networkidle' });
      await sleep(600);
      out.sourceTitle = await phone.evaluate(() => {
        const title = document.querySelector('.read-title');
        if (!title) return null;
        const box = title.getBoundingClientRect();
        const leading = parseFloat(getComputedStyle(title).lineHeight) || 24;
        return { width: Math.round(box.width), lines: Math.round(box.height / leading) };
      });
    }

    await phone.goto(BASE + '/connections', { waitUntil: 'networkidle' });
    await sleep(600);
    out.pill = await phone.evaluate(() => {
      const pill = document.querySelector('.conn .pill');
      return pill
        ? { width: Math.round(pill.getBoundingClientRect().width), text: pill.innerText.trim() }
        : null;
    });
    await phoneCtx.close();
  } catch (error) {
    out.fatal = String(error && error.stack ? error.stack : error);
  } finally {
    if (browser) await browser.close();
  }
  console.log(JSON.stringify(out, null, 2));
})();
