/* Phase 7 mobile audit: iPhone portrait (390x844) across every /preview layout
 * and every edge state.
 *
 * Three things are checked, because each has already broken once:
 *   1. horizontal overflow — a long anchor_quote or a 3-way conflict row is
 *      exactly what pushes a card past the viewport;
 *   2. the bottom tuner bar covering a control the test then needs to click
 *      (it covered the sidebar Profile button in Phase 4);
 *   3. the B3 inspector sheet and the Profile modal being reachable, readable
 *      and closable at that width, since on mobile they are full-screen rather
 *      than a side pane.
 *
 * Run by tests/test_phase7_production_resilience.py; skipped cleanly when node
 * or playwright is unavailable. */
const { chromium } = require('playwright');

const WIDTH = 390;
const HEIGHT = 844;
const LAYOUTS = ['B1', 'B2', 'B3', 'merged'];
const STATES = [
  'happy', 'blocked', 'zero-match', 'first-run',
  'three-way', 'long-quote', 'ingesting', 'empty', 'offline',
];

/** Widest element that sticks out, for a failure message that names a culprit. */
async function overflow(page) {
  return page.evaluate((width) => {
    const doc = document.documentElement;
    const offenders = [];
    for (const node of document.querySelectorAll('body *')) {
      const box = node.getBoundingClientRect();
      if (box.width === 0 && box.height === 0) continue;
      if (box.right > width + 1) {
        offenders.push({
          tag: node.tagName.toLowerCase(),
          id: node.id || '',
          cls: (node.className || '').toString().slice(0, 60),
          right: Math.round(box.right),
        });
      }
    }
    return {
      scrollWidth: doc.scrollWidth,
      clientWidth: doc.clientWidth,
      bodyScrollWidth: document.body.scrollWidth,
      offenders: offenders.slice(0, 5),
    };
  }, WIDTH);
}

/** Is the centre of this element the thing a tap would actually hit? */
async function tappable(page, selector) {
  return page.evaluate((sel) => {
    const node = document.querySelector(sel);
    if (!node) return { found: false };
    const box = node.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) return { found: true, visible: false };
    const x = Math.min(Math.max(box.left + box.width / 2, 1), window.innerWidth - 1);
    const y = Math.min(Math.max(box.top + box.height / 2, 1), window.innerHeight - 1);
    const hit = document.elementFromPoint(x, y);
    const covered = !(hit === node || node.contains(hit) || (hit && hit.contains(node)));
    return {
      found: true,
      visible: true,
      covered,
      coveredBy: covered && hit ? `${hit.tagName.toLowerCase()}#${hit.id}.${hit.className}`.slice(0, 80) : '',
      inViewport: box.top >= 0 && box.bottom <= window.innerHeight + 1,
    };
  }, selector);
}

