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
  // Kept, because §7.2 replaces the placeholder with `Reply to Brain…` and a
  // typewriter still cycling would overwrite it a second later.
  const cycling = typewriter(input);

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

  /** A line with no link goes to the command router.
   *
   *  `app/omnibar.py` has resolved targets, written confirmations and undone
   *  actions since Phase 4, and `/api/omnibar`, `/api/omnibar/run` and
   *  `/api/omnibar/undo` have been mounted the whole time. Nothing here
   *  called them: the bar answered "Brain reads sources, not questions" to
   *  everything without an `http`, under a placeholder cycling "Ask Brain
   *  anything…". Checkpoint B's hybrid bar — deterministic filtering while
   *  typing, a command on Enter — was built on the server and unreachable
   *  from the page.
   */
  async function command(raw) {
    say("Reading that…");
    let plan;
    try {
      ({ plan } = await post("/api/omnibar", { text: raw, submitted: true }));
    } catch (error) {
      say(error.message, "error");
      return;
    }

    if (plan.mode !== "command") {
      // `clarify` carries a question the router itself wrote, so it is shown.
      // `filter` is not shown at all: the router's "Showing matches" names a
      // results panel Home does not have, and writing a replacement sentence
      // here would be inventing product copy. The branch says nothing.
      say(plan.mode === "clarify" && plan.message ? plan.message : "");
      return;
    }

    // The target was inferred from a phrase, so the person confirms which
    // object it landed on before anything moves — this is about ambiguity,
    // not about danger.
    if (plan.requires_confirmation) {
      const verb = plan.action ? plan.action.replace(/_/g, " ") : "Run";
      const agreed = await confirmDialog({
        title: plan.confirm_text || "Run this?",
        // The title already names the match, so repeating it here says
        // nothing. What the person cannot see is whether this is a one-way
        // door.
        body: "Brain matched your words to this one. You can undo it afterwards.",
        confirmLabel: verb.charAt(0).toUpperCase() + verb.slice(1),
      });
      if (!agreed) {
        say("");
        return;
      }
    }

    let receipt;
    try {
      ({ receipt } = await post("/api/omnibar/run", { plan }));
    } catch (error) {
      say(error.message, "error");
      return;
    }
    input.value = "";
    say("");
    refreshCounts();
    toast(
      receipt.text,
      receipt.undo_action
        ? {
            action: receipt.undo_label || "Undo",
            onAction: async () => {
              try {
                await post("/api/omnibar/undo", {
                  action: receipt.undo_action,
                  params: receipt.undo_params,
                });
              } catch (error) {
                toast(error.message);
                return;
              }
              window.location.reload();
            },
          }
        : undefined
    );
  }

  async function submit() {
    const raw = input.value.trim();
    if (!raw) return;
    const match = raw.match(URL_RE);
    if (!match) {
      // Once §7.2's thread is open, a line without a link is an interjection
      // and the thread's own handler answers it.
      if (!node.querySelector("#thread")) await command(raw);
      return;
    }
    const note = raw.replace(match[0], "").trim();
    // §7.2's first hard requirement: "The composer is **never disabled**
    // while Brain works." It was disabled for the length of the request —
    // brief, but the same promise, and the thread below depends on it.
    say("Queued. Brain is reading it.");
    try {
      const queued = await post("/api/ingest", { url: match[0], user_note: note });
      input.value = "";
      refreshCounts();
      if (queued.duplicate) {
        // Navigating silently to the old one looks like it worked and then
        // shows a page with no work on it.
        say("You already have this one. Opening it.");
        setTimeout(() => window.location.assign(href(`/sources/${queued.source_id}`)), 900);
        return;
      }
      // §7.2: submit turns Home into the thread, in place. Navigating to the
      // source's page instead would be a page where the composer is gone —
      // and §7.2's purpose is that "ingestion is a conversation, not a
      // progress bar. The person must be able to interrupt while Brain
      // works."
      say("");
      openThread(node, {
        sourceId: queued.source_id,
        jobId: queued.job_id,
        label: match[0],
        instruction: note,
        cycling,
      });
    } catch (error) {
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
      if (last?.source_id) {
        say("");
        openThread(node, {
          sourceId: last.source_id,
          jobId: last.job_id,
          label: chosen.length === 1 ? chosen[0].name : `${chosen.length} files`,
          instruction: input.value.trim(),
          cycling,
        });
        input.value = "";
      } else say("Queued. Brain is reading it.");
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

/** `fetch`, but a dead network is a sentence rather than a rejection.
 *
 *  `fetch` rejects on a dropped connection, and that rejection travelled all
 *  the way out of `api()` and `post()` as an unhandled promise rejection —
 *  the page simply stopped, with "Failed to fetch" in a console nobody has
 *  open. §6 rule 11: a thing that is unavailable says what is missing.
 */
async function reach(path, init) {
  try {
    return await fetch(path, init);
  } catch (_) {
    const error = new Error("Brain could not be reached. Check your connection and try again.");
    error.reason = "offline";
    throw error;
  }
}

export async function api(path) {
  const response = await reach(path, {
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
/* §7.2 HOME — the thread state                                            */
/*                                                                          */
/* "Ingestion is a conversation, not a progress bar. The person must be      */
/* able to interrupt while Brain works."                                    */
/*                                                                          */
/* The thread lives in this page only. §7.2's own last requirement says so:  */
/* "Reopening Home later returns to the empty state. The thread is           */
/* reachable from the source it produced." So there is nothing to persist    */
/* and nothing to restore — the source's own page is the record.             */
/* ---------------------------------------------------------------------- */

function threadLine(who, html, { cls = "" } = {}) {
  return `<div class="turn-row" data-who="${esc(who)}"><div class="bubble ${esc(
    cls
  )}">${html}</div></div>`;
}

/** §7.2's result table: three rows, each a pill. */
function resultTable(data) {
  const rows = [
    data.summary && { label: "Summary", state: "active", pill: "Ready" },
    data.rules.length && {
      label: `${data.rules.length} rule${data.rules.length > 1 ? "s" : ""}`,
      state: "active",
      pill: "Added",
    },
    data.skills.length && {
      label: `${data.skills.length} skill${data.skills.length > 1 ? "s" : ""}`,
      // §7.2's table: amber when they are waiting on the person.
      state: data.skills.some((s) => s.status === "proposed") ? "pending" : "active",
      pill: data.skills.some((s) => s.status === "proposed") ? "Need review" : "Ready",
    },
  ].filter(Boolean);
  if (!rows.length) {
    // §6 rule 11 rather than an empty table: a source can yield nothing, and
    // the thread has to be able to say so.
    return `<p class="thread-note">Nothing came out of this one. The transcript is
      still there if you want to read it.</p>`;
  }
  return `<div class="result-table">${rows
    .map(
      (row) => `
      <div class="result-row">
        <span>${esc(row.label)}</span>
        <span class="pill" data-state="${esc(row.state)}">${esc(row.pill)}</span>
      </div>`
    )
    .join("")}</div>`;
}

/** Turn Home into §7.2's thread, in place.
 *
 *  `node` is the Home frame. The greeting, the pitch and the action links go;
 *  the thread takes the space and the composer stays exactly where it was,
 *  which is what makes the interruption possible at all.
 */
function openThread(node, { sourceId, jobId, label, instruction, cycling }) {
  const home = self_or_within(node, ".home");
  home.dataset.thread = "true";
  node.querySelector(".home-greeting").remove();
  node.querySelector(".home-pitch").remove();
  node.querySelector(".home-actions")?.remove();

  const thread = el('<div class="thread" id="thread" role="log" aria-live="polite"></div>');
  node.querySelector(".omnibar").before(thread);

  const input = node.querySelector("#omni-input");
  // Stopped first: the typewriter owns the placeholder, and §7.2's is an
  // instruction that has to hold still.
  cycling?.stop();
  input.placeholder = "Reply to Brain…";

  const add = (html, who = "brain", options) => {
    thread.insertAdjacentHTML("beforeend", threadLine(who, html, options));
    thread.scrollTop = thread.scrollHeight;
    return thread.lastElementChild;
  };

  add(
    `<span class="source-chip">${icon("sources")}<span>${esc(label)}</span></span>${
      instruction ? `<p class="thread-said">${esc(instruction)}</p>` : ""
    }`,
    "you"
  );
  add(
    instruction
      ? `On it. I'll read the whole thing and keep what matches what you asked for.`
      : `On it. I'll read the whole thing and keep what matters.`
  );

  const work = add(
    `<div class="work" id="thread-work">
       <div class="work-head"><span>Working on it</span>
         <span class="pill" data-state="archived">6 steps</span></div>
       <ul class="work-steps"></ul>
     </div>`,
    "brain",
    // The work block draws its own card, so the bubble must not draw a
    // second one around it.
    { cls: "bubble-bare" }
  );

  // One line, not three: a template literal keeps its own indentation, and
  // the hint rendered with it baked in.
  const hint = el(
    '<p class="thread-hint" id="thread-hint">Brain is working. You can keep typing,' +
      ' or leave — Activity will show when it lands.</p>'
  );
  node.querySelector(".omnibar").after(hint);

  // §7.2: an interjection goes to the job, and the reply says what it will
  // actually reach. The composer is never taken away to do it.
  const interject = async (text) => {
    add(`<p class="thread-said">${esc(text)}</p>`, "you");
    try {
      const sent = await post(`/api/sources/${encodeURIComponent(sourceId)}/instruct`, { text });
      const reach = sent.will_affect || [];
      add(
        `Noted — I'll take that in. ${
          reach.length
            ? `Still on ${esc(reach[0].toLowerCase())}, so nothing is lost.`
            : "It applies from here."
        }`
      );
    } catch (error) {
      // §6 rule 11: a reassurance Brain cannot keep is worse than the truth.
      add(esc(error.message || "I could not take that in."), "brain", { cls: "bubble-error" });
    }
  };

  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    const text = input.value.trim();
    // A second link is a second source, which the frame's own submit handles.
    if (!text || /\bhttps?:\/\/\S+/i.test(text)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    input.value = "";
    interject(text);
  });

  watchThread({ node, sourceId, work, hint, add });
}

/** The work block in the thread, from real step rows — the same `/job` read
 *  §7.3 polls, because it is the same block. */
function watchThread({ node, sourceId, work, hint, add }) {
  const steps = work.querySelector(".work-steps");
  const header = work.querySelector(".work-head span");

  const tick = async () => {
    if (!node.isConnected) return;
    let progress;
    try {
      progress = await api(`/api/sources/${encodeURIComponent(sourceId)}/job`);
    } catch {
      setTimeout(tick, 4000);
      return;
    }
    steps.innerHTML = progress.steps.map(workStep).join("");

    const dead = ["failed", "blocked_by_platform"].includes(progress.status);
    if (!progress.finished && !dead) {
      setTimeout(tick, 1500);
      return;
    }

    // §7.2: "work block collapses → `Processed in 2m 14s · 6 steps`".
    work.querySelector(".work").dataset.collapsed = "true";
    header.textContent = dead
      ? "Stopped"
      : progress.elapsed_sec
        ? `Processed in ${humanDuration(progress.elapsed_sec)} · ${progress.steps.length} steps`
        // Under a second. "Processed in 0s" is accurate and says nothing.
        : `Processed · ${progress.steps.length} steps`;
    hint.remove();

    if (dead) {
      add(
        `${esc(progress.error_message || "I could not read that one.")}
         <div class="btn-row">
           <a class="btn" data-variant="secondary" href="/sources/${esc(
             sourceId
           )}">See what happened</a>
         </div>`,
        "brain",
        { cls: "bubble-error" }
      );
      return;
    }

    const data = await api(`/api/sources/${encodeURIComponent(sourceId)}`);
    const needsReview = data.skills.filter((skill) => skill.status === "proposed").length;
    add(
      `${resultTable(data)}
       <div class="btn-row">
         <a class="btn" data-variant="secondary" href="/sources/${esc(
           sourceId
         )}">Open the source</a>
         ${
           needsReview
             ? `<a class="btn" data-variant="primary" href="/skills">Review the skills</a>`
             : ""
         }
       </div>`,
      "brain",
      { cls: "bubble-wide" }
    );

    // §7.2's two suggestion chips. Both are things this source makes possible
    // right now, not generic prompts.
    add(
      `<div class="chip-row">
         ${
           data.has_transcript
             ? `<a class="chip" href="/transcripts/${esc(sourceId)}">${icon(
                 "transcripts"
               )}Read the transcript</a>`
             : ""
         }
         ${
           data.rules.length
             ? `<a class="chip" href="/rules">${icon("rules")}See the rules it added</a>`
             : `<a class="chip" href="/">${icon("plus")}Teach Brain something else</a>`
         }
       </div>`,
      "brain",
      { cls: "bubble-bare" }
    );
    refreshCounts();
  };
  setTimeout(tick, 600);
}

/* ---------------------------------------------------------------------- */
/* list furniture                                                          */
/* ---------------------------------------------------------------------- */

const TYPE_ICON = {
  youtube: "video", article: "sources", github: "doc",
  pdf: "doc", docx: "doc", text: "doc", image: "image",
  recording: "mic", user_note: "rules",
};

/** §7.7's `Fix this` tooltip: "whose tooltip states the remedy". Two of these
 *  are the plan's own words — "Make it public, then retry" and "No text layer
 *  — retype or re-scan" — and the rest follow the same shape: what the person
 *  does, not what went wrong. "Brain did not fail; Brain needs the person."
 *
 *  Keyed on the provider's reason code rather than its message, because the
 *  message is a sentence written for the person and matching on prose is how
 *  a tooltip silently becomes wrong. */
const REMEDY = {
  blocked_by_platform: "Make it public, then retry",
  platform_blocked_caller: "YouTube refused this server, not the video — paste the transcript instead",
  blocked_url: "That address is not one Brain can reach — check it, then retry",
  no_transcript: "No captions on this one — add them, or paste the text instead",
  no_text_layer: "No text layer — retype or re-scan",
  corrupt_file: "Brain could not open the file — re-export it and try again",
  empty_content: "Nothing readable was there — paste the text instead",
  fetch_failed: "The source did not answer — retry",
  timeout: "It took too long to read — retry, or paste the text instead",
  unsupported_content_type: "Brain does not read this kind of link yet — paste the text instead",
  unsupported_file: "Brain does not read this kind of file yet — paste the text instead",
  image_too_large: "That image is over the size cap — shrink it and try again",
  tool_missing: "Brain is missing a tool for this type. Nothing you can fix",
  js_runtime_missing: "The server is missing a tool YouTube needs. Nothing you can fix",
  vision_unavailable: "Reading images is not switched on for this Brain. Nothing you can fix",

  /* The model host, not the source. These reach the person through the same
     envelope — writing a rule embeds it — and every one of them used to land
     on `default`, which tells someone whose API key is wrong to go and make
     their own sentence reachable. */
  bad_api_key: "The model host rejected the key — check LLM_API_KEY, then retry",
  model_forbidden: "That key is not allowed to use that model — check the plan, or LLM_MODEL",
  model_not_found: "No such model at that host — check LLM_MODEL and LLM_BASE_URL",
  model_unreachable: "The model host did not answer — retry in a moment",
  model_error: "The model host failed on this one — retry",
  embedding_error: "The embedding model answered wrongly — check the embedding settings, then retry",
  rate_limited: "The model host is rate-limiting this key — wait a moment, then retry",

  default: "Make it reachable, then retry",
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
        // §7.4: "hover actions `Copy`, `Download`, `⋯`", and behind the `⋯`,
        // Rename, View the source, Generate a summary, Archive. No Delete
        // here — §7.4 says so outright, and §6 rule 8 says why.
        actions:
          cardButton({ act: "copy", id: row.id, glyph: "copy" }) +
          cardButton({ act: "download", id: row.id, glyph: "download" }) +
          cardMenu(row.id, [
            { act: "rename", label: "Rename" },
            row.canonical_url && { act: "visit", label: "View the source" },
            // §6 rule 11: the item is offered only when it can do something.
            // A source that already has a summary has nothing to generate.
            !row.summary_count && { act: "summarise", label: "Generate a summary" },
            { sep: true },
            { act: "archive", label: "Archive" },
          ]),
      })
    ),
  }));

  const node = listFrame({
    title: "Transcripts",
    search: "Search transcripts",
    controlsHtml: sortButton(),
    groups,
    empty: {
      text: "No transcripts yet. Give Brain a video or a recording and the full text lands here, searchable and ready to copy.",
      action: "Teach Brain",
      href: "/",
    },
  });
  wireListSearch(node);
  wireSort(node);
  wireMenus(node);

  const lookup = new Map(rows.map((row) => [row.id, row]));
  const text = async (id) => {
    const data = await api(`/api/transcripts/${encodeURIComponent(id)}`);
    return data.turns
      .map((turn) => (turn.t_ms !== null ? `[${stamp(turn.t_ms)}] ${turn.text}` : turn.text))
      .join("\n\n");
  };

  wireCardActions(node, {
    copy: async (id, button) => copyText(button, await text(id)),
    download: async (id) =>
      download(fileName(lookup.get(id)?.title, "txt"), await text(id)),
    visit: (id) => window.open(lookup.get(id).canonical_url, "_blank", "noopener"),
    rename: (id, button) => renameRow(id, button, lookup),
    summarise: async (id, button) => {
      try {
        await post(`/api/sources/${encodeURIComponent(id)}/summarise`, {});
      } catch (error) {
        toast(error.message || "Could not write a summary.");
        return;
      }
      refreshCounts();
      toast("Summary written.", {
        action: "Read it",
        onAction: () => window.location.assign(href(`/sources/${id}`)),
      });
      button.closest(".menu")?.querySelector('[data-act="summarise"]')?.remove();
    },
    archive: (id, button) => archiveRow("source", id, button, node),
  });
  return node;
};

