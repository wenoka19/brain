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

  function render(pathname) {
    const hit = matchRoute(pathname);
    const route = hit?.route;
    const frame = FRAMES[route?.frame ?? "home"];

    content.replaceChildren();
    content.append(frame(hit?.params ?? {}, context));
    absolutise(content);
    syncNav(route);

    // The page changed under someone who cannot see it change.
    if (live) live.textContent = document.title;
    content.focus({ preventScroll: true });
    window.scrollTo(0, 0);
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
  router.render(window.location.pathname);
}
