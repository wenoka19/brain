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
| `BRAIN_ENV=test pytest` | **632 passed** |
| `python -m app.cli selftest` | **28/28 checks, green** |
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
| `LLM_MODEL` | — | Default `z-ai/glm-5.3-flash`. Must be vision-capable for image ingestion. `deepseek/deepseek-v4.1-flash` and `qwen/qwen3.8-flash` are drop-in. |
| `LLM_REASONING_EFFORT` | — | Default `low`, sent as OpenRouter's `reasoning.effort`. Empty string omits the field. Ignored for non-OpenRouter hosts. |
| `EMBEDDING_API_KEY` | — | Defaults to `LLM_API_KEY`. |
| `EMBEDDING_BASE_URL` | — | Defaults to `LLM_BASE_URL`. |
| `EMBEDDING_MODEL` | — | Default `qwen/qwen3-embedding-8b`, requested at 256 dimensions and prefix-sliced if the host returns its native 4096. |
| `BRAIN_SEARCH_FLOOR` / `BRAIN_LINK_FLOOR` | — | Override the auto-calibrated retrieval floors. |
| `R2_ENDPOINT_URL` (or `R2_ENDPOINT`) | for portability | S3 endpoint for tenant snapshots. All four R2 vars must be set, or snapshotting is off and tenants live only on the node's disk. |
| `R2_BUCKET` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | for portability | Credentials for the same. |
| `R2_TENANT_PREFIX` | — | Key prefix for tenant snapshots. Default `tenants/`. |
| `R2_REGION` | — | Default `auto`, which is what R2 wants. |
| `BRAIN_SNAPSHOT_DIR` | — | Where `VACUUM INTO` writes before upload. Default `$BRAIN_DATA_DIR/snapshots`. |
| `MAX_LOCAL_TENANTS` | — | `0` (default) keeps every tenant file on local disk. Above `0`, the least recently used *already-synced* tenants are evicted past that many files. |
| `BRAIN_SHARD_INDEX` / `BRAIN_SHARD_TOTAL` | — | Default `0`/`1` (single node). Above `1`, each node serves the tenants where `crc32(user_id) % total == index`. |
| `BRAIN_SHARD_URLS` | with sharding | Index-aligned base URLs; a request for another shard gets a `307`. |
| `BRAIN_SHARD_INSTANCES` | with sharding | Index-aligned Fly machine ids; a request for another shard gets `fly-replay: instance=…` instead. Takes precedence over `BRAIN_SHARD_URLS`. |
| `BRAIN_SHARD_INSTANCE_TEMPLATE` | — | Address for a shard with no explicit entry. Default `shard-{index}`, i.e. `fly-replay: instance=shard-2`. Empty string requires an explicit address instead. |

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


---

## Calibration run — the hashed embedder, measured

`python -m app.cli calibrate` runs the pipeline over a multi-topic corpus and
measures the two distributions the retrieval floors exist to separate. It pins
both floors to zero for the run, so the setting being calibrated cannot bound
its own measurement.

Measured against the deterministic `v1-hash` embedder at 256d, 11 claims across
4 topics:

```
query -> claim
  on-topic      n=17  min=0.000  med=0.265  p95=0.569  max=0.692
  off-topic     n=38  min=-0.208 med=0.000  p95=0.102  max=0.144

claim -> claim (different sources)
  same topic    n=9   min=0.080  med=0.163  p95=0.344  max=0.415
  cross topic   n=38  min=-0.011 med=0.096  p95=0.303  max=0.484
```

**`search_similarity_floor` = 0.183 measured, 0.18 shipped — confirmed.** The
floor sits in the gap between off-topic noise (p95 0.102, max 0.144) and the
on-topic median (0.265). The command also reports the cost it had been hiding:
**47% of same-topic matches score below it**, because a lexical embedder
legitimately gives zero to two claims that share a subject but no words. No
threshold rescues those; they are not retrievable by that probe at all.

**`link_similarity_floor` cannot be derived statistically, and the command now
says so instead of inventing a number.** Three findings, in order:

1. Deriving it from topic separation fails outright — same-topic claim pairs
   (median 0.163) score *below* cross-topic ones (p95 0.303).
2. Deriving it from "the weakest pair that became a link" is worse than
   useless, because it is backwards. The weakest link in the corpus scored
   **0.060** and is **spurious**: *"We always review warehouse pick paths
   before committing"* linked as `contradicts` to *"Never send renewal paper
   before the executive review"* — two unrelated topics sharing only generic
   words, caught by the asymmetric-negation rule.
