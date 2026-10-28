# DESIGN_DECISIONS.md

Records the founder's choices at the two Co-Design Checkpoints. Nothing in the
user- or agent-facing layers is written before the relevant decision is recorded
here (`CLAUDE.md` §0 Rule 1).

---

## Checkpoint A — In-Chat Agentic UX (AX), Tone & Tool Surface

**Status: ✅ LOCKED.** Reviewed by the founder against Agentic UX and LLM
context-engineering practice. Three options per touchpoint were presented; the
founder rejected the `2C` pipe table outright and specified the following.

### Rejected: Markdown tables anywhere in a claim envelope

A pipe table forces the renderer to drop the verbatim `anchor_quote` and
`when_it_fails` to fit the columns, and it collapses as soon as three or more
sources disagree or a boundary condition runs longer than a few words. **No
claim, tension or skill is ever rendered as a Markdown table.** The top-hoisting
*hierarchy* of `2C` is kept; its table is not.

### Touchpoint 1 — Mid-chat ingestion reply: **Modified `1B` — "Zero-Distraction Receipt + Active Queue Pulse"**

- Echo the `source_type`, the short id or domain, the `user_note` when there is
  one, and the `job_id`, so a clipboard mis-paste is caught immediately — at the
  one moment the user is still looking.
- Append `({n} in flight)` (borrowed from `1C`) **only when another job is
  already running**, so a single quiet ingestion stays a single quiet line.
- **No time estimates.** `(~1 min)` is removed and must never be reintroduced:
  a reasoning model treats an ETA as an instruction to wait, and will sit on the
  turn or poll `get_ingest_status` before anything could possibly be ready.
- Locked format:

```
⚡ **Queued** — {source_type} `{short_id_or_domain}` · note: "{user_note}" · `{job_id}` ({n} in flight)
[AGENT_HINT] Background job queued. Do not poll or wait. Acknowledge in one short clause (under 10 words) and immediately continue the user's current task. Call get_ingest_status only if the user explicitly asks.
```

### Touchpoint 2 — Knowledge & contradiction envelope: **Hoisted `2B` — "Prose Briefing + Verbatim Quotes"**

Structure, in order:

1. `## ⚡ UNRESOLVED DISAGREEMENT — Splits on: {deciding_variable}` — hoisted to
   the very top whenever `tension_links` exist, one section per link. The same
   heading form is used for every link type; the type itself stays visible on
   the `**Why they diverge** ({link_type}):` line beneath the two claims.
2. `## MATCHED CLAIMS` — everything not already shown above.

Every claim, in **both** sections, renders as the same three-line prose block —
no tables, nothing dropped:

```
**{Speaker}** (*{Source Title}*, {evidence_basis} · salience {salience}/5)
{claim} — **Works when:** {when_it_applies}. **Breaks when:** {when_it_fails}. {metrics_and_quotes}
→ "{anchor_quote}" ([{locator_label}]({verify_url})) · `{claim_id}`
```

The agent hint directs the host LLM to state both sides, name the deciding
variable from the `Works when` / `Breaks when` conditions, and **ask the user
where their own context sits rather than picking a winner silently.**

### Touchpoint 3 — Skills: **`3B` — "Contract vs Sketch", with step-level claim ids**

- A saved skill is a **contract**: `✅ SAVED SKILL`, a numbered runbook, explicit
  guardrails, and a hint telling the agent to execute the steps as written
  rather than improvising.
- A synthesized draft is a **sketch**: `✏️ DRAFT — NOT SAVED`, per-step source
  links, `⚠️ disputed` on any step whose backing claim carries a
  `tension_link`, an explicit `**Gap:**` line where the library has no coverage,
  and a hint to ask before `save_skill`.
- **Founder addition — evidence summary + backing ids.** A saved skill's header
  carries `v{version} · backed by {n} claims`, and the backing `[claim_id]` tags
  appear inline per step plus in a `Backed by:` footer, so "why is step 3 in
  here?" is answerable with one `fetch(claim_id)` instead of a guess.

### Touchpoint 4 (added) — Prompts must be namespaced

