/* §7.2's sequence, and its four hard requirements.
 *
 *     BRAIN_ENV=test BRAIN_DATA_DIR=/tmp/r BRAIN_NEXT_UI=1 BRAIN_INLINE_WORKER=0 \
 *       python3 -m uvicorn app.main:app --port 8142
 *     BRAIN_ENV=test BRAIN_DATA_DIR=/tmp/r node tests/browser/thread_probe.js
 *
 * The inline worker is OFF on purpose: the job has to sit queued for the
 * interruption to have something to interrupt. The probe drains the queue
 * itself, from a separate process, which is also how it proves §7.2's
 * "leaving the page does not cancel anything".
 *
 * The URL is unique per run — the same tenant twice takes the duplicate
 * branch, which correctly navigates to the source it already has.
 */
const { chromium } = require('playwright');
const { execFile } = require('child_process');
const BASE = process.env.BRAIN_URL || 'http://127.0.0.1:8142';
// Unique per run: the same tenant twice would take the duplicate branch,
// which correctly navigates to the source it already has.
const SOURCE = process.env.SOURCE_URL ||
  `https://www.youtube.com/watch?v=${Date.now().toString(36).padStart(11, 'x')}`;

const drain = () => new Promise((resolve) => {
  execFile('python3', ['-c',
    'import sys; sys.path.insert(0,"/home/user/brain");' +
    'from app.core import engine as E; from app.db import control as c;' +
    'u=c.ensure_user(c.control(),"founder@example.com");' +
    'E.run_pending_jobs_sync(u["user_id"], limit=2)'], { env: process.env }, () => resolve());
});

(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/401/.test(m.text())) out.errors.push(m.text()); });
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);
  await page.goto(BASE + '/next', { waitUntil: 'networkidle' });
  await page.waitForSelector('#omni-input');

  out.before = {
    greeting: await page.locator('.home-greeting').count(),
    pitch: await page.locator('.home-pitch').count(),
    actions: await page.locator('.home-actions').count(),
  };

  // Submit a link with an instruction, the way §7.1 describes.
  await page.fill('#omni-input', `${SOURCE} Pull out how they run planning cycles.`);
  await page.keyboard.press('Enter');
  await page.waitForSelector('.thread');

  out.opened = {
    greetingGone: (await page.locator('.home-greeting').count()) === 0,
    pitchGone: (await page.locator('.home-pitch').count()) === 0,
    actionsGone: (await page.locator('.home-actions').count()) === 0,
    placeholder: await page.getAttribute('#omni-input', 'placeholder'),
    // §7.2's first hard requirement.
    composerEnabled: !(await page.locator('#omni-input').isDisabled()),
    turns: await page.locator('.turn-row').evaluateAll(
      ns => ns.map(n => `${n.dataset.who}: ${n.innerText.replace(/\n/g, ' ⏎ ').slice(0, 72)}`)),
    hint: (await page.locator('#thread-hint').textContent()).trim(),
    steps: await page.locator('#thread-work .work-steps li').count(),
  };

  // §7.2: interrupt mid-flight. The job is still queued.
  await page.fill('#omni-input', 'Actually keep the hiring bits too.');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(900);
  out.interjected = {
    stillEnabled: !(await page.locator('#omni-input').isDisabled()),
    turns: await page.locator('.turn-row').evaluateAll(
      ns => ns.map(n => `${n.dataset.who}: ${n.innerText.replace(/\n/g, ' ⏎ ').slice(0, 72)}`)),
    fieldCleared: (await page.inputValue('#omni-input')) === '',
  };

  // And the instruction really reached the job.
  out.instructionOnJob = await new Promise((resolve) => {
    execFile('python3', ['-c',
      'import sys; sys.path.insert(0,"/home/user/brain");' +
      'from app.db import control as c;' +
      'u=c.ensure_user(c.control(),"founder@example.com");' +
      'print(c.list_jobs(c.control(), u["user_id"], limit=1)[0]["user_note"])'],
      { env: process.env }, (e, so) => resolve((so || '').trim()));
  });

  // Now let it finish, and watch the thread land.
  await drain();
  await page.waitForFunction(
    () => document.querySelector('.result-table') !== null, null, { timeout: 20000 },
  ).catch(() => {});
  out.landed = {
    workCollapsed: await page.getAttribute('#thread-work', 'data-collapsed'),
    header: (await page.locator('#thread-work .work-head span').first().textContent()).trim(),
    hintGone: (await page.locator('#thread-hint').count()) === 0,
    table: await page.locator('.result-row').evaluateAll(
      ns => ns.map(n => n.innerText.replace(/\n/g, ' — '))),
    buttons: (await page.locator('.bubble .btn').allTextContents()).map(t => t.trim()),
    chips: (await page.locator('.bubble-bare .chip').allTextContents()).map(t => t.trim()),
  };

  // §7.2: "Reopening Home later returns to the empty state."
  await page.goto(BASE + '/next', { waitUntil: 'networkidle' });
  await page.waitForSelector('#omni-input');
  out.reopened = {
    threadGone: (await page.locator('.thread').count()) === 0,
    greetingBack: (await page.locator('.home-greeting').count()) === 1,
  };
  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
