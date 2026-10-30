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

FRAMES.home = async function home(_params, ctx) {
  // The greeting reads a name, so it has to fetch one. §7.1 renders
  // "Good evening, Expedit." — reading an empty context produced
  // "Good evening, there." on every load, which is a greeting from nobody.
  let name = ctx?.user?.name ?? "";
  if (!name) {
    try {
      const profile = await api("/api/profile");
      name = profile.display_name || (profile.email || "").split("@")[0] || "there";
    } catch (error) {
      if (error?.reason === "needs_sign_in") throw error;
      name = "there";
    }
  }
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

        <p class="omni-status" id="omni-status" role="status" hidden></p>

        <input type="file" id="omni-file" class="sr-only"
               accept=".pdf,.docx,.txt,.md,image/png,image/jpeg,image/webp" multiple>
      </div>
    </div>`);

  const input = node.querySelector("#omni-input");
  const picker = node.querySelector("#omni-file");
  const status = node.querySelector("#omni-status");
  typewriter(input);

  function say(message, tone = "") {
    status.textContent = message;
    status.hidden = !message;
    status.dataset.tone = tone;
  }

  /** §7.1: "Submit creates a source, starts ingestion."
   *
   *  The whole purpose of the frame — "one job: get the first source in" —
   *  and the one thing it could not do. A link in the field is the source and
   *  anything else around it is the instruction, which is what makes "paste a
   *  link and tell it what you want" one action rather than two.
   */
  const URL_RE = /\bhttps?:\/\/\S+/i;

  async function submit() {
    const raw = input.value.trim();
    if (!raw) return;
    const match = raw.match(URL_RE);
    if (!match) {
      say("Paste a link, or drop a file — Brain reads sources, not questions, for now.");
      return;
    }
    const note = raw.replace(match[0], "").trim();
    input.disabled = true;
    say("Queued. Brain is reading it.");
    try {
      const queued = await post("/api/ingest", { url: match[0], user_note: note });
      input.value = "";
      input.disabled = false;
      refreshCounts();
      if (queued.duplicate) {
        // Navigating silently to the old one looks like it worked and then
        // shows a page with no work on it.
        say("You already have this one. Opening it.");
        setTimeout(() => window.location.assign(href(`/sources/${queued.source_id}`)), 900);
        return;
      }
      // §7.1: submit turns Home into the thread. Until §7.2 exists, the
      // source's own page is where the work is visible — and it is the page
      // §7.2 ends on anyway.
      window.location.assign(href(`/sources/${queued.source_id}`));
    } catch (error) {
      input.disabled = false;
      say(error.message, "error");
    }
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  /** A dropped or chosen file goes the same way, through `/api/upload`. */
  async function send(files) {
    const chosen = [...(files || [])];
    if (!chosen.length) return;
    say(`Reading ${chosen.length === 1 ? chosen[0].name : `${chosen.length} files`}…`);
    try {
      let last = null;
      for (const file of chosen) {
        const form = new FormData();
        form.append("file", file);
        if (input.value.trim()) form.append("user_note", input.value.trim());
        const response = await fetch("/api/upload", {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-Brain-Request": "1", "X-Brain-CSRF": csrfToken() },
          body: form,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.ok === false) {
          throw new Error(payload.message || `Could not read ${file.name}`);
        }
        last = payload;
      }
      refreshCounts();
      if (last?.source_id) window.location.assign(href(`/sources/${last.source_id}`));
      else say("Queued. Brain is reading it.");
    } catch (error) {
      say(error.message, "error");
    }
  }

  picker.addEventListener("change", () => send(picker.files));
  node.querySelector("#omni-add").addEventListener("click", () => picker.click());

  // "Teach Brain → focuses the omnibar and opens the file picker" (§7.1).
  node.querySelector('[data-action="teach"]').addEventListener("click", (event) => {
    event.preventDefault();
    input.focus();
    picker.click();
  });

  // §6 rule 11: an unavailable control says what is missing rather than
  // sitting there doing nothing. There is no speech-to-text behind this yet.
  node.querySelector("#omni-mic").addEventListener("click", () => {
    say("Recording is not wired up yet. Paste a link or drop a file instead.");
  });

  node.dataset.acceptsDrop = "true";
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
  if (response.status === 401 || response.status === 403) {
    // No session. §7.14 is the door, and a 401 rendered as a page error tells
    // the person nothing they can act on (§6 rule 11).
    const error = new Error("Sign in to open your library.");
    error.reason = "needs_sign_in";
    throw error;
  }
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

/** "2m 14s", the way §7.3 writes it. */
export function humanDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${m}m ${rest}s` : `${m}m`;
}

