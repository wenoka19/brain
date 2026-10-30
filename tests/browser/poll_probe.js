/* §9.1: "The UI polls or streams from those rows — the work block is a view
 * of real state, never a fake animation."
 *
 * Nothing polled. The block was a snapshot taken by the single page read, so
 * the running step's spinner kept turning after that step had finished, and
 * `processing` never became `done` without a reload.
 *
 * This is the only way to show it: the page is loaded with a job still
 * QUEUED, the queue is drained by a separate process, and the block has to
 * notice on its own.
 *
 *     SOURCE_ID=src_... node tests/browser/poll_probe.js
 */
const { chromium } = require('playwright');
const BASE = 'http://127.0.0.1:8119';
const { execFile } = require('child_process');
(async () => {
  const out = { errors: [] };
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const page = await (await b.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  page.on('pageerror', e => out.errors.push('pageerror: ' + e.message));
  await page.goto(BASE + '/next', { waitUntil: 'domcontentloaded' });
  await page.evaluate((u) => fetch(u + '/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'founder@example.com' }),
  }), BASE);

  const id = process.env.SOURCE_ID;
  await page.goto(`${BASE}/next/sources/${id}`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.work-steps');

  const snapshot = () => page.evaluate(() => ({
    pill: document.querySelector('.source-head .pill')?.textContent.trim(),
    header: document.querySelector('.work-head span')?.textContent.trim(),
    states: [...document.querySelectorAll('.work-steps li')].map(li => li.dataset.state),
  }));
  out.atLoad = await snapshot();

  // Drain the queue from outside the browser.
  await new Promise((resolve) => {
    execFile('python3', ['-c',
      'import sys; sys.path.insert(0,"/home/user/brain");' +
      'from app.core import engine as E;' +
      'from app.db import control as c;' +
      'u=c.ensure_user(c.control(),"founder@example.com");' +
      'print(E.run_pending_jobs_sync(u["user_id"], limit=2))'],
      { env: { ...process.env } }, (err, so, se) => { out.drained = (so || se || '').slice(0, 60); resolve(); });
  });

  // The frame reloads itself when the job lands, so wait for that.
  await page.waitForFunction(
    () => document.querySelector('.source-head .pill')?.textContent.trim() === 'In memory',
    null, { timeout: 15000 },
  ).catch(() => {});
  out.afterDrain = await snapshot();
  out.noticedWithoutAReload = out.afterDrain.pill === 'In memory';

  console.log(JSON.stringify(out, null, 2));
  await b.close();
})();