/** §7.4's toolbar carries a `Newest` sort. One control, two orders — a button
 *  that says `Newest` and does nothing is §6 rule 11's exact complaint. */
function sortButton() {
  return `<button class="btn" data-variant="secondary" data-sort="desc">${icon(
    "filter"
  )}Newest</button>`;
}

function wireSort(node) {
  const button = node.querySelector("[data-sort]");
  if (!button) return;
  button.addEventListener("click", () => {
    const newest = button.dataset.sort === "desc";
    button.dataset.sort = newest ? "asc" : "desc";
    button.innerHTML = `${icon("filter")}${newest ? "Oldest" : "Newest"}`;
    // §6 rule 14 groups by date, so reversing the rows means reversing the
    // groups as well as the rows inside them.
    // `listFrame` returns the `.wrap` itself, so the frame node IS the
    // container — querying inside it for one found nothing and the reorder
    // threw halfway through, leaving the rows reversed and the groups not.
    const wrap = self_or_within(node, ".wrap");
    const groups = [...node.querySelectorAll(".date-group")].reverse();
    for (const group of groups) {
      const stack = group.querySelector(".stack");
      for (const row of [...stack.children].reverse()) stack.append(row);
      wrap.append(group);
    }
  });
}

/** §7.4's `Rename`, inline rather than in a dialog: the title is already on
 *  screen, and a dialog to change one line of text is a dialog too many. */
async function renameRow(id, button, lookup) {
  const card = button.closest(".item-card");
  const title = card.querySelector(".item-title");
  const was = title.textContent;
  const field = el(
    `<input class="input item-rename" value="${esc(was)}" aria-label="Rename">`
  );
  title.replaceWith(field);
  field.focus();
  field.select();

  // Enter and blur both finish, and Enter causes a blur — so without this the
  // second call tried to replace a node that was already gone.
  let closed = false;
  const finish = async (save) => {
    if (closed) return;
    closed = true;
    const wanted = field.value.trim();
    field.replaceWith(title);
    if (!save || !wanted || wanted === was) return;
    try {
      await post(`/api/sources/${encodeURIComponent(id)}/rename`, { title: wanted });
    } catch (error) {
      toast(error.message || "Could not rename it.");
      return;
    }
    title.textContent = wanted;
    const row = lookup?.get(id);
    if (row) row.title = wanted;
  };

  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
}

/** Archive from a list, with §9.4's Undo toast and no dialog: archiving is
 *  reversible, which is the whole reason §7.12 exists. */
async function archiveRow(kind, id, button, node) {
  const card = button.closest(".item-card, .exp-card");
  try {
    await post(`/api/archive/${kind}/${encodeURIComponent(id)}`, {});
  } catch (error) {
    toast(error.message || "Could not archive it.");
    return;
  }
  card.remove();
  hideEmptiedGroups(node);
  refreshCounts();
  toast("Archived.", {
    action: "Undo",
    onAction: async () => {
      try {
        await post(`/api/archive/${kind}/${encodeURIComponent(id)}`, { restore: true });
      } catch {
        toast("Could not put it back.");
        return;
      }
      window.location.reload();
    },
  });
}

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

  const node = listFrame({
    title: "Summaries",
    search: "Search summaries",
    controlsHtml: sortButton(),
    groups,
    empty: {
      text: "No summaries yet. Every source Brain reads gets one, so you never have to read the whole thing twice.",
      action: "Teach Brain",
      href: "/",
    },
  });
  wireListSearch(node);
  wireSort(node);
  return node;
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

      // §7.7's three states. The data was in the payload and the frame was
      // throwing it away, so a queued source and a blocked one looked
      // identical to one sitting happily in memory.
      const pill =
        row.status === "processing"
          ? { state: "pending", label: "Processing" }
          : row.status === "blocked"
            ? { state: "pending", label: "Needs you" }
            : null;

      // §7.7: "then what matters for that type" — a domain and a duration for
      // a link, a format and a page count for a document, dimensions for an
      // image. One meta line for every type was the plan's "never a platform
      // name" rule kept and its other half dropped.
      const kind = TYPE_WORD[row.source_type] || "Document";
      const specifics =
        kind === "Link"
          ? [host, duration(row.duration_sec)]
          : kind === "Document"
            ? [FORMAT_WORD[row.source_type] || "", row.word_count ? `${Number(row.word_count).toLocaleString()} words` : ""]
            : [row.word_count ? `${Number(row.word_count).toLocaleString()} words` : ""];

      return itemCard({
        href: `/sources/${row.id}`,
        icon: TYPE_ICON[row.source_type] || "doc",
        title: row.title,
        meta: [
          kind,
          ...specifics,
          yielded.length ? { cls: "item-yield", text: yielded.join(" · ") } : null,
        ],
        pill,
        // §7.7: a blocked row carries a `Fix this` button, and its tooltip
        // states the remedy rather than restating the problem.
        actions:
          (row.status === "blocked"
            ? cardButton({
                act: "fix",
                id: row.id,
                label: "Fix this",
                title: REMEDY[row.block_reason] || REMEDY.default,
              })
            : "") +
          cardMenu(row.id, [
            { act: "rename", label: "Rename" },
            // §7.3's menu, on the row it belongs to. Only a source with an
            // address can be read again, so the item appears only then —
            // §6 rule 11 rather than a menu item that answers with a refusal.
            row.canonical_url && { act: "reprocess", label: "Re-process" },
            { sep: true },
            { act: "archive", label: "Archive" },
          ]),
      });
    }),
  }));

  const lookup = new Map(rows.map((row) => [row.id, row]));

  const node = listFrame({
    title: "Sources",
    search: "Search sources",
    controlsHtml: `${sortButton()}
               <a class="btn" data-variant="primary" href="/">${icon("plus")}Teach Brain</a>`,
    groups,
    empty: {
      text: "Nothing here yet. Paste a link or drop a file and Brain reads it, keeps what matters, and hands it to every AI you work with.",
      action: "Teach Brain",
      href: "/",
    },
  });

  wireListSearch(node);
  wireSort(node);
  wireMenus(node);

  /* §7.7: "`Needs you` ... carries a **Fix this** button whose tooltip states
     the remedy". The tooltip states it; the button does the retry, which is
     the only thing left to do once the person has made the source reachable.
     Before `op_reprocess_source` existed this button could only say the
     remedy and then ask the person to go and find the retry themselves. */
  wireCardActions(node, {
    fix: async (id, button) => {
      try {
        await post(`/api/sources/${encodeURIComponent(id)}/reprocess`, {});
      } catch (error) {
        answer(button, error.message || "Still cannot read it");
        return;
      }
      const card = button.closest(".item-card");
      card.querySelector(".pill").textContent = "Processing";
      button.remove();
      toast("Reading it again.", {
        action: "Watch it",
        onAction: () => window.location.assign(href(`/sources/${id}`)),
      });
    },
    rename: (id, button) => renameRow(id, button, lookup),
    archive: (id, button) => archiveRow("source", id, button, node),
    reprocess: async (id, button) => {
      try {
        await post(`/api/sources/${encodeURIComponent(id)}/reprocess`, {});
      } catch (error) {
        toast(error.message || "Could not read it again.");
        return;
      }
      toast("Reading it again.", {
        action: "Watch it",
        onAction: () => window.location.assign(href(`/sources/${id}`)),
      });
    },
  });
  return node;
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

  const node = el(`
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
        // The chip is named Summary and went to the source page. §7.5's strip
        // links what this transcript produced, so it goes to the summary.
        data.summary_id && {
          href: `/summaries/${data.summary_id}`, icon: "summaries", label: "Summary",
        },
        // Counts, and only when there are any: a chip reading "Rules" that
        // lands on an unfiltered library is a link, not a derived strip.
        data.rule_count && {
          href: `/sources/${source.id}`, icon: "rules",
          label: `${data.rule_count} rule${data.rule_count > 1 ? "s" : ""}`,
        },
        data.skill_count && {
          href: `/sources/${source.id}`, icon: "skills",
          label: `${data.skill_count} skill${data.skill_count > 1 ? "s" : ""}`,
        },
        source.canonical_url && { href: source.canonical_url, icon: "video", label: "Original source" },
      ])}
      <div class="read-bar">
        <span class="search">${slot("search")}
          <input class="input" id="in-page" placeholder="Search in this transcript"
                 aria-label="Search in this transcript">
          <span class="match-count" id="matches" hidden></span>
        </span>
        <button class="btn" data-variant="secondary" id="copy">${icon("copy")}Copy</button>
        ${downloadMenu("tx-download", ["txt", "md", "srt"])}
      </div>
      <div class="transcript-body" id="body">${bodyHtml}</div>
    </div>`);

  wireReadBar(node);
  wireMenus(node);

  const plain = data.turns
    .map((turn) => (turn.t_ms !== null ? `[${stamp(turn.t_ms)}] ${turn.text}` : turn.text))
    .join("\n\n");

  node.querySelector("#copy").addEventListener("click", (event) =>
    copyText(event.currentTarget, plain)
  );

  for (const item of node.querySelectorAll("#tx-download .menu-item")) {
    item.addEventListener("click", () => {
      const format = item.dataset.format;
      const title = source.title;
      if (format === "txt") download(fileName(title, "txt"), plain);
      else if (format === "md") {
        download(
          fileName(title, "md"),
          `# ${title}\n\n` +
            data.turns
              .map((turn) =>
                turn.t_ms !== null
                  ? `**${stamp(turn.t_ms)}** — ${turn.text}`
                  : turn.text
              )
              .join("\n\n")
        );
      } else download(fileName(title, "srt"), toSrt(data.turns), "application/x-subrip");
      node.querySelector("#tx-download").hidden = true;
    });
  }

  // §7.5: "a timestamp opens the source at that second".
  return node;
};

