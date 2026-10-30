/* `R-19`: the app's own browser audit, replacing the two Playwright suites
 * that drove `index.html` and went with it.
 *
 * This one PRINTS and the pytest wrapper decides. Keeping the expectations
 * in Python and the measurements here is what the two deleted suites did,
 * and it is why `mobile_audit.js` could report a 577px overflow without
 * anyone having to encode "577 is too wide" in JavaScript.
 *
 * Three things, over every route the server actually claims:
 *
 *   1. The page renders at all — no uncaught error, and content in #content.
 *      The cutover shipped a frames.js that failed to parse once already;
 *      every Python test passed and no page loaded.
 *   2. It does not scroll sideways at 390px, naming the offending element
 *      when it does. A phone that scrolls sideways is the clearest possible
 *      signal that a layout was only ever looked at on a laptop.
 *   3. Stored text reaches the DOM as text. A malicious transcript is the
 *      attack CLAUDE.md §2.1 is written against, and a payload that got
 *      through would run with the viewer's session cookie in hand.
 */
const { chromium } = require('playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8000';
const PHONE = { width: 390, height: 844 };
const DESK = { width: 1280, height: 900 };

// The payload: if any of it is ever parsed as markup rather than shown as
// text, `window.__xss` is set and the page has a stored-XSS hole.
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';

const ROUTES = [
  '/', '/library/transcripts', '/library/summaries', '/library/sources',
  '/rules', '/skills', '/activity', '/connections', '/archive', '/profile',
  '/signin',
];

async function measure(page, path) {
  const errors = [];
  const onError = (e) => errors.push(String(e.message || e));
  page.on('pageerror', onError);
  const response = await page.goto(BASE + path, { waitUntil: 'networkidle' });
  await page.waitForTimeout(250);

  const seen = await page.evaluate(() => {
    const doc = document.documentElement;
    const offenders = [];
    // Name the element, not just the number: "scrollWidth 577" sends someone
    // hunting, "the .toolbar is 577 wide" is a fix.
    // The folded sidebar is parked off-canvas on purpose until the menu
    // button opens it, so it is not an overflow — only what the document
    // actually scrolls to is.
    const parked = document.querySelector('.sidebar[data-open="false"], .sidebar:not([data-open])');
    for (const node of document.querySelectorAll('body *')) {
      if (parked && (node === parked || parked.contains(node))) continue;
      const box = node.getBoundingClientRect();
      if (box.width === 0 || box.height === 0) continue;
      if (box.right > doc.clientWidth + 1 || box.left < -1) {
        const name = node.tagName.toLowerCase() +
          (node.className && typeof node.className === 'string'
            ? '.' + node.className.trim().split(/\s+/).join('.')
            : '');
        if (!offenders.includes(name)) offenders.push(name);
      }
    }
    return {
      scrollWidth: doc.scrollWidth,
      clientWidth: doc.clientWidth,
      offenders: offenders.slice(0, 6),
      content: (document.getElementById('content')?.innerText || '').trim().length,
      xss: Boolean(window.__xss),
    };
  });

  page.off('pageerror', onError);
  return { path, status: response ? response.status() : 0, errors, ...seen };
}

(async () => {
  const out = { base: BASE, phone: [PHONE.width, PHONE.height], routes: [], errors: [] };
  let browser;
  try {
    browser = await chromium.launch({ args: ['--no-sandbox'] });

    // --- sign in and plant the payload, on a desktop viewport -------------
    const deskCtx = await browser.newContext({ viewport: DESK });
    const desk = await deskCtx.newPage();
    desk.on('pageerror', (e) => out.errors.push('signin: ' + e.message));
    await desk.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await desk.evaluate(async (base) => {
      await fetch(base + '/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: 'audit@example.com' }),
      });
    }, BASE);

    const csrf = await desk.evaluate(() =>
      document.cookie.split('; ').find((c) => c.startsWith('brain_csrf='))?.split('=')[1]);
    out.plantedPayload = await desk.evaluate(async ([base, token, payload]) => {
      const res = await fetch(base + '/api/rule', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Brain-Request': '1',
          'X-Brain-CSRF': token,
        },
        body: JSON.stringify({
          claim: payload + ' always measure the baseline before changing anything',
          when_it_applies: payload,
        }),
      });
      return res.ok;
    }, [BASE, csrf, XSS]);
    const storage = await deskCtx.storageState();
    await deskCtx.close();

    // --- every route, at phone width --------------------------------------
    const phoneCtx = await browser.newContext({ viewport: PHONE, storageState: storage });
    const page = await phoneCtx.newPage();
    for (const path of ROUTES) {
      out.routes.push(await measure(page, path));
    }
    await phoneCtx.close();

    // --- and the desktop width, where the sidebar is shown ----------------
    const wideCtx = await browser.newContext({ viewport: DESK, storageState: storage });
    const wide = await wideCtx.newPage();
    out.wide = [];
    for (const path of ROUTES) {
      out.wide.push(await measure(wide, path));
    }
    await wideCtx.close();
  } catch (error) {
    out.fatal = String(error && error.stack ? error.stack : error);
  } finally {
    if (browser) await browser.close();
  }
  console.log(JSON.stringify(out, null, 2));
})();
