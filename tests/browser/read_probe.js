/* §7.5 and §7.6 in a real browser: every control on the two reading surfaces
 * has to do its job.
 *
 *     BRAIN_ENV=test BRAIN_DATA_DIR=/tmp/r1 python3 -m uvicorn app.main:app --port 8111
 *     node tests/browser/read_probe.js
 *
 * This file exists because both frames returned their node BEFORE their own
 * wiring ran. The markup was right, the ids were right, the handlers were
 * written and correct, and nothing reached the console — every control was
 * simply inert. Only a browser shows that, which is why the static test in
 * `tests/test_redesign_controls.py` is backed by this one.
 *
 * It checks: the in-page search marks and counts and clears, a needle in
 * nothing says "none", Copy reaches the clipboard with the timestamps
 * attached, the download menu opens with only producible formats and closes
 * on Escape, and Brief/Full switches real content — or says there is none.
 */
const { chromium } = require('playwright');
const BASE = process.env.BRAIN_URL || 'http://127.0.0.1:8111';

(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({
    viewport: { width: 1280, height: 900 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') out.errors.push(m.text()); });
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));

  // The reads behind these frames need a session.
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((b) => fetch(b + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }).then(r => r.status), BASE);

  // ---- the transcript reader -------------------------------------------
  await page.goto(BASE + '/next/library/transcripts', { waitUntil: 'networkidle' });
  await page.click('.item-card');
  await page.waitForSelector('#in-page');

  const word = await page.evaluate(() =>
    document.querySelector('.turn-text').textContent.trim().split(/\s+/)[0]);
  await page.fill('#in-page', word);
  await page.waitForTimeout(150);
  out.transcript = {
    needle: word,
    marks: await page.locator('.transcript-body mark').count(),
    matchCount: (await page.locator('#matches').textContent()).trim(),
    countVisible: await page.isVisible('#matches'),
  };

  // Clearing must put the text back, not leave the marks behind.
  await page.fill('#in-page', '');
  await page.waitForTimeout(150);
  out.transcript.marksAfterClear = await page.locator('.transcript-body mark').count();

  // A needle that is in no turn says so.
  await page.fill('#in-page', 'zzqqxx');
  await page.waitForTimeout(150);
  out.transcript.noMatchSays = (await page.locator('#matches').textContent()).trim();
  await page.fill('#in-page', '');

  await page.click('#copy');
  await page.waitForTimeout(120);
  out.transcript.copyAnswer = (await page.locator('#copy').textContent()).trim();
  out.transcript.clipboard = (await page.evaluate(() => navigator.clipboard.readText()))
    .slice(0, 48);

  // The download menu opens, lists only producible formats, and closes on Escape.
  await page.click('[data-menu-for="tx-download"]');
  await page.waitForTimeout(120);
  out.transcript.menuFormats = (await page.locator('#tx-download .menu-item').allTextContents())
    .map(t => t.trim());
  out.transcript.menuExpanded =
    await page.getAttribute('[data-menu-for="tx-download"]', 'aria-expanded');
  out.transcript.menuOpen = await page.isVisible('#tx-download');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(120);
  out.transcript.menuClosedOnEscape = !(await page.isVisible('#tx-download'));

  // The sticky bar gains its border only once stuck.
  out.transcript.stuckAtTop = await page.getAttribute('.read-bar', 'data-stuck');
  out.transcript.scrollRoom = await page.evaluate(() =>
    document.documentElement.scrollHeight - window.innerHeight);
  await page.evaluate(() => window.scrollTo(0, 1200));
  await page.waitForTimeout(250);
  out.transcript.scrolledTo = await page.evaluate(() => Math.round(window.scrollY));
  out.transcript.stuckAfterScroll = await page.getAttribute('.read-bar', 'data-stuck');

  // ---- the summary reader ----------------------------------------------
  await page.goto(BASE + '/next/library/summaries', { waitUntil: 'networkidle' });
  await page.click('.item-card');
  await page.waitForSelector('.segmented');
  const readTime = async () => {
    const cells = await page.locator('.read-meta span').allTextContents();
    return (cells.find(t => t.includes('min read')) || '').trim();
  };
  out.summary = {
    briefPoints: await page.locator('#points .point').count(),
    briefRead: await readTime(),
    chaptersHiddenInBrief: !(await page.isVisible('#chapters')),
  };
  await page.click('.segment[data-view="full"]');
  await page.waitForTimeout(150);
  out.summary.fullPoints = await page.locator('#points .point').count();
  out.summary.fullRead = await readTime();
  out.summary.fullLabel = (await page.locator('.segment[data-view="full"]').textContent()).trim();
  out.summary.fullPressed = await page.getAttribute('.segment[data-view="full"]', 'aria-pressed');
  await page.click('#sum-copy');
  await page.waitForTimeout(120);
  out.summary.copyAnswer = (await page.locator('#sum-copy').textContent()).trim();
  out.summary.chaptersShownInFull = await page.isVisible('#chapters');
  out.summary.chapterCount = await page.locator('#chapters .chapter').count();
  out.summary.chapterNote = await page.locator('#chapters .section-note').count();

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
