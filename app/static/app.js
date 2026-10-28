/* brain — Web Control Plane behaviour.
 *
 * Checkpoint B rules enforced here:
 *   - typing only ever FILTERS (locally, 0ms, no request);
 *   - a URL or dropped file INGESTS and raises a stepper, ending in a
 *     peak-end summary banner;
 *   - Enter PROPOSES a mutation, which the user confirms with one tap, and
 *     execution leaves a one-line receipt with [Undo / Revert to vN];
 *   - no chat thread and no sparkle icon anywhere.
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  view: "everything",
  scope: "all",
  claims: [],
  skills: [],
  counts: {},
  jobs: [],
  filter: "",
  plan: null,
  csrf: readCookie("brain_csrf") || "",
  signedIn: false,
  profile: null,
  expanded: new Set(),
  preview: location.pathname === "/preview",
  banners: {},
};

function readCookie(name) {
  const hit = document.cookie.split("; ").find((row) => row.startsWith(name + "="));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : "";
}

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}

function icon(name, cls = "icon") {
  return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

async function api(path, options = {}) {
  const init = { headers: { Accept: "application/json" }, ...options };
  if (init.body && !(init.body instanceof FormData)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  if ((init.method || "GET") !== "GET") init.headers["X-Brain-CSRF"] = state.csrf;
  if (state.forceOffline) return offlinePayload();
  let response;
  try {
    response = await fetch(path, init);
  } catch (_) {
    // A dropped connection used to reject here, which meant every caller's
    // `if (!data.ok)` branch was skipped and the page half-rendered in silence.
    // It now comes back as an ordinary failed payload, so the error strip the
    // rest of the app already uses does the talking.
    return offlinePayload();
  }
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok && !payload.reason) payload.reason = "http_" + response.status;
  payload.status = response.status;
  return payload;
}

function offlinePayload() {
  return {
    ok: false,
    reason: "offline",
    status: 0,
    message: "brain could not be reached. Check your connection — nothing was lost.",
  };
}

/* ===================== strips: confirm + receipt ===================== */
function clearStrips() { $("#strips").innerHTML = ""; }

function showConfirm(plan) {
  state.plan = plan;
  const note = plan.message ? `<span class="strip-note">${esc(plan.message)}</span>` : "";
  $("#strips").innerHTML = `
    <div class="strip strip-confirm">
      <div class="strip-text">${esc(plan.confirm_text)}${note}</div>
      <button class="btn btn-primary btn-sm" id="confirm-yes">Confirm</button>
      <button class="btn btn-sm btn-quiet" id="confirm-no">Cancel</button>
    </div>`;
  $("#confirm-yes").onclick = runPlan;
  $("#confirm-no").onclick = () => { state.plan = null; clearStrips(); };
}

function showReceipt(receipt) {
  const undo = receipt.undo_action
    ? `<button class="btn btn-sm" id="receipt-undo">${icon("undo")} ${esc(receipt.undo_label || "Undo")}</button>`
    : "";
  $("#strips").innerHTML = `
    <div class="strip ${receipt.ok ? "strip-receipt" : "strip-error"}">
      <div class="strip-text">${esc(receipt.text)}</div>
      ${undo}
      <button class="btn btn-sm btn-quiet" id="receipt-dismiss">Dismiss</button>
    </div>`;
  $("#receipt-dismiss").onclick = clearStrips;
  if (receipt.undo_action) {
    $("#receipt-undo").onclick = async () => {
      const out = await api("/api/omnibar/undo", {
        method: "POST",
        body: { action: receipt.undo_action, params: receipt.undo_params },
      });
      showReceipt(out.receipt || { ok: false, text: out.message || "Could not undo." });
      await refresh();
    };
  }
}

function showError(message) {
  $("#strips").innerHTML =
    `<div class="strip strip-error"><div class="strip-text">${esc(message)}</div>
     <button class="btn btn-sm btn-quiet" id="receipt-dismiss">Dismiss</button></div>`;
  $("#receipt-dismiss").onclick = clearStrips;
}

async function runPlan() {
  if (!state.plan) return;
  const out = await api("/api/omnibar/run", { method: "POST", body: { plan: state.plan } });
  state.plan = null;
  // Clearing the box without clearing the filter would leave the grid showing
  // nothing, filtered by a command the user can no longer see.
  $("#omni-input").value = "";
  state.filter = "";
  setMode("filter");
  if (out.receipt) showReceipt(out.receipt);
  else showError(out.message || "That command could not be run.");
  await refresh();
}

/* ===================== the omni-bar ===================== */
let planTimer = null;

function setMode(mode) { $("#omni").dataset.mode = mode; }

function onType() {
  const text = $("#omni-input").value;
  // 0ms: filtering is local, never a request.
  state.filter = text.trim().toLowerCase();
  const isUrl = /^(https?:\/\/|www\.)\S+$/i.test(text.trim());
  setMode(isUrl ? "ingest" : text.trim() ? "command" : "filter");
  $("#omni-hint").innerHTML = isUrl
    ? '<span class="omni-kbd">↵</span> to add to your library'
    : '<span class="omni-kbd">↵</span> to command';
  renderCards();
  // The scope tokens still need the server's parse, but only for display.
  clearTimeout(planTimer);
  planTimer = setTimeout(async () => {
    if (!text.includes("@")) return;
    const out = await api("/api/omnibar", { method: "POST", body: { text, submitted: false } });
    if (out.plan) {
      state.scope = out.plan.scope === "archived" ? "all" : out.plan.scope;
      syncScopeChips();
      renderCards();
    }
  }, 120);
}

