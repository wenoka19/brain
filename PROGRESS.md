# PROGRESS — `brain`

Status board for the build. Updated and committed after every phase and
checkpoint, per `CLAUDE.md` §0 Rule 2.

| Phase | Scope | State |
|---|---|---|
| 1 | Security primitives, dual SQLite DBs, hybrid search | ✅ complete |
| 2 | Gatekeeper, grounded extraction, contradiction linker | ✅ complete |
| **Checkpoint A** | In-chat Agentic UX (AX), tone & tool surface | ✅ locked by founder |
| 3 | MCP server + OAuth 2.1 / 30-day refresh | ✅ complete |
| **Checkpoint B** | Web app, Omni-Bar, theme feel | ✅ locked by founder |
| 4–5 | Web app, CLI, CI/CD, production packaging | ✅ complete |
| 6 | Live AI providers + security hardening | ✅ complete |

---

## Phase 1 — Autonomous Core (complete)

**Built**

- `.claude/setup.sh` — idempotent VM setup; verifies `sqlite-vec` loads and the
  SQLite build has FTS5 before anything else runs.
- `app/config.py` — `BRAIN_ENV` fork (`test` / `prod`), all caps, quotas, rate
  limits and the **tenant path resolver** (`tenant_db_path`) that refuses any
  `user_id` outside `^[A-Za-z0-9_-]{1,64}$`.
- `app/security/ssrf.py` — scheme/credential/port screening, DNS resolution of
  every A/AAAA record, IPv4+IPv6 private/loopback/link-local/CGNAT/metadata
  blocklist, IPv4-mapped-IPv6 unwrapping, and `safe_get()` with **redirects
  disabled** so each hop is re-resolved and re-validated. In `BRAIN_ENV=test`
  an offline DNS table is used and unknown hosts **fail closed**.
- `app/security/sanitizer.py` — control-character + invisible-Unicode stripper
  (zero-width, bidi overrides, Unicode Tags block `U+E0000–U+E007F`, variation
  selectors, BOM), FTS5 query sanitizer that quotes every term and drops
  reserved keywords, and the `[UNTRUSTED_LIBRARY_DATA_*]` spotlighting wrapper
  with **fence-breakout neutralisation** (a transcript cannot close our fence
  and then forge an `[AGENT_HINT]`).
- `app/security/ratelimit.py` — per-user sliding windows (30 tool calls/min,
  10 ingestions/min) plus the runaway-agent circuit breaker on identical
  `(tool, args)` fingerprints inside 15s.
- `app/db/control.py` — `users`, `oauth_sessions`, `ingestion_jobs`,
  `audit_logs`; SHA-256-only token storage, 1h access / absolute 30-day
  refresh with rotation that never extends the ceiling, instant revocation,
  atomic quota reservation, and job leasing with expiry recovery.
- `app/db/tenant.py` — full tenant schema, LRU connection cache, FTS5
  external-content indexes and `vec0` 256-d cosine indexes, all kept in sync by
  triggers.
- `app/db/search.py` — BM25 + cosine KNN fused with **Reciprocal Rank Fusion**
  (k=60), with `tension_links` joined onto every claim hit, symmetrically.

**Design note — how `claims_vec` stays trigger-synchronised.** A trigger on
`epistemic_claims` cannot fill a vec0 row, because the embedding is not a column
of that table. Embeddings therefore land in the staging tables `claim_vectors` /
`skill_vectors`, and triggers fan them out to `claims_vec` / `skills_vec`.
Triggers on the content tables propagate `status` and `source_id` changes and
handle deletes, so `status = 'active'` filtering *inside* the KNN `WHERE` clause
is always correct and the index is never stale.

**Verified** — `tests/test_phase1_db_and_security.py`, 82 tests, all green:
SSRF (12 hostile URL shapes), spotlighting fence-breakout, FTS5
`integrity-check` after update/delete triggers, archive propagation into the
vec index, cross-tenant isolation, path-traversal refusal, token-hash-only
storage, refresh rotation, quota atomicity, lease recovery, and RRF ranking.


---

## Phase 2 — Autonomous Core (complete)

**Built**