3. The shipped **0.15 is therefore doing real work**: it excludes that false
   positive while admitting all three genuine outbound links (0.155, 0.239,
   0.415). Dropping the floor to the "derived" 0.042 would have admitted it.

So `calibrate` now prints every proposed link with its score and both claims,
marks the ones the shipped floor excludes, and states plainly that where to
draw the line is a judgement call about the operator's own sources rather than
a statistic.

**A code comment this disproved.** `HeuristicLinkClassifier` claimed
"unrelated claims share no content words at all" and that the vector lane had
already decided topicality. The warehouse/renewal pair refutes both: the
classifier's shared-token guard passes it, and only the cosine floor stops it.
The comment now says the floor and the guard share that job and neither is
sufficient alone.

**Floors unchanged** — 0.18 / 0.15 for the hashed embedder — now with measured
evidence rather than a reasoned estimate.

### Still not measured

The **neural floors (0.42 / 0.38) remain reasoned, not measured.** Calibrating
them needs a live embedding model, which needs two environment changes this
session does not have: an API key, and network access to the model host. Once
both exist, `python -m app.cli calibrate` produces the same table for the live
embedder in one command.


---

## Phase 7 — Scale-Ready Portability Layer

Stage 1 is a single Railway box. This phase makes Stage 2 (Hetzner bare-metal
NVMe) and Stage 3 (multi-shard Fly) a configuration change rather than a
rewrite, by removing the only two things that tie a tenant to a machine: where
its database file lives, and which process is allowed to write it.

### 1. Litestream is now strictly `control.db`

`litestream.yml` no longer carries the `users/user_*.db` wildcard. Litestream
watches each database with `inotify`; one watcher per tenant exhausts file
descriptors and inotify watches well before 10,000 tenants. The asymmetry is
deliberate and documented in the file itself:

* **`control.db`** — identity, OAuth sessions, the job queue, the audit log.
  Must lose nothing, so it keeps continuous WAL shipping at a 30s interval.
* **tenant databases** — reconstructible from their sources, so they trade a
  few seconds of exposure for being one self-contained file per tenant in
  object storage.

`tests/test_phase5_cli_and_packaging.py` now asserts the *absence* of the
tenant pattern, with the reason, so it cannot be re-added by accident.

### 2. Event-driven snapshots + lazy hydration (`app/storage/r2_sync.py`)

Writes mark, a thread flushes:

* Every successful tenant write calls `r2_sync.mark_dirty(user_id)` — a dict
  write behind a lock, so a `remember_this` never waits on a network. In the
  engine this is a `@_syncs_tenant` decorator on the ten write operations plus
  one call inside `process_job`'s `finish()`, so an ingestion that fails
  *after* writing a source row still marks the tenant.
* A background thread (`start_background`, started by both `app/main.py`'s
  lifespan and the standalone worker) snapshots anything dirty for longer than
  `snapshot_debounce_sec`. A burst of five claims costs **one** PUT, not five.
* The snapshot is `VACUUM INTO` — a single consistent file with no WAL
  sidecar, taken from a live database without blocking writers for a file
  copy. The temp file is removed whether or not the upload worked.
* **A failed upload keeps the tenant dirty.** The flag is cleared only by a
  2xx, and only if nothing was written during the upload. Losing a write
  silently is the one outcome the design refuses.
* **Lazy hydration on open**: `tenant_db.tenant(user_id)` downloads the tenant
  from R2 when the local file is absent, atomically (`.hydrating` staging file
  then `os.replace`, so a half-written database is never openable). Absent
  both locally and in R2 means a new user, and gets a fresh schema.
* **LRU disk guard** (`MAX_LOCAL_TENANTS`, default `0` = unlimited) evicts the
  least recently used tenants past the cap — and *never* one whose latest
  write is unsynced, since `is_dirty` is cleared only by a successful upload.
  The worst case of a wrong guess is a slower next request, never a lost
  write. Eviction is also skipped entirely when R2 is unconfigured, because
  there the file is the only copy.

SigV4 is implemented directly over `httpx` (~70 lines) rather than through
`boto3`, which would add ~50MB and a dependency tree to serve four verbs.

### 3. Stateless shard routing (`app/main.py`)

`ShardRoutingMiddleware` computes the owner of a request's tenant as
`crc32(user_id) % BRAIN_SHARD_TOTAL` — chosen over `hash()` precisely because
`hash()` is randomised per interpreter, so every node and process agrees
without a router to keep in sync. At the default `1`/`0` it falls straight
through.