async function onSubmit() {
  const text = $("#omni-input").value.trim();
  if (!text) return;
  const out = await api("/api/omnibar", { method: "POST", body: { text, submitted: true } });
  if (!out.plan) { showError(out.message || "Could not read that."); return; }
  const plan = out.plan;

  if (plan.mode === "ingest") {
    const result = await api("/api/ingest", { method: "POST", body: { url: plan.text } });
    $("#omni-input").value = "";
    state.filter = "";
    if (!result.ok) { showError(result.message); return; }
    if (result.duplicate) { showReceipt({ ok: true, text: "Already in your library — " + result.title }); }
    await refresh();
    pumpJobs();
    return;
  }
  if (plan.mode === "command" && plan.requires_confirmation) { showConfirm(plan); return; }
  if (plan.mode === "clarify") { showError(plan.message); return; }
  // A filter: nothing to confirm, the grid already reflects it.
  state.filter = plan.text.toLowerCase();
  if (plan.message) showError(plan.message);
  renderCards();
}

function syncScopeChips() {
  $$("#scope-chips .scope-chip").forEach((chip) => {
    chip.setAttribute("aria-pressed", String(chip.dataset.scope === state.scope));
  });
}

/* ===================== ingestion stepper + banner ===================== */
const STAGES = ["URL", "Captions", "Claims", "Grounding", "Links"];
const STAGE_BY_STATUS = { queued: 1, fetching: 2, extracting: 4, linking: 5, ready: 5 };

function stepper(status) {
  const reached = STAGE_BY_STATUS[status] || 0;
  const done = status === "ready";
  return STAGES.map((label, index) => {
    const position = index + 1;
    let stage = "todo";
    if (done || position < reached) stage = "done";
    else if (position === reached) stage = "active";
    return `<span class="step" data-state="${stage}"><span class="step-dot"></span>${label}</span>` +
      (position < STAGES.length ? '<span class="step-arrow">➔</span>' : "");
  }).join("");
}

function renderJobs() {
  const host = $("#jobs");
  const live = state.jobs.filter(
    (job) => !["ready"].includes(job.status) || state.banners[job.job_id]
  );
  host.innerHTML = live.map((job) => {
    const label = esc(job.title || job.input_target || job.source_type);
    if (job.status === "ready") {
      const extra = state.banners[job.job_id] || {};
      return `<div class="banner" data-job="${esc(job.job_id)}">
        <div class="job-title">${icon("check")} ${label}</div>
        <p class="banner-stats">${job.claims_extracted} claims grounded${
          extra.tensions ? ` · ⚡ ${extra.tensions} new disagreement${extra.tensions === 1 ? "" : "s"}` : ""
        }${extra.rejected ? ` · ${extra.rejected} quote${extra.rejected === 1 ? "" : "s"} rejected as ungrounded` : ""}</p>
        <div class="card-actions">
          <button class="btn btn-sm btn-primary" data-view-source="${esc(job.source_id)}">View claims</button>
          <button class="btn btn-sm btn-quiet" data-dismiss-job="${esc(job.job_id)}">Dismiss</button>
        </div></div>`;
    }
    if (job.status === "failed" || job.status === "blocked_by_platform") {
      // Rule 4a: a block is never a dead end — rescue it inline.
      return `<div class="job job-blocked" data-job="${esc(job.job_id)}">
        <div class="job-title">${icon("conflict")} ${
          job.status === "blocked_by_platform" ? "Platform refused this source" : "This source failed"
        }</div>
        <p class="banner-stats">${esc(job.error_message || label)}<br>
          Nothing was charged to your quota.</p>
        <div class="card-actions">
          <button class="btn btn-sm" data-rescue="${esc(job.job_id)}">${icon("edit")} Paste transcript / notes ▾</button>
          <label class="btn btn-sm">${icon("ingest")} Drop file instead
            <input type="file" class="hidden" data-rescue-file="${esc(job.job_id)}"></label>
          <button class="btn btn-sm btn-quiet" data-dismiss-job="${esc(job.job_id)}">Dismiss</button>
        </div>
        <div class="rescue hidden" data-rescue-slot="${esc(job.job_id)}">
          <textarea placeholder="Paste the transcript or your own notes here…"></textarea>
          <div class="card-actions">
            <button class="btn btn-sm btn-primary" data-rescue-go="${esc(job.job_id)}">Rescue this source</button>
            <span class="row-sub">Goes straight into this job's extraction &amp; grounding.</span>
          </div>
        </div></div>`;
    }
    return `<div class="job" data-job="${esc(job.job_id)}">
      <div class="job-title">${label}</div>
      <div class="stepper">${stepper(job.status)}</div></div>`;
  }).join("");
}

let pumping = false;
async function pumpJobs() {
  if (pumping) return;
  pumping = true;
  try {
    for (let round = 0; round < 8; round += 1) {
      const pending = state.jobs.filter(
        (job) => !["ready", "failed", "blocked_by_platform"].includes(job.status)
      );
      if (!pending.length) break;
      const out = await api("/api/jobs/run", { method: "POST", body: {} });
      (out.processed || []).forEach((done) => {
        if (done.status === "ready") {
          state.banners[done.job_id] = { tensions: done.tensions, rejected: done.rejected };
        }
      });
      await refresh();
    }
  } finally { pumping = false; }
}

/* ===================== cards ===================== */
function matchesFilter(text) {
  return !state.filter || String(text).toLowerCase().includes(state.filter);
}

function visibleClaims() {
  if (state.scope === "skills") return [];
  return state.claims.filter((claim) =>
    matchesFilter([claim.claim, claim.speaker, claim.source_title, (claim.domain_tags || []).join(" "),
                   claim.when_it_applies, claim.anchor_quote].join(" ")));
}

function visibleSkills() {
  if (state.scope === "claims") return [];
  return state.skills.filter((skill) =>
    matchesFilter([skill.name, skill.slug, skill.description, skill.trigger_when,
                   (skill.execution_steps || []).join(" ")].join(" ")));
}