/** §18.4: a rule's locator reads `14:02`, not `842`.
 *
 *  The grounder stores whole seconds for a video and a bare page number for a
 *  document, so what it means depends on `locator_type` — printing the raw
 *  value made a timestamp look like a word count.
 */
export function locatorLabel(rule) {
  const value = rule.locator_value || "";
  if (!value) return "";
  if (rule.locator_type === "timestamp") {
    const seconds = Number(value);
    return Number.isFinite(seconds) ? duration(seconds) : value;
  }
  if (rule.locator_type === "page") return `p. ${value}`;
  return value;
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
/* §7.3 SOURCE DETAIL — the work block                                     */
/* ---------------------------------------------------------------------- */

/** §4.4. The marker is the state: a dot, a spinner, a check. A running step
 *  reveals nothing — that rule is enforced in the markup and not only in the
 *  data, so a future caller cannot leak a result early. */
function workStep(step) {
  // A failed step is not a finished one. Rendering `failed` as `done` turned
  // a blocked ingestion into a page that claimed it had worked.
  const settled = step.state === "done" || step.state === "skipped" || step.state === "failed";
  return `
    <li class="work-step" data-state="${esc(step.state)}">
      <span class="work-marker">${
        step.state === "done" || step.state === "skipped" ? icon("check") : ""
      }</span>
      <span>${esc(step.label)}</span>
      ${settled && step.detail ? `<span class="work-result">${esc(step.detail)}</span>` : ""}
    </li>`;
}

FRAMES.source = async function source(params) {
  const data = await api(`/api/sources/${encodeURIComponent(params.id)}`);
  const src = data.source;
  const progress = data.progress;
  const running = progress && !progress.finished;
  // §7.7's third state. `blocked_by_platform` is the plan's "Needs you": a
  // private post, a scanned PDF with no text layer, a rate-limited host —
  // Brain did not fail, Brain needs the person.
  const blocked = progress && ["failed", "blocked_by_platform"].includes(progress.status);

  const stepsHtml = progress ? progress.steps.map(workStep).join("") : "";
  // §7.3's header: "Working on it" while it runs, "Done in 2m 14s" after.
  const elapsed = blocked
    ? "Stopped"
    : progress && progress.finished
      ? progress.elapsed_sec !== null && progress.elapsed_sec !== undefined
        ? `Done in ${humanDuration(progress.elapsed_sec)}`
        : "Done"
      : "Working on it";

  // §18.4: the block collapses on Home and stays open here.
  const workHtml = progress
    ? `<div class="work">
         <div class="work-head">
           <span>${esc(elapsed)}</span>
           <span class="pill" data-state="archived">${esc(String(progress.steps.length))} steps</span>
         </div>
         <ul class="work-steps">${stepsHtml}</ul>
       </div>
       ${
         blocked
           ? `<p class="work-hint" data-tone="error">${esc(
               progress.error_message || "Brain could not read this one."
             )}</p>`
           : `<p class="work-hint">You can leave this page. Brain keeps working and
                Activity will show when it lands.</p>`
       }`
    : "";

  const rulesHtml = data.rules
    .map(
      (rule) => `
      <li class="yield-row">
        <span class="yield-text">${esc(rule.claim)}</span>
        <span class="yield-time">${
          rule.deep_link
            ? `<a href="${esc(rule.deep_link)}" target="_blank" rel="noopener">${esc(
                locatorLabel(rule)
              )}</a>`
            : esc(locatorLabel(rule))
        }</span>
      </li>`
    )
    .join("");

  const needingReview = data.skills.filter((s) => s.status === "proposed").length;
  const skillsHtml = data.skills
    .map(
      (skill) => `
      <li class="yield-row">
        <span class="yield-text">${esc(skill.name)}</span>
        ${
          skill.status === "proposed"
            ? `<button class="btn" data-size="sm" data-variant="primary"
                       data-approve="${esc(skill.id)}">Approve</button>`
            : ""
        }
      </li>`
    )
    .join("");

  const node = el(`
    <div class="wrap">
      <a class="action-link back" href="/library/sources">${icon("caret", "icon back-caret")}Sources</a>

      <header class="source-head">
        <span class="source-thumb">${icon(TYPE_ICON[src.source_type] || "doc")}</span>
        <div>
          <h1 class="read-title">${esc(src.title)}</h1>
          <p class="read-meta">${metaHtml([
            FORMAT_WORD[src.source_type] || src.source_type,
            duration(src.duration_sec),
            `added ${dateLabel(src.created_at).toLowerCase()}`,
          ])}</p>
          <span class="pill" data-state="${blocked || running ? "pending" : "active"}">${
            blocked ? "Needs you" : running ? "Processing" : "In memory"
          }</span>
        </div>
      </header>

      ${
        data.instruction
          ? `<div class="note-box">
               <span class="field-label">Your instruction</span>
               <p class="note-text">${esc(data.instruction)}</p>
             </div>`
          : ""
      }

      ${workHtml}

      ${data.summary || data.rules.length || data.skills.length ? `
      <section class="section">
        <div class="yield-head">
          <h2 class="section-title">What came out of it</h2>
          ${
            data.has_transcript
              ? `<a class="btn" data-variant="secondary" href="/transcripts/${esc(src.id)}">Open transcript</a>`
              : ""
          }
        </div>

        ${
          data.summary
            ? `<article class="yield-card">
                 <div class="yield-card-head">${slot("summaries")}<span>Summary</span></div>
                 <p class="yield-gist">${esc(data.summary.gist)}</p>
               </article>`
            : ""
        }

        ${
          data.rules.length
            ? `<article class="yield-card">
                 <div class="yield-card-head">${slot("rules")}<span>Rules</span>
                   <span class="pill" data-state="active">${esc(String(data.rules.length))} added</span>
                 </div>
                 <ul class="yield-list">${rulesHtml}</ul>
               </article>`
            : ""
        }

        ${
          data.skills.length
            ? `<article class="yield-card">
                 <div class="yield-card-head">${slot("skills")}<span>Skills</span>
                   ${
                     needingReview
                       ? `<span class="pill" data-state="pending">${esc(String(needingReview))} need review</span>`
                       : ""
                   }
                 </div>
                 <ul class="yield-list">${skillsHtml}</ul>
               </article>`
            : ""
        }
      </section>` : ""}
    </div>`);

  wireApprovals(node);
  return node;
};

/** Approve, wherever an Approve button appears. */
function wireApprovals(node) {
  for (const button of node.querySelectorAll("[data-approve]")) {
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      button.disabled = true;
      try {
        await post(`/api/skills/${encodeURIComponent(button.dataset.approve)}/review`, {
          decision: "approve",
        });
        button.replaceWith(el('<span class="pill" data-state="active">Approved</span>'));
        refreshCounts();
      } catch (error) {
        button.disabled = false;
        button.textContent = error.message.slice(0, 40);
      }
    });
  }
}