/** SubRip, from the turns we already have. A transcript with no moments
 *  cannot become subtitles, and says so rather than writing an empty file. */
function toSrt(turns) {
  const timed = turns.filter((turn) => turn.t_ms !== null);
  if (!timed.length) return "This source has no timeline, so it has no subtitles.\n";
  const clock = (ms) => {
    const total = Math.max(0, Math.round(ms / 1000));
    const h = String(Math.floor(total / 3600)).padStart(2, "0");
    const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const sec = String(total % 60).padStart(2, "0");
    return `${h}:${m}:${sec},000`;
  };
  return timed
    .map((turn, index) => {
      const next = timed[index + 1];
      const end = next ? next.t_ms : turn.t_ms + 4000;
      return `${index + 1}\n${clock(turn.t_ms)} --> ${clock(end)}\n${turn.text}\n`;
    })
    .join("\n");
}

/** §7.5's in-page search: a live `<mark>` highlight and a match count.
 *
 *  Rebuilt from the text each time rather than patched: a highlight applied
 *  over a previous highlight is how a search box starts eating its own
 *  markup.
 */
function wireReadBar(node) {
  const field = node.querySelector("#in-page");
  const count = node.querySelector("#matches");
  const bar = node.querySelector(".read-bar");
  if (!field) return;

  const paragraphs = [...node.querySelectorAll(".turn-text, .point-text")];
  const originals = paragraphs.map((p) => p.textContent);

  field.addEventListener("input", () => {
    const needle = field.value.trim();
    let hits = 0;
    paragraphs.forEach((paragraph, index) => {
      const text = originals[index];
      if (!needle) {
        paragraph.textContent = text;
        return;
      }
      const parts = text.split(new RegExp(`(${escapeRe(needle)})`, "ig"));
      paragraph.replaceChildren(
        ...parts.map((part, i) => {
          if (i % 2 === 0) return document.createTextNode(part);
          hits += 1;
          const mark = document.createElement("mark");
          mark.textContent = part;
          return mark;
        })
      );
    });
    count.hidden = !needle;
    count.textContent = needle ? (hits ? `${hits} match${hits > 1 ? "es" : ""}` : "none") : "";
  });

  // §7.5: "The bar gains its bottom border only once stuck."
  const sentinel = el('<span class="stick-sentinel"></span>');
  bar.before(sentinel);
  new IntersectionObserver(
    ([entry]) => {
      bar.dataset.stuck = String(!entry.isIntersecting);
    },
    { threshold: 1 }
  ).observe(sentinel);
}

function escapeRe(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

FRAMES.summary = async function summary(params) {
  const data = await api(`/api/summaries/${encodeURIComponent(params.id)}`);
  const { summary: record, source } = data;

  // A point's time cell is conditioned on having a MOMENT, not on having an
  // off-site link: a document has moments and no URL, and conditioning on the
  // link meant every non-video summary lost all its timestamps.
  const pointHtml = (point) => `
      <li class="point">
        <span class="point-text">${esc(point.text)}</span>
        <span class="point-time">${
          point.t_ms === null || point.t_ms === undefined
            ? ""
            : point.deep_link
              ? `<a href="${esc(point.deep_link)}" target="_blank" rel="noopener">${esc(stamp(point.t_ms))}</a>`
              : esc(stamp(point.t_ms))
        }</span>
      </li>`;

  const pointsHtml = data.points.map(pointHtml).join("");

  const chaptersHtml = (record.chapters || [])
    .map(
      (chapter) => `
      <section class="chapter">
        <h3 class="chapter-title">${esc(chapter.title)}</h3>
        <span class="chapter-range">${esc(stamp(chapter.from_ms))} – ${esc(stamp(chapter.to_ms))}</span>
        <p class="chapter-text">${esc(chapter.text)}</p>
      </section>`
    )
    .join("");

  const node = el(`
    <div class="wrap" data-read>
      <a class="action-link back" href="/library/summaries">${icon("caret", "icon back-caret")}Summaries</a>
      <h1 class="read-title">${esc(source.title || "")}</h1>
      <p class="read-meta">${metaHtml([
        FORMAT_WORD[source.source_type] || source.source_type,
        // "42:18 of video" for a PDF, and " of video" when there is no
        // duration at all — the fragment is dropped unless there is one.
        source.duration_sec ? `${duration(source.duration_sec)} of video` : null,
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
        <button class="btn" data-variant="secondary" id="sum-copy">${icon("copy")}Copy</button>
        ${downloadMenu("sum-download", ["txt", "md"])}
      </div>
      <p class="gist">${esc(record.gist)}</p>
      <span class="field-label">Key points</span>
      <ol class="points" id="points">${pointsHtml}</ol>
      <div class="chapters" id="chapters" hidden>
        <span class="field-label">Chapter by chapter</span>
        ${chaptersHtml || "<p class=\"section-note\">This source has no timeline, so it has no chapters.</p>"}
      </div>
    </div>`);

  wireMenus(node);

  /* §7.6: "The read time in the meta updates with the control — the control
     must change something real." Brief is the gist and the first five points;
     Full adds the rest and the chapter walkthrough. Both readings come from
     the same stored record, so switching re-renders rather than re-fetches. */
  const BRIEF_POINTS = 5;
  const list = node.querySelector("#points");
  const chapters = node.querySelector("#chapters");
  const readCell = [...node.querySelectorAll(".read-meta span")].find((cell) =>
    cell.textContent.includes("min read")
  );

  function show(view) {
    const points = view === "brief" ? data.points.slice(0, BRIEF_POINTS) : data.points;
    list.innerHTML = points.map(pointHtml).join("");
    chapters.hidden = view === "brief" || !(record.chapters || []).length;

    const words =
      record.gist.split(/\s+/).length +
      points.reduce((n, point) => n + String(point.text).split(/\s+/).length, 0) +
      (view === "full"
        ? (record.chapters || []).reduce((n, c) => n + String(c.text).split(/\s+/).length, 0)
        : 0);
    if (readCell) readCell.textContent = `${Math.max(1, Math.round(words / 220))} min read`;
  }

  /* §6 rule 11, applied to a control that is wired and still has nothing to
     do: a short source yields five points or fewer and no chapters, so Full
     and Brief are the same page. Rather than let the person click and watch
     nothing move, `Full` says why for two seconds and leaves `Brief` set. */
  const nothingMore =
    data.points.length <= BRIEF_POINTS && !(record.chapters || []).length;

  for (const segment of node.querySelectorAll(".segment")) {
    segment.addEventListener("click", () => {
      if (segment.dataset.view === "full" && nothingMore) {
        answer(segment, "That is the whole summary");
        return;
      }
      for (const other of node.querySelectorAll(".segment")) {
        other.setAttribute("aria-pressed", String(other === segment));
      }
      show(segment.dataset.view);
    });
  }
  show("brief");

  const plain = () => {
    const shown = [...list.querySelectorAll(".point-text")].map((p) => p.textContent);
    return `${source.title || ""}\n\n${record.gist}\n\n${shown
      .map((text, i) => `${i + 1}. ${text}`)
      .join("\n")}\n`;
  };

  node.querySelector("#sum-copy").addEventListener("click", (event) =>
    copyText(event.currentTarget, plain())
  );
  for (const item of node.querySelectorAll("#sum-download .menu-item")) {
    item.addEventListener("click", () => {
      const title = source.title || "summary";
      if (item.dataset.format === "txt") download(fileName(title, "txt"), plain());
      else {
        const shown = [...list.querySelectorAll(".point-text")].map((p) => p.textContent);
        download(
          fileName(title, "md"),
          `# ${title}\n\n${record.gist}\n\n## Key points\n\n` +
            shown.map((text) => `- ${text}`).join("\n") +
            "\n"
        );
      }
      node.querySelector("#sum-download").hidden = true;
    });
  }
  return node;
};

/* ---------------------------------------------------------------------- */
/* controls that do something, or say why not                              */
/*                                                                          */
/* §6 rule 11: "An unavailable control says what is missing and offers to    */
/* fix it, rather than greying out. Clicking Copy with nothing to copy       */
/* answers 'Generate your link first' for 2s, then returns to 'Copy'."       */
/*                                                                          */
/* A button that is drawn and does nothing is worse than one that is not     */
/* drawn: it is a promise the page cannot keep, and the person finds out by  */
/* clicking it.                                                              */
/* ---------------------------------------------------------------------- */

/** Say something on a button for two seconds, then give it back. */
export function answer(button, message) {
  if (button.dataset.answering) return;
  const original = button.innerHTML;
  button.dataset.answering = "1";
  button.textContent = message;
  setTimeout(() => {
    button.innerHTML = original;
    delete button.dataset.answering;
  }, 2000);
}

export async function copyText(button, text) {
  if (!text) {
    answer(button, "Nothing to copy yet");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    answer(button, "Copied");
  } catch {
    // A denied clipboard is not a crash, and silence is not an answer.
    answer(button, "Your browser blocked the copy");
  }
}

export function download(name, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------------------------------------------------------------- */
/* §4.9 TOAST and §4.8 / §9.4 THE CONFIRM DIALOG                           */
/*                                                                          */
/* Both molecules were styled and never instantiated, which is why §9.4's    */
/* table had no enforcement anywhere: not one destructive control in the app */
/* confirmed, and the only one §9.4 gives an Undo to had no toast to put it  */
/* in.                                                                      */
/* ---------------------------------------------------------------------- */

/** §4.9. One at a time, dismissed by its own action or by time.
 *
 *  §9.4 gives `Restore` an Undo toast and no dialog: undoing an undo is
 *  cheap, so the gate belongs after the act rather than before it.
 */
export function toast(message, { action, onAction, ms = 6000 } = {}) {
  document.querySelector(".toast")?.remove();
  const node = el(`
    <div class="toast" role="status">
      <span>${esc(message)}</span>
      ${action ? `<button type="button">${esc(action)}</button>` : ""}
    </div>`);
  const close = () => node.remove();
  node.querySelector("button")?.addEventListener("click", () => {
    close();
    onAction?.();
  });
  document.body.append(node);
  setTimeout(close, ms);
  return close;
}

/** §4.8 and §9.4's dialog. Resolves true when the person went through with
 *  it, false on Cancel, Escape, or a click on the scrim.
 *
 *  `word` adds the typed confirmation §9.4 asks for on the library and the
 *  account — one layer, not two (`R-16`). With no `word` it is the plain
 *  confirm §9.4 gives to deleting a single archived item and to revoking a
 *  connection.
 *
 *  **The second step is gone, by the founder's decision.** §9.4 specified
 *  "step 1 type DELETE, step 2 identity check", and `OPEN-2` left what that
 *  check should be undecided. It could not be anything: brain has no
 *  password and no mail sender, so the step was a field that accepted any
 *  character and was never sent to the server. Worse, the front door has no
 *  lock at all — anyone who can open this dialog can already read, archive
 *  and export the whole library — so a second gate on one door protected
 *  nothing while looking as though it did. §6 rule 11's argument, applied to
 *  a security control rather than a button.
 *
 *  What survives is the half that was always real: the typed word is checked
 *  **at the engine** as well as here, so neither this dialog nor a bug in it
 *  is what stands between the person and an irreversible act.
 *
 *  §18.8's detail that still holds: the word is printed in danger, semibold.
 */
export function confirmDialog({ title, body, confirmLabel = "Delete", word = "" }) {
  return new Promise((resolve) => {
    const typed = Boolean(word);
    const scrim = el(`
      <div class="scrim" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="dialog">
          <h2 class="dialog-title">${esc(title)}</h2>
          <p class="dialog-body">${esc(body)}</p>
          ${
            typed
              ? `<span class="field-label">Type <span class="danger-word">${esc(
                   word
                 )}</span> to confirm</span>
                 <input class="input" id="confirm-word" autocomplete="off"
                        aria-label="Type ${esc(word)} to confirm">`
              : ""
          }
          <div class="dialog-foot">
            <button class="btn" data-variant="ghost" data-act="cancel">Cancel</button>
            <button class="btn" data-variant="danger-solid" data-act="go"${
              typed ? " disabled" : ""
            }>${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`);

    const done = (answer) => {
      scrim.remove();
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    };
    const onKey = (event) => {
      if (event.key === "Escape") done(false);
    };

    const go = scrim.querySelector('[data-act="go"]');

    if (typed) {
      const field = scrim.querySelector("#confirm-word");
      // §9.4: "The confirm button is disabled until the field validates."
      field.addEventListener("input", () => {
        go.disabled = field.value.trim() !== word;
      });
      // Enter from the field is the same as pressing the button, once it is
      // live — otherwise typing the word and hitting Enter does nothing and
      // reads as the dialog being stuck.
      field.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !go.disabled) done(true);
      });
    }

    scrim.querySelector('[data-act="cancel"]').addEventListener("click", () => done(false));
    go.addEventListener("click", () => {
      if (!go.disabled) done(true);
    });
    // A click on the scrim itself, not on the panel, is a dismissal.
    scrim.addEventListener("click", (event) => {
      if (event.target === scrim) done(false);
    });
    document.addEventListener("keydown", onKey);
    document.body.append(scrim);
    (scrim.querySelector("#confirm-word") || go).focus();
  });
}