function skillCard(skill) {
  const steps = skill.execution_steps || [];
  const disputed = (skill.linked_claim_ids || []).filter((id) =>
    (state.claims.find((c) => c.id === id) || {}).tensions?.length).length;
  const demo = /\(demo\)/.test(skill.name) ? '<span class="pill pill-demo">demo</span>' : "";
  return `<article class="card card-skill" data-skill="${esc(skill.id)}">
    <button class="card-head" data-inspect-skill="${esc(skill.id)}">
      <div style="min-width:0">
        <div class="card-title">${icon("check")} ${esc(skill.name)}</div>
        <div class="card-meta">
          <span class="pill pill-slug">${esc(skill.slug)}</span>
          <span class="pill">v${skill.version}</span>
          <span class="pill pill-trigger">${esc(steps.length)} step${steps.length === 1 ? "" : "s"}</span>
          ${demo}
          ${disputed ? `<span class="pill pill-conflict">${icon("conflict")} ${disputed} step disputed</span>` : ""}
        </div>
      </div>
      <span class="card-caret">›</span>
    </button></article>`;
}

function claimCard(claim) {
  const expanded = state.expanded.has(claim.id);
  const tension = (claim.tensions || [])[0];
  const demo = (claim.domain_tags || []).includes("demo")
    ? '<span class="pill pill-demo">demo</span>' : "";
  const locator = claim.locator_type === "timestamp"
    ? formatSeconds(claim.locator_value)
    : claim.locator_type === "page" ? "p." + claim.locator_value
    : claim.locator_value || "source";

  // Outer Doll: the distilled claim headline, NOT the raw quote.
  const head = `<button class="card-head" data-expand="${esc(claim.id)}">
      <div style="min-width:0">
        <div class="card-title">${esc(claim.claim)}</div>
        <div class="card-meta">
          <span class="pill">${esc(claim.speaker || claim.source_author || claim.source_title)}</span>
          ${claim.deep_link
            ? `<a class="pill pill-locator" href="${esc(claim.deep_link)}" target="_blank"
                  rel="noopener noreferrer" data-stop>${icon("locator")} ${esc(locator)} ↗</a>`
            : `<span class="pill pill-locator">${icon("locator")} ${esc(locator)}</span>`}
          <span class="pill">works: ${esc(trim(claim.when_it_applies, 42))}</span>
          ${demo}
          ${tension ? `<span class="pill pill-conflict" data-inspect-tension="${esc(claim.id)}"
               role="button" tabindex="0">${icon("conflict")} Disagreement</span>` : ""}
        </div>
      </div>
      <span class="card-caret">›</span>
    </button>`;

  // Inner Doll: the verbatim grounded quote and the editable fields.
  const body = expanded ? `<div class="card-body">
      <p class="quote">${icon("quote")} “${esc(claim.anchor_quote)}”</p>
      <dl class="field">
        <dt>Works when</dt><dd><span class="editable" contenteditable="plaintext-only"
            data-edit-field="when_it_applies" data-edit-claim="${esc(claim.id)}"
            >${esc(claim.when_it_applies)}</span></dd>
        <dt>Breaks when</dt><dd><span class="editable" contenteditable="plaintext-only"
            data-edit-field="when_it_fails" data-edit-claim="${esc(claim.id)}"
            >${esc(claim.when_it_fails || "—")}</span></dd>
        ${claim.metrics_and_quotes ? `<dt>Metrics</dt><dd>${esc(claim.metrics_and_quotes)}</dd>` : ""}
        <dt>Evidence</dt><dd>${esc(claim.evidence_basis)} · salience ${claim.salience}/5</dd>
        <dt>Tags</dt><dd>${esc((claim.domain_tags || []).join(", ") || "untagged")}</dd>
        <dt>Source</dt><dd>${esc(claim.source_title || "")} · <code>${esc(claim.id)}</code></dd>
      </dl>
      <div class="card-actions">
        ${tension ? `<button class="btn btn-sm" data-inspect-tension="${esc(claim.id)}">${icon("conflict")} Resolve side by side</button>` : ""}
        <button class="btn btn-sm" data-archive-claim="${esc(claim.id)}">${icon("archive")} ${
          state.view === "archived" ? "Restore" : "Archive"}</button>
      </div>
    </div>` : "";

  return `<article class="card card-claim" data-claim="${esc(claim.id)}"
    aria-expanded="${expanded}">${head}${body}</article>`;
}