export async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Brain-Request": "1",
      "X-Brain-CSRF": csrfToken(),
    },
    body: JSON.stringify(body ?? {}),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.message || `Request failed (${response.status})`);
    error.reason = payload.reason || String(response.status);
    throw error;
  }
  return payload;
}

/** The double-submit token the API checks. Read from the cookie it was set
 *  in, which is what makes it a double submit rather than a second secret. */
function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)brain_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

/* ---------------------------------------------------------------------- */
/* §7.8 RULES, §7.9 SKILLS                                                 */
/* ---------------------------------------------------------------------- */

/** §4.2's expandable card. Collapsed shows one clamped line; the caret turns.
 *  `R-6`: the group heading above it carries no count and no dash. */
function expandable({ id, icon: glyph, title, sub, metaHtml: meta, bodyHtml, state = "" }) {
  return `
    <article class="exp-card" data-open="false"${state ? ` data-state="${esc(state)}"` : ""}>
      <button class="exp-head" type="button" data-toggle="${esc(id)}" aria-expanded="false">
        <!-- Points DOWN at rest: §4.2 rotates it -90 degrees when closed, so
             the base must be the open position or the two states swap. -->
        <span class="exp-caret slot">${icon("chevron-down")}</span>
        <span class="item-type">${icon(glyph)}</span>
        <span class="item-text">
          <span class="item-title">${esc(title)}</span>
          <span class="item-meta exp-summary">${meta ?? esc(sub)}</span>
        </span>
      </button>
      <div class="exp-body">${bodyHtml}</div>
    </article>`;
}