(async () => {
  const base = process.env.BASE;
  const report = { viewport: [WIDTH, HEIGHT], errors: [], states: [], sheet: {}, profile: {} };
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const context = await browser.newContext({
    viewport: { width: WIDTH, height: HEIGHT },
    deviceScaleFactor: 3,
    isMobile: true,
    hasTouch: true,
    userAgent:
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 ' +
      '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
  });
  const page = await context.newPage();
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    if (/Failed to load resource/.test(m.text())) return;
    report.errors.push('console: ' + m.text());
  });
  page.on('pageerror', (e) => report.errors.push('pageerror: ' + e.message));

  // Sign in once, on the real page, and seed the real fixtures.
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await page.fill('#signin-email', 'founder@example.com');
  await page.click('#signin-go');
  await page.waitForSelector('#omni-wrap:not(.hidden)');
  if (await page.isVisible('#seed-demo')) {
    await page.click('#seed-demo');
    await page.waitForTimeout(800);
  }

  // ---- every layout x every state ----------------------------------------
  for (const layout of LAYOUTS) {
    for (const state of STATES) {
      await page.goto(`${base}/preview?layout=${layout}&state=${state}`, {
        waitUntil: 'networkidle',
      });
      await page.waitForSelector('#tuner:not(.hidden)');
      await page.waitForTimeout(220);
      const measured = await overflow(page);
      report.states.push({ layout, state, ...measured });
    }
  }

  // ---- the B3 conflict sheet, at mobile width ----------------------------
  await page.goto(`${base}/preview?layout=B3&state=happy`, { waitUntil: 'networkidle' });
  await page.waitForSelector('#tuner:not(.hidden)');
  await page.waitForTimeout(250);

  const conflict = await page.$('[data-inspect-tension]');
  report.sheet.found = Boolean(conflict);
  if (conflict) {
    await conflict.click();
    await page.waitForSelector('#inspector[data-open="true"]');
    await page.waitForTimeout(350);
    report.sheet.open = true;
    report.sheet.overflow = await overflow(page);
    report.sheet.closeButton = await tappable(page, '#inspector-close');
    report.sheet.box = await page.$eval('#inspector', (n) => {
      const b = n.getBoundingClientRect();
      return {
        left: Math.round(b.left), right: Math.round(b.right),
        top: Math.round(b.top), bottom: Math.round(b.bottom),
        width: Math.round(b.width),
      };
    });
    // The tuner is a fixed bottom bar; the sheet's own content must not be
    // hidden underneath it, and its close control must stay hittable.
    report.sheet.tunerBox = await page.$eval('#tuner', (n) => {
      const b = n.getBoundingClientRect();
      return { top: Math.round(b.top), height: Math.round(b.height) };
    });
    report.sheet.textLength = (await page.textContent('#inspector')).trim().length;
    await page.click('#inspector-close');
    await page.waitForTimeout(300);
    report.sheet.closed = (await page.getAttribute('#inspector', 'data-open')) !== 'true';
    // And the library behind it is intact, not scrolled off or blanked.
    report.sheet.cardsAfterClose = (await page.$$('.card')).length;
  }

  // ---- the Profile modal, at mobile width --------------------------------
  // On a phone the sidebar is an off-canvas drawer, so Profile is reached from
  // the fixed bottom bar instead. Both bars are fixed to the bottom in the
  // /preview harness, which is exactly where they could cover each other.
  report.profile.sidebarButton = await tappable(page, '#open-profile');
  report.profile.button = await tappable(page, '#open-profile-mobile');
  report.profile.barBox = await page.$eval('#mobile-bar', (n) => {
    const b = n.getBoundingClientRect();
    return { top: Math.round(b.top), bottom: Math.round(b.bottom) };
  });
  report.profile.tunerBox = await page.$eval('#tuner', (n) => {
    const b = n.getBoundingClientRect();
    return { top: Math.round(b.top), bottom: Math.round(b.bottom) };
  });
  await page.click('#open-profile-mobile');
  await page.waitForSelector('#profile-scrim:not(.hidden)');
  await page.waitForTimeout(400);
  report.profile.open = await page.isVisible('#profile-scrim');
  report.profile.overflow = await overflow(page);
  report.profile.closeButton = await tappable(page, '#profile-close');
  report.profile.blocks = await page.$$eval('#profile-scrim .block h3', (ns) =>
    ns.map((n) => n.textContent.trim()));
  report.profile.box = await page.$eval('#profile-scrim .modal', (n) => {
    const b = n.getBoundingClientRect();
    return {
      left: Math.round(b.left), right: Math.round(b.right),
      top: Math.round(b.top), bottom: Math.round(b.bottom),
    };
  });
  // Two ways out, because a modal you can only leave by reloading is a trap.
  await page.click('#profile-close');
  await page.waitForTimeout(300);
  report.profile.closedByButton = !(await page.isVisible('#profile-scrim'));
  await page.click('#open-profile-mobile');
  await page.waitForSelector('#profile-scrim:not(.hidden)');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  report.profile.closedByEscape = !(await page.isVisible('#profile-scrim'));

  // ---- offline, at mobile width ------------------------------------------
  await page.goto(`${base}/preview?layout=merged&state=offline`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(350);
  report.offline = {
    strip: (await page.$$('.strip-error')).length,
    text: ((await page.$('.strip-error')) ? await page.textContent('.strip-error') : '').trim(),
    overflow: await overflow(page),
  };

  await browser.close();
  process.stdout.write(JSON.stringify(report));
})().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