function formatSeconds(value) {
  const total = parseInt(value, 10);
  if (Number.isNaN(total)) return String(value || "source");
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

function trim(text, limit) {
  const value = String(text || "");
  return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}

function renderCards() {
  const claims = visibleClaims();
  const skills = visibleSkills();
  $("#cards").innerHTML = [...skills.map(skillCard), ...claims.map(claimCard)].join("");
  $("#omni-counts").textContent = state.filter || state.scope !== "all"
    ? `${claims.length} claim${claims.length === 1 ? "" : "s"} · ${skills.length} skill${skills.length === 1 ? "" : "s"}`
    : "";
  renderEmptyState(claims.length + skills.length);
}

function renderEmptyState(visible) {
  const host = $("#empty-slot");
  const libraryEmpty = !state.claims.length && !state.skills.length;

  if (libraryEmpty && state.profile && state.profile.first_run) {
    host.innerHTML = `<div class="empty">
      <h2>Here's brain with three demo items</h2>
      <p>Everything below will be real and editable — one skill and two claims
         that genuinely disagree with each other.</p>
      <div class="empty-actions">
        <button class="btn btn-primary" id="seed-demo">Load the demo</button>
        <button class="btn" id="open-profile-empty">Connect to Claude</button>
      </div></div>`;
    $("#seed-demo").onclick = async () => {
      await api("/api/demo/seed", { method: "POST", body: {} });
      await refresh();
    };
    $("#open-profile-empty").onclick = openProfile;
    return;
  }
  if (libraryEmpty) {
    host.innerHTML = `<div class="empty"><h2>Your library is empty</h2>
      <p>Paste a YouTube link, an article or a GitHub markdown file into the bar above.</p></div>`;
    return;
  }
  if (!visible && state.filter) {
    // Rule 4b: zero matches offers one-click synthesis, not a shrug.
    host.innerHTML = `<div class="empty">
      <h2>Nothing matches “${esc(state.filter)}”</h2>
      <p>${state.counts.claims || 0} claims searched.</p>
      <div class="empty-actions">
        <button class="btn btn-primary" id="synth-go">Draft a skill from the closest claims</button>
        <button class="btn" id="clear-filter">Clear filter</button>
      </div></div>`;
    $("#clear-filter").onclick = () => {
      $("#omni-input").value = ""; state.filter = ""; renderCards();
    };
    $("#synth-go").onclick = async () => {
      const out = await api("/api/synthesize", { method: "POST", body: { task: state.filter } });
      if (!out.ok) { showError(out.message); return; }
      openSynthesis(out);
    };
    return;
  }
  host.innerHTML = "";
}

/* ===================== inspector pane ===================== */
function openInspector(title, html) {
  $("#inspector-title").textContent = title;
  $("#inspector-body").innerHTML = html;
  $("#inspector").dataset.open = "true";
  $("#inspector").setAttribute("aria-hidden", "false");
}

function closeInspector() {
  $("#inspector").dataset.open = "false";
  $("#inspector").setAttribute("aria-hidden", "true");
}

function sideBlock(claim, cls) {
  return `<div class="side ${cls}">
    <div class="card-title">${esc(claim.claim)}</div>
    <div class="card-meta">
      <span class="pill">${esc(claim.speaker || "Unattributed")}</span>
      <span class="pill">${esc(claim.evidence_basis)}</span>
      ${claim.deep_link ? `<a class="pill pill-locator" href="${esc(claim.deep_link)}"
        target="_blank" rel="noopener noreferrer">${icon("locator")} verify ↗</a>` : ""}
    </div>
    <p class="quote">“${esc(claim.anchor_quote)}”</p>
    <dl class="field">
      <dt>Works when</dt><dd>${esc(claim.when_it_applies || "—")}</dd>
      <dt>Breaks when</dt><dd>${esc(claim.when_it_fails || "—")}</dd>
    </dl></div>`;
}

function inspectTension(claimId) {
  const claim = state.claims.find((item) => item.id === claimId);
  if (!claim || !(claim.tensions || []).length) return;
  const blocks = claim.tensions.map((tension) => {
    const other = {
      claim: tension.other_claim, speaker: tension.other_speaker,
      evidence_basis: tension.other_evidence_basis, anchor_quote: tension.other_anchor_quote,
      when_it_applies: tension.other_when_it_applies, when_it_fails: tension.other_when_it_fails,
      deep_link: tension.other_deep_link,
    };
    return `<div class="splits-on">${icon("conflict")} <strong>${esc(tension.link_type)}</strong> —
        ${esc(tension.divergence_summary)}</div>
      <div class="side-by-side">${sideBlock(claim, "side-a")}${sideBlock(other, "side-b")}</div>`;
  }).join("<hr>");
  openInspector("Disagreement", blocks + `<div class="card-actions">
      <button class="btn btn-sm" data-archive-claim="${esc(claim.id)}">${icon("archive")} Archive this side</button>
    </div>`);
}

function inspectSkill(skillId) {
  const skill = state.skills.find((item) => item.id === skillId);
  if (!skill) return;
  const ids = skill.linked_claim_ids || [];
  const steps = (skill.execution_steps || []).map((step, index) => {
    const claimId = ids[index];
    const backing = claimId ? `<div class="step-claim">${esc(claimId)}</div>` : "";
    return `<li><span class="editable" contenteditable="plaintext-only"
      data-edit-step="${index}" data-edit-skill="${esc(skill.id)}">${esc(step)}</span>${backing}</li>`;
  }).join("");
  openInspector(skill.name, `
    <div class="card-meta">
      <span class="pill pill-slug">${esc(skill.slug)}</span>
      <span class="pill">v${skill.version}</span>
      <span class="pill">backed by ${ids.length} claim${ids.length === 1 ? "" : "s"}</span>
    </div>
    <dl class="field" style="margin-top:12px">
      <dt>Trigger</dt><dd><span class="editable" contenteditable="plaintext-only"
        data-edit-trigger="${esc(skill.id)}">${esc(skill.trigger_when)}</span></dd>
      <dt>Guardrails</dt><dd>${esc(skill.guardrails || "—")}</dd>
    </dl>
    <h3 style="margin:14px 0 6px;font-size:.92em">Steps</h3>
    <ol class="steps">${steps}</ol>
    <div class="card-actions">
      <button class="btn btn-sm btn-primary" data-save-skill="${esc(skill.id)}">${icon("check")} Save changes</button>
      ${skill.version > 1 ? `<button class="btn btn-sm" data-revert-skill="${esc(skill.id)}"
        data-version="${skill.version - 1}">${icon("undo")} Revert to v${skill.version - 1}</button>` : ""}
      <button class="btn btn-sm" data-archive-skill="${esc(skill.id)}">${icon("archive")} Archive</button>
    </div>`);
}

function openSynthesis(data) {
  if (data.origin === "none") {
    openInspector("No coverage", `<p>Your library has nothing on this yet.</p>`);
    return;
  }
  const draft = data.draft_skill || data.skill;
  const steps = (draft.execution_steps || []).map((step) => `<li>${esc(step)}</li>`).join("");
  openInspector(data.origin === "saved" ? draft.name : "Draft — not saved", `
    <div class="card-meta"><span class="pill">${esc(data.origin)}</span></div>
    <dl class="field" style="margin-top:12px">
      <dt>Would trigger</dt><dd>${esc(draft.trigger_when)}</dd>
    </dl>
    <ol class="steps">${steps}</ol>
    ${draft.guardrails ? `<p class="row-sub">Guardrails: ${esc(draft.guardrails)}</p>` : ""}
    <div class="card-actions">
      <button class="btn btn-sm btn-primary" id="save-draft">${icon("check")} Save as a skill</button>
    </div>`);
  const saveDraft = $("#save-draft");
  if (saveDraft) {
    saveDraft.onclick = async () => {
      const out = await api("/api/skill", { method: "POST", body: {
        slug: draft.slug, name: draft.name, description: draft.description || "",
        trigger_when: draft.trigger_when, execution_steps: draft.execution_steps || [],
        guardrails: draft.guardrails || "", linked_claim_ids: draft.linked_claim_ids || [],
      }});
      if (!out.ok) { showError(out.message); return; }
      closeInspector();
      showReceipt({ ok: true, text: `Saved “${draft.name}”` });
      await refresh();
    };
  }
}

/* ===================== profile modal ===================== */
async function openProfile() {
  const data = await api("/api/profile");
  if (!data.ok) { showError(data.message || "Could not load your profile."); return; }
  state.profile = { ...(state.profile || {}), ...data };
  $("#mcp-url").value = data.mcp_url || "";
  $("#about-you").value = data.about_you_context || "";
  $("#sessions").innerHTML = (data.sessions || []).length
    ? data.sessions.map((session) => `<div class="row">
        <div class="row-main"><div>${esc(session.client_name)}</div>
          <div class="row-sub">Expires in ${session.expires_in_days} day${session.expires_in_days === 1 ? "" : "s"}</div></div>
        <button class="btn btn-sm btn-danger" data-revoke="${esc(session.session_id)}">Revoke</button>
      </div>`).join("")
    : '<p class="row-sub">No clients connected yet. Paste the link above into Claude, ChatGPT or Cursor.</p>';

  const quotas = data.quotas || {};
  $("#quotas").innerHTML = [
    ["Media minutes", quotas.media_minutes_used, quotas.media_minutes_cap, "min"],
    ["Files", quotas.files_used, quotas.files_cap, "files"],
  ].map(([label, used, cap, unit]) => {
    const pct = cap ? Math.min(100, Math.round((used / cap) * 100)) : 0;
    return `<div><div class="row-sub">${label} — ${used} / ${cap} ${unit}</div>
      <div class="meter${pct > 85 ? " warn" : ""}"><span style="width:${pct}%"></span></div></div>`;
  }).join("");

  const trash = await api("/api/trash");
  $("#trash").innerHTML = (trash.items || []).length
    ? trash.items.map((item) => `<div class="row">
        <div class="row-main"><div>${esc(item.title)}</div>
          <div class="row-sub">${esc(item.source_type)} · recoverable for 30 days</div></div>
        <button class="btn btn-sm" data-restore="${esc(item.id)}">${icon("undo")} Restore</button>
        <button class="btn btn-sm btn-danger" data-delete="${esc(item.id)}">${icon("trash")} Delete</button>
      </div>`).join("")
    : '<p class="row-sub">Trash is empty.</p>';

  const audit = await api("/api/audit-logs?limit=25");
  $("#audit-log").innerHTML = (audit.entries || []).length
    ? (audit.blocked_count
        ? `<p class="row-sub">${icon("conflict")} ${audit.blocked_count} blocked attempt${
            audit.blocked_count === 1 ? "" : "s"}</p>`
        : "") +
      audit.entries.map((entry) => `<div class="row">
        <div class="row-main">
          <div${entry.blocked ? ' class="audit-blocked"' : ""}>${esc(entry.reasoning_trace)}</div>
          <div class="row-sub">${esc(entry.created_at)} · ${esc(entry.surface)}${
            entry.client_name ? " · " + esc(entry.client_name) : ""}${
            entry.target_id ? " · " + esc(entry.target_id) : ""}</div>
        </div>
      </div>`).join("")
    : '<p class="row-sub">Nothing yet. Actions appear here as they happen.</p>';

  $$("#theme-choice [data-theme-pick]").forEach((button) => {
    button.classList.toggle("btn-primary", button.dataset.themePick === (data.theme_mode || "light"));
  });
  $("#profile-scrim").classList.remove("hidden");
}

function closeProfile() { $("#profile-scrim").classList.add("hidden"); }

let aboutTimer = null;
function onAboutInput() {
  clearTimeout(aboutTimer);
  $("#about-status").textContent = "Saving…";
  aboutTimer = setTimeout(async () => {
    const out = await api("/api/profile", {
      method: "POST", body: { about_you_context: $("#about-you").value },
    });
    $("#about-status").textContent = out.ok ? "Saved" : (out.message || "Could not save");
  }, 600);
}

/* ===================== theme ===================== */
function applyTheme(mode, { persist = true } = {}) {
  const resolved = mode === "warm_sunset" && !isAfterSix() ? "warm_sunset" : mode;
  document.documentElement.dataset.theme = resolved;
  if (persist) {
    try { localStorage.setItem("brain.theme", mode); } catch (_) { /* private mode */ }
  }
  $$("[data-theme-pick]").forEach((button) => {
    button.classList.toggle("btn-primary", button.dataset.themePick === mode);
  });
}

function isAfterSix() {
  const hour = new Date().getHours();
  return hour >= 18 || hour < 6;
}

function initTheme() {
  let stored = "light";
  try { stored = localStorage.getItem("brain.theme") || "light"; } catch (_) { /* ignore */ }
  if (stored === "auto") {
    applyTheme(isAfterSix() ? "warm_sunset" : "light", { persist: false });
  } else {
    applyTheme(stored, { persist: false });
  }
}

/* ===================== /preview harness ===================== */
let harnessReady = false;
function initHarness() {
  if (!state.preview || harnessReady) return;
  harnessReady = true;
  $("#harness").classList.remove("hidden");
  $("#tuner").classList.remove("hidden");
  document.body.dataset.preview = "true";
  const params = new URLSearchParams(location.search);
  setLayout(params.get("layout") || "merged");
  $("#state-pick").value = params.get("state") || "happy";
  applyEdgeState($("#state-pick").value);

  $$("[data-layout-pick]").forEach((button) => {
    button.onclick = () => { setLayout(button.dataset.layoutPick); syncPreviewUrl(); };
  });
  $("#state-pick").onchange = () => { applyEdgeState($("#state-pick").value); syncPreviewUrl(); };
  $("#harness-reseed").onclick = async () => {
    await api("/api/demo/seed", { method: "POST", body: {} });
    await refresh();
  };

  const tune = (id, apply) => {
    const slider = $(id);
    slider.oninput = () => { apply(slider.value); updateContrast(); };
  };
  tune("#t-hue", (v) => setVar("--accent", `hsl(${v} 72% 56%)`));
  tune("#t-border", (v) => setVar("--border-subtle", mixBorder(v)));
  tune("#t-radius", (v) => setVar("--radius", v + "px"));
  tune("#t-density", (v) => setVar("--density", String(v / 100)));
  tune("#t-duration", (v) => setVar("--duration-ui", v + "ms"));
  $("#auto-sunset").onchange = (event) => {
    if (event.target.checked) {
      try { localStorage.setItem("brain.theme", "auto"); } catch (_) { /* ignore */ }
      applyTheme(isAfterSix() ? "warm_sunset" : "light", { persist: false });
    }
  };
  $("#copy-tokens").onclick = copyTokens;
  updateContrast();
  syncPreviewUrl();
}

function setLayout(layout) {
  $("#shell").dataset.layout = ["B1", "B2", "B3", "merged"].includes(layout) ? layout : "merged";
  $$("[data-layout-pick]").forEach((button) => {
    button.classList.toggle("btn-primary", button.dataset.layoutPick === layout);
  });
}

function syncPreviewUrl() {
  const url = `?layout=${$("#shell").dataset.layout}&state=${$("#state-pick").value}`;
  history.replaceState(null, "", url);
  $("#harness-url").textContent = url;
}

function setVar(name, value) { document.documentElement.style.setProperty(name, value); }

function mixBorder(strength) {
  const dark = document.documentElement.dataset.theme !== "light";
  const level = Number(strength) / 100;
  return dark
    ? `rgba(255,255,255,${(0.04 + level * 0.22).toFixed(3)})`
    : `rgba(16,16,26,${(0.03 + level * 0.18).toFixed(3)})`;
}

function applyEdgeState(name) {
  state.edgeState = name;
  // `offline` is shaped like every other harness state: nothing is faked, the
  // real request path is made to fail so the real error strip renders.
  state.forceOffline = name === "offline";
  if (state.forceOffline) {
    showError(offlinePayload().message);
    renderJobs();
    renderCards();
    return;
  }
  clearStrips();
  // The harness never fakes data: it shapes the real thing instead.
  if (name === "zero-match") {
    $("#omni-input").value = "renewal negotiation tactics";
    state.filter = "renewal negotiation tactics";
  } else if (name === "ingesting") {
    state.jobs = [{ job_id: "job_preview", status: "extracting", title: "Outbound Masterclass",
                    source_type: "youtube", claims_extracted: 0, error_message: "" }];
  } else if (name === "blocked") {
    state.jobs = [{ job_id: "job_preview", status: "blocked_by_platform",
                    title: "Members-only talk", source_type: "youtube", claims_extracted: 0,
                    error_message: "YouTube refused that video (members-only or age-restricted)." }];
  } else if (name === "empty") {
    state.claims = []; state.skills = []; state.jobs = [];
  } else {
    state.filter = "";
    $("#omni-input").value = "";
  }
  if (name === "first-run") {
    state.claims = []; state.skills = []; state.jobs = [];
    state.profile = { ...(state.profile || {}), first_run: true };
  }
  renderJobs();
  renderCards();
}

function luminance(rgb) {
  const channel = (value) => {
    const v = value / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
}

function readRgb(name, fallbackEl) {
  const probe = document.createElement("span");
  probe.style.color = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  (fallbackEl || document.body).appendChild(probe);
  const parsed = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g) || [0, 0, 0];
  probe.remove();
  return parsed.slice(0, 3).map(Number);
}

function ratio(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

function updateContrast() {
  const card = readRgb("--bg-card");
  const pairs = [
    ["text", readRgb("--text-primary"), 4.5],
    ["muted", readRgb("--text-muted"), 4.5],
    ["border", readRgb("--border-subtle"), 3],
  ];
  $("#contrast").innerHTML = pairs.map(([label, colour, floor]) => {
    const value = ratio(colour, card);
    const pass = value >= floor;
    return `<span class="${pass ? "contrast-pass" : "contrast-fail"}">${label} ${value.toFixed(2)}:1 ${
      pass ? "✓" : "⚠"}</span>`;
  }).join(" ");
}

function copyTokens() {
  const names = ["--bg-canvas", "--bg-card", "--bg-card-hover", "--bg-inset", "--border-subtle",
    "--border-strong", "--text-primary", "--text-muted", "--accent", "--accent-quiet",
    "--accent-conflict", "--accent-ok", "--ease-ui", "--duration-ui", "--radius", "--density"];
  const computed = getComputedStyle(document.documentElement);
  const block = `:root[data-theme="${document.documentElement.dataset.theme}"] {\n` +
    names.map((name) => `  ${name}:${computed.getPropertyValue(name).trim()};`).join("\n") + "\n}";
  copyText(block, $("#copy-tokens"));
}

function copyText(text, button) {
  const done = () => {
    if (!button) return;
    const original = button.innerHTML;
    button.innerHTML = icon("check") + " Copied";
    setTimeout(() => { button.innerHTML = original; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); } catch (_) { /* nothing more we can do */ }
  area.remove();
  done();
}

/* ===================== data + events ===================== */
async function refresh() {
  const data = await api("/api/cards?view=" + encodeURIComponent(state.view));
  if (!data.ok) { showError(data.message || "Could not load your library."); return; }
  state.claims = data.claims || [];
  state.skills = data.skills || [];
  state.counts = data.library_counts || {};
  state.jobs = data.active_jobs || [];
  $('[data-count="all"]').textContent = (state.counts.claims || 0) + (state.counts.skills || 0);
  $('[data-count="skills"]').textContent = state.counts.skills || 0;
  $('[data-count="claims"]').textContent = state.counts.claims || 0;
  $('[data-count="archived"]').textContent = state.counts.archived_claims || 0;
  renderJobs();
  renderCards();
}

function setView(view) {
  state.view = view;
  $$(".nav-item[data-view]").forEach((button) =>
    button.setAttribute("aria-current", String(button.dataset.view === view)));
  $$(".mobile-bar [data-view]").forEach((button) =>
    button.setAttribute("aria-current", String(button.dataset.view === view)));
  $("#sidebar").dataset.open = "false";
  refresh();
}

async function signIn(email) {
  const out = await api("/api/login", { method: "POST", body: { email } });
  if (!out.ok) { showError(out.message || "Could not sign in."); return; }
  state.csrf = out.csrf;
  await boot();
}

async function boot() {
  const me = await api("/api/me");
  state.signedIn = Boolean(me.signed_in);
  state.profile = me;
  $("#signin").classList.toggle("hidden", state.signedIn);
  $("#omni-wrap").classList.toggle("hidden", !state.signedIn);
  if (!state.signedIn) return;
  if (me.theme_mode) {
    let stored = null;
    try { stored = localStorage.getItem("brain.theme"); } catch (_) { /* ignore */ }
    if (!stored) applyTheme(me.theme_mode, { persist: false });
  }
  await refresh();
  if (state.preview) applyEdgeState($("#state-pick").value);
  pumpJobs();
}

/* delegated clicks — one listener for the whole page */
document.addEventListener("click", async (event) => {
  const target = event.target;
  const hit = (attr) => target.closest(`[${attr}]`);

  if (target.closest("[data-stop]")) return; // let deep links through

  const copy = hit("data-copy");
  if (copy) { copyText($(copy.dataset.copy).value, copy); return; }

  // The disagreement badge lives *inside* the card-head button, so it has to be
  // tested before `data-expand` or it would toggle the accordion instead of
  // opening the inspector.
  const tension = hit("data-inspect-tension");
  if (tension) {
    event.preventDefault();
    event.stopPropagation();
    inspectTension(tension.dataset.inspectTension);
    return;
  }
  const skill = hit("data-inspect-skill");
  if (skill) { inspectSkill(skill.dataset.inspectSkill); return; }

  const expand = hit("data-expand");
  if (expand) {
    const id = expand.dataset.expand;
    state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
    renderCards();
    return;
  }

  const archiveClaim = hit("data-archive-claim");
  if (archiveClaim) {
    const restore = state.view === "archived";
    const out = await api(`/api/archive/claim/${archiveClaim.dataset.archiveClaim}`,
      { method: "POST", body: { restore } });
    if (!out.ok) { showError(out.message); return; }
    showReceipt({ ok: true, text: restore ? "Restored" : "Archived — recoverable any time",
      undo_action: restore ? "archive_item" : "restore_item",
      undo_params: { item_type: "claim", id: archiveClaim.dataset.archiveClaim }, undo_label: "Undo" });
    closeInspector();
    await refresh();
    return;
  }
  const archiveSkill = hit("data-archive-skill");
  if (archiveSkill) {
    const out = await api(`/api/archive/skill/${archiveSkill.dataset.archiveSkill}`,
      { method: "POST", body: {} });
    if (!out.ok) { showError(out.message); return; }
    closeInspector();
    showReceipt({ ok: true, text: "Archived", undo_action: "restore_item",
      undo_params: { item_type: "skill", id: archiveSkill.dataset.archiveSkill }, undo_label: "Undo" });
    await refresh();
    return;
  }
  const revert = hit("data-revert-skill");
  if (revert) {
    const out = await api(`/api/skill/${revert.dataset.revertSkill}/revert`,
      { method: "POST", body: { version: Number(revert.dataset.version) } });
    if (!out.ok) { showError(out.message); return; }
    closeInspector();
    showReceipt({ ok: true, text: `Reverted to v${revert.dataset.version}` });
    await refresh();
    return;
  }
  const saveSkill = hit("data-save-skill");
  if (saveSkill) {
    const skillId = saveSkill.dataset.saveSkill;
    const record = state.skills.find((item) => item.id === skillId);
    if (!record) return;
    const steps = $$("[data-edit-step]", $("#inspector-body")).map((node) => node.textContent.trim());
    const trigger = $("[data-edit-trigger]", $("#inspector-body"));
    const out = await api("/api/skill", { method: "POST", body: {
      slug: record.slug, name: record.name, description: record.description,
      trigger_when: trigger ? trigger.textContent.trim() : record.trigger_when,
      execution_steps: steps, guardrails: record.guardrails,
      output_template: record.output_template, linked_claim_ids: record.linked_claim_ids,
    }});
    if (!out.ok) { showError(out.message); return; }
    const previous = record.version;
    closeInspector();
    showReceipt({ ok: true, text: `Updated “${record.name}” · now v${out.skill.version}`,
      undo_action: "revert_skill", undo_params: { skill_id: record.id, version: previous },
      undo_label: `Revert to v${previous}` });
    await refresh();
    return;
  }

  const rescue = hit("data-rescue");
  if (rescue) {
    const slot = $(`[data-rescue-slot="${rescue.dataset.rescue}"]`);
    if (slot) slot.classList.toggle("hidden");
    return;
  }
  const rescueGo = hit("data-rescue-go");
  if (rescueGo) {
    const jobId = rescueGo.dataset.rescueGo;
    const slot = $(`[data-rescue-slot="${jobId}"]`);
    const text = slot ? $("textarea", slot).value : "";
    const out = await api(`/api/jobs/${jobId}/rescue`, { method: "POST", body: { text } });
    if (!out.ok) { showError(out.message); return; }
    state.banners[jobId] = { tensions: out.tensions, rejected: out.rejected };
    showReceipt({ ok: true, text: `Rescued · ${out.claims} claims grounded from your paste` });
    await refresh();
    return;
  }
  const dismiss = hit("data-dismiss-job");
  if (dismiss) {
    delete state.banners[dismiss.dataset.dismissJob];
    state.jobs = state.jobs.filter((job) => job.job_id !== dismiss.dataset.dismissJob);
    renderJobs();
    return;
  }
  const viewSource = hit("data-view-source");
  if (viewSource) {
    delete state.banners[Object.keys(state.banners)[0]];
    setView("library");
    return;
  }

  const revoke = hit("data-revoke");
  if (revoke) {
    await api(`/api/sessions/${revoke.dataset.revoke}/revoke`, { method: "POST", body: {} });
    await openProfile();
    return;
  }
  const restore = hit("data-restore");
  if (restore) {
    await api(`/api/archive/source/${restore.dataset.restore}`, { method: "POST", body: { restore: true } });
    await openProfile();
    await refresh();
    return;
  }
  const remove = hit("data-delete");
  if (remove) {
    if (!window.confirm("Permanently delete this source and every claim from it? This cannot be undone.")) return;
    const out = await api(`/api/delete/${remove.dataset.delete}`, { method: "POST", body: {} });
    if (!out.ok) { showError(out.message); return; }
    await openProfile();
    await refresh();
    return;
  }

  const themePick = hit("data-theme-pick");
  if (themePick) {
    applyTheme(themePick.dataset.themePick);
    if (state.signedIn) {
      await api("/api/profile", { method: "POST", body: { theme_mode: themePick.dataset.themePick } });
    }
    updateContrast();
    return;
  }

  const nav = target.closest("[data-view]");
  if (nav) { setView(nav.dataset.view); return; }
});

/* field-level edits commit on blur */
document.addEventListener("focusout", async (event) => {
  const node = event.target.closest && event.target.closest("[data-edit-claim]");
  if (!node) return;
  const claimId = node.dataset.editClaim;
  const field = node.dataset.editField;
  const value = node.textContent.trim();
  const record = state.claims.find((item) => item.id === claimId);
  if (!record || (record[field] || "") === value || value === "—") return;
  const out = await api(`/api/claim/${claimId}`, { method: "POST", body: { [field]: value } });
  if (!out.ok) { showError(out.message); return; }
  await refresh();
});

/* drag & drop a file or a link onto the page */
["dragover", "drop"].forEach((name) => {
  document.addEventListener(name, async (event) => {
    event.preventDefault();
    if (name !== "drop" || !state.signedIn) return;
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) {
      const form = new FormData();
      form.append("file", file);
      const out = await api("/api/upload", { method: "POST", body: form });
      if (!out.ok) { showError(out.message); return; }
      await refresh();
      pumpJobs();
      return;
    }
    const text = event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("text/plain");
    if (text) {
      $("#omni-input").value = text.trim();
      onType();
      await onSubmit();
    }
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeInspector();
    closeProfile();
    state.plan = null;
    clearStrips();
  }
  if (event.key === "/" && document.activeElement !== $("#omni-input")) {
    event.preventDefault();
    $("#omni-input").focus();
  }
});

/* ===================== wiring ===================== */
window.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initHarness();
  const input = $("#omni-input");
  input.addEventListener("input", onType);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); onSubmit(); }
  });
  $$("#scope-chips .scope-chip").forEach((chip) => {
    chip.onclick = () => { state.scope = chip.dataset.scope; syncScopeChips(); renderCards(); };
  });
  $("#open-profile").onclick = openProfile;
  $("#open-profile-mobile").onclick = openProfile;
  $("#profile-close").onclick = closeProfile;
  $("#profile-scrim").addEventListener("click", (event) => {
    if (event.target === $("#profile-scrim")) closeProfile();
  });
  $("#inspector-close").onclick = closeInspector;
  $("#about-you").addEventListener("input", onAboutInput);
  $("#signin-go").onclick = () => signIn($("#signin-email").value.trim());
  $("#signin-email").addEventListener("keydown", (event) => {
    if (event.key === "Enter") signIn($("#signin-email").value.trim());
  });
  $("#sidebar-toggle").onclick = () => {
    const bar = $("#sidebar");
    bar.dataset.open = bar.dataset.open === "true" ? "false" : "true";
  };
  boot();
});