function wireExpanders(node) {
  for (const head of node.querySelectorAll("[data-toggle]")) {
    head.addEventListener("click", () => {
      const card = head.closest(".exp-card");
      const open = card.dataset.open === "true";
      card.dataset.open = String(!open);
      head.setAttribute("aria-expanded", String(!open));
    });
  }
}

FRAMES.rules = async function rules() {
  const data = await api("/api/rules");

  const card = (rule) => {
    const written = !rule.provenance.title || rule.source_id === "src_user_axioms";
    const origin = written ? "Written here" : rule.provenance.title;
    const detail = [rule.when_it_applies, rule.when_it_fails && `Not when ${rule.when_it_fails}`]
      .filter(Boolean)
      .join(" ");
    return expandable({
      id: rule.id,
      icon: "rules",
      title: rule.claim,
      sub: [origin, rule.provenance.locator].filter(Boolean).join(" · "),
      bodyHtml: `
        <p class="rule-full">${esc(detail || rule.claim)}</p>
        ${
          rule.anchor_quote
            ? `<blockquote class="rule-quote">${esc(rule.anchor_quote)}</blockquote>`
            : ""
        }
        <div class="chip-row">
          <span class="chip">${icon("sources")}${esc(origin)}</span>
          ${
            rule.provenance.deep_link
              ? `<a class="chip" href="${esc(rule.provenance.deep_link)}" target="_blank" rel="noopener">${icon("clock")}${esc(rule.provenance.locator)}</a>`
              : ""
          }
        </div>
        <div class="btn-row">
          <button class="btn" data-variant="secondary">Edit</button>
          <button class="btn" data-variant="ghost">Archive</button>
        </div>`,
    });
  };

  const section = (title, note, rows) =>
    rows.length
      ? `<section class="section">
           <div class="group-head">
             <h2 class="group-title">${esc(title)}</h2>
             <p class="group-note">${esc(note)}</p>
           </div>
           <div class="stack">${rows.map(card).join("")}</div>
         </section>`
      : "";

  const anything = data.mine.length + data.from_your_sources.length;
  const node = el(`
    <div class="wrap">
      <h1 class="page-title">Rules</h1>
      <div class="toolbar">
        <span class="search">${slot("search")}
          <input class="input" placeholder="Search rules" aria-label="Search rules">
          <kbd class="kbd">Ctrl K</kbd>
        </span>
        <a class="btn" data-variant="primary" href="/rules?create=1">${icon("plus")}Write a rule</a>
      </div>
      ${
        anything
          ? section("My rules", "Written by you, or pulled from your own AI chats.", data.mine) +
            section(
              "From your sources",
              "Extracted by Brain, traceable to where they were said.",
              data.from_your_sources
            )
          : `<div class="empty">
               <p>No rules yet. Tell Brain how you work, or give it something to
                  read, and the standing instructions it finds land here.</p>
               <a class="btn" data-variant="primary" href="/">Teach Brain</a>
             </div>`
      }
    </div>`);

  wireExpanders(node);
  return node;
};