/* ---------------------------------------------------------------------- */
/* §4.1's HOVER ACTIONS                                                     */
/*                                                                          */
/* The slot and §4.7's menu were both built, styled, and instantiated by no   */
/* caller: every `itemCard` was a bare row, so the only thing a transcript    */
/* or a source could do was open. §7.4 names three controls on the card and   */
/* four items behind the `⋯`.                                                */
/*                                                                          */
/* They are buttons inside an `<a>`, which §4.1 asks for — "the whole row     */
/* opens the item; buttons inside call stopPropagation()". So every handler   */
/* here cancels the anchor as well as the bubble, or acting on a row would    */
/* navigate away from it.                                                    */
/* ---------------------------------------------------------------------- */

/** One hover action: an icon button, or a labelled one when it needs a word. */
function cardButton({ act, id, glyph, label, title = "", variant = "secondary" }) {
  const attrs = `data-act="${esc(act)}" data-id="${esc(id)}"${
    title ? ` title="${esc(title)}"` : ""
  }`;
  return label
    ? `<button class="btn" data-size="sm" data-variant="${esc(variant)}" ${attrs}>${
        glyph ? icon(glyph) : ""
      }${esc(label)}</button>`
    : `<button class="icon-btn" aria-label="${esc(act)}" ${attrs}>${icon(glyph)}</button>`;
}

/** §4.7's overflow menu on a card. `items` are `{act, label, danger, sep}`,
 *  and §4.7's own rule — "a separator precedes destructive items" — is why
 *  `sep` exists rather than being inferred. */
function cardMenu(id, items) {
  return `
    <span class="menu-wrap">
      <button class="icon-btn" aria-label="More" data-menu-for="m-${esc(id)}"
              aria-haspopup="true" aria-expanded="false">${icon("more")}</button>
      <span class="menu" id="m-${esc(id)}" hidden role="menu">
        ${items
          .filter(Boolean)
          .map((item) =>
            item.sep
              ? `<span class="menu-sep"></span>`
              : `<button class="menu-item" role="menuitem"${
                  item.danger ? ' data-variant="danger"' : ""
                } data-act="${esc(item.act)}" data-id="${esc(id)}">${esc(item.label)}</button>`
          )
          .join("")}
      </span>
    </span>`;
}

/** Bind every `[data-act]` in a frame to one handler map.
 *
 *  Delegated from the frame root rather than per button: a list re-renders a
 *  row after an action (a rename changes the title), and a per-button
 *  listener would be lost with the element it was on.
 */
function wireCardActions(node, handlers) {
  node.addEventListener("click", async (event) => {
    const control = event.target.closest("[data-act]");
    if (!control || !node.contains(control)) return;
    const handler = handlers[control.dataset.act];
    if (!handler) return;
    // The row is a link. Acting on it must not also follow it.
    event.preventDefault();
    event.stopPropagation();
    control.closest(".menu")?.setAttribute("hidden", "");
    await handler(control.dataset.id, control);
  });
}

/** The element itself, or the one inside it — whichever matches.
 *
 *  A frame's root often IS the thing a later lookup wants: `listFrame`
 *  returns the `.wrap`, and Home returns the `.home`. `querySelector` only
 *  searches descendants, so both lookups returned null and the code that
 *  followed threw on the next property access. It cost two debugging rounds
 *  before it cost a third, so it is a function now.
 */
export function self_or_within(node, selector) {
  return node.matches?.(selector) ? node : node.querySelector(selector);
}

/** §6 rule 13, after a row leaves rather than after a filter runs: "a section
 *  with zero items hides itself, title included". Removing the last card in a
 *  date group otherwise leaves its `Today` heading standing over nothing. */
export function hideEmptiedGroups(node) {
  for (const group of node.querySelectorAll(".date-group, .section")) {
    const rows = [...group.querySelectorAll(".item-card, .exp-card")];
    if (rows.length) group.hidden = !rows.some((row) => !row.hidden);
  }
}

/** A file name from a title, safe on every filesystem. */
export function fileName(title, extension) {
  const stem = String(title || "brain")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return `${stem || "brain"}.${extension}`;
}

/** §7.5's `Download ▾`. Only the formats that can actually be produced here:
 *  §6 rule 11 again — offering `.pdf` and then doing nothing when it is
 *  chosen is the dead control one layer down. */
function downloadMenu(id, formats) {
  return `
    <span class="menu-wrap">
      <button class="btn" data-variant="secondary" data-menu-for="${esc(id)}"
              aria-haspopup="true" aria-expanded="false">
        ${icon("download")}Download${icon("chevron-down")}
      </button>
      <span class="menu" id="${esc(id)}" hidden role="menu">
        ${formats
          .map(
            (format) =>
              `<button class="menu-item" role="menuitem" data-format="${esc(format)}">
                 .${esc(format)}
               </button>`
          )
          .join("")}
      </span>
    </span>`;
}

/** §4.7. Closes on outside click and on Escape, both of which the plan names
 *  and neither of which happens by itself. */
function wireMenus(node) {
  // Outside click and Escape are document-level, and the frame they belong to
  // is replaced on every navigation. Left attached, each visit would add two
  // more listeners that keep the whole detached frame alive; so the first
  // event after the frame leaves the document takes them off.
  const scope = new AbortController();
  const close = () => {
    if (!node.isConnected) {
      scope.abort();
      return;
    }
    for (const menu of node.querySelectorAll(".menu:not([hidden])")) {
      menu.hidden = true;
      node
        .querySelector(`[data-menu-for="${menu.id}"]`)
        ?.setAttribute("aria-expanded", "false");
    }
  };

  for (const trigger of node.querySelectorAll("[data-menu-for]")) {
    trigger.addEventListener("click", (event) => {
      // Both, not just the bubble. §4.1 puts these buttons inside the card's
      // own `<a>`, so stopping propagation alone left the anchor to activate:
      // opening the menu on a transcript row navigated to the transcript, and
      // the menu was drawn onto a page that was already being replaced.
      event.preventDefault();
      event.stopPropagation();
      const menu = node.querySelector(`#${CSS.escape(trigger.dataset.menuFor)}`);
      const opening = menu.hidden;
      close();
      menu.hidden = !opening;
      trigger.setAttribute("aria-expanded", String(opening));
    });
  }

  document.addEventListener("click", close, { signal: scope.signal });
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Escape") close();
    },
    { signal: scope.signal }
  );
  return close;
}

/** Live filtering for a list's own search field.
 *
 *  §7.4, §7.7, §7.8, §7.9 and §7.12 all draw one. Drawing a field that
 *  filters nothing is the same broken promise as a dead button.
 */
