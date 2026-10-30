/* brain — the router and the frames.
   MASTER_PLAN.md §5 (routes), §7 (frames), §6 (global rules).

   Vanilla, no framework, no build step, and no inline handler anywhere: the
   app is served under `script-src 'self'`, so everything binds from here.

   The router owns `#content` and nothing else. A frame is a function that
   returns a node; it never reaches outside the node it was given. */

const ROUTES = [
  { path: "/", frame: "home", nav: "/" },
  { path: "/library/transcripts", frame: "transcripts", nav: "/library/transcripts" },
  { path: "/transcripts/:id", frame: "transcript", nav: "/library/transcripts" },
  { path: "/library/summaries", frame: "summaries", nav: "/library/summaries" },
  { path: "/summaries/:id", frame: "summary", nav: "/library/summaries" },
  { path: "/library/sources", frame: "sources", nav: "/library/sources" },
  { path: "/sources/:id", frame: "source", nav: "/library/sources" },
  { path: "/rules", frame: "rules", nav: "/rules" },
  { path: "/skills", frame: "skills", nav: "/skills" },
  { path: "/activity", frame: "activity", nav: "/activity" },
  { path: "/connections", frame: "connections", nav: "/connections" },
  { path: "/archive", frame: "archive", nav: "/archive" },
  { path: "/profile", frame: "profile", nav: "/profile" },
  { path: "/signin", frame: "signin", nav: null },
];

/* ---------------------------------------------------------------------- */
/* helpers                                                                 */
/* ---------------------------------------------------------------------- */

/** Escape before interpolation, always. Every string that reaches the DOM as
 *  markup goes through here; anything else is set with `textContent`. */
export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function icon(name, cls = "icon") {
  return `<svg class="${esc(cls)}" aria-hidden="true"><use href="#i-${esc(name)}"/></svg>`;
}

export function slot(name) {
  return `<span class="slot">${icon(name)}</span>`;
}