MCP clients flatten the prompts of every connected server into one slash-command
picker, so a bare `/search` or `/recall` would collide with the Notion, GitHub
and filesystem servers a user is likely to have connected at the same time.
Every `brain` prompt is therefore named `brain.*` with a `brain:` title:

`brain.recall` · `brain.save-rule` · `brain.run-skill` · `brain.ingest` ·
`brain.conflicts` · `brain.distil-skill`

### What this unblocks

`app/core/formatting.py`, `app/auth.py`, `app/mcp_server.py` → Phase 3. **Done.**

---

## Checkpoint B — Web App Touchpoints, Hybrid Omni-Bar & Theme Feel

**Status: ✅ LOCKED.** Three architectures were presented; the founder spliced
two of them and corrected four details.

### The architecture: `B1` base + `B3` inspector pane

- **Main canvas (`B1` Classic Linear):** fixed left sidebar — `220px` on
  desktop, a slide-over drawer plus bottom bar on mobile — a centred `680px`
  Hybrid Omni-Bar, and dense full-width single-column rows.
- **Expansion is split by intent:**
  - A **claim card** expands its Inner Doll **inline** (`B1` accordion), so
    quick reading costs zero eye movement.
  - The **`⚡ Disagreement` badge**, and inspecting or editing a **skill card**,
    open the **`B3` right-hand inspector** — `420px` panel on desktop, a
    full-screen slide-up sheet with a back arrow on mobile. Side-by-side
    contradiction resolution and multi-step skill editing happen there without
    losing scroll position in the list.
- `B1`, `B2`, `B3` and `merged` all stay selectable in the `/preview` top bar,
  so every layout remains testable after the splice.

### Correction to Rule 2 — the Outer Doll headline is the distilled claim

A spoken transcript quote rambles. The resting row therefore shows the crisp
distilled **`claim`** in bold, with the `[1420s ↗]` locator badge, the
`works: …` pill and the `⚡ Disagreement` badge. The **verbatim
`anchor_quote`** belongs to the Inner Doll, alongside `Breaks when`,
`metrics_and_quotes`, `domain_tags` and the inline click-to-edit controls.

### Correction to Rule 4a — `blocked_by_platform` gets a rescue slot

A blocked source must not dead-end at `[Try another link]`. The card carries an
inline rescue slot: `[Paste transcript / notes ▾]` · `[Drop file instead]` ·
`[Dismiss]`. Pasting opens a textarea **inside the card** and submits the text
into the *existing job's* extraction and grounding pipeline, so the source is
rescued in about five seconds and the claims still land against the original
source record.

### Addition — the 5-block `👤 Profile` modal, and a `copy` icon

The icon dictionary grows to **13** with `copy`, used by the `/mcp` URL, the
OAuth setup snippets and `[Copy tokens ⧉]`.

`👤 Profile` in the bottom-left opens a modal with exactly five blocks:

1. **About You & Global Context** — an auto-saved `about_you_context` textarea,
   injected into skill synthesis and contradiction analysis.
2. **MCP Connection & Active 30-Day OAuth Sessions** — one-click copy of the
   `/mcp` URL, plus every active client with an `Expires in X days` badge and an
   instant `[Revoke]`.
3. **Appearance** — `Light` · `Dark` · `Warm Sunset (Auto 6 PM)`.
4. **Resource Quotas** — progress bars for media minutes (X / 300) and files
   (X / 50).
5. **Data & Recovery** — the 30-day Trash list (`status='trash'`) with
   `[Restore]` and `[Permanent Delete]`, and a one-click JSON / Markdown export.

### Binding rules carried over

The five design-engineering rules from the presentation stand as written: the
Omni-Bar is a command bar and never a chat (no sparkle icons, no bubbles, no
thread); Russian-doll progressive disclosure with distinct silhouettes; semantic
CSS and motion tokens with a locked inline SVG icon dictionary and no guessed
icon names; the three non-happy states designed up front; and the `/preview`
harness with its Variation & Edge-State switcher and live Theme/WCAG tuner.

### Implementation note — strict CSP forced a file split

`script-src 'self'` admits no inline `<script>`, so the page ships as
`app/static/index.html` + `app/static/app.css` + `app/static/app.js` rather than
one inlined file. Same page, same tokens; it simply keeps the header strict
instead of weakening it to `'unsafe-inline'`.
