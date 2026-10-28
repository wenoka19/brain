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

**Status: 🛑 OPEN — three architectural options presented in chat, awaiting the
founder's hybrid-merge.**

The founder set five design-engineering rules that any accepted option must
satisfy. They are binding on `app/static/index.html` and `app/omnibar.py`
regardless of which layout is chosen:

1. **Tesler's & Jakob's Law — the Omni-Bar is a command bar, never a chatbot.**
   No `✨` sparkle icons, no chat bubbles, no thread. Typing filters cards in
   0ms; dropping a URL or file raises a Live Ingestion Stepper Card
   (`[✓ URL] ➔ [● Captions] ➔ [○ Claims] ➔ [○ Grounding] ➔ [○ Links]`) ending in
   a Peak-End Summary Banner; `Enter` on a natural-language command mutates
   cards in place and leaves a one-line receipt bar with `[Undo / Revert to vN]`.
   `@` scoping is supported (`@Skills`, `@Library`, `@skill-slug`).
2. **"Russian Doll" progressive disclosure.** Skill cards and claim cards have
   distinct silhouettes. The resting grid shows only the three highest-signal
   elements (the Outer Doll); clicking expands the Inner Doll with the verbatim
   quote, steps, guardrails and inline click-to-edit fields.
3. **Semantic tokens + a locked icon dictionary.** No guessed external icon
   names — a 12-icon inline SVG dictionary. Semantic CSS variables
   (`--bg-canvas`, `--bg-card`, `--border-subtle`, `--accent-conflict`,
   `--ease-ui`, `--duration-ui`) across `light`, `dark` and `warm_sunset`, the
   last using a gentler `260ms` easing and auto-activating at 18:00 local.
4. **Non-happy edge states designed up front:** `blocked_by_platform` recovery,
   a 0-match search offering one-click skill synthesis, and first-run onboarding
   pre-loaded with one demo skill and two conflicting demo claims.
5. **A preview harness:** a top-bar Variation & Edge-State Switcher and a
   bottom-bar Live Theme/Contrast Tuner on the preview route, so the founder can
   test and tune every variation and state visually.

Blocked until this is filled in: `app/static/index.html`, `app/omnibar.py`,
Phases 4 & 5.
