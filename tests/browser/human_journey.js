/* Phase 7: the human entry door, end to end, in a real browser.
 *
 * Cold visit -> sign up -> land in a seeded workspace -> Connect to Claude ->
 * copy the /mcp link -> the OAuth consent card -> authorize -> the session
 * shows up in Profile -> a background change is picked up on tab focus
 * without losing what the person was in the middle of.
 *
 * Also carries the XSS canary: every field a human or an agent can write is
 * filled with an `onerror` payload, and `window.__xss` must stay undefined. */
const { chromium } = require('playwright');

const EMAIL = 'journey@example.com';
const CANARY = '<img src=x onerror=window.__xss=1>';

async function overflow(page, width) {
  return page.evaluate((w) => {
    const offenders = [];
    for (const node of document.querySelectorAll('body *')) {
      const box = node.getBoundingClientRect();
      if (box.width === 0 && box.height === 0) continue;
      if (box.right > w + 1) {
        offenders.push({
          tag: node.tagName.toLowerCase(), id: node.id || '',
          cls: (node.className || '').toString().slice(0, 60), right: Math.round(box.right),
        });
      }
    }
    return { scrollWidth: document.documentElement.scrollWidth, offenders: offenders.slice(0, 5) };
  }, width);
}

(async () => {
  const base = process.env.BASE;
  const out = { errors: [], xss: {}, mobile: [] };
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const watch = (target) => {
    target.on('console', (m) => {
      if (m.type() !== 'error') return;
      if (/Failed to load resource/.test(m.text())) return;
      out.errors.push('console: ' + m.text());
    });
    target.on('pageerror', (e) => out.errors.push('pageerror: ' + e.message));
  };
  let page = await context.newPage();
  watch(page);

  // ---- 1. the cold door -------------------------------------------------
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  out.door = {
    cardVisible: await page.isVisible('#signin .enter-card'),
    heading: (await page.textContent('#signin h1')).trim(),
    // The sidebar and nav bar must be out of the way of the one thing to do.
    sidebarHidden: !(await page.isVisible('.sidebar')),
    omniHidden: !(await page.isVisible('#omni-wrap')),
    // Theme tokens, not hard-coded colours.
    cardBg: await page.$eval('#signin .enter-card', (n) => getComputedStyle(n).backgroundColor),
    tokenBg: await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--bg-card').trim()),
    points: (await page.$$('#signin .enter-points li')).length,
  };
  // A bad address is answered on the card, not in a strip the card hides.
  await page.fill('#signin-email', 'not-an-email');
  await page.click('#signin-go');
  await page.waitForTimeout(200);
  out.door.inlineError = await page.isVisible('#signin-error');

  // ---- 2. sign up, land in a seeded workspace ---------------------------
  await page.fill('#signin-email', EMAIL);
  await page.click('#signin-go');
  await page.waitForSelector('#omni-wrap:not(.hidden)');
  await page.waitForTimeout(600);
  out.workspace = {
    cards: (await page.$$('.card')).length,
    skills: (await page.$$('.card-skill')).length,
    conflicts: (await page.$$('[data-inspect-tension]')).length,
    signInGone: !(await page.isVisible('#signin')),
  };

  // ---- 3. Connect to Claude -> copy the link ----------------------------
  await page.click('#connect-claude');
  await page.waitForSelector('#profile-scrim:not(.hidden)');
  await page.waitForTimeout(500);
  const mcpUrl = await page.inputValue('#mcp-url');
  out.connect = { mcpUrl, endsWithMcp: /\/mcp$/.test(mcpUrl) };
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.click('[data-copy="#mcp-url"]');
  await page.waitForTimeout(120);
  out.connect.buttonText = (await page.textContent('[data-copy="#mcp-url"]')).trim();
  out.connect.clipboard = await page.evaluate(() => navigator.clipboard.readText());
  await page.waitForTimeout(1400);
  out.connect.textRestored = !(await page.textContent('[data-copy="#mcp-url"]')).includes('Copied');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  out.connect.closedByEscape = !(await page.isVisible('#profile-scrim'));

  // ---- 4. the OAuth consent card ----------------------------------------
  const registered = await page.evaluate(async (b) => {
    const response = await fetch(b + '/oauth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_name: 'Claude Desktop',
        redirect_uris: ['http://127.0.0.1:1/callback'],
      }),
    });
    return { status: response.status, body: await response.json() };
  }, base);
  out.consent = { registerStatus: registered.status };
  const clientId = registered.body.client_id;
  const challenge = process.env.CHALLENGE;
  const authorizeUrl =
    `${base}/oauth/authorize?response_type=code&client_id=${encodeURIComponent(clientId)}` +
    `&redirect_uri=${encodeURIComponent('http://127.0.0.1:54321/callback')}` +
    `&code_challenge=${challenge}&code_challenge_method=S256&state=journey-state`;
  await page.goto(authorizeUrl, { waitUntil: 'networkidle' });
  out.consent.heading = (await page.textContent('.consent-card h1')).replace(/\s+/g, ' ').trim();
  out.consent.grants = await page.$$eval('.consent-grants li', (ns) =>
    ns.map((n) => n.textContent.replace(/\s+/g, ' ').trim()));
  // Already signed in to the web app: it should not ask who we are again.
  out.consent.identity = await page.isVisible('.consent-identity');
  out.consent.emailPrompt = await page.isVisible('#email');
  out.consent.theme = await page.getAttribute('html', 'data-theme');
  out.consent.cardBg = await page.$eval('.consent-card', (n) => getComputedStyle(n).backgroundColor);
  out.consent.hasCancel = await page.isVisible('#consent-cancel');

  // Approving redirects to the client's loopback port, where nothing is
  // listening — so the browser ends up on its own error page and `page.url()`
  // reads `chrome-error://`. The *request* is the evidence, so that is what is
  // captured.
  const [callback] = await Promise.all([
    page
      .waitForRequest((request) => request.url().includes('127.0.0.1:54321'), { timeout: 15000 })
      .catch(() => null),
    page.click('.consent-actions .btn-primary'),
  ]);
  out.consent.redirected = callback ? callback.url() : page.url();


  // ---- 5. the session shows up in Profile -------------------------------
  // Nothing listens on the client's loopback port, so the browser lands on its
  // own error page and keeps navigating there. A fresh tab in the same context
  // (same cookies, same session) is cleaner than racing that navigation.
  await page.close();
  page = await context.newPage();
  watch(page);

  // A code is not a session: the client exchanges it at /token, and only then
  // does anything show up in Profile. Run through the context's request API
  // rather than a page, because the page that holds the code is mid-navigation
  // to an error.
  const code = out.consent.redirected.match(/[?&]code=([^&]+)/);
  if (code) {
    const exchanged = await context.request.post(base + '/oauth/token', {
      form: {
        grant_type: 'authorization_code',
        code: decodeURIComponent(code[1]),
        code_verifier: process.env.VERIFIER,
        redirect_uri: 'http://127.0.0.1:54321/callback',
        client_id: clientId,
      },
    });
    const body = await exchanged.json();
    out.consent.exchange = {
      status: exchanged.status(),
      hasToken: Boolean(body.access_token),
      expiresIn: body.expires_in,
    };
  } else {
    out.consent.exchange = { status: 0, hasToken: false };
  }

  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await page.waitForSelector('#omni-wrap:not(.hidden)');
  await page.click('#open-profile');
  await page.waitForSelector('#profile-scrim:not(.hidden)');
  await page.waitForTimeout(500);
  out.session = {
    rows: (await page.$$('#sessions .row')).length,
    text: (await page.textContent('#sessions')).replace(/\s+/g, ' ').trim(),
    hasRevoke: (await page.$$('#sessions [data-revoke]')).length,
  };
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);

  // ---- 6. the watermark sync, preserving what the user was doing --------
  await page.fill('#omni-input', 'cold');
  await page.waitForTimeout(250);
  const beforeFilter = await page.inputValue('#omni-input');
  // A change made elsewhere, exactly as an agent on /mcp would make it.
  out.sync = {};
  out.sync.editStatus = await page.evaluate(async () => {
    const node = document.querySelector('[data-expand]');
    if (!node) return 'no-claim';
    const response = await fetch('/api/claim/' + node.dataset.expand, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Brain-CSRF': document.cookie.split('brain_csrf=')[1].split(';')[0],
        'X-Brain-Request': '1',
      },
      body: JSON.stringify({ claim: 'Edited from somewhere else entirely' }),
    });
    return response.status;
  });
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(1200);
  out.sync.filterKept = (await page.inputValue('#omni-input')) === beforeFilter;
  out.sync.picked = (await page.textContent('#cards')).includes('somewhere else entirely');
  out.sync.watermark = await page.evaluate(() => window.__brainWatermark || null);

  // ---- 7. the XSS canary -------------------------------------------------
  await page.evaluate(async (canary) => {
    const csrf = document.cookie.split('brain_csrf=')[1].split(';')[0];
    const send = (path, body) =>
      fetch(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Brain-CSRF': csrf,
          'X-Brain-Request': '1',
        },
        body: JSON.stringify(body),
      });
    await send('/api/omnibar/run', { plan: null }).catch(() => {});
    await send('/api/skill', {
      slug: 'canary-skill',
      name: canary,
      description: canary,
      trigger_when: canary,
      execution_steps: [canary, canary],
      guardrails: canary,
      output_template: canary,
    });
    await send('/api/profile', { about_you_context: canary });
    const claimId = document.querySelector('[data-expand]').dataset.expand;
    await send('/api/claim/' + claimId, {
      claim: canary, when_it_applies: canary, when_it_fails: canary,
      metrics_and_quotes: canary, domain_tags: [canary],
    });
  }, CANARY);
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await page.waitForSelector('#omni-wrap:not(.hidden)');
  await page.waitForTimeout(700);
  // Expand and inspect, so the canary passes through every renderer there is.
  const expandable = await page.$('[data-expand]');
  if (expandable) { await expandable.click(); await page.waitForTimeout(250); }
  const skillHead = await page.$('.card-skill .card-head');
  if (skillHead) { await skillHead.click(); await page.waitForTimeout(350); }
  await page.click('#open-profile');
  await page.waitForTimeout(600);
  out.xss = {
    // `undefined` would vanish from the JSON, so it is reported as a string.
    fired: await page.evaluate(() => (window.__xss === undefined ? "no" : String(window.__xss))),
    imgTags: await page.evaluate(() => document.querySelectorAll('#cards img, #inspector img, #profile-scrim img').length),
    // The payload must be on the page as *text*, which proves it round-tripped
    // and was escaped rather than silently dropped.
    visibleAsText: (await page.textContent('body')).includes('onerror=window.__xss=1'),
  };
  await page.keyboard.press('Escape');

  // ---- 8. the same journey at 390x844 ------------------------------------
  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3,
  });
  const small = await phone.newPage();
  small.on('pageerror', (e) => out.errors.push('mobile pageerror: ' + e.message));
  await small.goto(base + '/', { waitUntil: 'networkidle' });
  out.mobile.push({ where: 'door', ...(await overflow(small, 390)) });
  await small.fill('#signin-email', EMAIL);
  await small.click('#signin-go');
  await small.waitForSelector('#omni-wrap:not(.hidden)');
  await small.waitForTimeout(600);
  out.mobile.push({ where: 'workspace', ...(await overflow(small, 390)) });

  for (const layout of ['B1', 'B2', 'B3', 'merged']) {
    for (const state of ['happy', 'blocked', 'zero-match', 'first-run', 'three-way',
                         'long-quote', 'ingesting', 'empty', 'offline']) {
      await small.goto(`${base}/preview?layout=${layout}&state=${state}`, { waitUntil: 'networkidle' });
      await small.waitForSelector('#tuner:not(.hidden)');
      await small.waitForTimeout(180);
      out.mobile.push({ where: `${layout}/${state}`, ...(await overflow(small, 390)) });
    }
  }
  // The tuner starts collapsed on a phone, so it cannot cover a bottom action.
  out.tuner = {
    collapsed: await small.getAttribute('#tuner', 'data-collapsed'),
    handleVisible: await small.isVisible('#tuner-handle'),
    height: await small.$eval('#tuner', (n) => Math.round(n.getBoundingClientRect().height)),
  };
  await small.click('#tuner-handle');
  await small.waitForTimeout(250);
  out.tuner.expandedHeight = await small.$eval('#tuner', (n) => Math.round(n.getBoundingClientRect().height));

  // The consent card on a phone, where a trapped Authorize button is fatal.
  await small.goto(authorizeUrl, { waitUntil: 'networkidle' });
  out.consentMobile = {
    ...(await overflow(small, 390)),
    buttonInView: await small.evaluate(() => {
      const btn = document.querySelector('.consent-actions .btn-primary');
      const box = btn.getBoundingClientRect();
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      return { covered: !(hit === btn || btn.contains(hit)), bottom: Math.round(box.bottom) };
    }),
  };

  await browser.close();
  process.stdout.write(JSON.stringify(out));
})().catch((error) => {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