FRAMES.skills = async function skills() {
  const data = await api("/api/skills");

  const card = (skill, kind) =>
    expandable({
      id: skill.id,
      icon: "skills",
      title: skill.name,
      sub: skill.description,
      state: kind === "proposed" ? "proposed" : kind === "inactive" ? "inactive" : "",
      bodyHtml: `
        <span class="field-label">When to use it</span>
        <p class="rule-full">${esc(skill.trigger_when || "—")}</p>
        <span class="field-label">What it does</span>
        <ol class="steps-list steps-plain">${(skill.execution_steps || [])
          .map((step) => `<li>${esc(step)}</li>`)
          .join("")}</ol>
        <div class="btn-row">
          ${
            kind === "proposed"
              ? `<button class="btn" data-variant="primary" data-approve="${esc(skill.id)}">Approve</button>
                 <button class="btn" data-variant="secondary">Edit</button>
                 <button class="btn" data-variant="ghost" data-dismiss="${esc(skill.id)}">Dismiss</button>`
              : `<button class="btn" data-variant="secondary">Edit</button>
                 <button class="icon-btn" aria-label="More">${icon("more")}</button>`
          }
        </div>`,
    });

  const section = (title, note, rows, kind) =>
    rows.length
      ? `<section class="section">
           <div class="group-head">
             <h2 class="group-title">${esc(title)}</h2>
             <p class="group-note">${esc(note)}</p>
           </div>
           <div class="stack">${rows.map((row) => card(row, kind)).join("")}</div>
         </section>`
      : "";

  const total = data.from_your_sources.length + data.active.length + data.inactive.length;
  const node = el(`
    <div class="wrap">
      <h1 class="page-title">My skills</h1>
      <div class="toolbar">
        <span class="search">${slot("search")}
          <input class="input" placeholder="Search skills" aria-label="Search skills">
          <kbd class="kbd">Ctrl K</kbd>
        </span>
        <a class="btn" data-variant="primary" href="/skills?create=1">${icon("plus")}Add a skill</a>
      </div>
      ${
        total
          ? section(
              "From your sources",
              "Drafted by Brain. Not in use until you approve them.",
              data.from_your_sources,
              "proposed"
            ) +
            section("Active", "Available to every connection.", data.active, "active") +
            section("Inactive", "Kept, but your AI cannot reach them.", data.inactive, "inactive")
          : `<div class="empty">
               <p>No skills yet. When Brain reads something that describes how to
                  do a thing, it drafts one here for you to approve.</p>
               <a class="btn" data-variant="primary" href="/">Teach Brain</a>
             </div>`
      }
    </div>`);

  wireExpanders(node);
  wireApprovals(node);
  for (const button of node.querySelectorAll("[data-dismiss]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      await post(`/api/skills/${encodeURIComponent(button.dataset.dismiss)}/review`, {
        decision: "dismiss",
      }).catch(() => {});
      button.closest(".exp-card").remove();
      refreshCounts();
    });
  }
  return node;
};

/* ---------------------------------------------------------------------- */
/* §7.12 ARCHIVE                                                           */
/* ---------------------------------------------------------------------- */

const ARCHIVE_ICON = { source: "sources", rule: "rules", skill: "skills", summary: "summaries" };