- `app/ingestion/gatekeeper.py` — URL normaliser (every YouTube link shape
  collapses to one canonical `watch?v=`; `github.com/.../blob/...` rewrites to
  `raw.githubusercontent.com`; tracking params stripped, identity params kept),
  V1 scope enforcement (playlists, channels, livestreams, raw media, >120 min),
  and file validation: extension allow-list, 20MB cap, **magic-byte sniffing**
  (a renamed `.exe` is refused), and a DOCX zip-bomb guard on both inflated
  size (30MB) and per-entry compression ratio.
- `app/ingestion/providers.py` — the determinism seam. `BRAIN_ENV=test` gets a
  hashed 256-d embedder, offline fixtures, and rule-based extractor/classifier;
  prod gets live fetchers with the same deterministic extractor as a documented
  fallback until an LLM key is configured.
- `app/ingestion/fetchers.py` — yt-dlp via **argument arrays only**
  (`shell=False`, `--no-config`, `--no-playlist`, `--max-filesize 25M`, timeout,
  and `--` immediately before the URL), WebVTT parsing into timestamped cues,
  HTML extraction that drops scripts/nav/footer, Markdown/DOCX heading sections,
  and PDF parsing capped at 200 pages with per-page locators.
- `app/ingestion/extractor.py` — cue-boundary-aware semantic chunker (so a
  quote never straddles two locators), Toulmin schema coercion, and the
  **grounding gate**.
- `app/ingestion/grounder.py` — `rapidfuzz` verbatim matcher. A quote below 88
  similarity is rejected outright, and an accepted quote is **replaced by the
  span actually found in the source**, so what we store and cite is the
  source's own words. The matching cue supplies the exact locator: video
  seconds, PDF page, or section heading.
- `app/ingestion/linker.py` — batched Top-K cosine probe, then shape
  classification. Same-source pairs and duplicate links are skipped.