function wireListSearch(node, { rowSelector = ".item-card, .exp-card" } = {}) {
  const field = node.querySelector(".search .input");
  if (!field) return;
  const empty = node.querySelector(".empty");

  field.addEventListener("input", () => {
    const needle = field.value.trim().toLowerCase();
    let shown = 0;
    for (const row of node.querySelectorAll(rowSelector)) {
      const match = !needle || row.textContent.toLowerCase().includes(needle);
      row.hidden = !match;
      if (match) shown += 1;
    }
    // §6 rule 13: a section with nothing left in it hides itself, title too.
    for (const group of node.querySelectorAll(".date-group, .section")) {
      const rows = [...group.querySelectorAll(rowSelector)];
      if (rows.length) group.hidden = !rows.some((row) => !row.hidden);
    }
    let note = node.querySelector(".search-note");
    if (!note) {
      note = el('<p class="search-note"></p>');
      node.querySelector(".toolbar").after(note);
    }
    note.hidden = !needle;
    note.textContent = needle && !shown ? `Nothing here matches “${field.value.trim()}”.` : "";
    if (empty) empty.hidden = Boolean(needle);
  });
}

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
  // "Done in 0s" is accurate and says nothing, so under a second it is just
  // "Done" — the same answer the header gives when the duration is unknown.
  const elapsed = blocked
    ? "Stopped"
    : progress && progress.finished
      ? progress.elapsed_sec
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
                       data-act="approve" data-id="${esc(skill.id)}">Approve</button>`
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
        <!-- §7.3's "overflow (Rename, Re-process, Archive)". The frame had no
             menu at all, so all three were unreachable from the one page the
             source has. -->
        ${cardMenu(src.id, [
          { act: "rename", label: "Rename" },
          src.canonical_url && { act: "reprocess", label: "Re-process" },
          { sep: true },
          // `R-18`: only when there is actually a file to delete. A link
          // source has nothing held, and an upload whose 30 days are up is
          // already gone — the server decides, not the page.
          data.file && data.file.held && { act: "delete-file", label: "Delete the file now" },
          { act: "archive", label: "Archive" },
        ])}
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
                 <div class="yield-card-head">${slot("summaries")}<span>Summary</span>
                   <span class="yield-actions">
                     ${cardButton({ act: "copy-summary", id: src.id, glyph: "copy" })}
                     ${cardButton({ act: "download-summary", id: src.id, glyph: "download" })}
                   </span>
                 </div>
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

  wireMenus(node);
  wireCardActions(node, {
    approve: async (id, button) => {
      button.disabled = true;
      try {
        await post(`/api/skills/${encodeURIComponent(id)}/review`, { decision: "approve" });
      } catch (error) {
        button.disabled = false;
        toast(error.message || "Could not approve it.");
        return;
      }
      button.replaceWith(el('<span class="pill" data-state="active">Approved</span>'));
      refreshCounts();
    },
    // §7.3's "Copy summary · Download summary". The card drew the gist with
    // no controls at all, so two of the five actions the frame names did not
    // exist on it.
    "copy-summary": (id, button) => copyText(button, summaryText(data)),
    "download-summary": () =>
      download(fileName(src.title, "md"), summaryMarkdown(data), "text/markdown"),
    rename: (id, button) => renameSourceHeader(id, button, src),
    reprocess: async (id) => {
      try {
        await post(`/api/sources/${encodeURIComponent(id)}/reprocess`, {});
      } catch (error) {
        toast(error.message || "Could not read it again.");
        return;
      }
      window.location.reload();
    },
    "delete-file": async (id) => {
      const ok = await confirmDialog({
        title: "Delete the file Brain was sent?",
        body:
          `Brain deletes “${data.file.filename}” now instead of in ` +
          `${data.file.days} days. The rules, the summary and the transcript stay — ` +
          `the file was only what Brain read them from. This returns no credits: ` +
          `they paid for the reading, which is already done.`,
        confirmLabel: "Delete the file",
      });
      if (!ok) return;
      try {
        await post(`/api/sources/${encodeURIComponent(id)}/file`, {}, "DELETE");
      } catch (error) {
        toast(error.message || "Could not delete it.");
        return;
      }
      window.location.reload();
    },
    archive: async (id) => {
      try {
        await post(`/api/archive/source/${encodeURIComponent(id)}`, {});
      } catch (error) {
        toast(error.message || "Could not archive it.");
        return;
      }
      refreshCounts();
      window.location.assign(href("/library/sources"));
    },
  });

  if (running) watchTheWork(node, src.id);
  return node;
};

/** §9.1: "The UI polls or streams from those rows — the work block is a view
 *  of real state, never a fake animation."
 *
 *  Nothing polled, so the block was a snapshot taken once: the running step's
 *  spinner kept spinning after that step had finished, and `processing` never
 *  became `done` without a reload. `GET /api/sources/:id/job` existed and was
 *  called by nothing.
 *
 *  It is also what makes §7.3's own promise true — "You can leave this page.
 *  Brain keeps working" — in the direction that matters here: come back, and
 *  the page tells you where it got to.
 */
function watchTheWork(node, sourceId) {
  const block = node.querySelector(".work");
  if (!block) return;
  let stop = false;

  const tick = async () => {
    if (stop || !node.isConnected) return;         // the frame was replaced
    let progress;
    try {
      progress = await api(`/api/sources/${encodeURIComponent(sourceId)}/job`);
    } catch {
      // A failed poll is not a failed job. Try again on the next beat.
      setTimeout(tick, 4000);
      return;
    }
    block.querySelector(".work-steps").innerHTML = progress.steps.map(workStep).join("");
    if (!progress.finished && !["failed", "blocked_by_platform"].includes(progress.status)) {
      setTimeout(tick, 2000);
      return;
    }
    // Done, one way or the other: the header pill, the elapsed time and
    // "What came out of it" all change, so the page is drawn again rather
    // than patched in six places.
    stop = true;
    refreshCounts();
    window.location.reload();
  };
  setTimeout(tick, 2000);
}

/** §7.3's `Rename`, in the header where the title is. */
async function renameSourceHeader(id, button, src) {
  const title = button.closest(".wrap").querySelector(".read-title");
  const was = title.textContent;
  const field = el(`<input class="input title-rename" value="${esc(was)}" aria-label="Rename">`);
  title.replaceWith(field);
  field.focus();
  field.select();

  let closed = false;
  const finish = async (save) => {
    if (closed) return;
    closed = true;
    const wanted = field.value.trim();
    field.replaceWith(title);
    if (!save || !wanted || wanted === was) return;
    try {
      await post(`/api/sources/${encodeURIComponent(id)}/rename`, { title: wanted });
    } catch (error) {
      toast(error.message || "Could not rename it.");
      return;
    }
    title.textContent = wanted;
    src.title = wanted;
  };
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
}

/** The summary as the person would paste it, and as a file.
 *  Both are built from what the frame already has. */
function summaryText(data) {
  const points = (data.summary.points || []).map(
    (point, i) => `${i + 1}. ${point.text}${point.t_ms != null ? ` [${stamp(point.t_ms)}]` : ""}`
  );
  return `${data.source.title}\n\n${data.summary.gist}\n\n${points.join("\n")}\n`;
}

function summaryMarkdown(data) {
  const points = (data.summary.points || []).map(
    (point) => `- ${point.text}${point.t_ms != null ? ` — **${stamp(point.t_ms)}**` : ""}`
  );
  return [
    `# ${data.source.title}`,
    "",
    data.summary.gist,
    "",
    "## Key points",
    "",
    points.join("\n") || "—",
    data.source.canonical_url ? `\n---\n\n[The source](${data.source.canonical_url})` : "",
    "",
  ].join("\n");
}