FRAMES.archive = async function archive() {
  const data = await api("/api/archive");

  const card = (item) =>
    expandable({
      id: item.id,
      icon: ARCHIVE_ICON[item.kind] || "doc",
      title: item.label,
      // §7.12: the attribution matters. "When something vanishes from the
      // library untouched, this page says who" — in amber when it was Brain.
      sub: "",
      // §7.12: "the attribution matters. When something vanishes from the
      // library untouched, this page says who" — amber when it was Brain.
      metaHtml: `
        <span data-kind="${esc(item.kind)}">${esc(
          item.kind.charAt(0).toUpperCase() + item.kind.slice(1)
        )}</span>
        <span class="${item.archived_by === "brain" ? "item-attribution" : ""}">archived by ${esc(
          item.archived_by === "brain" ? "Brain" : "you"
        )}</span>
        <span>added ${esc(dateLabel(item.created_at).toLowerCase())}</span>`,
      bodyHtml: `
        ${item.detail ? `<p class="rule-full">${esc(item.detail)}</p>` : ""}
        ${
          item.extra
            ? `<span class="field-label">Original link</span>
               <input class="input" readonly value="${esc(item.extra)}">`
            : ""
        }
        <div class="btn-row">
          <button class="btn" data-variant="secondary">Edit</button>
          <button class="btn" data-variant="ghost" data-delete="${esc(item.id)}">
            Delete permanently
          </button>
        </div>`,
    });

  // §7.12 orders by ARCHIVE date, not creation date: "people come here to
  // undo something they just did."
  const groups = byDate(data.items, "archived_at", "Archived").map((group) => ({
    title: group.title,
    rows: group.rows.map(card),
  }));

  const node = el(`
    <div class="wrap">
      <h1 class="page-title">Archive</h1>
      <p class="section-note">Out of the way, still yours. Nothing here is
         reachable by your AI until you restore it.</p>
      <div class="toolbar">
        <span class="search">${slot("search")}
          <input class="input" placeholder="Search the archive" aria-label="Search the archive">
        </span>
      </div>
      <div class="filter-row">
        <button class="filter-pill" aria-pressed="true" data-filter="all">All</button>
        ${data.types
          .map(
            (type) =>
              `<button class="filter-pill" aria-pressed="false" data-filter="${esc(type)}">${esc(
                type.charAt(0).toUpperCase() + type.slice(1)
              )}</button>`
          )
          .join("")}
      </div>
      ${
        groups.length
          ? groups
              .map(
                (group) => `
            <section class="date-group">
              <h2 class="date-label">${esc(group.title)}</h2>
              <div class="stack">${group.rows.join("")}</div>
            </section>`
              )
              .join("")
          : `<div class="empty">
               <p>Nothing is set aside. What you or Brain archive lands here,
                  out of the way and still yours.</p>
               <a class="btn" data-variant="secondary" href="/library/sources">Back to Sources</a>
             </div>`
      }
    </div>`);

  wireExpanders(node);

  // The filter exists here and nowhere else, because this is the only
  // heterogeneous list in the app (§7.12).
  for (const pill of node.querySelectorAll("[data-filter]")) {
    pill.addEventListener("click", () => {
      const wanted = pill.dataset.filter;
      for (const other of node.querySelectorAll("[data-filter]")) {
        other.setAttribute("aria-pressed", String(other === pill));
      }
      for (const card of node.querySelectorAll(".exp-card")) {
        const kind = card.querySelector("[data-kind]")?.dataset.kind || "";
        card.hidden = wanted !== "all" && kind !== wanted;
      }
      for (const group of node.querySelectorAll(".date-group")) {
        // §6 rule 13: a section with zero items hides itself, title included.
        group.hidden = ![...group.querySelectorAll(".exp-card")].some((c) => !c.hidden);
      }
    });
  }
  return node;
};

/* ---------------------------------------------------------------------- */
/* §7.13 PROFILE                                                           */
/* ---------------------------------------------------------------------- */

function settingRow({ label, help, controlHtml }) {
  return `
    <div class="setting-row">
      <span class="setting-text">
        <span class="setting-label">${esc(label)}</span>
        <span class="setting-help">${esc(help)}</span>
      </span>
      <span class="setting-control">${controlHtml}</span>
    </div>`;
}

function switchHtml(name, on) {
  return `<button class="switch" role="switch" aria-checked="${on ? "true" : "false"}"
                  data-switch="${esc(name)}" aria-label="${esc(name)}"></button>`;
}

