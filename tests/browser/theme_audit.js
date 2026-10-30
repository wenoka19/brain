// Renders the same markup under both themes and prints every resolved colour,
// so a theme can be compared side by side rather than reasoned about.
//
//     node tests/browser/theme_audit.js
//
// The pair worth watching is the primary button: light is a teal fill with a
// white label, dark is a lifted teal with an INK label. A single accent cannot
// be light enough to read as text on a dark card and dark enough to carry
// white, which is why `--on-accent` exists.

const { chromium } = require('playwright');
const fs = require('fs');
const S = '/home/user/brain/app/static/';
(async () => {
  const css = ['tokens.css','atoms.css','molecules.css'].map(f => fs.readFileSync(S+f,'utf8')).join('\n');
  const b = await chromium.launch();
  const page = await b.newPage();
  const out = {};
  for (const theme of ['light','dark']) {
    await page.setContent(`<!doctype html><html data-theme="${theme}"><head><style>${css}</style></head>
      <body><div class="item-card" id="card"><span class="item-text"><span class="item-title">T</span>
      <span class="item-meta"><span class="item-yield">3 rules</span></span></span>
      <span class="pill" data-state="active" id="pa">Added</span>
      <span class="pill" data-state="pending" id="pp">Needs you</span></div>
      <button class="btn" data-variant="primary" id="bp">Teach Brain</button>
      <button class="btn" data-variant="danger-solid" id="bd">Delete</button>
      <input class="input" id="inp"><div class="toast" id="toast">Restored</div>
      <span class="step-dot" data-state="current" id="sd">1</span></body></html>`);
    out[theme] = await page.evaluate(() => {
      const cs = (id,p) => getComputedStyle(document.getElementById(id)).getPropertyValue(p);
      const root = p => getComputedStyle(document.documentElement).getPropertyValue(p).trim();
      return {
        bodyBg: getComputedStyle(document.body).backgroundColor,
        bodyFg: getComputedStyle(document.body).color,
        cardBg: cs('card','background-color'),
        primaryBg: cs('bp','background-color'), primaryFg: cs('bp','color'),
        dangerBg: cs('bd','background-color'), dangerFg: cs('bd','color'),
        pillActive: cs('pa','background-color') + ' / ' + cs('pa','color'),
        pillPending: cs('pp','background-color') + ' / ' + cs('pp','color'),
        inputBorder: cs('inp','border-top-color'),
        toastBg: cs('toast','background-color'),
        stepDot: cs('sd','background-color'),
        accent: root('--accent'), onAccent: root('--on-accent'),
      };
    });
  }
  console.log(JSON.stringify(out, null, 1));
  await b.close();
})();