export function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** §7.1: "<12 morning, <18 afternoon, else evening", on the LOCAL hour. */
export function greeting(hour = new Date().getHours()) {
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/** The prefix the surface is mounted under, "" once it owns the origin. */
export function base() {
  return document.body?.dataset.base ?? "";
}

/** A logical path ("/rules") as a real URL ("/next/rules"). */
export function href(path) {
  const b = base();
  if (!b) return path;
  return path === "/" ? b || "/" : b + path;
}

/** A real pathname back to its logical one. */
export function logical(pathname) {
  const b = base();
  if (!b || !pathname.startsWith(b)) return pathname;
  return pathname.slice(b.length) || "/";
}

/** `/transcripts/abc` against `/transcripts/:id` → { id: "abc" }. */
export function matchRoute(pathname) {
  const parts = logical(pathname).replace(/\/+$/, "").split("/").filter(Boolean);
  for (const route of ROUTES) {
    const want = route.path.split("/").filter(Boolean);
    if (want.length !== parts.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < want.length; i += 1) {
      if (want[i].startsWith(":")) params[want[i].slice(1)] = decodeURIComponent(parts[i]);
      else if (want[i] !== parts[i]) { ok = false; break; }
    }
    if (ok) return { route, params };
  }
  return null;
}

/* ---------------------------------------------------------------------- */
/* the frame registry                                                      */
/* ---------------------------------------------------------------------- */

export const FRAMES = Object.create(null);

/** A frame that has not been built yet still has a URL, a title and a place in
 *  the navigation. It says so plainly rather than rendering a blank page —
 *  §6 rule 12: an empty state says what will appear here. */
function placeholder(name) {
  return () => el(`
    <div class="wrap">
      <h1 class="page-title">${esc(name[0].toUpperCase() + name.slice(1))}</h1>
      <div class="empty">
        <p>This frame has its route and its place in the navigation. Its
           contents are the next thing to be built.</p>
      </div>
    </div>`);
}

for (const { frame } of ROUTES) FRAMES[frame] = placeholder(frame);

/* ---------------------------------------------------------------------- */
/* §7.1 HOME — empty state                                                 */
/* ---------------------------------------------------------------------- */

/** The cycling placeholder of §7.1.
 *
 *  Timing is the plan's, to the millisecond: 50ms per character, 26ms
 *  deleting, 2200ms hold, 240ms between phrases.
 *
 *  Two rules matter more than the timing. It stops PERMANENTLY on the first
 *  focus or input and never restarts — a placeholder that resumes while
 *  someone is thinking is a distraction they cannot dismiss. And it does not
 *  start at all under reduced motion: string one shows statically.
 */
export function typewriter(input, strings, { onStop } = {}) {
  const PHRASES = strings ?? [
    "Ask Brain anything…",
    "Paste a YouTube link and Brain will watch it…",
    "Drop a document or a screenshot…",
  ];

  if (reducedMotion()) {
    input.placeholder = PHRASES[0];
    return { stop() {} };
  }

  let timer = null;
  let stopped = false;
  let phrase = 0;
  let chars = 0;
  let erasing = false;

  const stop = () => {
    if (stopped) return;
    stopped = true;
    clearTimeout(timer);
    input.placeholder = "";
    input.removeEventListener("focus", stop);
    input.removeEventListener("input", stop);
    if (onStop) onStop();
  };

  const tick = () => {
    if (stopped) return;
    const text = PHRASES[phrase];
    if (!erasing) {
      chars += 1;
      input.placeholder = text.slice(0, chars);
      if (chars === text.length) {
        erasing = true;
        timer = setTimeout(tick, 2200);
        return;
      }
      timer = setTimeout(tick, 50);
      return;
    }
    chars -= 1;
    input.placeholder = text.slice(0, chars);
    if (chars === 0) {
      erasing = false;
      phrase = (phrase + 1) % PHRASES.length;
      timer = setTimeout(tick, 240);
      return;
    }
    timer = setTimeout(tick, 26);
  };

  input.addEventListener("focus", stop, { once: true });
  input.addEventListener("input", stop, { once: true });
  timer = setTimeout(tick, 50);
  return { stop };
}

FRAMES.home = function home(_params, ctx) {
  const name = ctx?.user?.name ?? "there";
  const node = el(`
    <div class="home">
      <div class="home-stack">
        <h1 class="home-greeting">${esc(greeting())}, ${esc(name)}.</h1>
        <p class="home-pitch">Paste a link or drop a file. Brain reads it, keeps
          what matters, and hands it to every AI you work with.</p>

        <div class="omnibar" id="omnibar">
          <button class="icon-btn" id="omni-add" aria-label="Add a document or an image">
            ${icon("plus")}
          </button>
          <input id="omni-input" aria-label="Ask Brain anything" autocomplete="off">
          <button class="icon-btn" id="omni-mic" aria-label="Record and transcribe">
            ${icon("mic")}
          </button>
        </div>

        <div class="home-actions">
          <a class="action-link" href="#" data-action="teach">${slot("plus")}Teach Brain</a>
          <a class="action-link" href="/skills?create=1">${slot("skills")}Add a skill</a>
          <a class="action-link" href="/rules?create=1">${slot("rules")}Write a rule</a>
        </div>

        <input type="file" id="omni-file" class="sr-only"
               accept=".pdf,.docx,.txt,.md,image/png,image/jpeg,image/webp" multiple>
      </div>
    </div>`);

  const input = node.querySelector("#omni-input");
  typewriter(input);

  const picker = node.querySelector("#omni-file");
  node.querySelector("#omni-add").addEventListener("click", () => picker.click());

  // "Teach Brain → focuses the omnibar and opens the file picker" (§7.1).
  node.querySelector('[data-action="teach"]').addEventListener("click", (event) => {
    event.preventDefault();
    input.focus();
    picker.click();
  });

  return node;
};

/* ---------------------------------------------------------------------- */
/* reading from the API                                                    */
/* ---------------------------------------------------------------------- */

export async function api(path) {
  const response = await fetch(path, {
    headers: { "X-Brain-Request": "1" },
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body.ok === false) {
    const error = new Error(body.message || `Request failed (${response.status})`);
    error.reason = body.reason || String(response.status);
    throw error;
  }
  return body;
}

/** §6 rule 11: an unavailable thing says what is missing, rather than a blank
 *  page or a spinner that never ends. */
export function failed(error) {
  const node = el(`
    <div class="wrap">
      <div class="empty">
        <p>${esc(error?.message || "Something went wrong reading your library.")}</p>
        <button class="btn" data-variant="secondary" data-retry>Try again</button>
      </div>
    </div>`);
  // Bound here, not as an attribute: the app is served under `script-src
  // 'self'` and an inline handler would be refused by its own CSP.
  node.querySelector("[data-retry]").addEventListener("click", () => {
    window.location.reload();
  });
  return node;
}

/* ---------------------------------------------------------------------- */
/* list furniture                                                          */
/* ---------------------------------------------------------------------- */

const TYPE_ICON = {
  youtube: "video", article: "sources", github: "doc",
  pdf: "doc", docx: "doc", text: "doc", image: "image",
  recording: "mic", user_note: "rules",
};

/** §7.7: "Never a platform name as the type." Only on the Sources list. */
const TYPE_WORD = {
  youtube: "Link", article: "Link", github: "Link",
  pdf: "Document", docx: "Document", text: "Document",
  image: "Image", recording: "Recording",
};

/** Everywhere but the Sources list, the meta leads with the platform or the
 *  format, which is what every capture shows (§18.4's Q-1). */
const FORMAT_WORD = {
  youtube: "YouTube", article: "Article", github: "GitHub",
  pdf: "PDF", docx: "DOCX", text: "Text", image: "Image", recording: "Recording",
};

export function duration(seconds) {
  if (!seconds) return "";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = s % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${m}:${String(rest).padStart(2, "0")}`;
}

export function stamp(ms) {
  if (ms === null || ms === undefined) return "";
  return duration(Math.round(ms / 1000)) || "0:00";
}

/** §6 rule 14: `Today`, `Yesterday`, then `October 26`. */
export function dateLabel(value, now = new Date()) {
  const when = new Date(String(value).replace(" ", "T") + (String(value).endsWith("Z") ? "" : "Z"));
  if (Number.isNaN(when.getTime())) return "Earlier";
  const days = Math.floor((startOfDay(now) - startOfDay(when)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return when.toLocaleDateString(undefined, { month: "long", day: "numeric" });
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** Group rows into §6 rule 14's date sections, newest first. */
export function byDate(rows, field = "created_at", prefix = "") {
  const groups = [];
  for (const row of rows) {
    const label = (prefix ? `${prefix} ` : "") + dateLabel(row[field]).toLowerCase();
    const title = prefix ? label.charAt(0).toUpperCase() + label.slice(1) : dateLabel(row[field]);
    const last = groups[groups.length - 1];
    if (last && last.title === title) last.rows.push(row);
    else groups.push({ title, rows: [row] });
  }
  return groups;
}

function metaHtml(parts) {
  return parts
    .filter(Boolean)
    .map((part) =>
      typeof part === "string"
        ? `<span>${esc(part)}</span>`
        : `<span class="${esc(part.cls)}">${esc(part.text)}</span>`
    )
    .join("");
}

/** §4.1's item card. One component; only the icon, the three metadata
 *  fragments and the pill change per surface. */
export function itemCard({ href, icon: glyph, title, meta, pill, actions = "" }) {
  return `
    <a class="item-card" href="${esc(href)}">
      <span class="item-type">${icon(glyph)}</span>
      <span class="item-text">
        <span class="item-title">${esc(title)}</span>
        <span class="item-meta">${metaHtml(meta)}</span>
      </span>
      ${pill ? `<span class="pill" data-state="${esc(pill.state)}">${esc(pill.label)}</span>` : ""}
      ${actions ? `<span class="item-actions">${actions}</span>` : ""}
    </a>`;
}

export function listFrame({ title, search, controlsHtml = "", groups, empty }) {
  if (!groups.length) {
    return el(`
      <div class="wrap">
        <h1 class="page-title">${esc(title)}</h1>
        <div class="toolbar">
          <span class="search">${slot("search")}
            <input class="input" placeholder="${esc(search)}" aria-label="${esc(search)}">
            <kbd class="kbd">Ctrl K</kbd>
          </span>${controlsHtml}
        </div>
        <div class="empty">
          <p>${esc(empty.text)}</p>
          <a class="btn" data-variant="primary" href="${esc(empty.href)}">${esc(empty.action)}</a>
        </div>
      </div>`);
  }
  return el(`
    <div class="wrap">
      <h1 class="page-title">${esc(title)}</h1>
      <div class="toolbar">
        <span class="search">${slot("search")}
          <input class="input" placeholder="${esc(search)}" aria-label="${esc(search)}">
          <kbd class="kbd">Ctrl K</kbd>
        </span>${controlsHtml}
      </div>
      ${groups
        .map(
          (group) => `
        <section class="date-group">
          <h2 class="date-label">${esc(group.title)}</h2>
          <div class="stack">${group.rows.join("")}</div>
        </section>`
        )
        .join("")}
    </div>`);
}

/* ---------------------------------------------------------------------- */
/* §7.4 TRANSCRIPTS, §7.6 SUMMARIES, §7.7 SOURCES                          */
/* ---------------------------------------------------------------------- */

FRAMES.transcripts = async function transcripts() {
  const { transcripts: rows } = await api("/api/transcripts");
  const groups = byDate(rows).map((group) => ({
    title: group.title,
    rows: group.rows.map((row) =>
      itemCard({
        href: `/transcripts/${row.id}`,
        icon: TYPE_ICON[row.source_type] || "doc",
        title: row.title,
        meta: [
          FORMAT_WORD[row.source_type] || row.source_type,
          duration(row.duration_sec),
          `${Number(row.word_count || 0).toLocaleString()} words`,
        ],
      })
    ),
  }));

  return listFrame({
    title: "Transcripts",
    search: "Search transcripts",
    controlsHtml: `<button class="btn" data-variant="secondary">${icon("filter")}Newest</button>`,
    groups,
    empty: {
      text: "No transcripts yet. Give Brain a video or a recording and the full text lands here, searchable and ready to copy.",
      action: "Teach Brain",
      href: "/",
    },
  });
};

FRAMES.summaries = async function summaries() {
  const { summaries: rows } = await api("/api/summaries");
  const groups = byDate(rows).map((group) => ({
    title: group.title,
    rows: group.rows.map((row) =>
      itemCard({
        href: `/summaries/${row.id}`,
        icon: "summaries",
        title: row.source_title,
        meta: [
          FORMAT_WORD[row.source_type] || row.source_type,
          duration(row.duration_sec),
          `${row.read_minutes} min read`,
        ],
      })
    ),
  }));

  return listFrame({
    title: "Summaries",
    search: "Search summaries",
    controlsHtml: `<button class="btn" data-variant="secondary">${icon("filter")}Newest</button>`,
    groups,
    empty: {
      text: "No summaries yet. Every source Brain reads gets one, so you never have to read the whole thing twice.",
      action: "Teach Brain",
      href: "/",
    },
  });
};

FRAMES.sources = async function sources() {
  const { sources: rows } = await api("/api/sources");
  const groups = byDate(rows).map((group) => ({
    title: group.title,
    rows: group.rows.map((row) => {
      // §7.7: the yield closes the meta line, in green. A source with nothing
      // green produced nothing — which is the most useful column on the page.
      const yielded = [];
      if (row.rule_count) yielded.push(`${row.rule_count} rule${row.rule_count > 1 ? "s" : ""}`);
      if (row.skill_count) yielded.push(`${row.skill_count} skill${row.skill_count > 1 ? "s" : ""}`);

      const host = (row.canonical_url || "").replace(/^https?:\/\//, "").split("/")[0];
      return itemCard({
        href: `/sources/${row.id}`,
        icon: TYPE_ICON[row.source_type] || "doc",
        title: row.title,
        meta: [
          TYPE_WORD[row.source_type] || "Document",
          host,
          duration(row.duration_sec),
          yielded.length ? { cls: "item-yield", text: yielded.join(" · ") } : null,
        ],
      });
    }),
  }));

  return listFrame({
    title: "Sources",
    search: "Search sources",
    controlsHtml: `<button class="btn" data-variant="secondary">${icon("filter")}Newest</button>
               <a class="btn" data-variant="primary" href="/">${icon("plus")}Teach Brain</a>`,
    groups,
    empty: {
      text: "Nothing here yet. Paste a link or drop a file and Brain reads it, keeps what matters, and hands it to every AI you work with.",
      action: "Teach Brain",
      href: "/",
    },
  });
};

/* ---------------------------------------------------------------------- */
/* §7.5 TRANSCRIPT, §7.6 SUMMARY — the reading surfaces                    */
/* ---------------------------------------------------------------------- */

function derivedStrip(items) {
  return `<div class="chip-row">${items
    .filter(Boolean)
    .map((item) => `<a class="chip" href="${esc(item.href)}">${icon(item.icon)}${esc(item.label)}</a>`)
    .join("")}</div>`;
}

FRAMES.transcript = async function transcript(params) {
  const data = await api(`/api/transcripts/${encodeURIComponent(params.id)}`);
  const source = data.source;

  // `R-1`: no speakers. The body is timestamped blocks, which is what the
  // ingestion pipeline actually records.
  const bodyHtml = data.turns
    .map(
      (turn) => `
      <div class="turn">
        <span class="turn-time">${
          turn.deep_link
            ? `<a href="${esc(turn.deep_link)}" target="_blank" rel="noopener">${esc(stamp(turn.t_ms))}</a>`
            : esc(turn.label || "")
        }</span>
        <p class="turn-text">${esc(turn.text)}</p>
      </div>`
    )
    .join("");

  return el(`
    <div class="wrap" data-read>
      <a class="action-link back" href="/library/transcripts">${icon("caret", "icon back-caret")}Transcripts</a>
      <h1 class="read-title">${esc(source.title)}</h1>
      <p class="read-meta">${metaHtml([
        FORMAT_WORD[source.source_type] || source.source_type,
        duration(source.duration_sec),
        `${Number(data.word_count || 0).toLocaleString()} words`,
        `added ${dateLabel(source.created_at).toLowerCase()}`,
      ])}</p>
      ${derivedStrip([
        data.has_summary && { href: `/sources/${source.id}`, icon: "summaries", label: "Summary" },
        { href: "/rules", icon: "rules", label: "Rules" },
        { href: "/skills", icon: "skills", label: "Skills" },
        source.canonical_url && { href: source.canonical_url, icon: "video", label: "Original source" },
      ])}
      <div class="read-bar">
        <span class="search">${slot("search")}
          <input class="input" id="in-page" placeholder="Search in this transcript"
                 aria-label="Search in this transcript">
        </span>
        <button class="btn" data-variant="secondary" id="copy">${icon("copy")}Copy</button>
        <button class="btn" data-variant="secondary">${icon("download")}Download</button>
      </div>
      <div class="transcript-body" id="body">${bodyHtml}</div>
    </div>`);
};

FRAMES.summary = async function summary(params) {
  const data = await api(`/api/summaries/${encodeURIComponent(params.id)}`);
  const { summary: record, source } = data;

  const pointsHtml = data.points
    .map(
      (point) => `
      <li class="point">
        <span class="point-text">${esc(point.text)}</span>
        <span class="point-time">${
          point.deep_link
            ? `<a href="${esc(point.deep_link)}" target="_blank" rel="noopener">${esc(stamp(point.t_ms))}</a>`
            : ""
        }</span>
      </li>`
    )
    .join("");

  return el(`
    <div class="wrap" data-read>
      <a class="action-link back" href="/library/summaries">${icon("caret", "icon back-caret")}Summaries</a>
      <h1 class="read-title">${esc(source.title || "")}</h1>
      <p class="read-meta">${metaHtml([
        FORMAT_WORD[source.source_type] || source.source_type,
        `${duration(source.duration_sec)} of video`,
        `${record.read_minutes} min read`,
        dateLabel(record.created_at).toLowerCase(),
      ])}</p>
      ${
        record.instruction
          ? `<div class="note-box">
               <span class="field-label">Summarized with your instruction</span>
               <p class="note-text">${esc(record.instruction)}</p>
             </div>`
          : ""
      }
      <div class="read-bar">
        <span class="segmented" role="group" aria-label="How much detail">
          <button class="segment" aria-pressed="true" data-view="brief">Brief</button>
          <button class="segment" aria-pressed="false" data-view="full">Full</button>
        </span>
        <span class="read-bar-spacer"></span>
        <button class="btn" data-variant="secondary">${icon("copy")}Copy</button>
        <button class="btn" data-variant="secondary">${icon("download")}Download</button>
      </div>
      <p class="gist">${esc(record.gist)}</p>
      <span class="field-label">Key points</span>
      <ol class="points">${pointsHtml}</ol>
    </div>`);
};

/* ---------------------------------------------------------------------- */
/* the router                                                              */
/* ---------------------------------------------------------------------- */

export function createRouter({ root, nav, announcer, context = {} } = {}) {
  const content = root ?? document.getElementById("content");
  const navRoot = nav ?? document.getElementById("sidebar");
  const live = announcer ?? document.getElementById("announcer");

  function syncNav(route) {
    if (!navRoot) return;
    for (const item of navRoot.querySelectorAll("[data-route]")) {
      const current = item.dataset.route === route?.nav;
      if (current) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
  }

  /* Static markup carries logical hrefs. Rewriting them once, in place, is
     what makes a hard reload and an open-in-new-tab land on the right page —
     intercepting the click alone would not. */
  function absolutise(scope) {
    for (const link of scope.querySelectorAll("a[href^='/']")) {
      const path = link.getAttribute("href");
      if (base() && path.startsWith(base())) continue;
      if (!matchRoute(path.split("?")[0])) continue;
      const [p, q] = path.split("?");
      link.setAttribute("href", href(p) + (q ? `?${q}` : ""));
    }
  }

  let generation = 0;

  async function render(pathname) {
    const hit = matchRoute(pathname);
    const route = hit?.route;
    const frame = FRAMES[route?.frame ?? "home"];

    // A frame that fetches can resolve after the person has already moved on.
    // The generation counter is what stops a slow page from painting itself
    // over the fast one that replaced it.
    const mine = ++generation;
    let node;
    try {
      node = await frame(hit?.params ?? {}, context);
    } catch (error) {
      node = failed(error);
    }
    if (mine !== generation) return route ?? null;

    content.replaceChildren();
    content.append(node);
    absolutise(content);
    syncNav(route);

    // The page changed under someone who cannot see it change.
    if (live) live.textContent = document.title;
    content.focus({ preventScroll: true });
    window.scrollTo(0, 0);
    refreshCounts();
    return route ?? null;
  }

  function go(href, { replace = false } = {}) {
    const url = new URL(href, window.location.origin);
    if (url.origin !== window.location.origin) return false;
    if (replace) window.history.replaceState({}, "", url);
    else window.history.pushState({}, "", url);
    render(url.pathname);
    return true;
  }

  /* A single delegated listener for every internal link, so a frame never
     wires its own navigation and nothing leaks when a frame is replaced. */
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
    const url = new URL(link.getAttribute("href"), window.location.origin);
    if (url.origin !== window.location.origin) return;
    if (!matchRoute(url.pathname)) return;
    event.preventDefault();
    go(url.href);
  });

  window.addEventListener("popstate", () => render(window.location.pathname));

  if (navRoot) absolutise(navRoot);
  if (document.querySelector(".topbar")) absolutise(document.querySelector(".topbar"));

  return { render, go, matchRoute, absolutise };
}

/* ---------------------------------------------------------------------- */
/* shell behaviour                                                         */
/* ---------------------------------------------------------------------- */

/** §5: the counts, and the one nav item that shows a queue instead.
 *
 *  Refreshed on every route change rather than once at boot: approving a
 *  skill or archiving a rule changes them, and a number that only updates on
 *  reload is a number nobody trusts.
 */
export async function refreshCounts() {
  let counts;
  try {
    ({ counts } = await api("/api/counts"));
  } catch {
    return; // A count is not worth an error state.
  }
  for (const node of document.querySelectorAll("[data-count]")) {
    const key = node.dataset.count;
    const value = counts[key] ?? 0;
    // §18.5: the amber pill REPLACES the count while the queue has something
    // in it, and becomes the plain total when it empties.
    if (key === "skills" && counts.needs_review) {
      node.textContent = String(counts.needs_review);
      node.dataset.alert = "true";
    } else {
      node.textContent = value ? String(value) : "";
      delete node.dataset.alert;
    }
  }
}

export function wireShell(router) {
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("nav-scrim");
  const menu = document.getElementById("menu-btn");

  function setNav(open) {
    if (!sidebar) return;
    sidebar.dataset.open = String(open);
    if (menu) menu.setAttribute("aria-expanded", String(open));
    if (scrim) scrim.hidden = !open;
  }

  if (menu) menu.addEventListener("click", () => setNav(sidebar.dataset.open !== "true"));
  if (scrim) scrim.addEventListener("click", () => setNav(false));

  // Navigating closes the folded nav; otherwise it covers the page it opened.
  document.addEventListener("click", (event) => {
    if (event.target.closest("#sidebar a")) setNav(false);
  });

  document.addEventListener("keydown", (event) => {
    // "Ctrl K focuses the omnibar from any page" (§7.1). It never types into
    // the field it is pressed in, so it is prevented before anything else.
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      const omni = document.getElementById("omni-input");
      if (omni) {
        event.preventDefault();
        omni.focus();
        omni.select?.();
      } else {
        event.preventDefault();
        router.go(href("/"));
        document.getElementById("omni-input")?.focus();
      }
      return;
    }
    if (event.key === "Escape") setNav(false);
  });

  // "drag onto the page → same as +; the whole content area is a drop target"
  const content = document.getElementById("content");
  if (content) {
    const over = (on) => (event) => {
      event.preventDefault();
      if (on) content.dataset.dropping = "true";
      else delete content.dataset.dropping;
    };
    content.addEventListener("dragover", over(true));
    content.addEventListener("dragleave", over(false));
    content.addEventListener("drop", over(false));
  }

  return { setNav };
}

/* ---------------------------------------------------------------------- */
/* boot                                                                    */
/* ---------------------------------------------------------------------- */

if (typeof document !== "undefined" && document.getElementById("content")) {
  const router = createRouter();
  wireShell(router);
  router.render(window.location.pathname).then(refreshCounts);
  window.addEventListener("popstate", refreshCounts);
}