export async function post(path, body, method = "POST") {
  const response = await reach(path, {
    method,
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
 *  `R-6`: the group heading above it carries no count and no dash.
 *
 *  **The caret is the last thing in the row**, per §18.6 — "on the right,
 *  pointing right when closed and down when open" — and §6 rule 1, which puts
 *  every action on the right. It was the first child, which put it at the far
 *  left of every Rules, Skills and Archive card.
 *
 *  `headHtml` is the slot §7.9's `Review` button and `R-9`'s `Restore` need.
 *  It sits outside the toggle rather than inside it: a `<button>` inside a
 *  `<button>` is invalid, and a click on the inner one would open the card as
 *  well as do its own job.
 */
function expandable({
  id,
  icon: glyph,
  title,
  sub,
  metaHtml: meta,
  bodyHtml,
  state = "",
  headHtml = "",
}) {
  return `
    <article class="exp-card" data-open="false"${state ? ` data-state="${esc(state)}"` : ""}>
      <div class="exp-top">
        <button class="exp-head" type="button" data-toggle="${esc(id)}" aria-expanded="false">
          <span class="item-type">${icon(glyph)}</span>
          <span class="item-text">
            <span class="item-title">${esc(title)}</span>
            <span class="item-meta exp-summary">${meta ?? esc(sub)}</span>
          </span>
        </button>
        ${headHtml ? `<span class="exp-actions">${headHtml}</span>` : ""}
        <button class="exp-caret-btn" type="button" data-toggle="${esc(id)}"
                tabindex="-1" aria-hidden="true">
          <!-- Points DOWN at rest: §4.2 rotates it -90 degrees when closed, so
               the base must be the open position or the two states swap. -->
          <span class="exp-caret slot">${icon("chevron-down")}</span>
        </button>
      </div>
      <div class="exp-body">${bodyHtml}</div>
    </article>`;
}

function wireExpanders(node) {
  for (const head of node.querySelectorAll("[data-toggle]")) {
    head.addEventListener("click", () => {
      const card = head.closest(".exp-card");
      const open = card.dataset.open === "true";
      card.dataset.open = String(!open);
      // The row and the caret are two toggles for one disclosure, so the
      // state goes on the one that announces it, not on whichever was hit.
      card.querySelector(".exp-head").setAttribute("aria-expanded", String(!open));
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
      // `locatorLabel` turns `1420` into `23:40` and `14` into `p. 14`.
      // The raw `locator_value` was printed here while the source detail
      // rendered the same claim through `locatorLabel`, so one rule read
      // `· 1420` on this page and `23:40` on the other.
      sub: [origin, locatorLabel(rule)].filter(Boolean).join(" · "),
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
              ? `<a class="chip" href="${esc(rule.provenance.deep_link)}" target="_blank" rel="noopener">${icon("clock")}${esc(locatorLabel(rule))}</a>`
              : ""
          }
        </div>
        <div class="btn-row">
          <button class="btn" data-variant="secondary" data-act="edit"
                  data-id="${esc(rule.id)}">Edit</button>
          <button class="btn" data-variant="ghost" data-act="archive"
                  data-id="${esc(rule.id)}">Archive</button>
        </div>`,
    });
  };

  const lookup = new Map([...data.mine, ...data.from_your_sources].map((r) => [r.id, r]));

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
  wireListSearch(node);
  openCreatePanel(node, "rule");

  /* §7.8's two actions. They were plain buttons with no data attribute and
     no handler, although both doors existed on the backend all along. */
  wireCardActions(node, {
    edit: (id, button) => editRule(id, button, lookup),
    archive: (id, button) => archiveRow("claim", id, button, node),
  });
  return node;
};

/** §7.8's `Edit`, in place in the expanded card.
 *
 *  §7.8: "A rule without its reasoning gets deleted by someone who no longer
 *  remembers why it exists. Always store both the short form and the full
 *  form." Both are editable here — the short form is what every list and
 *  every agent reads, and the long one is the reason it survives. */
async function editRule(id, button, lookup) {
  const body = button.closest(".exp-body");
  if (body.querySelector(".rule-edit")) return;
  const rule = lookup.get(id);
  const row = button.closest(".btn-row");

  const form = el(`
    <div class="rule-edit">
      <label class="field-label" for="e-claim-${esc(id)}">The rule</label>
      <textarea class="input create-area" id="e-claim-${esc(id)}" rows="2">${esc(
        rule.claim
      )}</textarea>
      <label class="field-label" for="e-applies-${esc(id)}">When it applies</label>
      <input class="input" id="e-applies-${esc(id)}" value="${esc(rule.when_it_applies || "")}">
      <div class="btn-row">
        <button class="btn" data-variant="primary" data-save>Save</button>
        <button class="btn" data-variant="ghost" data-cancel>Cancel</button>
      </div>
    </div>`);
  row.before(form);
  row.hidden = true;
  form.querySelector("textarea").focus();

  const close = () => {
    form.remove();
    row.hidden = false;
  };
  form.querySelector("[data-cancel]").addEventListener("click", close);
  form.querySelector("[data-save]").addEventListener("click", async (event) => {
    const claim = form.querySelector("textarea").value.trim();
    const applies = form.querySelector("input").value.trim();
    if (!claim) {
      form.querySelector("textarea").focus();
      return;
    }
    event.currentTarget.disabled = true;
    try {
      await post(`/api/claim/${encodeURIComponent(id)}`, {
        claim,
        when_it_applies: applies,
      });
    } catch (error) {
      event.currentTarget.disabled = false;
      toast(error.message || "Could not save that.");
      return;
    }
    rule.claim = claim;
    rule.when_it_applies = applies;
    const card = button.closest(".exp-card");
    card.querySelector(".item-title").textContent = claim;
    card.querySelector(".rule-full").textContent = claim;
    close();
    toast("Saved.");
  });
}

FRAMES.skills = async function skills() {
  const data = await api("/api/skills");

  const card = (skill, kind) =>
    expandable({
      id: skill.id,
      icon: "skills",
      title: skill.name,
      sub: skill.description,
      state: kind === "proposed" ? "proposed" : kind === "inactive" ? "inactive" : "",
      // §7.9: "proposed → a **`Review` button**. Not a pill. A state the
      // person cannot act on is a dead end." It is the queue's one entry
      // point, which is why it is the collapsed card's one visible action.
      //
      // `R-7`: an approved card carries `⋯` alone — the capture's pencil is
      // dropped, because §7.9 also lists `Edit` in the menu and §6 rule 2
      // forbids the same action in two places.
      headHtml:
        kind === "proposed"
          ? cardButton({
              act: "review",
              id: skill.id,
              label: "Review",
              variant: "secondary",
            })
          : cardMenu(skill.id, [
              { act: "edit", label: "Edit" },
              // §7.9: "Activation lives in the menu. There is no toggle
              // switch on a skill card."
              kind === "active"
                ? { act: "deactivate", label: "Deactivate" }
                : { act: "activate", label: "Activate" },
              { act: "export", label: "Export .md" },
              { sep: true },
              // `R-14`: this sets the skill aside. §6 rule 8 — "only the
              // person deletes, and only from Archive or Profile" — and
              // §7.9's `Delete (confirms)` cannot both hold on a live card.
              { act: "delete", label: "Delete", danger: true },
            ]),
      bodyHtml: `
        <span class="field-label">When to use it</span>
        <p class="rule-full">${esc(skill.trigger_when || "—")}</p>
        <span class="field-label">What it does</span>
        <ol class="steps-list steps-plain">${(skill.execution_steps || [])
          .map((step) => `<li>${esc(step)}</li>`)
          .join("")}</ol>
        ${provenanceChips(skill)}
        ${
          kind === "proposed"
            ? `<div class="btn-row">
                 <button class="btn" data-variant="primary" data-act="approve"
                         data-id="${esc(skill.id)}">Approve</button>
                 <button class="btn" data-variant="secondary" data-act="edit"
                         data-id="${esc(skill.id)}">Edit</button>
                 <button class="btn" data-variant="ghost" data-act="dismiss"
                         data-id="${esc(skill.id)}">Dismiss</button>
               </div>`
            : ""
        }`,
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

  const lookup = new Map(
    [...data.from_your_sources, ...data.active, ...data.inactive].map((s) => [s.id, s])
  );

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
  wireListSearch(node);
  wireMenus(node);
  openCreatePanel(node, "skill");

  /* §7.9's review flow and its overflow menu, which was `⋯` with no handler:
     `Activate`/`Deactivate`, `Export .md` and `Delete` were all unreachable,
     and `POST /api/skills/:id/state` — which the backend already served —
     was called by nothing in the web app, so a skill could never enter or
     leave the `Inactive` section the frame draws. */
  wireCardActions(node, {
    // §7.9: "`Review` ... A state the person cannot act on is a dead end."
    // It opens the card, which is where Approve, Edit and Dismiss live.
    review: (id, button) => {
      const card = button.closest(".exp-card");
      card.dataset.open = "true";
      card.querySelector(".exp-head").setAttribute("aria-expanded", "true");
      card.querySelector('[data-act="approve"]')?.focus();
    },
    approve: async (id, button) => {
      button.disabled = true;
      try {
        await post(`/api/skills/${encodeURIComponent(id)}/review`, { decision: "approve" });
      } catch (error) {
        button.disabled = false;
        toast(error.message || "Could not approve it.");
        return;
      }
      refreshCounts();
      toast("Approved. Every connection can reach it now.");
      window.location.assign(href("/skills"));
    },
    dismiss: async (id, button) => {
      const card = button.closest(".exp-card");
      try {
        await post(`/api/skills/${encodeURIComponent(id)}/review`, { decision: "dismiss" });
      } catch (error) {
        toast(error.message || "Could not dismiss it.");
        return;
      }
      card.remove();
      hideEmptiedGroups(node);
      refreshCounts();
      toast("Dismissed. It is in your Archive.", {
        action: "Undo",
        onAction: async () => {
          await post(`/api/archive/skill/${encodeURIComponent(id)}`, { restore: true }).catch(
            () => {}
          );
          window.location.assign(href("/skills"));
        },
      });
    },
    edit: (id) => window.location.assign(href(`/skills/${id}?edit=1`)),
    activate: (id, button) => setSkillState(id, "active", button),
    deactivate: (id, button) => setSkillState(id, "inactive", button),
    // §7.9's `Export .md`. Built here from the skill the frame already holds:
    // a round trip to fetch what is on screen is a round trip for nothing.
    export: (id) => {
      const skill = lookup.get(id);
      download(fileName(skill.name, "md"), skillMarkdown(skill), "text/markdown");
    },
    // `R-14`. §7.9 names this `Delete`; §6 rule 8 says only Archive and
    // Profile destroy anything. The dialog says where it goes.
    delete: async (id, button) => {
      const skill = lookup.get(id);
      const yes = await confirmDialog({
        title: `Delete “${skill.name}”?`,
        body:
          "It moves to your Archive, where you can put it back or delete it for " +
          "good. Your AI cannot reach it from there.",
        confirmLabel: "Delete",
      });
      if (!yes) return;
      await archiveRow("skill", id, button, node);
    },
  });
  return node;
};

async function setSkillState(id, state, button) {
  try {
    await post(`/api/skills/${encodeURIComponent(id)}/state`, { state });
  } catch (error) {
    toast(error.message || "Could not change that.");
    return;
  }
  refreshCounts();
  // The card has to move between §7.9's sections, and the sections carry the
  // notes that explain what each state means — so the page redraws rather
  // than leaving a card sitting under a heading that now contradicts it.
  toast(state === "active" ? "Active. Every connection can reach it." : "Inactive. Kept, out of reach.");
  window.location.assign(href("/skills"));
}

/** §7.9's `Export .md`, in the shape a host agent reads: the trigger first,
 *  because without it a model never reaches for the skill, then the steps. */
function skillMarkdown(skill) {
  const steps = (skill.execution_steps || []).map((step, i) => `${i + 1}. ${step}`).join("\n");
  return [
    `# ${skill.name}`,
    "",
    skill.description || "",
    "",
    "## When to use it",
    "",
    skill.trigger_when || "—",
    "",
    "## What it does",
    "",
    steps || "—",
    skill.guardrails ? `\n## Guardrails\n\n${skill.guardrails}` : "",
    skill.source_title ? `\n---\n\nFrom: ${skill.source_title}` : "",
    "",
  ].join("\n");
}

/* ---------------------------------------------------------------------- */
/* §7.1's two create links, which landed nowhere                            */
/*                                                                          */
/* `Write a rule` and `Add a skill` link to `/rules?create=1` and            */
/* `/skills?create=1`. No frame read the query at all, so both opened the    */
/* list they were already on with nothing new on it — §7.1 names the result  */
/* as "`/rules` with the create panel open".                                */
/* ---------------------------------------------------------------------- */

const CREATE_PANEL = {
  rule: {
    title: "Write a rule",
    note: "A standing instruction. Every AI you connect reads it before it answers.",
    endpoint: "/api/rule",
    fields: [
      { name: "claim", label: "The rule", placeholder: "Never use em dashes in anything you draft for me.", area: true },
      { name: "when_it_applies", label: "When it applies", placeholder: "always" },
      { name: "when_it_fails", label: "When it does not", placeholder: "Optional — when to ignore it", optional: true },
    ],
  },
  skill: {
    title: "Add a skill",
    note: "A procedure worth reusing. The trigger matters most — without it a model never reaches for the skill.",
    endpoint: "/api/skill",
    fields: [
      { name: "name", label: "Name", placeholder: "Run a cycle planning session" },
      { name: "description", label: "What it is", placeholder: "One line, so a model can tell it apart from the others." },
      { name: "trigger_when", label: "When to use it", placeholder: "When planning the next six weeks" },
      { name: "steps", label: "What it does", placeholder: "One step per line.", area: true },
    ],
  },
};

/** Open the panel when the URL asks for it, and only then. */
function openCreatePanel(node, kind) {
  if (new URLSearchParams(window.location.search).get("create") !== "1") return;
  const spec = CREATE_PANEL[kind];
  const panel = el(`
    <section class="section create-panel">
      <div class="group-head">
        <h2 class="group-title">${esc(spec.title)}</h2>
        <p class="group-note">${esc(spec.note)}</p>
      </div>
      <div class="card-group">
        ${spec.fields
          .map(
            (field) => `
          <div class="create-field">
            <label class="field-label" for="f-${esc(field.name)}">${esc(field.label)}</label>
            ${
              field.area
                ? `<textarea class="input create-area" id="f-${esc(field.name)}"
                             placeholder="${esc(field.placeholder)}" rows="3"></textarea>`
                : `<input class="input" id="f-${esc(field.name)}"
                          placeholder="${esc(field.placeholder)}">`
            }
          </div>`
          )
          .join("")}
        <div class="btn-row">
          <button class="btn" data-variant="primary" id="create-save">Save</button>
          <a class="btn" data-variant="ghost" href="/${kind === "rule" ? "rules" : "skills"}">Cancel</a>
          <span class="save-note" id="create-note" hidden></span>
        </div>
      </div>
    </section>`);

  // Above the list: the person came here to add something, not to read.
  node.querySelector(".toolbar").after(panel);
  panel.querySelector(".input").focus();

  const note = panel.querySelector("#create-note");
  panel.querySelector("#create-save").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const values = {};
    for (const field of spec.fields) {
      values[field.name] = panel.querySelector(`#f-${field.name}`).value.trim();
    }
    const missing = spec.fields.find((field) => !field.optional && !values[field.name]);
    if (missing) {
      note.hidden = false;
      note.textContent = `${missing.label} is needed.`;
      panel.querySelector(`#f-${missing.name}`).focus();
      return;
    }
    button.disabled = true;
    try {
      if (kind === "skill") {
        await post(spec.endpoint, {
          slug: values.name,
          name: values.name,
          description: values.description,
          trigger_when: values.trigger_when,
          execution_steps: values.steps.split("\n").map((line) => line.trim()).filter(Boolean),
        });
      } else {
        await post(spec.endpoint, values);
      }
    } catch (error) {
      button.disabled = false;
      note.hidden = false;
      note.textContent = error.message || "Could not save that.";
      return;
    }
    refreshCounts();
    window.location.assign(href(kind === "rule" ? "/rules" : "/skills"));
  });
}

/** §7.8 and §7.9 both end their expanded body with provenance. A skill that
 *  came out of a source names it; one written here says so. */
function provenanceChips(skill) {
  const origin =
    skill.origin_type === "github_import"
      ? "Imported from GitHub"
      : skill.origin_type === "synthesized"
        ? "Synthesized on request"
        : skill.source_title || "Written here";
  return `
    <div class="chip-row">
      <span class="chip">${icon("sources")}${esc(origin)}</span>
      ${
        skill.source_id
          ? `<a class="chip" href="/sources/${esc(skill.source_id)}">${icon(
              "clock"
            )}Open the source</a>`
          : ""
      }
      <span class="chip">${icon("clock")}v${esc(String(skill.version || 1))}</span>
    </div>`;
}

/* ---------------------------------------------------------------------- */
/* §7.12 ARCHIVE                                                           */
/* ---------------------------------------------------------------------- */

const ARCHIVE_ICON = { source: "sources", rule: "rules", skill: "skills", summary: "summaries" };

/* ---------------------------------------------------------------------- */
/* §7.10 ACTIVITY                                                          */
/*                                                                          */
/* "Two sections, because they answer two questions." Usage is how much is   */
/* left; Calls is what the agents actually did. The rows are 40px and one    */
/* line — "denser than an item card because a log is scanned, not browsed".  */
/* ---------------------------------------------------------------------- */

/** Bytes as the person would say them. */
function sizeLabel(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} kB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

/** §4.6's meter card. A bar only where there is a cap to measure against — a
 *  bar against a number nothing checks is decoration. Every meter the page
 *  shows today does have a cap; the capless branch stays because `R-17`'s
 *  reasoning is that a figure may outlive its limit, and the card should
 *  degrade to an honest number rather than draw a fictional bar. */
function meterCard(meter) {
  const { used, cap, label, unit } = meter;
  const shown = unit === "bytes" ? sizeLabel(used) : Number(used).toLocaleString();
  const of =
    cap === null || cap === undefined
      ? ""
      : ` of ${unit === "bytes" ? sizeLabel(cap) : Number(cap).toLocaleString()}`;
  const pct = cap ? Math.min(100, Math.round((used / cap) * 100)) : null;
  return `
    <article class="meter-card">
      <span class="meter-label">${esc(label)}</span>
      <p class="meter-figure">${esc(shown)}${of ? `<small>${esc(of)}</small>` : ""}</p>
      ${
        pct === null
          ? `<p class="meter-note">No limit on this one.</p>`
          : `<span class="meter" role="img"
                   aria-label="${esc(String(pct))} percent of ${esc(label)} used">
               <span data-level="${pct >= 90 ? "high" : pct >= 70 ? "warn" : "ok"}"
                     style="width:${pct}%"></span>
             </span>
             <p class="meter-note">${esc(
               unit === "rate"
                 ? "Resets every minute."
                 : unit === "credits"
                   // `R-18`: the count, not a percentage. Against a 500,000
                   // allowance a real month's use rounds to 0%, so the card
                   // read "1,919 of 500,000" and "100% left." in the same
                   // breath. And the number is the useful one anyway: it
                   // answers "can I send this PDF". The reset is named
                   // because a monthly allowance that does not say it comes
                   // back reads as a wall.
                   ? `${(cap - used).toLocaleString()} left. Back to full on the 1st.`
                   : `${100 - pct}% left.`
             )}</p>`
      }
    </article>`;
}

FRAMES.activity = async function activity() {
  const data = await api("/api/activity");
  // Named `…Html` and built before the template, which is the convention the
  // escaping guard enforces: a hole into markup has to be visibly trusted at
  // the point it is interpolated, not trusted because of what it calls.
  // `R-17`: no storage meter. §7.10's capture shows `Storage 2.1 GB of
  // 10 GB`; the founder struck it, and what the person sends us is swept
  // after 30 days anyway, so the bytes on disk say nothing about their use.
  // `R-18`: credits, not minutes. Minutes metered YouTube and nothing else,
  // so an article or a PDF moved no meter at all; credits are characters of
  // source text, which every source type has.
  const metersHtml = ["sources", "credits", "calls"]
    .map((key) => meterCard(data.usage[key]))
    .join("");

  // `R-18`: a meter says how much is left, which is only actionable once
  // you know where it went. Dearest first, this period, because that is the
  // period the meter above is counting.
  const spendHtml = (data.spend || []).length
    ? `<section class="section">
         <h2 class="section-title">Where your credits went</h2>
         <p class="section-note">This month, dearest first. A credit is one character Brain read.</p>
         <div class="card-group">
           ${data.spend
             .map(
               (item) => `
             <div class="setting-row">
               <span class="setting-text">
                 <span class="setting-label">${esc(item.title)}</span>
                 <span class="setting-help">${esc(item.source_type)}${
                   item.still_here ? "" : " · no longer in your library"
                 }</span>
               </span>
               <span class="setting-control"><span class="chip">${esc(
                 Number(item.credits).toLocaleString()
               )}</span></span>
             </div>`
             )
             .join("")}
         </div>
       </section>`
    : "";

  const row = (call) => `
    <div class="call" data-open="false">
      <button class="call-head" type="button" data-toggle="${esc(String(call.id))}"
              aria-expanded="false">
        <span class="call-time">${esc(clockLabel(call.at))}</span>
        <span class="call-dot" data-ok="${call.ok}"></span>
        <span class="call-conn">${esc(call.connection)}</span>
        <code class="call-tool">${esc(call.tool)}</code>
        <span class="call-target">${esc(call.target || call.asked_for_short || "")}</span>
        <span class="call-latency">${esc(latencyLabel(call.latency_ms))}</span>
        <span class="pill" data-state="${call.ok ? "active" : "pending"}">${
          call.ok ? "OK" : "Failed"
        }</span>
      </button>
      <div class="call-body">
        <span class="field-label">Asked for</span>
        <pre class="call-pre">${esc(call.asked_for || "—")}</pre>
        <span class="field-label">Returned</span>
        <p class="call-returned">${esc(call.returned || "—")}</p>
        <div class="chip-row">
          <span class="chip">${icon("doc")}${esc(
            call.payload_bytes === null || call.payload_bytes === undefined
              ? "No payload"
              : sizeLabel(call.payload_bytes)
          )}</span>
          ${
            call.connection_id
              ? `<a class="chip" href="/connections">${icon("connections")}${esc(
                  call.connection
                )}</a>`
              : ""
          }
        </div>
        ${
          call.remedy
            ? `<p class="work-hint" data-tone="error">${esc(call.remedy)}</p>`
            : ""
        }
      </div>
    </div>`;

  const node = el(`
    <div class="wrap">
      <h1 class="page-title">Activity</h1>

      <section class="section">
        <h2 class="section-title">Usage</h2>
        <p class="section-note">What you have used, against what Brain enforces.</p>
        <div class="meter-row">
          ${metersHtml}
        </div>
      </section>

      ${spendHtml}

      <section class="section">
        <h2 class="section-title">Calls</h2>
        <p class="section-note">Every call your AI made. Click one to see what it
           asked for and what came back.</p>
        ${
          data.calls.length
            ? `<div class="call-log">${data.calls.map(row).join("")}</div>`
            : `<div class="empty">
                 <p>Nothing has called Brain yet. Connect an AI and every call it
                    makes lands here, with what it asked for and what came back.</p>
                 <a class="btn" data-variant="primary" href="/connections">Connect an AI</a>
               </div>`
        }
      </section>
    </div>`);

  // §7.10: "Click expands: `Asked for`, `Returned`, `Payload`, `Connection`."
  for (const head of node.querySelectorAll(".call-head")) {
    head.addEventListener("click", () => {
      const call = head.closest(".call");
      const open = call.dataset.open === "true";
      call.dataset.open = String(!open);
      head.setAttribute("aria-expanded", String(!open));
    });
  }
  return node;
};

/* ---------------------------------------------------------------------- */
/* §7.11 CONNECTIONS                                                       */
/*                                                                          */
/* "One server. Two ways to authenticate. This is verified behaviour, not a  */
/* choice." The key goes in the URL because a chat app's connector dialog    */
/* has no header field — so there is one link, pasted once, and nothing      */
/* else to copy.                                                            */
/*                                                                          */
/* The empty state is the frame. §7.11 calls it "the most important empty    */
/* state in the app", and the `Connected` section with its sentence about    */
/* revocation does not render until there is something to revoke.            */
/* ---------------------------------------------------------------------- */

/** §7.11's agent instruction. Not JSON — an instruction, because what an
 *  agent needs is to be told where to put it, and the placeholder is an
 *  amber `<mark>` until the link exists. */
function agentBlock(link) {
  const target = link
    ? esc(link)
    : `<mark>PASTE_YOUR_GENERATED_LINK_HERE</mark>`;
  return `Add this MCP server to this project.

  name:      brain
  url:       ${target}
  transport: streamable HTTP

The key is already inside the URL, so it needs no auth
header and no environment variable. Put it wherever this
project configures MCP servers, then list the tools to
confirm it answers.`;
}

FRAMES.connections = async function connections() {
  const data = await api("/api/connections");
  const live = data.connections.filter((item) => !item.revoked);
  // Generated in this session only. §7.11's link carries the key, and the
  // key exists in the response that minted it and nowhere else — so a page
  // reload cannot show it again, and the frame says so rather than printing
  // a link that is missing its key.
  let link = "";

  const row = (item) => `
    <div class="conn" data-id="${esc(item.id)}">
      <span class="conn-name">${esc(item.name)}</span>
      <span class="item-meta">${metaHtml([
        item.how,
        item.key_prefix ? `${item.key_prefix}…` : null,
        item.last_call_at ? `last call ${dateLabel(item.last_call_at).toLowerCase()}` : "never used",
        `added ${dateLabel(item.created_at).toLowerCase()}`,
      ])}</span>
      <span class="pill" data-state="${item.revoked ? "archived" : "active"}">${
        item.revoked ? "Revoked" : "Live"
      }</span>
      ${
        item.revoked
          ? ""
          : cardMenu(item.id, [
              { act: "rename", label: "Rename" },
              item.how === "Key" ? { act: "roll", label: "Roll the key" } : null,
              { sep: true },
              { act: "revoke", label: "Revoke", danger: true },
            ])
      }
    </div>`;

  const node = el(`
    <div class="wrap">
      <h1 class="page-title">Connections</h1>

      <section class="section connect-card" data-state="${esc(live.length ? "live" : "empty")}">
        ${
          live.length
            ? `<h2 class="section-title">Add another</h2>
               <p class="section-note">A second AI gets its own link, so revoking one
                  leaves the rest working.</p>`
            : `<span class="pill" data-state="pending">Not connected</span>
               <h2 class="connect-title">Brain is not reachable by anything yet</h2>
               <p class="connect-lede">Generate your link, then paste it once into the
                  AI you use. That link is the whole setup — it carries your key, so
                  there is nothing else to copy.</p>`
        }
        <div class="connect-row">
          <input class="input" id="link" readonly
                 placeholder="${
                   live.length
                     ? "A link is shown once, when it is made. Roll a key below for a new one."
                     : "Your link appears here"
                 }"
                 aria-label="Your connection link">
          <button class="btn" data-variant="primary" id="generate">Generate my link</button>
          <button class="btn" data-variant="secondary" id="copy-link">${icon("copy")}Copy</button>
        </div>
      </section>

      <section class="section">
        <h2 class="section-title">How to paste it in</h2>
        <p class="section-note">Two kinds of place to put it. Open the one you use.</p>

        <article class="exp-card how" data-open="false">
          <div class="exp-top">
            <button class="exp-head" type="button" data-how="ai" aria-expanded="false">
              <span class="item-type">${icon("ai")}</span>
              <span class="item-text">
                <span class="item-title">An AI app</span>
                <span class="item-meta">Claude, ChatGPT, or anything with a connector
                  setting · Show me how</span>
              </span>
            </button>
            <button class="exp-caret-btn" type="button" data-how="ai"
                    tabindex="-1" aria-hidden="true">
              <span class="exp-caret slot">${icon("chevron-down")}</span>
            </button>
          </div>
          <div class="exp-body">
            <ol class="steps-list">
              <li>
                <span class="step-title">Open the connector settings in your AI.</span>
                <ul class="step-paths">
                  <li><b>Claude</b> — Settings → Connectors → <b>Add custom connector</b></li>
                  <li><b>ChatGPT</b> — Settings → Apps &amp; Connectors → <b>Advanced</b>.
                      If no custom option appears, turn on developer mode under
                      Settings → Security and login.</li>
                  <li><b>Others</b> — look for <b>MCP</b>, <b>Connectors</b> or
                      <b>Custom server</b></li>
                </ul>
              </li>
              <li>
                <span class="step-title">Paste your link and name it Brain.</span>
                <p class="step-note">Leave every other field alone.</p>
              </li>
              <li>
                <span class="step-title">Brain is live.</span>
                <p class="step-note">Ask it “what rules do I have about planning?”.</p>
              </li>
            </ol>
          </div>
        </article>

        <article class="exp-card how" data-open="false">
          <div class="exp-top">
            <button class="exp-head" type="button" data-how="agent" aria-expanded="false">
              <span class="item-type">${icon("agent")}</span>
              <span class="item-text">
                <span class="item-title">An agent or an editor</span>
                <span class="item-meta">Cursor, Claude Code, Codex, n8n, LangGraph,
                  CrewAI · Show me how</span>
              </span>
            </button>
            <button class="exp-caret-btn" type="button" data-how="agent"
                    tabindex="-1" aria-hidden="true">
              <span class="exp-caret slot">${icon("chevron-down")}</span>
            </button>
          </div>
          <div class="exp-body">
            <ol class="steps-list">
              <li>
                <span class="step-title">Hand your agent this instruction.</span>
                <pre class="code" id="agent-block">${agentBlock("")}</pre>
                <button class="btn" data-variant="secondary" id="copy-agent">${icon(
                  "copy"
                )}Copy</button>
              </li>
              <li>
                <span class="step-title">Ask it to list Brain's tools.</span>
                <p class="step-note">Eight, starting with <code>search</code>. If they
                   come back, it is connected.</p>
              </li>
            </ol>
          </div>
        </article>
      </section>

      ${
        live.length
          ? `<section class="section">
               <h2 class="section-title">Connected</h2>
               <p class="section-note">One credential per connection. Revoking one
                  leaves the others working.</p>
               <div class="conn-list">${data.connections.map(row).join("")}</div>
             </section>`
          : ""
      }
    </div>`);

  const field = node.querySelector("#link");
  const agentPre = node.querySelector("#agent-block");

  const show = (generated) => {
    link = generated;
    field.value = generated;
    agentPre.innerHTML = agentBlock(generated);
    node.querySelector("#generate").innerHTML = "Generate another";
  };

  node.querySelector("#generate").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const made = await post("/api/connections", { name: "My AI" });
      show(made.link);
      toast("Your link is ready. Paste it into your AI.");
    } catch (error) {
      toast(error.message || "Could not generate a link.");
    } finally {
      button.disabled = false;
    }
  });

  // §6 rule 11, named in §7.11 word for word: "Clicking Copy before
  // generating answers `Generate your link first` in amber for 2s. The button
  // always reads `Copy`."
  node.querySelector("#copy-link").addEventListener("click", (event) => {
    if (!link) {
      answer(event.currentTarget, "Generate your link first");
      return;
    }
    copyText(event.currentTarget, link);
  });
  node.querySelector("#copy-agent").addEventListener("click", (event) => {
    if (!link) {
      answer(event.currentTarget, "Generate your link first");
      return;
    }
    copyText(event.currentTarget, agentPre.textContent);
  });

  // §7.11: "Two tiles, visibly clickable — a chevron that rotates on open".
  for (const trigger of node.querySelectorAll("[data-how]")) {
    trigger.addEventListener("click", () => {
      const card = trigger.closest(".exp-card");
      const open = card.dataset.open === "true";
      card.dataset.open = String(!open);
      card.querySelector(".exp-head").setAttribute("aria-expanded", String(!open));
    });
  }

  wireMenus(node);
  wireCardActions(node, {
    rename: (id, button) => renameConnection(id, button),
    roll: async (id, button) => {
      const yes = await confirmDialog({
        title: "Roll this key?",
        body:
          "The link you pasted stops working immediately, and you paste the new one " +
          "in its place. The connection keeps its name and its history.",
        confirmLabel: "Roll the key",
      });
      if (!yes) return;
      try {
        const rolled = await post(`/api/connections/${encodeURIComponent(id)}/roll`, {});
        show(rolled.link);
        toast("Rolled. Paste the new link into that AI.");
        node.querySelector("#link").scrollIntoView({ block: "center" });
      } catch (error) {
        toast(error.message || "Could not roll that key.");
      }
    },
    // §9.4: "Revoke a connection | confirm dialog".
    revoke: async (id, button) => {
      const card = button.closest(".conn");
      const name = card.querySelector(".conn-name").textContent.trim();
      const yes = await confirmDialog({
        title: `Revoke ${name}?`,
        body:
          "That AI stops reaching Brain at once. Your other connections keep working, " +
          "and what it already did stays in Activity.",
        confirmLabel: "Revoke",
      });
      if (!yes) return;
      try {
        await post(`/api/connections/${encodeURIComponent(id)}/revoke`, {});
      } catch (error) {
        toast(error.message || "Could not revoke it.");
        return;
      }
      window.location.assign(href("/connections"));
    },
  });
  return node;
};

