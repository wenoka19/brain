// Measures every atom of MASTER_PLAN.md §3 in a real browser and prints what
// it found. Run it after any change to tokens.css or atoms.css:
//
//     node tests/browser/atoms_audit.js
//
// This exists because the static tests cannot see the failure that matters
// most: CSS that is valid, parses, and silently does not apply. `.v-rule` was
// declared `height: 18px` on an inline span, which is ignored — it measured 17
// and only a real layout showed it.
//
// Chromium and Playwright are preinstalled; no options are needed.

const { chromium } = require('playwright');
const fs = require('fs');
const S = '/home/user/brain/app/static/';

(async () => {
  const css = fs.readFileSync(S + 'tokens.css', 'utf8') + '\n' + fs.readFileSync(S + 'atoms.css', 'utf8');
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1512, height: 982 } });
  await page.setContent(`<!doctype html><html><head><style>${css}</style></head><body>
    <button class="btn" data-variant="primary" id="b-primary">Teach Brain</button>
    <button class="btn" data-variant="secondary" id="b-secondary">Copy</button>
    <button class="btn" data-variant="ghost" id="b-ghost">Dismiss</button>
    <button class="btn" data-variant="danger" id="b-danger">Delete my library</button>
    <button class="btn" data-variant="danger-solid" id="b-dangersolid">Delete</button>
    <button class="btn" data-size="sm" data-variant="secondary" id="b-sm">Small</button>
    <a class="nav-item" id="nav-rest"><span class="slot"><svg viewBox="0 0 24 24"><path d="M4 4h16"/></svg></span><span class="nav-label">Transcripts</span><span class="nav-count">24</span></a>
    <a class="nav-item" id="nav-current" aria-current="page"><span class="slot"><svg viewBox="0 0 24 24"><path d="M4 4h16"/></svg></span><span class="nav-label">Skills</span><span class="nav-count" data-alert="true">2</span></a>
    <button class="icon-btn" id="ib"><svg class="icon" viewBox="0 0 24 24"><path d="M4 4h16"/></svg></button>
    <input class="input" id="inp" placeholder="Search transcripts">
    <select class="select" id="sel"><option>English</option></select>
    <button class="switch" role="switch" aria-checked="false" id="sw-off"></button>
    <button class="switch" role="switch" aria-checked="true" id="sw-on"></button>
    <span class="pill" data-state="active" id="p-active">Added</span>
    <span class="pill" data-state="pending" id="p-pending">Needs you</span>
    <span class="pill" data-state="archived" id="p-archived">Archived</span>
    <div class="meter" id="meter"><span style="width:40%"></span></div>
    <kbd class="kbd" id="kbd">Ctrl K</kbd>
    <hr class="divider" id="div"><span class="v-rule" id="vr"></span>
    <a class="action-link" id="al"><span class="slot"><svg viewBox="0 0 24 24"><path d="M4 4h16"/></svg></span>Teach Brain</a>
    <pre class="code" id="code">url: <mark class="ph">paste_your_link_here</mark></pre>
  </body></html>`);

  const r = await page.evaluate(() => {
    const box = (id) => { const e = document.getElementById(id); const b = e.getBoundingClientRect();
      return { w: Math.round(b.width), h: Math.round(b.height) }; };
    const cs = (id, p) => getComputedStyle(document.getElementById(id)).getPropertyValue(p);
    const pseudo = (id, p, sel) => getComputedStyle(document.getElementById(id), sel).getPropertyValue(p);
    return {
      btnHeight: box('b-primary').h,
      btnSmHeight: box('b-sm').h,
      btnPadding: cs('b-primary', 'padding-left'),
      btnNowrap: cs('b-primary', 'white-space'),
      btnFlex: cs('b-primary', 'flex'),
      btnRadius: cs('b-primary', 'border-radius'),
      primaryBg: cs('b-primary', 'background-color'),
      primaryFg: cs('b-primary', 'color'),
      secondaryShadow: cs('b-secondary', 'box-shadow'),
      dangerColor: cs('b-danger', 'color'),
      dangerSolidBg: cs('b-dangersolid', 'background-color'),
      navHeight: box('nav-rest').h,
      navRadius: cs('nav-rest', 'border-radius'),
      navRestBg: cs('nav-rest', 'background-color'),
      navCurrentBg: cs('nav-current', 'background-color'),
      navCurrentWeight: cs('nav-current', 'font-weight'),
      navCountNumerals: getComputedStyle(document.querySelector('#nav-rest .nav-count')).fontVariantNumeric,
      navCountColor: getComputedStyle(document.querySelector('#nav-rest .nav-count')).color,
      alertBg: getComputedStyle(document.querySelector('#nav-current .nav-count')).backgroundColor,
      alertRadius: getComputedStyle(document.querySelector('#nav-current .nav-count')).borderRadius,
      slot: box('nav-rest') && (() => { const s = document.querySelector('#nav-rest .slot').getBoundingClientRect();
        return { w: Math.round(s.width), h: Math.round(s.height) }; })(),
      iconInSlot: (() => { const s = document.querySelector('#nav-rest .slot svg').getBoundingClientRect();
        return { w: Math.round(s.width), h: Math.round(s.height) }; })(),
      iconBtn: box('ib'),
      inputHeight: box('inp').h,
      inputBorder: cs('inp', 'border-top-color'),
      selectHeight: box('sel').h,
      selectAppearance: cs('sel', 'appearance'),
      switchOff: box('sw-off'),
      switchOffBg: cs('sw-off', 'background-color'),
      switchOnBg: cs('sw-on', 'background-color'),
      knobSize: pseudo('sw-on', 'width', '::after'),
      knobShift: pseudo('sw-on', 'transform', '::after'),
      pillDot: pseudo('p-active', 'width', '::before'),
      pillActiveBg: cs('p-active', 'background-color'),
      pillPendingBg: cs('p-pending', 'background-color'),
      pillArchivedBg: cs('p-archived', 'background-color'),
      pillRadius: cs('p-active', 'border-radius'),
      meterHeight: box('meter').h,
      meterFillW: Math.round(document.querySelector('#meter span').getBoundingClientRect().width),
      kbdBorder: cs('kbd', 'border-top-width'),
      dividerH: box('div').h,
      vRuleH: box('vr').h,
      actionLinkSize: cs('al', 'font-size'),
      codeFont: cs('code', 'font-family').slice(0, 24),
      codeBg: cs('code', 'background-color'),
      phBg: getComputedStyle(document.querySelector('.ph')).backgroundColor,
      phWeight: getComputedStyle(document.querySelector('.ph')).fontWeight,
      phTransform: getComputedStyle(document.querySelector('.ph')).textTransform,
    };
  });
  console.log(JSON.stringify(r, null, 1));
  await browser.close();
})();
