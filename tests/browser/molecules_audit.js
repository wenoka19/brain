// Measures the molecules of MASTER_PLAN.md §4 in a real browser.
//
//     node tests/browser/molecules_audit.js
//
// The container is deliberately narrow (520px) so the §4.1 failure actually
// has a chance to happen: at full width the long title simply fits, and a
// truncation test that cannot fail proves nothing. At 520px the title
// overflows 865px into 247px, ellipsises, and does not reach the pill.
//
// Chromium and Playwright are preinstalled; no options are needed.

const { chromium } = require('playwright');
const fs = require('fs');
const S = '/home/user/brain/app/static/';
(async () => {
  const css = ['tokens.css','atoms.css','molecules.css'].map(f => fs.readFileSync(S+f,'utf8')).join('\n');
  const b = await chromium.launch();
  const page = await b.newPage({ viewport: { width: 1512, height: 982 } });
  await page.setContent(`<!doctype html><html><head><style>${css}</style></head><body style="padding:24px;width:520px">
    <div class="item-card" id="card">
      <span class="item-type"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4h16"/></svg></span>
      <span class="item-text">
        <span class="item-title" id="title">A title long enough that it must truncate rather than collide with the pill beside it, which is exactly the failure §4.1 calls out by name</span>
        <span class="item-meta"><span>Link</span><span>youtube.com</span><span class="item-yield">3 rules · 2 skills</span></span>
      </span>
      <span class="pill" data-state="pending">Processing</span>
      <span class="item-actions" id="acts"><button class="icon-btn"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4h16"/></svg></button></span>
    </div>
    <div class="exp-card" data-open="false" id="expclosed"><div class="exp-head"><span class="exp-caret slot"><svg class="icon" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></span><span class="item-text"><span class="item-title">Rule</span></span></div><div class="exp-body">hidden</div></div>
    <div class="exp-card" data-open="true" id="expopen"><div class="exp-head"><span class="exp-caret slot"><svg class="icon" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></span></div><div class="exp-body" id="expbody">shown</div></div>
    <div class="exp-card" data-state="proposed" id="proposed"><div class="exp-head">proposed</div></div>
    <div class="omnibar" id="omni"><button class="icon-btn"><svg class="icon" viewBox="0 0 24 24"><path d="M12 5v14"/></svg></button><input placeholder="Ask Brain anything..."><button class="icon-btn"><svg class="icon" viewBox="0 0 24 24"><path d="M12 5v8"/></svg></button></div>
    <div class="work"><div class="work-head">Working on it<span class="pill" data-state="archived">6 steps</span></div>
      <ul class="work-steps">
        <li class="work-step" data-state="done"><span class="work-marker"><svg class="icon" viewBox="0 0 24 24"><path d="M4 12.5l5 5L20 6.5"/></svg></span><span>Reading the source</span><span class="work-result" id="res-done">youtube.com/watch?v=…</span></li>
        <li class="work-step" data-state="running" id="running"><span class="work-marker"></span><span>Extracting rules</span><span class="work-result" id="res-running">should be hidden</span></li>
        <li class="work-step" data-state="pending"><span class="work-marker"></span><span>Drafting skills</span><span class="work-result" id="res-pending">should be hidden</span></li>
      </ul></div>
    <div class="msg" data-from="user"><div id="ubub">User bubble</div></div>
    <div class="msg" data-from="brain"><div id="bbub">Brain has no bubble</div></div>
    <div class="steps-list-wrap"><ol class="steps-list"><li id="step1">First</li><li>Second</li></ol></div>
    <button class="filter-pill" aria-pressed="true" id="fp-on">All</button>
    <button class="filter-pill" aria-pressed="false" id="fp-off">Source</button>
    <div class="meter-card" id="mc"><div class="meter-label">Calls</div><div class="meter-figure">1,284 <small>of 5,000</small></div><div class="meter">
      <span style="width:26%"></span></div><div class="meter-note">Resets November 1</div></div>
    <div class="steps"><span class="step-dot" data-state="current" id="sd1">1</span><span class="steps-rule"></span><span class="step-dot" id="sd2">2</span></div>
    <div class="toast" id="toast">Restored<button>Undo</button></div>
  </body></html>`);
  const r = await page.evaluate(() => {
    const el = id => document.getElementById(id);
    const vis = id => { const s = getComputedStyle(el(id)); return s.display !== 'none' && s.opacity !== '0'; };
    const cs = (id,p) => getComputedStyle(el(id)).getPropertyValue(p);
    const t = el('title');
    return {
      cardMinHeight: cs('card','min-height'),
      titleTruncates: t.scrollWidth > t.clientWidth && cs('title','text-overflow') === 'ellipsis',
      titleOverlapsPill: (() => { const a=t.getBoundingClientRect(), p=document.querySelector('#card .pill').getBoundingClientRect(); return a.right > p.left + 1; })(),
      actionsHiddenAtRest: cs('acts','opacity') === '0',
      itemTextIsColumn: cs('title','display') === 'block' && getComputedStyle(document.querySelector('.item-text')).flexDirection === 'column',
      caretClosed: cs('expclosed','display') && getComputedStyle(document.querySelector('#expclosed .exp-caret')).transform,
      caretOpen: getComputedStyle(document.querySelector('#expopen .exp-caret')).transform,
      closedBodyHidden: getComputedStyle(document.querySelector('#expclosed .exp-body')).display === 'none',
      openBodyShown: vis('expbody'),
      proposedBorder: cs('proposed','box-shadow').includes('122, 85, 0'),
      omniRadius: cs('omni','border-radius'),
      doneResultShown: vis('res-done'),
      runningResultHidden: !vis('res-running'),
      pendingResultHidden: !vis('res-pending'),
      spinnerAnimates: getComputedStyle(document.querySelector('#running .work-marker'), '::before').animationName,
      userBubbleMax: cs('ubub','max-width'),
      brainMax: cs('bbub','max-width'),
      userAligned: getComputedStyle(document.querySelector('.msg[data-from="user"]')).justifyContent,
      stepCounter: getComputedStyle(el('step1'), '::before').content,
      filterOnBg: cs('fp-on','background-color'),
      filterOffBg: cs('fp-off','background-color'),
      stepDotCurrent: cs('sd1','background-color'),
      stepDotNext: cs('sd2','background-color'),
      toastBg: cs('toast','background-color'),
      meterFigureNumerals: getComputedStyle(document.querySelector('.meter-figure')).fontVariantNumeric,
      titleScroll: t.scrollWidth, titleClient: t.clientWidth,
      titleOverflow: cs('title','text-overflow'),
    };
  });
  console.log(JSON.stringify(r, null, 1));
  await b.close();
})();