FRAMES.profile = async function profile() {
  const data = await api("/api/profile");
  const wants = data.ingestion;

  const themes = ["light", "dark"]
    .map(
      (theme) => `
      <button class="theme-option" data-theme-pick="${esc(theme)}"
              aria-pressed="${data.theme_mode === theme ? "true" : "false"}">
        <span class="theme-swatch" data-swatch="${esc(theme)}"></span>
        <span>${esc(theme === "light" ? "Light" : "Dark")}</span>
      </button>`
    )
    .join("");

  const node = el(`
    <div class="wrap" data-narrow>
      <h1 class="page-title">Profile</h1>

      <section class="section">
        <h2 class="section-title">Account</h2>
        <p class="section-note">Who Brain thinks you are.</p>
        <div class="card-group">
          <div class="setting-row">
            <span class="avatar avatar-lg">${esc((data.email || "?")[0].toUpperCase())}</span>
            <span class="setting-text">
              <!-- The email twice is not an identity. With no name set, the
                   address is the identity and the help line says nothing. -->
              <span class="setting-label">${esc(data.display_name || data.email)}</span>
              <span class="setting-help">${esc(data.display_name ? data.email : "")}</span>
            </span>
          </div>
          ${settingRow({
            label: "Name",
            help: "What Brain calls you on the home screen.",
            controlHtml: `<input class="input" id="display-name" value="${esc(data.display_name)}"
                                 placeholder="Your name">`,
          })}
          ${settingRow({
            label: "Email",
            help: "Your library is keyed to this address.",
            controlHtml: `<input class="input" readonly value="${esc(data.email)}">`,
          })}
          ${settingRow({
            label: "Sign out",
            help: "Ends this session everywhere you are signed in.",
            controlHtml: `<button class="btn" data-variant="secondary" id="sign-out">Sign out</button>`,
          })}
        </div>
      </section>

      <section class="section">
        <h2 class="section-title">Appearance</h2>
        <p class="section-note">How Brain looks and which language it speaks to you in.</p>
        <div class="card-group">
          ${settingRow({
            label: "Theme",
            help: "Pick one, or let Brain follow the time of day.",
            controlHtml: `<span class="theme-row">${themes}</span>`,
          })}
          ${settingRow({
            label: "Language",
            help: "Brain speaks to you in this language.",
            controlHtml: `<select class="select" id="locale">
                <option value=""${data.locale ? "" : " selected"}>Follow my browser</option>
                <option value="en"${data.locale === "en" ? " selected" : ""}>English</option>
                <option value="fr"${data.locale === "fr" ? " selected" : ""}>Français</option>
              </select>`,
          })}
        </div>
      </section>

      <section class="section">
        <h2 class="section-title">What Brain does with new sources</h2>
        <p class="section-note">Without this, these choices get asked on every upload.</p>
        <div class="card-group">
          ${settingRow({
            label: "Write a summary",
            help: "Every source gets one, so you never have to read the whole thing.",
            controlHtml: switchHtml("want_summary", wants.want_summary),
          })}
          ${settingRow({
            label: "Extract rules",
            help: "Standing instructions Brain finds in what you feed it.",
            controlHtml: switchHtml("want_rules", wants.want_rules),
          })}
          ${settingRow({
            label: "Draft skills",
            help: "Procedures worth reusing. They wait for your approval either way.",
            controlHtml: switchHtml("want_skills", wants.want_skills),
          })}
          ${settingRow({
            label: "Write them in",
            help: "Summaries and rules come out in this language, whatever the source.",
            controlHtml: `<select class="select" id="write-in">
                <option value="source"${wants.write_in === "source" ? " selected" : ""}>The language of the source</option>
                <option value="en"${wants.write_in === "en" ? " selected" : ""}>English</option>
                <option value="fr"${wants.write_in === "fr" ? " selected" : ""}>Français</option>
              </select>`,
          })}
        </div>
      </section>

      <section class="section">
        <h2 class="section-title">Your data</h2>
        <p class="section-note">Everything Brain holds is yours to take or to remove.</p>
        <div class="card-group">
          ${settingRow({
            label: "Export everything",
            help: "Sources, transcripts, summaries, rules and skills, as Markdown in a zip.",
            controlHtml: `<a class="btn" data-variant="secondary" href="/api/export">Export</a>`,
          })}
          ${settingRow({
            label: "Delete my library",
            help: "Removes every source and everything made from it. Your account stays.",
            controlHtml: `<button class="btn" data-variant="danger">Delete library</button>`,
          })}
          ${settingRow({
            label: "Delete my account",
            help: "Closes the account and everything in it.",
            controlHtml: `<button class="btn" data-variant="danger">Delete account</button>`,
          })}
        </div>
      </section>
      <p class="save-note" id="save-note" hidden></p>
    </div>`);

  const note = node.querySelector("#save-note");
  async function save(fields) {
    try {
      await post("/api/profile", fields);
      note.textContent = "Saved.";
      note.hidden = false;
    } catch (error) {
      note.textContent = error.message;
      note.hidden = false;
    }
  }

  for (const toggle of node.querySelectorAll("[data-switch]")) {
    toggle.addEventListener("click", () => {
      const on = toggle.getAttribute("aria-checked") !== "true";
      toggle.setAttribute("aria-checked", String(on));
      save({ [toggle.dataset.switch]: on });
    });
  }
  for (const option of node.querySelectorAll("[data-theme-pick]")) {
    option.addEventListener("click", () => {
      const theme = option.dataset.themePick;
      document.documentElement.dataset.theme = theme;
      for (const other of node.querySelectorAll("[data-theme-pick]")) {
        other.setAttribute("aria-pressed", String(other === option));
      }
      save({ theme_mode: theme });
    });
  }
  node.querySelector("#display-name").addEventListener("change", (event) =>
    save({ display_name: event.target.value })
  );
  node.querySelector("#locale").addEventListener("change", (event) =>
    save({ locale: event.target.value })
  );
  node.querySelector("#write-in").addEventListener("change", (event) =>
    save({ write_in: event.target.value })
  );
  node.querySelector("#sign-out").addEventListener("click", async () => {
    await post("/api/logout", {}).catch(() => {});
    window.location.assign(href("/signin"));
  });
  return node;
};