- `app/core/engine.py` — the 8 canonical operations returning **plain
  structured data** (no Markdown — that is Checkpoint A's layer), the
  fetch → extract → ground → link pipeline, and `run_pending_jobs_sync()`.

**Design notes**

- *Nothing an extractor says is trusted.* The extractor proposes; the grounder
  verifies. A deliberately hallucinating extractor is tested, and gets zero
  claims through.
- *A failure costs no quota.* A YouTube job books a provisional 15 minutes at
  queue time, reconciles against the real duration once known, and releases
  everything if the job fails or is blocked by the platform.
- *Similarity is not disagreement.* Two experts who agree look identical to a
  vector index, so a link is only written when the pair's shape says something:
  an asymmetric stance inversion (contradicts) or an added boundary condition
  (refines). A claim that names where something *breaks down* refines the advice
  it bounds rather than contradicting it.
- *Known limitation of the deterministic extractor.* Link **typing** and the
  `when_it_applies` field are approximate under the rule-based extractor; this
  is the main thing a prod LLM extractor improves. The grounding guarantee is
  unaffected — it is arithmetic, not judgement.

**Verified** — `tests/test_phase2_ingestion_grounding.py`, 134 tests. Full
suite: **216 passed**. Covers hostile URL shapes, renamed-payload uploads, zip
bombs, yt-dlp argument-array safety, VTT/HTML/PDF/DOCX parsing, verbatim vs
paraphrase vs hallucinated quotes, a lying extractor, a crashing extractor,
quota release on failure, duration reconciliation, cross-tenant job invisibility,
end-to-end prompt-injection containment, and byte-identical determinism across
two tenants on the same input.


---

## Checkpoint A — locked

The founder reviewed the three options per touchpoint against Agentic UX and
LLM context-engineering practice, **rejected Markdown tables outright** (they
force the renderer to drop the verbatim quote and `when_it_fails`, and collapse
at three or more disagreeing sources), and specified: modified `1B` receipt with
no time estimate, hoisted `2B` prose briefing, `3B` contract-vs-sketch with
step-level claim ids. Full specification in `DESIGN_DECISIONS.md`.

## Phase 3 — Co-designed MCP server + OAuth 2.1 (complete)

**Built**

- `app/core/formatting.py` — the locked envelopes. No Markdown table is
  emitted anywhere (asserted by test). Conflicts hoist above `## MATCHED
  CLAIMS`; every claim renders as the same three-line prose block carrying
  speaker, evidence basis, salience, `Works when` / `Breaks when`, the verbatim
  quote, a locator deep link (`23:40`, `p.14`, section) and the `claim_id`.
  Library content is fenced and **the `[AGENT_HINT]` is emitted after the fence**,
  so injected text can neither forge a hint nor be read as one. Every envelope
  is trimmed to the 1,500-token budget, and the fence and hint are never the
  thing that gets trimmed.
- `app/auth.py` — OAuth 2.1: RFC 9728 + RFC 8414 metadata, RFC 7591 dynamic
  registration, mandatory PKCE `S256` (`plain` refused), single-use 60-second
  authorization codes, audience-bound 1h JWT access tokens, absolute 30-day
  refresh with rotation, and RFC 7009 revocation that always answers 200 so a
  caller cannot probe which tokens exist. Verification requires **both** a valid
  signature *and* a live `control.db` row, so revocation takes a millisecond and
  a token we did not mint is never accepted.
- `app/mcp_server.py` — the 8 typed tools over Streamable HTTP at `/mcp`, each
  passing the same gate: authenticate → rate-limit / loop-break → execute →
  render → audit. Tool descriptions are written for the host LLM ("does not
  block", "never in a loop", "not deletion", "never on your own judgement").
  Six slash helpers are registered as prompts, all namespaced `brain.*`
  (`brain.recall`, `brain.save-rule`, `brain.run-skill`, `brain.ingest`,
  `brain.conflicts`, `brain.distil-skill`): clients flatten every connected
  server's prompts into one picker, so a bare `/search` or `/recall` would
  collide with Notion, GitHub and filesystem servers.
- `app/main.py` — FastAPI host with strict CSP, `X-Frame-Options: DENY`, HSTS
  and the `/mcp` transport's DNS-rebinding allow-list.

**Design notes and bugs caught**

- *The `[AGENT_HINT]` must live outside the untrusted fence.* Inside it, a
  hostile transcript could forge one. Asserted positionally in the tests.
- *A deciding variable is derived, never invented.* It comes from a quantified
  boundary in either side's conditions ("above 500 emails per day" → "daily
  email volume"); with nothing to go on, the header says so.
- *SQLite connections are now cached per thread.* The caches were process-wide,
  which breaks the moment an ASGI server runs sync work in a threadpool — two
  threads interleaving `BEGIN IMMEDIATE` on one connection would corrupt each
  other's transactions. Caught by the first HTTP test.
- *The `/mcp` transport needs its hostname declared.* DNS-rebinding protection
  answers `421` for any undeclared `Host`, so a deployed server rejects every
  request unless `BRAIN_PUBLIC_URL` / `BRAIN_ALLOWED_HOSTS` are set. Caught by
  the live-transport test and now covered by one.
- *Cosine KNN has no notion of "too far".* Without a floor, `search` answered a
  nonsense query with the library's least-unrelated claims — junk the host LLM
  might then cite. Measured against the v1-hash embedder (on-topic 0.29+,
  off-topic ≤0.14 on collisions alone) and floored at 0.18.
- *Smaller fixes found by rendering real output:* each tension was rendering
  twice (once per side, because `link_id` was not carried into the claim view);
  the heuristic extractor was emitting the *failure* clause as `Works when`; and
  `get_ingest_status` was the one tool whose calls were not being audited.

**Verified** — `tests/test_phase3_mcp_and_auth.py`. Full suite: **322 passed**.
Covers the locked envelope shape line by line, the no-tables rule across all
three renderers, fence-breakout containment, the token budget under a loaded
library, the RFC 7636 appendix-B PKCE vector, open-redirect refusal, code
replay, cross-audience token refusal, forged-signature refusal, signature-only
(no session row) refusal, rotation that never extends the 30-day ceiling,
instant revocation, every tool's schema and description, prompt namespacing and
argument requirements, the active-queue pulse appearing only when something else
is running, the loop breaker and rate limiter through the real tool gate, and a
full OAuth 2.1 → `/mcp` → `tools/call` round trip over HTTP.

### Environment variables Phase 3 introduces

| Variable | Required | Purpose |
|---|---|---|
| `BRAIN_JWT_SECRET` | **yes in prod** | HS256 signing key. Signing refuses to start without it in prod, since an ephemeral key would silently invalidate every live session on restart. |
| `BRAIN_PUBLIC_URL` | recommended | Public origin; sets the OAuth issuer and the transport Host allow-list. |
| `BRAIN_ALLOWED_HOSTS` | optional | Extra comma-separated hostnames for the allow-list. |
| `BRAIN_OWNER_EMAIL` | optional | Single-user shortcut: skips the consent form at `/authorize`. |

> The consent page at `/authorize` is deliberately unstyled. It is the minimum
> functional surface for Phase 3; how it should *look* is a Checkpoint B
> question and nothing about it is decided yet.


---

## Checkpoint B — locked

The founder spliced two of the three presented architectures — **`B1` linear
canvas + `B3` inspector pane** — and corrected four details: the Outer Doll
headline is the distilled `claim` rather than the raw quote, a
`blocked_by_platform` card carries an inline rescue slot, the icon dictionary
grows to 13 with `copy`, and the Profile modal has five named blocks. Full
specification in `DESIGN_DECISIONS.md`.

## Phase 4 — the Web Control Plane (complete)

**Built**

- `app/omnibar.py` — the hybrid router. Typing only ever filters (no request at
  all); a URL or dropped file ingests; `Enter` produces a **plan**, never an
  effect. Commands resolve by deterministic grammar — archive, restore, trash,
  rename, `edit step N of X to Y`, remember — with targets resolved either by
  recency ("yesterday's video") or by `rapidfuzz` against the user's own skill
  names, source titles and claim text. An LLM is consulted only when no pattern
  matches, and even then it can only return a plan. Execution returns a receipt
  carrying its own undo, which for a skill edit is `Revert to vN` against the
  `skill_versions` snapshot.
- `app/webapi.py` — `/api/*` and the `/preview` route. Browser sessions are a
  signed `SameSite=Strict` cookie, deliberately separate from MCP tokens, and
  every mutation also needs a double-submit CSRF header.
- `app/static/index.html` + `app.css` + `app.js` — the B1+B3 canvas, the
  13-icon inline sprite, three themes on semantic tokens, the Russian-doll
  cards, the inspector pane, the five-block Profile modal, the three non-happy
  states, and the `/preview` harness with its layout/edge-state switcher and
  live theme & WCAG tuner.
- New engine operations for the web surface only: `op_rescue_job`,
  `op_trash_item`, `op_list_trash`, `op_permanent_delete`, `op_revert_skill`,
  `op_update_claim`, `op_profile`, `op_update_profile`, `op_export`,
  `op_seed_demo`, `op_clear_demo`, `op_list_cards`.

**Design notes**

- *Hard deletion exists, and an agent cannot reach it.* `op_permanent_delete`
  refuses anything not already in Trash, lives behind `POST /api/delete/{id}`,
  and is not imported by `app/mcp_server.py` — asserted by test.
- *The grounded evidence is not editable from the UI.* Inline editing covers
  `claim`, the two conditions, metrics and tags; `anchor_quote` and its locator
  are ignored if a client sends them, because rewriting the evidence would break
  the library's one guarantee.
- *Strict CSP forced a file split.* `script-src 'self'` admits no inline
  `<script>`, so the page is three files instead of one. A test asserts there is
  no inline script and no `on*=` attribute anywhere in the HTML.
- *Playwright found two bugs a unit test would not have.* The fixed bottom
  tuner covered the sidebar's Profile button, and the `⚡ Disagreement` badge —
  which sits inside the card-head button — toggled the accordion instead of
  opening the inspector, because the delegated handler tested `data-expand`
  first.

## Phase 5 — CLI, worker, CI/CD, packaging (complete)

- `app/worker.py` — standalone async worker. Jobs are claimed under
  `BEGIN IMMEDIATE` with a lease, so replicas never collide; a worker killed
  mid-job leaves a lease that expires and is retried, bounded by `attempts`; a
  crashing job fails cleanly and refunds its reserved quota.
- `app/cli.py` — `doctor` (host capability report, and it fails in prod without
  `BRAIN_JWT_SECRET`), `selftest` (22 end-to-end checks in a throwaway
  directory), `preview-ax` (prints every Checkpoint A envelope against a real
  seeded library), `preview-ui` (serves the Checkpoint B harness), plus `serve`
  and `worker`.
- `Dockerfile` (multi-stage, non-root, healthcheck, Litestream included),
  `entrypoint.sh` (restores the replica *before* serving, supports
  `BRAIN_ROLE=web|worker`, `exec`s so signals arrive), `litestream.yml` (30s
  sync, 30-day retention matching the Trash window, tenant databases matched by
  pattern so a new user replicates without a config change), `railway.json` and
  `fly.toml` (both pinned to a single writer, because SQLite is
  single-writer).
- `.github/workflows/ci.yml` — pytest + doctor + selftest + preview-ax on
  Python 3.11/3.12/3.13, a browser job, and a Docker job that boots the image
  and curls `/healthz`.

> `tests/test_phase5_cli_and_packaging.py` is a fifth test file, beyond the four
> `CLAUDE.md` §3 lists. The packaging is a deliverable: a Dockerfile that stops
> copying `app/static`, or an entrypoint that stops restoring before it serves,
> is exactly the kind of failure no unit test catches.

---

## Final status

| | |
|---|---|
| `BRAIN_ENV=test pytest` | **554 passed** |
| `python -m app.cli selftest` | **27/27 checks, green** |
| `python -m app.cli doctor` | 0 failures, 0 warnings |
| `python -m app.cli preview-ax` | all three touchpoints render, 793/1500 tokens, no tables |

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `BRAIN_ENV` | — | `test` (deterministic, offline) or `prod`. Defaults to `test`. |
| `BRAIN_DATA_DIR` | — | Database location. `/data` in prod. |
| `BRAIN_JWT_SECRET` | **prod** | HS256 signing key. Signing refuses to start without it. |
| `BRAIN_PUBLIC_URL` | prod | OAuth issuer and the `/mcp` transport Host allow-list. Without it, `/mcp` answers 421. |
| `BRAIN_ALLOWED_HOSTS` | — | Extra comma-separated hostnames. |
| `BRAIN_OWNER_EMAIL` | — | Single-user shortcut past the consent form. |
| `BRAIN_PREVIEW` | — | `1` enables `/preview` outside test mode. |
| `BRAIN_ROLE` | — | `web` (default) or `worker`. |
| `R2_*`, `LITESTREAM_REPLICA_URL` | prod | Replication to Cloudflare R2. |
| `LLM_API_KEY` | prod (for live AI) | Turns on the live extractor, link classifier and vision. Unset = rule-based fallback. |
| `LLM_BASE_URL` | — | OpenAI-compatible base. Default `https://openrouter.ai/api/v1`. |
| `LLM_MODEL` | — | Default `google/gemini-2.5-flash`. Must be vision-capable for image ingestion. |
| `EMBEDDING_API_KEY` | — | Defaults to `LLM_API_KEY`. |
| `EMBEDDING_BASE_URL` | — | Defaults to `LLM_BASE_URL`. |
| `EMBEDDING_MODEL` | — | Default `text-embedding-3-small`, requested at 256 dimensions. |
| `BRAIN_SEARCH_FLOOR` / `BRAIN_LINK_FLOOR` | — | Override the auto-calibrated retrieval floors. |

### Known limitations, stated plainly

Three of the four gaps listed here at the end of Phase 5 were closed in Phase 6.
What remains:

- **Web sign-in is email-only.** It establishes the tenant on a single-person
  server; the real credentials live on the MCP side. A real IdP is the obvious
  next step if this ever has more than one user.
- **The neural retrieval floors (0.42 / 0.38) are reasoned, not measured.**
  They are calibrated for `text-embedding-3-small` at 256 dimensions from its
  published behaviour, where the hashed embedder's floors were measured against
  this library directly. A different model, or a different dimension count,
  needs `BRAIN_SEARCH_FLOOR` / `BRAIN_LINK_FLOOR` set after looking at real
  scores.
- **`yt-dlp` is a large third-party dependency** parsing attacker-supplied
  pages. It now runs with a scrubbed environment, `--no-exec`, `shell=False`
  and a flag terminator, but it is still the widest piece of attack surface in
  the system.


---

## Phase 6 — Live AI providers + security hardening (complete)

### Part A — the four honest gaps, closed

- **Live extractor and contradiction classifier** (`LiveLLMExtractor`,
  `LiveLinkClassifier`) over OpenAI-compatible `/chat/completions`, in JSON
  mode at temperature 0. Configured by `LLM_BASE_URL` (default OpenRouter),
  `LLM_API_KEY` and `LLM_MODEL` (default `google/gemini-2.5-flash`).
- **Live 256-dimension embeddings** (`LiveEmbedder`) over `/embeddings`, asking
  the host for `dimensions: 256` directly and L2-normalising what comes back.
  `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` default to the LLM's, since most
  hosts serve both off one key.
- **Live vision** (`LiveVisionProvider`) — a validated image is base64-encoded
  into a data URI and sent to the vision-capable `LLM_MODEL`, which returns a
  title, description, verbatim transcription and standalone observations. Each
  becomes its own cue, so a claim grounds against the exact sentence.
- **Floors follow the embedder.** `config.py` picks `0.18 / 0.15` for the
  hashed embedder and `0.42 / 0.38` for a dense neural one, overridable with
  `BRAIN_SEARCH_FLOOR` / `BRAIN_LINK_FLOOR`.

**Design notes**

- *The grounder still has the last word.* A live model's `anchor_quote` is a
  hypothesis like any other: it goes through the same `rapidfuzz >= 88` gate,
  and a test feeds the adapter a hallucinated quote to prove the claim is
  dropped. That is what makes a live model safe to trust here.
- *Source text is fenced inside the prompt too.* Both prompts carry the
  material between `<<<SOURCE_TEXT>>>` markers with an explicit instruction not
  to obey anything inside it — the same discipline applied on the way out.
- *Each slot degrades independently.* No `LLM_API_KEY` means the heuristic
  extractor and no vision; no embedding key means the hashed embedder. An LLM
  outage falls back rather than losing the source — **except** for embeddings,
  which raise instead, because silently mixing two vector spaces in one index
  is worse than failing the job. `doctor` reports each slot separately so a
  half-configured host is visible.

### Part B — the five hardening upgrades

1. **BOLA.** No route takes a `user_id` or a path; `webapi` and `mcp_server`
   each contain exactly one `build_context` call, fed only by the signed cookie
   or the verified JWT subject. Tests post `user_id`, `sub`, `email`, `tenant`
   and `db_path` at a live endpoint and confirm the attacker's own library
   filled while the victim's stayed empty, and that a real id from another
   tenant 404s on read, archive and delete.
2. **`lint_skill_safety`.** Blocks delimiter escapes, forged `[AGENT_HINT]` /
   `[SECURITY:` headers, prompt overrides, tool-command injection, destructive
   and exfiltration instructions, invisible Unicode tags and bidi overrides,
   and `javascript:` / `data:` URIs. It runs before `save_skill`, during GitHub
   `SKILL.md` ingestion, and on Omni-Bar skill edits; every refusal is audited
   with its reason. 21 attack strings blocked, 11 benign skill steps allowed —
   the first pattern set flagged "Act as a reviewer for the copy", so the
   `act as` family was replaced with restriction-removal vocabulary.
3. **Omni-Bar integrity.** `Enter` still returns a plan before anything
   executes, and a skill edit routed through the bar now passes the same linter
   `save_skill` uses, leaving the skill's version untouched when refused.
4. **Scrubbed subprocess environment.** `yt-dlp` sees exactly
   `PATH, HOME, TMPDIR, LC_ALL, LANG` — never `LLM_API_KEY`,
   `BRAIN_JWT_SECRET` or the R2 credentials — plus `--no-exec`, `cwd` in a
   scratch directory, and the existing `shell=False` and `--` terminator. The
   container user is `brainuser`.
5. **The audit log.** `audit_logs.reasoning_trace` (added by an idempotent
   additive migration, so an existing `control.db` upgrades on boot),
   `GET /api/audit-logs` serving only the caller's own rows, and
   `6 · Recent activity & audit log` in the Profile modal with blocked attempts
   highlighted.

**Bugs Phase 6 caught**

- *The browser test found another real one.* After running an Omni-Bar command
  the input cleared but `state.filter` did not, so the grid stayed filtered by
  a command the user could no longer see — an empty library, apparently.
- An additive migration was needed at all: `CREATE TABLE IF NOT EXISTS` does
  nothing to an existing table, so `reasoning_trace` would have been missing on
  any database created before this phase, and every audit write would have
  failed.

**Verified** — `tests/test_phase6_security_and_live_providers.py`, 120 tests,
every live adapter driven through `httpx.MockTransport` so the suite needs no
key and no network. Full suite: **554 passed**. `selftest`: **27/27**.
