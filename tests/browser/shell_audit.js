// Drives the redesigned surface in a real browser against a real server.
//
//     BRAIN_ENV=test BRAIN_NEXT_UI=1 python3 -m uvicorn app.main:app --port 8099 &
//     node tests/browser/shell_audit.js
//
// It checks the things only a running page can show: that the typewriter
// actually types and then stops forever on focus, that Back restores the
// previous frame, that a deep link survives a hard reload, that Ctrl K reaches
// the omnibar from a page that does not have one, and that Home fits a single
// 1512x982 viewport with no scroll.
//
// That last check is why this file exists. The icon sprite, declared
// `width="0" height="0"`, still generated a line box and pushed the whole page
// down 21px. Nothing in the source looks wrong; only a layout shows it.

const { chromium } = require('playwright');
const BASE = process.env.BRAIN_URL || 'http://127.0.0.1:8099';
(async () => {
  const b = await chromium.launch();
  const page = await b.newPage({ viewport: { width: 1512, height: 982 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));

  await page.goto(BASE + '/next', { waitUntil: 'networkidle' });
  const out = {};

  out.greetingShown = await page.locator('.home-greeting').innerText();
  out.pitch = (await page.locator('.home-pitch').innerText()).slice(0, 40) + '…';
  out.navHrefsRewritten = await page.locator('.sidebar a[href="/next/rules"]').count();
  out.homeIsCurrent = await page.locator('.nav-item[aria-current="page"] .nav-label').innerText();

  // The typewriter must be typing, then stop forever on focus.
  await page.waitForTimeout(400);
  const p1 = await page.locator('#omni-input').getAttribute('placeholder');
  await page.waitForTimeout(300);
  const p2 = await page.locator('#omni-input').getAttribute('placeholder');
  out.typewriterTypes = p2.length > p1.length && 'Ask Brain anything…'.startsWith(p2);
  await page.locator('#omni-input').focus();
  await page.waitForTimeout(500);
  out.typewriterStopsOnFocus = (await page.locator('#omni-input').getAttribute('placeholder')) === '';

  // Navigate by click, then Back.
  await page.locator('.sidebar a[href="/next/rules"]').click();
  await page.waitForTimeout(120);
  out.afterClickURL = new URL(page.url()).pathname;
  out.afterClickCurrent = await page.locator('.nav-item[aria-current="page"] .nav-label').innerText();
  await page.goBack();
  await page.waitForTimeout(120);
  out.afterBackURL = new URL(page.url()).pathname;
  out.afterBackIsHome = await page.locator('.home-greeting').count() === 1;

  // A deep link must survive a hard reload.
  await page.goto(BASE + '/next/transcripts/abc123', { waitUntil: 'networkidle' });
  out.deepLinkRenders = await page.locator('.page-title').innerText();
  out.deepLinkNav = await page.locator('.nav-item[aria-current="page"] .nav-label').innerText();

  // Ctrl K from a page that is not Home.
  await page.keyboard.press('Control+k');
  await page.waitForTimeout(150);
  out.ctrlKFocusesOmnibar = await page.evaluate(() => document.activeElement?.id);

  // §14.12: no horizontal scroll, and Home fits one viewport.
  await page.goto(BASE + '/next', { waitUntil: 'networkidle' });
  out.noHScroll1512 = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  out.homeFitsOneScreen = await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight + 1);

  // 390x844 — the phone.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(120);
  out.phoneSidebarHidden = await page.locator('.sidebar').evaluate(n => getComputedStyle(n).transform !== 'none');
  out.phoneTopbarShown = await page.locator('.topbar').isVisible();
  out.phoneNoHScroll = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
  await page.locator('#menu-btn').click();
  await page.waitForTimeout(250);
  out.phoneMenuOpens = await page.locator('.sidebar').evaluate(n => n.dataset.open === 'true');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(250);
  out.escapeClosesMenu = await page.locator('.sidebar').evaluate(n => n.dataset.open === 'false');

  out.consoleErrors = errors;
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