A request for another shard gets `fly-replay: instance=…` (204) when Fly
machine ids are configured, or a `307` to that shard's base URL. Discovery,
sign-in and health paths belong to no tenant and are served anywhere. The
identity is read from a **verified** bearer token or a **signed** session
cookie, so a client cannot steer its own routing.

`doctor` now fails — not warns — if sharding is on while a shard has no
address, or while R2 is unconfigured: the first means two nodes could write
one tenant (last-snapshot-wins), the second means a shard cannot hydrate a
tenant it has never seen.

### What the tests prove, and what they don't

`tests/test_scale_portability.py` (41 tests) runs every R2 call through an
`httpx.MockTransport` holding objects in a dict, so it exercises the real
signing, the real `VACUUM INTO`, and the real atomic rename. The end-to-end
case snapshots a tenant, deletes the file off the node, and reads the same
claim back out of what hydration returned. `selftest` runs that same
round-trip as its 28th check.

**Not proven: that the SigV4 signature is one Cloudflare accepts.** `botocore`
is not installed in this environment, so there is no reference implementation
to cross-check against. The tests prove the canonical request has the
documented shape, that the signature is deterministic and changes with every
input it should (method, key, path, secret, region, date), and that the headers
are well formed. The first live PUT against a real bucket is still the real
verification — set the four `R2_*` vars and watch `doctor`, then one write.


---

## Phase 7b — Single-key OpenRouter setup

`LLM_API_KEY` alone now turns on the entire live stack. OpenRouter serves chat,
vision and `/embeddings` off one host, so `EMBEDDING_BASE_URL`,
`EMBEDDING_API_KEY` and `EMBEDDING_MODEL` exist only for an operator who
deliberately wants a second provider — none of them is needed for the default
path. (This supersedes the Phase 6 note that a second embedding host was
mandatory; the founder verified the OpenRouter `/embeddings` endpoint live on
2026-10-28.)

**Defaults**

| slot | default | why |
| --- | --- | --- |
| chat + vision | `z-ai/glm-5.3-flash` | native vision, a very large context, and cheap enough to extract every chunk of a two-hour transcript. `deepseek/deepseek-v4.1-flash` and `qwen/qwen3.8-flash` swap in through `LLM_MODEL` alone. |
| embeddings | `qwen/qwen3-embedding-8b` | Matryoshka-trained, so a 256-float prefix is itself a valid embedding. |

**Reasoning effort.** This class of model deliberates before answering.
Extraction is structured transcription, not a puzzle, so every chat payload
carries `reasoning: {"effort": "low"}` — latency and output tokens down, no
change in what the grounder will accept. It is gated on
`cfg.targets_openrouter`, because `reasoning` is OpenRouter's own parameter and
some OpenAI-compatible hosts reject a whole request for one unknown field.
`LLM_REASONING_EFFORT` raises it, and an empty value omits it.

**JSON parsing.** `_first_json_object` already stripped ```` ```json ```` fences
and surrounding prose; there are now tests pinning that behaviour across six
wrapper shapes, plus an end-to-end case proving a fenced reply still reaches the
`rapidfuzz >= 88` grounder with its quote intact.

**Matryoshka 256-dim guard.** `LiveEmbedder` asks for `dimensions: 256`, and if
the host ignores that and returns its native width (4096 for
`qwen3-embedding-8b`) it keeps the leading 256 floats and re-normalises to unit
length. The prefix, not a resample — which is what MRL training makes valid.
A *narrower* reply is refused rather than zero-padded: padding would produce a
vector comparable to nothing, and cosine distance would not complain. One test
pushes a sliced vector through `set_claim_embedding` and reads it back out of
`claims_vec` to prove it survives the `float[256]` column.

### The one thing to do with the live key before trusting retrieval

**The neural floors (0.42 / 0.38) are now wrong until recalibrated.** They were
reasoned for a model returning 256 dimensions natively. Slicing a 4096-float
Qwen vector to a 256-float prefix moves the whole similarity distribution, so
those numbers no longer describe the shipped default. `python -m app.cli
calibrate` measures the real on-topic vs. off-topic spread in one command and
prints the floors to set via `BRAIN_SEARCH_FLOOR` / `BRAIN_LINK_FLOOR`. Until
that runs, `search` may be either too strict or too permissive — the hashed
floors (0.18 / 0.15) remain measured and correct for `BRAIN_ENV=test`.