/* ---------------------------------------------------------------------- */
/* §7.14 SIGN IN                                                           */
/* ---------------------------------------------------------------------- */

FRAMES.signin = function signin() {
  const node = el(`
    <div class="signin">
      <div class="signin-card">
        <span class="brand-mark"></span>
        <h1 class="signin-title">Sign in to Brain</h1>
        <p class="signin-lede">Your library is keyed to your email. The same address
           next time signs you back into the same library; a new one creates it.</p>
        <form class="signin-form">
          <label class="field-label" for="email">Email</label>
          <input class="input" id="email" type="email" name="email" required
                 autocomplete="email" placeholder="you@example.com">
          <button class="btn" data-variant="primary" type="submit">Continue</button>
          <p class="signin-error" id="signin-error" hidden></p>
        </form>
        <hr class="divider">
        <ul class="signin-promises">
          <li>${icon("check")}Lands you in a workspace with three editable demo items.</li>
          <li>${icon("check")}Connects to Claude, ChatGPT or an agent with one link.</li>
          <li>${icon("check")}Agents can archive, never permanently delete.</li>
        </ul>
      </div>
    </div>`);

  const form = node.querySelector(".signin-form");
  const error = node.querySelector("#signin-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    try {
      await post("/api/login", { email: node.querySelector("#email").value.trim() });
      window.location.assign(href("/"));
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    }
  });
  return node;
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
      if (error?.reason === "needs_sign_in" && route?.frame !== "signin") {
        // Not an error state: a door. Replace, so Back does not bounce the
        // person between the page they cannot see and the form.
        return go(href("/signin"), { replace: true });
      }
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

  /** Returns the render it started, so a caller can act once the new frame
   *  exists — `Ctrl K` from a page with no omnibar focused nothing, because
   *  it reached for the field before the frame that owns it had rendered. */
  function go(href, { replace = false } = {}) {
    const url = new URL(href, window.location.origin);
    if (url.origin !== window.location.origin) return Promise.resolve(false);
    if (replace) window.history.replaceState({}, "", url);
    else window.history.pushState({}, "", url);
    return render(url.pathname);
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
        // The omnibar does not exist on this page yet, and `render` is async,
        // so focusing before it resolves focuses nothing.
        event.preventDefault();
        Promise.resolve(router.go(href("/"))).then(() => {
          document.getElementById("omni-input")?.focus();
        });
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
    // §7.1: "drag onto the page → same as `+`". Clearing the highlight and
    // dropping the files on the floor is not the same as `+`.
    content.addEventListener("drop", (event) => {
      over(false)(event);
      const picker = document.getElementById("omni-file");
      const files = event.dataTransfer?.files;
      if (!picker || !files || !files.length) return;
      picker.files = files;
      picker.dispatchEvent(new Event("change"));
    });
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