async function renameConnection(id, button) {
  const card = button.closest(".conn");
  const name = card.querySelector(".conn-name");
  const was = name.textContent.trim();
  const field = el(`<input class="input item-rename" value="${esc(was)}" aria-label="Rename">`);
  name.replaceWith(field);
  field.focus();
  field.select();

  let closed = false;
  const finish = async (save) => {
    if (closed) return;
    closed = true;
    const wanted = field.value.trim();
    field.replaceWith(name);
    if (!save || !wanted || wanted === was) return;
    try {
      await post(`/api/connections/${encodeURIComponent(id)}/rename`, { name: wanted });
    } catch (error) {
      toast(error.message || "Could not rename it.");
      return;
    }
    name.textContent = wanted;
  };
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
}

/** §7.10 prints `180 ms`. Two values need saying rather than printing:
 *  a call that never ran has no duration at all, and one that finished
 *  inside a millisecond is not instant — `0 ms` claims a precision the
 *  measurement does not have. */
function latencyLabel(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms === 0 ? "<1 ms" : `${ms} ms`;
}

/** §7.10's rows lead with a clock time, not a date: the log is read as
 *  "what happened today", and §6 rule 14's date grouping is for lists of
 *  things rather than for a stream of calls. */
function clockLabel(stamp) {
  const when = new Date(String(stamp).replace(" ", "T") + "Z");
  if (Number.isNaN(when.getTime())) return "";
  const today = new Date();
  const sameDay =
    when.getFullYear() === today.getFullYear() &&
    when.getMonth() === today.getMonth() &&
    when.getDate() === today.getDate();
  // §7.10's rows read `14:02`. `h23` rather than the locale's own cycle,
  // because the column is 56px and fixed — `02:37 AM` does not fit it, and a
  // log is scanned down a column rather than read a line at a time.
  const clock = when.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  return sameDay ? clock : `${dateLabel(stamp)} ${clock}`;
}

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
      // `R-9`: `Restore` lives in the header, once. The capture put a
      // secondary one here AND a primary one in the body; §6 rule 2 forbids
      // that, and §7.12's own argument — "people come here to undo something
      // they just did" — says which copy to keep.
      headHtml: `
        <button class="btn" data-size="sm" data-variant="secondary"
                data-restore="${esc(item.kind)}:${esc(item.id)}">${icon("restore")}Restore</button>
        <span class="menu-wrap">
          <button class="icon-btn" aria-label="More" data-menu-for="arch-${esc(item.id)}"
                  aria-haspopup="true" aria-expanded="false">${icon("more")}</button>
          <span class="menu" id="arch-${esc(item.id)}" hidden role="menu">
            <button class="menu-item" role="menuitem"
                    data-restore="${esc(item.kind)}:${esc(item.id)}">Restore</button>
            <span class="menu-sep"></span>
            <button class="menu-item" role="menuitem" data-variant="danger"
                    data-delete="${esc(item.kind)}:${esc(item.id)}">Delete permanently</button>
          </span>
        </span>`,
      bodyHtml: `
        ${item.detail ? `<p class="rule-full">${esc(item.detail)}</p>` : ""}
        ${
          item.extra
            ? `<span class="field-label">Original link</span>
               <input class="input" readonly value="${esc(item.extra)}">`
            : ""
        }`,
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
  wireListSearch(node);

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

  wireMenus(node);

  /* §7.12's one job. `R-9` puts it in the header; §9.4 gives it no dialog and
     an Undo toast instead, because undoing an undo is cheap. */
  for (const button of node.querySelectorAll("[data-restore]")) {
    button.addEventListener("click", async () => {
      const [kind, id] = button.dataset.restore.split(":");
      const card = button.closest(".exp-card");
      try {
        await post(`/api/archive/${kind}/${id}`, { restore: true });
      } catch (error) {
        toast(error.message || "Could not restore it.");
        return;
      }
      card.remove();
      hideEmptiedGroups(node);
      refreshCounts();
      toast("Restored.", {
        action: "Undo",
        onAction: async () => {
          try {
            await post(`/api/archive/${kind}/${id}`, {});
          } catch {
            toast("Could not put it back.");
            return;
          }
          window.location.assign(href("/archive"));
        },
      });
    });
  }

  /* §9.4: "Delete one archived item | confirm dialog". One step — the thing
     is already set aside, and the engine refuses anything that is not. */
  for (const button of node.querySelectorAll("[data-delete]")) {
    button.addEventListener("click", async () => {
      const [kind, id] = button.dataset.delete.split(":");
      const card = button.closest(".exp-card");
      const label = card.querySelector(".item-title").textContent.trim();
      const yes = await confirmDialog({
        title: `Delete this ${kind} for good?`,
        body: `“${label}” is removed from Brain permanently. This cannot be undone.`,
        confirmLabel: "Delete permanently",
      });
      if (!yes) return;
      try {
        await post(`/api/delete/${id}`, { item_type: kind });
      } catch (error) {
        toast(error.message || "Could not delete it.");
        return;
      }
      card.remove();
      hideEmptiedGroups(node);
      refreshCounts();
      toast("Deleted for good.");
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
          <!-- R-18. Nothing in the app told anyone that the file they
               dropped in was kept at all. The day count comes from the
               server, which reads it from the same setting the sweeper
               does, so the sentence cannot drift from the behaviour.
               (No backticks in an HTML comment: it sits inside a template
               literal, so one would end the string.) -->
          ${settingRow({
            label: "Keep the file Brain receives",
            help: data.retention.keep_uploads
              ? `On. Brain holds the file you send for ${data.retention.upload_days} days, then deletes it — ` +
                `long enough to look at if a source comes out wrong. What Brain read from it stays either way.`
              : `Off. Brain deletes the file the moment it finishes reading it. If a source comes out ` +
                `wrong there will be nothing left to check it against.`,
            controlHtml: switchHtml("keep_uploads", data.retention.keep_uploads),
          })}
          ${settingRow({
            // The help promised "Markdown in a zip" and the one link handed
            // back `brain-library.json`: there is no zip, and the endpoint
            // serves one file in whichever of the two formats is asked for.
            // Checkpoint B's block 5 says "a one-click JSON / Markdown
            // export", so both are offered and the sentence says what
            // arrives.
            label: "Export everything",
            help: "Sources, transcripts, summaries, rules and skills, as one file.",
            controlHtml: `<span class="btn-row">
              <a class="btn" data-variant="secondary" href="/api/export?format=markdown"
                 download>Markdown</a>
              <a class="btn" data-variant="secondary" href="/api/export?format=json"
                 download>JSON</a>
            </span>`,
          })}
          ${settingRow({
            label: "Delete my library",
            help: "Removes every source and everything made from it. Your account stays.",
            controlHtml: `<button class="btn" data-variant="danger"
                                  data-wipe="library">Delete library</button>`,
          })}
          ${settingRow({
            label: "Delete my account",
            help: "Closes the account and everything in it.",
            controlHtml: `<button class="btn" data-variant="danger"
                                  data-wipe="account">Delete account</button>`,
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

  /* `R-16`, closing `OPEN-2`: one confirmation layer, and it is the typed
     word. The second step the plan asked for could not be built honestly —
     there is no password and no mail sender — and a field that accepts
     anything and is never checked is worse than no field. */
  for (const button of node.querySelectorAll("[data-wipe]")) {
    button.addEventListener("click", async () => {
      const whole = button.dataset.wipe === "account";
      const yes = await confirmDialog({
        title: whole ? "Delete your account?" : "Delete your library?",
        // `R-18`: the last clause is the one people get wrong. Credits pay
        // for reading a source, not for keeping it, so clearing the library
        // returns none of them — and an agent used to be told the opposite.
        body: whole
          ? "Your library, your connections, your keys and every file you sent are destroyed. Export first if you want to keep anything. This cannot be undone."
          : "Every source, transcript, summary, rule and skill is destroyed, along with every file you sent. Your account and your settings stay. Your credits stay spent — they paid for the reading, which is already done. This cannot be undone.",
        confirmLabel: whole ? "Delete account" : "Delete library",
        word: "DELETE",
      });
      if (!yes) return;
      try {
        await post(whole ? "/api/delete-account" : "/api/delete-library", {
          typed: "DELETE",
        });
      } catch (error) {
        toast(error.message || "Could not do that.");
        return;
      }
      if (whole) {
        window.location.assign(href("/signin"));
        return;
      }
      refreshCounts();
      toast("Your library is empty.");
      window.location.assign(href("/profile"));
    });
  }
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
  // The sign-in page has no counts to show and no session to read them with,
  // so asking produced a guaranteed 401 in the console on every load of the
  // one page a signed-out person ever sees. Two of them, with the theme
  // read — enough noise to bury a real error.
  if (onSignIn()) return;
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

/** `R-19`: the theme the person chose, on every page load.
 *
 *  It was only ever applied by the Profile picker's click handler, and
 *  `shell.html` hardcodes `data-theme="light"` — so someone who chose Dark
 *  got Light back on every reload, and on every deep link. The choice is
 *  already stored and already served; nothing read it at boot.
 *
 *  It runs ahead of the first render and never blocks it: a theme that
 *  arrives a frame late is a flash, a render that waits on the network is a
 *  blank page.
 */
/** True on the one route that is reachable without a session. */
function onSignIn() {
  return matchRoute(window.location.pathname)?.route?.frame === "signin";
}

async function applyStoredTheme() {
  if (onSignIn()) return;
  try {
    const me = await api("/api/profile");
    const theme = me.theme_mode === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
    // Kept for the OAuth consent card, which is served outside the shell and
    // so cannot ask the API for it.
    try { localStorage.setItem("brain.theme", theme); } catch (_) {}
  } catch (_) {
    // Signed out, or offline. The document's own `data-theme` stands.
  }
}

if (typeof document !== "undefined" && document.getElementById("content")) {
  const router = createRouter();
  wireShell(router);
  applyStoredTheme();
  router.render(window.location.pathname).then(refreshCounts);
  window.addEventListener("popstate", refreshCounts);
}
