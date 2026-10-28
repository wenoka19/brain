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

### Touchpoint 1 — Mid-chat ingestion reply: **Modified `1B` — "Zero-Distraction Receipt"**

- Keep the URL / id echo and the user's note, so a clipboard mis-paste is caught
  immediately, at the one moment the user is still looking.
- **No time estimates.** `(~1 min)` is removed and must never be reintroduced:
  a reasoning model treats an ETA as an instruction to wait, and will sit on the
  turn or poll `get_ingest_status` before anything could possibly be ready.
- The hint is fixed, verbatim:
  `Background job queued. Do not poll or wait. Acknowledge in one short clause (under 10 words) and immediately continue the user's current task.`

### Touchpoint 2 — Knowledge & contradiction envelope: **Hoisted `2B` — "Prose Briefing + Verbatim Quotes"**

Structure, in order:

1. `## ⚡ UNRESOLVED DISAGREEMENT (Deciding variable: {variable})` — hoisted to
   the very top, one section per tension link.
2. `## MATCHED CLAIMS` — everything that is not already shown above.

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
- **Founder addition:** a saved skill also carries its backing `[claim_id]` tags
  (inline per step, plus a `Backed by:` footer), so "why is step 3 in here?" is
  answerable with one `fetch(claim_id)` instead of a guess.

### What this unblocks

`app/core/formatting.py`, `app/auth.py`, `app/mcp_server.py` → Phase 3.

---

## Checkpoint B — Web App Touchpoints, Hybrid Omni-Bar & Theme Feel

**Status: 🛑 OPEN — options to be presented after Phase 3 ships.**
Blocked until this is filled in: `app/static/index.html`, `app/omnibar.py`,
Phases 4 & 5.
