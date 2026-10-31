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
| `BRAIN_ENV=test pytest` | **808 passed** |
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
| `BRAIN_ALLOWED_HOSTS` | — | Extra comma-separated hostnames for the `/mcp` Host allow-list (the DNS-rebinding defence). |
| `BRAIN_ALLOWED_ORIGINS` | — | Origin policy for `/mcp` and the OAuth endpoints. Default `*`, because that surface is bearer-authenticated and a strict list breaks every browser client it was not told about. A comma-separated list restricts both the CORS headers and the transport's own check. |
| `BRAIN_OWNER_EMAIL` | — | Single-user shortcut past the consent form. |
| `BRAIN_PREVIEW` | — | `1` enables `/preview` outside test mode. |
| `BRAIN_ROLE` | — | `web` (default) or `worker`. |
| `R2_*`, `LITESTREAM_REPLICA_URL` | prod | Replication to Cloudflare R2. |
| `LLM_API_KEY` / `ZAI_API_KEY` | prod (for live AI) | Turns on the live extractor, link classifier, vision **and** embeddings. Unset = rule-based fallback. One Z.ai key is a complete setup. |
| `LLM_BASE_URL` | — | OpenAI-compatible base. Default `https://api.z.ai/api/paas/v4`. Also recognised: `https://open.bigmodel.cn/api/paas/v4`, `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`, `https://openrouter.ai/api/v1`. |
| `LLM_MODEL` | — | Default `glm-5.3-flash`. Must be vision-capable for image ingestion. `deepseek-v4.1-flash` and `qwen3.8-flash` are drop-in with the matching base URL. A marketplace prefix (`z-ai/…`) is stripped automatically on a first-party host. |
| `LLM_REASONING_EFFORT` | — | Default `low`, sent **only** to OpenRouter as its `reasoning.effort`. Never sent to Z.ai or DashScope, which reject unknown top-level keys with a 400. |
| `DASHSCOPE_API_KEY` / `EMBEDDING_API_KEY` | — | Setting either moves embeddings to DashScope (`text-embedding-v4`) on its own key. Unset = the LLM key and host. |
| `EMBEDDING_BASE_URL` | — | Overrides the derived host. Default: DashScope when an embedding key is set, otherwise `LLM_BASE_URL`. |
| `EMBEDDING_MODEL` | — | Default `text-embedding-v4` on DashScope, `embedding-3` on Z.ai. Requested at 256 dimensions and prefix-sliced if the host returns its native width. |
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


---

## Phase 7c — Off the marketplace: first-party Z.ai + DashScope

A reseller's platform fee is not a one-off cost, it is a standing percentage on
every token this product will ever extract — and both vendors serve the same
OpenAI-compatible surface themselves. The default path now goes direct, and
OpenRouter becomes an opt-in route rather than the only one.

### What one key buys

| keys set | chat + vision | embeddings |
| --- | --- | --- |
| `LLM_API_KEY` (or `ZAI_API_KEY`) | Z.ai `glm-5.3-flash` | Z.ai `embedding-3`, same key, same host |
| …plus `DASHSCOPE_API_KEY` (or `EMBEDDING_API_KEY`) | unchanged | DashScope `text-embedding-v4` on its own key |
| `LLM_BASE_URL=…openrouter.ai/api/v1` | marketplace, namespaced ids | marketplace |

Each vendor's own key name is accepted directly, because `ZAI_API_KEY` and
`DASHSCOPE_API_KEY` are what the two consoles hand out — there is nothing to
re-export. Resolution lives in properties (`resolved_llm_model`,
`resolved_embedding_base_url` / `_api_key` / `_model`) rather than in
`load_settings`, so a `Settings` built directly in a test resolves identically
to one built from the environment.

### Three ways this would have broken, closed

1. **The migration 404.** OpenRouter namespaces its catalogue
   (`z-ai/glm-5.3-flash`); the vendors do not (`glm-5.3-flash`). Carrying the
   prefix over fails as a 404 on the first ingestion, not at startup.
   `normalise_model_id` strips it for hosts known to namespace nothing — and
   **only** those: a self-hosted vLLM legitimately serves
   `meta-llama/Llama-3-8B`, so a `custom` host is left verbatim. The raw
   setting is preserved for reporting, and `doctor` prints the rewrite when one
   happens. The mirror case is caught too: an un-namespaced id on OpenRouter
   now draws a warning naming the prefix it wants.
2. **The 400 from an unknown key.** `reasoning: {"effort": "low"}` is
   OpenRouter's own parameter, and a first-party schema rejects an entire
   request for one unrecognised top-level field. It is gated on
   `cfg.targets_openrouter`, with a test per call site (extractor, classifier,
   vision) per host asserting the key is absent.
3. **Attribution headers going to the wrong gateway.** `X-Title` and
   `HTTP-Referer` are now set from the request URL inside `_post_json` rather
   than by each caller, so there is no site that can forget. A vendor's gateway
   is not owed the name of a marketplace we are not using.

`encoding_format: "float"` joins `dimensions: 256` on every embedding request:
without it a host may answer in base64, which `_normalise` would see as a
string rather than a vector. The Matryoshka guard is unchanged and now tested
across all three routes — Z.ai `embedding-3`, DashScope `text-embedding-v4` and
`qwen3-embedding-8b` — so whatever width comes back lands in `claims_vec` as a
unit-length `float[256]`.

`doctor` names the active vendor per slot (`Z.ai Direct`,
`Alibaba DashScope Direct`, `custom OpenAI-compatible host`), and warns rather
than stays silent when a route still goes through the marketplace.

### Still the one thing to do with the live key

**The neural floors (0.42 / 0.38) remain uncalibrated**, and the embedding model
has now changed again — `embedding-3` or `text-embedding-v4` rather than a
256-native OpenAI model. Run `python -m app.cli calibrate` once with the live
key and set `BRAIN_SEARCH_FLOOR` / `BRAIN_LINK_FLOOR` from what it measures.
The hashed floors (0.18 / 0.15) stay measured and correct for `BRAIN_ENV=test`.


---

## Phase 7d — Production resilience and mobile / MCP hardening

Four failure modes that only appear once something real is on the other end.
Three of them turned up defects that were already there.

### 1. Direct-vendor rate limits and transient retries

`_post_json` now distinguishes three outcomes instead of two:

* **retry** — 429, 500, 502, 503, 504, and `httpx.TimeoutException` /
  `TransportError`. A first-party endpoint enforces per-key concurrency and
  RPM, so a multi-claim ingestion burst walks straight into a 429; nothing
  about the request is wrong.
* **fail now** — 400, 401, 403, 404. Each is about how the request was *built*,
  so another identical attempt gets an identical answer three times as slowly.
  Each gets its own reason, and the 404 names `LLM_MODEL` and `LLM_BASE_URL`,
  because a stale marketplace model id is the likeliest cause right now.
* **backoff** — the host's own `Retry-After` wins when it sends one (both legal
  spellings), capped at 8s: a worker parked for ten minutes is worse than a
  failed job, since the queue has a lease and an attempt counter and this does
  not. Otherwise 0.5s, 1s, 2s. Three attempts total.

The sleep is injectable (`set_sleeper`), so the 20 retry tests assert the
delays rather than waiting them out. A rate-limited *extraction* still falls
back to the heuristic extractor rather than losing the source; a rate-limited
*embedding* still raises, because a hashed vector in a neural index would
corrupt every later search silently.

### 2. Graceful shutdown — and a flush that would never have run

`flush_all_dirty_tenants()` is the shutdown path, and `stop_background` now
stops the loop, joins it, flushes, and logs an error naming any tenant still
unsynced rather than exiting quietly.

**The defect:** the lifespan's flush was written *after* its `yield`. A
redeploy's `SIGTERM` arrives as a cancellation **at** the yield, so those lines
would simply never have run — the snapshot, the connection close, all of it.
Everything is in a `finally` block now, and a test asserts the source ordering,
because `TestClient` closes a lifespan cleanly and cannot provoke the real
case.

Also added a per-tenant snapshot lock: the loop and a shutdown flush can reach
the same tenant at once, and two concurrent `VACUUM INTO`s to the same path is
a corrupt upload. A threaded test races two flushers and asserts what lands is
a whole SQLite file.

### 3. CORS and the external MCP client — two doors, one of them locked

CORS is scoped to `/mcp`, `/.well-known/*` and the OAuth endpoints, and
deliberately **not** `/api/*`: the control plane authenticates with a
`SameSite=Strict` cookie plus a double-submit CSRF header, and advertising it
as cross-origin-callable would undo that for no gain. `WWW-Authenticate` and
`Mcp-Session-Id` are exposed because both are load bearing — without the first
the client cannot discover where to authenticate, without the second it cannot
continue the session it just opened.

**The defect the wire test found:** with CORS in place, an authenticated
request from `https://claude.ai` got a **403** — the MCP SDK's
`TransportSecuritySettings.allowed_origins` was empty, so every Origin it had
not been told about was rejected. Our CORS said come in and the transport
slammed the door, and only *after* authentication, which is the worst place to
discover it. `allowed_origins` exists to stop a drive-by page driving a *local*
MCP server, and that attack works because the browser attaches credentials by
itself — nothing on `/mcp` authenticates that way. So the Origin policy is now
explicit and unrestricted by default (`BRAIN_ALLOWED_ORIGINS`), the **Host**
allow-list stays strict as the real rebinding defence, and both middlewares
read one policy function.

The wire test walks the whole sequence on the real app, taking each step from
the previous step's response rather than a hard-coded path: 401 + challenge →
resource metadata at the URL the challenge named → RFC 8414 server metadata →
RFC 7591 registration → S256 PKCE authorize + consent → code exchange →
`initialize` → `tools/list` (all 8) → a real `remember_this` call. Plus: a
revoked token produces the same challenge, so a client with a dead token has
something to act on.

### 4. The phone — 390x844, every layout, every state

36 combinations (4 layouts x 9 states, `offline` added), all now at
`scrollWidth == 390` with **zero** overflowing elements.

**Three defects found.** The B3 inspector had `top:auto` with no `bottom` in
the mobile media query — and a fixed element with `top:auto` falls to its
static position, which measured **954px on an 844px screen**. The sheet opened
entirely below the fold: invisible, and nothing in it reachable. Second, the
harness bar and tuner sat on top of whatever was open, including the sheet's
back arrow and the Profile modal's Close button, so on a phone there was no way
out of either; an open overlay now wins over dev chrome. Third, the harness's
own control groups ran off the right edge — a design harness whose controls you
cannot reach on a phone fails at the one job it has.

**And one in the app itself:** `api()` called `fetch` with no `try`, so losing
the network *rejected* the promise, skipped every caller's `if (!data.ok)`
branch, and left the page half-rendered in silence. It now returns an ordinary
failed payload, so the error strip the app already had does the talking — no
new affordance, just the approved one actually firing. `offline` joins the
harness state list, shaped like every other one: nothing faked, the real
request path made to fail.


---

## Phase 7e — The human entry door, real-client interop, and the races

### 1. The entry door

`/` signed out is now a single card built from the theme tokens — no literal
colours, so the dark and Warm Sunset themes follow automatically, and the
sidebar and nav bar get out of the way so the one thing to do is the only thing
on screen. A bad address is answered **on the card**, not in a strip the card
would hide.

A first sign-in seeds the three demo items (Rule 4c), so nobody lands in an
empty canvas. Two conditions guard it, because the obvious one is wrong on its
own: `counts` sees only *active* rows, and clearing the demo archives it — so
counts alone would hand it straight back on the next sign-in. The demo slug
check (the same one `op_seed_demo` uses) sees archived rows too. The seeding
lives in `engine.seed_first_run_library`, not in `webapi`, because the control
plane must contain **exactly one** `build_context` call — that count is the
structural proof that no endpoint resolves a tenant any other way, and the
Phase 6 BOLA test asserts it.

`Connect to Claude ▸` opens Profile scrolled to block 2 and selects the link;
`[Copy]` flips to `Copied ✓` for 1.2s. Sessions read `Expires in 30 days` —
**not 29**, which is what `.days` truncation produced for a token minted a
moment earlier, on the very screen where the user made the connection.

### 2. The OAuth consent card — and the CSP that blocked the whole flow

The card is Linear-styled from `/app.css` (one definition of `--accent` for the
product) with `/consent.js` for the theme, because the CSP forbids inline
script. It states what the agent can do, that it **cannot permanently delete —
soft archive only**, and that the grant is 30 days revocable in Profile. It
asks who you are only when it does not already know.

**Found in the browser, not in any unit test:** approving never worked.
`form-action 'self'` is applied by the browser to the *redirect target* of a
form submission, and approving redirects to the client's own callback — so our
own CSP blocked the last step of every OAuth flow. Rather than loosen it
globally, the consent response now carries its own CSP naming the one origin it
is allowed to end at: the `redirect_uri` that client registered, already
validated. Everything else stays as tight as before.

### 3. Real-client interop

* **Behind a terminator.** `cmd_serve` passes `--proxy-headers
  --forwarded-allow-ips=*`, so all three launchers inherit it from one
  definition rather than three that drift. `auth.issuer()` upgrades `http://`
  to `https://` for any non-loopback host, in the single place every discovery
  URL is built from — an `http://` endpoint in a metadata document is refused
  as mixed content by a browser client and would put an authorization code in
  the clear.
* **RFC 8252 loopback.** A native client may register `http://127.0.0.1:…` or
  `http://localhost:…`; plain http anywhere else is refused, because a code on
  the wire in the clear is not a client's choice to make. At authorize time
  **only the port may vary** — a different path, host spelling, scheme or
  address is still a 400, since anything looser is an open redirector.
* **`/oauth/*` aliases** alongside the canonical paths, and the consent form
  posts back to whichever path it was served on.
* **Stream headers.** `X-Accel-Buffering: no` and `Cache-Control: no-cache,
  no-transform` on `/mcp` only — a buffered SSE stream looks to a client exactly
  like a hung tool call. `OPTIONS /mcp` still answers 204 before any auth check,
  advertising `MCP-Protocol-Version` and `DELETE`.

### 4. The five frontend safeguards

1. **Cross-tab sync.** `GET /api/sync-watermark` + `visibilitychange`, restoring
   filter text, scroll position, open inspector, paging and expanded cards —
   a refresh that loses your place is worse than stale data. **The first
   implementation was wrong:** counting tenant rows and taking the newest
   timestamp silently missed an *edit*, because editing a claim changes neither
   the count nor any timestamp (`epistemic_claims` has no `updated_at`). The
   watermark now comes from `audit_logs`, which already *is* the append-only
   mutation record — filtered to mutating actions, or a read would move it and
   the tab would refresh itself forever.
2. **100-row DOM cap** with `[Show more (X remaining)]`. Filtering still runs
   over the full in-memory arrays, so a term in claim 1,500 still matches; only
   layout cost is bounded.
3. **Mobile viewport.** `100dvh`, `env(safe-area-inset-bottom)` and a
   `visualViewport` listener maintaining `--vvh`/`--kb`, because the keyboard
   shrinks the *visual* viewport without changing `vh`. The `/preview` tuner is
   collapsed by default under 640px with a handle.
4. **Keyboard.** `/` focuses the bar only when the keystroke is not going into
   a field (otherwise it would eat the slash in a pasted URL); `Cmd/Ctrl+K`
   works from anywhere. `Escape` closes **one layer at a time** — profile, then
   inspector, then the pending plan, then the filter.
5. **`X-Brain-Request: 1`** on every mutating request, enforced server-side
   alongside the double-submit token. The two fail to different attacks: the
   token stops a forged request that can read nothing, and the header stops the
   one shape a token cannot — an HTML form post, which rides the cookie
   cross-origin and cannot set a custom header at all.

### 5. The races

**Singleflight hydration.** A cold node gets `/api/cards`, `/api/profile` and
`/api/jobs` at once; without a per-tenant lock each starts its own R2 GET and
they race on the same `.hydrating` path — one gets renamed into place and then
overwritten underneath an open connection. Five concurrent opens of a cold
tenant now cost **one** GET, and the test asserts exactly that under a
deliberately slow link.

### What the browser caught that nothing else did

The CSP form-action block (above), and one more: `#tuner-handle` was declared
`display:none` *after* the media query that reveals it, so same-specificity
ordering kept it hidden and the collapse control did not exist on a phone.

**XSS canary.** An `<img src=x onerror=window.__xss=1>` payload is written into
every claim, skill and profile field through the real API, then rendered through
cards, the expanded accordion, the inspector and the Profile modal.
`window.__xss` stays undefined, zero `img` elements appear, and the payload is
present **as text** — which proves it round-tripped and was escaped by `esc()`
rather than silently dropped.


---

## Phase 7f — Railway packaging: `VOLUME` out, and the mount problem behind it

**`VOLUME ["/data"]` is gone.** Railway refuses a build that declares one: the
volume is attached to the service in its dashboard and mounted at run time, so
a Dockerfile-declared anonymous volume both conflicts with that and hides the
mount. Fly (`[mounts]`) and plain `docker run -v` already supply the path
themselves, so nothing else needed the line. A test now asserts its *absence*,
with the reason, so it cannot come back.

**The second blocker, found while checking for more.** A run-time mount shadows
whatever the image did to that path at build time — so the Dockerfile's
`chown -R brainuser /data` is simply not there any more, and the volume arrives
owned by **root**. With `USER brainuser` set, the entrypoint's first `mkdir -p`
would have failed under `set -euo pipefail` and the container would have
crash-looped on boot, after a successful build.

The usual fix is to run the whole container as root. That is not worth it here:
this process parses untrusted PDFs, DOCX (ZIP) archives and remote HTML. So
instead:

* no `USER` instruction — the container starts as root;
* `entrypoint.sh` creates and `chown`s the mount, then **`exec`s itself** under
  uid 10001 via `setpriv` (falling back to `su`, and saying so loudly if
  neither exists). Every long-running process — uvicorn, the worker, Litestream
  — is downstream of that drop, and a test asserts the ordering;
* `docker run --user 10001` still works: the entrypoint sees it is already
  unprivileged and skips the drop.

**A hole in that guard, caught by testing it.** The writability check was
`mkdir -p`, which succeeds on a directory that already exists *whatever its
permissions* — so a read-only mount would have passed the check and surfaced
three layers deeper as a SQLite error. It is now a write canary, and the
failure message names the owner, the uid, and what to do about it.

All three boot paths were exercised for real in the sandbox: root-owned mount →
drops to 10001 and writes; already-unprivileged with a writable mount → runs
directly; already-unprivileged with a root-owned mount → fails with the
diagnostic instead of a stack trace.

### Not verified here

The image itself was never built — there is no Docker daemon in this
environment. What was tested is the entrypoint's shell logic, executed
directly, and the static shape of the Dockerfile. Two things only a real build
confirms: that `setpriv` is present in `python:3.12-slim` (it is part of
`util-linux`, which Debian marks essential, and the `su` fallback covers it if
not), and that Railway's builder is happy with the rest of the file.

---

## Phase 8 — THE CONFORMANCE REVIEW, AND THE CONTROLS IT FOUND INERT

Every built frame was read against its §7 section, the §6 global rules and the
capture notes of §18. The review returned 119 findings, 24 blocking — and
almost all of the blockers were one defect wearing eleven faces: **a control
the design specifies, drawn on the page, with nothing behind it.** §6 rule 11
is explicit that this is worse than a control that was never drawn, because
the person finds out by clicking.

Each finding was re-verified by hand before being acted on; the review had
read a working tree that was being edited while it read, and several of what
it reported had already been fixed.

### The worst of them: a frame that returned before its own wiring

```js
return el(`… <input id="in-page"> <button id="copy"> …`);
wireReadBar(node);   // never reached
```

§7.5 and §7.6 both had this. Nothing warns: the markup is right, the ids are
right, the handlers are written and correct, the page renders, no error reaches
the console, and every control is inert. It is now a rule in
`tests/test_redesign_controls.py` — a `return` at a frame's own indentation
ends the function, so any statement after it is dead — checked against every
frame, and a reintroduced early return fails it by name.

### What is now real

| Frame | What it could not do | Now |
|---|---|---|
| §7.5 transcript | in-page search, Copy, Download, the sticky border | marks and counts live, Copy carries the timestamps, `Download ▾` offers only producible formats, the border arrives only once stuck |
| §7.6 summary | Brief/Full, Copy, Download | switches real content — and where there is none to switch to (≤5 points, no chapters) `Full` says "That is the whole summary" rather than moving nothing |
| §7.3 source | Copy/Download the summary, the overflow menu, and **nothing polled** | both controls, `Rename`/`Re-process`/`Archive`, and a work block that follows real state — loaded with a job still queued, it notices the queue draining from another process |
| §7.4 transcripts | no card actions at all | `Copy`, `Download`, `⋯` with Rename / View the source / Generate a summary / Archive, hover-only at rest |
| §7.7 sources | the `Needs you` state and `Fix this` | both pills, the remedy as a tooltip keyed on the provider's reason code, and a `Fix this` that really retries |
| §7.8 rules | `Edit` and `Archive` were plain buttons | both wired, inline, with an Undo toast |
| §7.9 skills | no `Review` button; `⋯` had no handler | the queue's entry point exists; Activate/Deactivate, `Export .md` and Delete all work — `POST /api/skills/:id/state` had been served since the queue was built and called by nothing |
| §7.12 archive | **no `Restore` at all** | `R-9`'s header button, with §9.4's Undo toast |
| §7.13 profile | `Delete library` and `Delete account` had no handler and no endpoint | both, behind §9.4's two-step dialog |
| §7.1's create links | `?create=1` was read by no frame | a panel on both, and a web endpoint to write a rule |

### Backend the design asked for

* `op_rename_source`, `op_reprocess_source`, `op_generate_summary` — the three
  menu items with nothing behind them. All person-only: an agent renaming
  someone's library, spending their minutes on a re-read, or writing a summary
  they had switched off are decisions §2 rules out by the same argument that
  keeps `delete` off the MCP surface. `Generate a summary` runs the summariser
  over the **stored** text, which is the whole reason it exists on a transcript
  card.
* `op_delete_library` and `op_delete_account`, both requiring the typed word
  **at the engine** — a dialog is a frame's promise, and a frame is one bug
  away from firing a POST with an empty body.
* `ingestion_jobs.error_reason`, so §7.7's tooltip can state the remedy. A
  sentence written for the person cannot be matched on, and matching on prose
  is how a tooltip silently goes wrong the first time the wording improves.
* `skills` list joins its source title, because §7.9's expanded body ends with
  provenance and a read per card to draw it is a read per card too many.

### Four defects found by running it, not by reading it

1. **`[hidden]` did nothing on any atom.** `hidden` is a statement of fact and
   any explicit `display` beats it; nearly every atom sets one. At step 1 of
   §9.4's dialog both `Continue` and the final `Delete account` were on screen,
   the second merely disabled. `.menu[hidden]`, `.scrim[hidden]` and
   `.toast[hidden]` had each been re-asserting it by hand — the tell that it
   belonged in the base block once.
2. **Opening a card's menu navigated away from the list.** The trigger is a
   `<button>` inside the card's own `<a>`, and `wireMenus` stopped the bubble
   without cancelling the link, so §4.7's menu was drawn onto a page that was
   already being replaced.
3. **The loop breaker refused a person changing their mind.** Its fingerprint
   for `set_skill_state` was the skill id alone, so switching a skill off and
   straight back on inside 15 seconds read as one call made twice. Same for
   `review_skill` and `rename_source`; each fingerprint now carries what makes
   two calls different, and the guard still catches a genuine repeat.
4. **A blocked source was stamped as archived.** `_archival` stamped anything
   that was not `active`, so a source that had just failed to fetch carried an
   archive date and the words `archived by you`, having been archived by
   nobody.

### Two conflicts in the plan, ruled on rather than guessed

* **`R-14`** — §7.9 lists `Delete (confirms)` in a live skill's menu; §6 rule 8
  says "only the person deletes, and only from Archive or Profile". Rule 8
  wins, because §2's 30-day recovery buffer is a security property and a menu
  item is not a place to spend it. The item keeps §7.9's name, sets the skill
  aside, and the dialog says plainly where it went.
* **`R-15`** — `Delete permanently` requires the thing to be **set aside**,
  archived or trashed, not trashed specifically. §9.4 puts the control on an
  archived card and Trash is a source-level concept, so the original rule meant
  the button could only ever answer `not_in_trash` — and `not_found` for a
  rule, skill or summary, whose ids were resolved against `sources` alone.

`op_permanent_delete` also refuses an agent surface outright now. It was
previously unreachable only in the sense that no MCP tool wrapped it; §2's
"zero destructive LLM tools" is a property of the engine, not of the tool list.

### Still to build

§7.2 (Home's thread state), §7.10 Activity and §7.11 Connections. The last two
need the approved §9.2 rows: four columns on `audit_logs`, and a `connections`
entity accepting `?key=` in the same validation function.

---

## Phase 9 — THE LAST THREE FRAMES

§7.2, §7.10 and §7.11 — and the two rows of `STACK_AUDIT.md` §9.2 they
needed, which were the last unbuilt items on the approved list.

### §7.10 Activity

Four columns on `audit_logs`, written at `_run_tool` because that is the one
place that knows a call's arguments, its duration and its size. A row with a
`connection_id` is a call a connection made; the operations' own rows are the
internal trail §2 requires and are not calls.

The meters read what the product enforces rather than what the capture shows.
`Calls 1,284 of 5,000` and `2.1 GB of 10 GB` are caps that do not exist here;
`quota_files`, `quota_media_minutes` and the per-minute ceiling do. `OPEN-4`
is the founder's call: add the two caps, or keep the page honest.

### §7.11 Connections

The key goes in the URL, because a chat app's connector dialog has no header
field — §7.11 calls this verified behaviour rather than a choice. Both doors
meet at one validation function. Only the SHA-256 digest is stored; the
plaintext exists in the response that minted it and nowhere else.

**The cost of that design, paid explicitly:** a credential in a query string
is a credential in every access log. The ASGI middleware strips it from the
scope, but uvicorn writes its line from the raw request *before* any
middleware runs — so a logging filter redacts the value. The path still reads
`?key=[redacted]`: you can see a key was used, and §7.10 says which
connection used it, without the log being a list of working credentials.

### §7.2 Home, the thread

The composer is never disabled, an interjection reaches the job without
restarting it, and the acknowledgement says what the note will actually
reach — §7.2's own reply, *"Still on the summary, so nothing is lost"*, is
true only while the summary is ahead. Once no step is pending the engine
refuses rather than reassuring.

### Four defects only a render showed

1. **Every meter rendered as a vertical bar.** `.meter` is a `<span>` and
   §3.9's atom set no `display`. The atom was written before anything used
   it — the same trap `.v-rule` fell into, the second time it has cost a
   render.
2. **The web spent the agent's rate-limit budget.** §2's `30 tool calls/min`
   is about an agent in a runaway loop; a browser reads `/api/counts` plus a
   list per navigation. Clicking through the app for a minute returned a 429
   rendered as the page, in a sentence written for an LLM. The web now has
   its own window and its own sentences; the agent's ceiling is unchanged
   and asserted.
3. **An interjection also triggered submit's "paste a link" error**, so Brain
   acknowledged a message and rejected it in the same breath.
4. **A frame's root is not inside itself.** `querySelector` searches
   descendants, and `listFrame` returns the `.wrap` while Home returns the
   `.home`. Two debugging rounds before it became `self_or_within()`.

### Where the build is

All fourteen frames of §7 are built, every control is wired, and each one has
been rendered in a browser rather than inspected as source. Ten probes under
`tests/browser/` hold the half no static test can reach — including one that
loads a page with a job still queued and drains the queue from another
process, and one that makes real MCP calls with a key in the URL and reads
the row they produce.

## R-17 — storage out, 30-day retention in (30 Oct 2026)

Two founder rulings, built together because neither is honest alone.

**The usage screen shows no storage.** §7.10's capture meters `Storage 2.1 GB
of 10 GB`. The founder struck it: *"The storage should not be in our pricing
system… In the usage screen we shouldn't show the storage size."* So
`op_activity` returns no `storage` key — absent, not a capless figure, because
a number with no limit still invites the question of what the limit should be.
Three meters remain, and every one of them has a ceiling the product actually
enforces.

**And a received file is swept after 30 days.** *"We should keep them for 30
days and when those 30 days pass they should be deleted."* This closed a leak:
**nothing had ever deleted an upload.** `api_upload` writes the bytes to
`data_dir/uploads/{user_id}/` and `op_ingest_file` queues a job against that
path; the single `unlink` in the route is the over-size failure branch, so
every *successfully* processed file stayed on disk forever. Verified by
uploading a file and finding it still there after the job reached `ready` —
not by reading the code, which is how the branch was misread the first time.

`app/storage/retention.py` sweeps anything past `cfg.upload_retention_days`,
and the schedule hangs off `Worker`, the one role both production deployments
run (in-process under `BRAIN_ROLE=web`, standalone under `=worker`). It sweeps
before claiming rather than on the idle branch, so a container whose queue
never empties still sweeps; a failure is logged and the queue carries on.
`python -m app.cli sweep-uploads --dry-run` shows what is due without
deleting it, for the deployment that has a backlog from before retention
existed.

Age alone decides, with no look at `ingestion_jobs`: the lease is 180 seconds
and `attempts` is bounded at 3, so a live job resolves in minutes, and §7.3's
rescue takes pasted text rather than re-reading the file. The sweep therefore
needs no database, which is what lets the CLI and the tests run it directly.

Striking the meter without the sweep would only have hidden a number that was
still growing. That is the whole argument for doing both.

### The measurement that argues with the ruling

Recorded rather than quietly built around. The founder's unit is processed
media — *"the amount of our processed videos, that's the thing that counts"* —
and on this codebase a minute of video is the **cheapest** unit in the
product. There is no transcription and no OCR anywhere: YouTube runs `yt-dlp
--skip-download` against the caption track the platform already has, a
caption-less video is refused, a scanned PDF is refused. What costs money is
characters of source text (one extraction call per ~1,400 characters: ~7 for
a 10-minute video, 39 for an hour, 200–600 for a 200-page PDF, ~35,000 for a
20MB `.txt`), and above that, tension classification at up to `claims × 8`
calls per ingestion with nothing capping it. The founder's answer is `R-18` below — characters, which is the unit this
points at; the full ranking is in `MASTER_PLAN.md` §17 under `R-17`.

## R-18 — credits, one plan, and the person's say in what Brain keeps (30 Oct 2026)

The founder's answer to the lever question, and it is the one the measurement
pointed at: *"we should not use video minutes but some kind of credit thing…
A character is a credit, kind of like how Eleven Labs does it… In usage we
will replace that."* Plus a ruling that kept the scope small: *"it should be
just one plan, the free plan… It's not an urgency right now."* No tier, no
billing period beyond the monthly reset, no price, no payment code.

### One debit, at the one line where the number is real

Minutes had to be reserved up front and released on failure, because a
minute was guessable before the fetch — a flat provisional 15 for YouTube,
and nothing at all for an article. Characters are not guessable (`normalize_url`
knows no duration, no bytes, no page count, and `NormalizedTarget.estimated_minutes`
is a field nothing in the repo assigns) but they are *exactly known* the
moment the fetch returns, which is still above every LLM call. So there is
nothing to reserve: one atomic debit at the entry to `_ingest_content`, with
the ceiling inside the `WHERE` clause so two workers racing the same account
cannot both cross it.

**The billed number is what the extractor chunks, never `content.full_text`.**
`clean_text` truncates `full_text` at 2,000,000 characters while applying
itself to each cue *separately*, leaving the cues uncapped — and the chunker
consumes the cues. Billing `full_text` would have charged a 20MB text file
for a tenth of what it costs to read.

### What the mapping found that had to be fixed too

- **A job that burned its last attempt leaked forever.** `claim_next_job`
  filters on `attempts < max_attempts`, so such a job kept a non-terminal
  status and nothing ever ran the code that releases what it booked.
  `reap_exhausted_jobs` now runs at the top of every drain. **A docstring in
  `app/storage/retention.py`, written earlier the same day, asserted the
  opposite** — that such a job "is released rather than left pending". It was
  wrong, and it is corrected in place rather than quietly deleted.
- **`run_pending_jobs_sync` had no `try/except`** around `process_job`, unlike
  `Worker.drain_once`. Three crashes there made the leak permanent.
- **The `Sources` meter measured a different number from the one enforced** —
  a tenant source count against `quota_files`, which only uploads ever
  incremented — so it could read `3 of 50` while the enforced figure was
  `0 of 50`. It is now a true count with no cap.
- **The agent hint for a spent allowance told the agent to have the person
  archive sources to free room.** Nothing in the product has ever returned an
  allowance for removed content. A remedy that does nothing is worse than
  none, because the agent spends the person's time on it.
- **The classifier loop had no ceiling** — up to `claims × link_top_k` LLM
  calls, which at maturity dwarfs the extraction bill the price is derived
  from. `link_max_calls_per_job` bounds it, and reports when it truncates.

### The three retention controls

*"We should tell the people that we keep their thing there, somewhere in the
profile… People should also be able to say, 'Okay I don't want you to keep
it.'"* And: *"the person should be able to delete an upload sooner than 30
days."*

No new category was needed — the Profile already has **Your data**, and all
three belong there or on the source they concern. The day count comes from
`cfg.upload_retention_days`, the same setting the sweeper reads, so the
sentence cannot drift from the behaviour. The opt-out deletes on the failure
path too: a file someone asked not to keep is not kept because Brain had
trouble with it. And `op_delete_library` and `op_delete_account` now take the
uploads with them — before this, closing an account left the person's files
in staging for up to 30 days with nothing in the product able to reach them.

Deleting a file returns no credits, and all three dialogs say so: the credits
paid for reading the text, which is already done.

## R-19 — the cutover: one app, at the root (30 Oct 2026)

Two web apps were served side by side while the redesign was built: the
shipped control plane at `/` (`index.html` + `app.css` + `app.js`) and the
redesign at `/next` (`shell.html` + four CSS layers + `frames.js`). That was
right while the new one was half-built — the founder always had a working
Brain — and it became a liability the moment both were complete. Every change
had to land twice. `R-18` is the example: the credits meter had to be edited
in both files, or `/` would have gone on saying "Media minutes 0 / 300".

So: `/` serves the app, the `/next` prefix is gone, `shell.html` carries
`data-base=""`, and the three old files are deleted. `/next/...` 302-redirects
rather than 404s, because somebody bookmarked it — 302 and not 301, since a
permanent redirect is cached indefinitely and an undo would have to fight
every browser that saw it. The `BRAIN_NEXT_UI` gate is gone too: a flag that
can turn the whole product off is a way to ship an outage, not a safety
feature.

### What had to be dealt with first

**Trash.** The old app was the only surface that could list it or restore
from it, and the new app has no Trash screen and no way to fill one — its
model is active → archived → deleted for good, with Archive as §2's 30-day
buffer (`R-14`). Deleting the old app would have left any row at
`status='trash'` reachable by nothing. So `op_trash_item` and `op_list_trash`
are gone, the omnibar's "delete the X" archives, and `tenant.migrate` folds
existing trashed rows into Archive attributed to the person — they put it
there. `op_permanent_delete` still accepts the status, because a database
restored from a pre-fold backup can hold one.

**The OAuth consent card.** It shared `/app.css` with the control plane, for
a good reason: one definition of `--accent` for the whole product. It now
loads `tokens.css` + `atoms.css` + a small `consent.css`, which declares no
colour of its own. It also stopped promising a feature this same change
deleted — "after 30 days in Trash" now names Archive.

**The automated browser coverage.** The sharpest finding. Three Playwright
suites ran in CI, and all three drove the old app; the redesign's fourteen
probes print JSON, exit 0 regardless, and are spawned by nothing. Deleting
the old suites would have left the new app looking fully tested — 1,400
passing tests — while nothing had ever opened it. `tests/test_redesign_browser.py`
and `tests/browser/app_audit.js` replace them: every route renders without
throwing, nothing scrolls sideways at 390px, and a stored
`<img src=x onerror=...>` reaches the DOM as text.

### What the browser suite found the moment it existed

**Two routes scrolled sideways on a phone.** `/rules` measured 577px against
a 390px viewport and `/library/sources` 488px. The cause was one declaration:
the mobile breakpoint said `grid-template-columns: 1fr`, and a bare `1fr` is
`minmax(auto, 1fr)` — its minimum is the column's *min-content*, so one wide
descendant stretches the page. The desktop rule twelve lines up already had
`minmax(0, 1fr)`. Two earlier guesses at this (a wrapping toolbar, a
`min-width: 0` on the search field) were reverted once the real cause was
found: they fixed nothing and would have changed a layout §3.3 specifies.

### Three more things the cutover exposed

- **`consent.css` was never `git add`ed.** It would have shipped as a 404 on
  the one screen rendered outside the shell — the first thing a person sees
  when connecting Claude. Two packaging tests now guard it: every file the
  layout promises exists, and nothing in `app/` still references a deleted
  asset.
- **`theme_mode` still defaulted to `warm_sunset`**, a theme `R-10` dropped,
  so every new account started on something the picker does not offer and
  nothing renders. Worse, the theme was applied only by the picker's click
  handler — `shell.html` hardcodes `data-theme="light"`, so anyone who chose
  Dark got Light back on every reload. Default fixed, old rows migrated, and
  the choice is now read at boot.
- **A dropped connection was an unhandled promise rejection.** `api()` and
  `post()` called `fetch` directly, so the page simply stopped with "Failed
  to fetch" in a console nobody has open. Both go through a wrapper that
  turns it into a sentence (§6 rule 11).

### Left undone, deliberately

- **No paging.** The old app rendered at most 100 cards and kept filtering
  over the full array; the new one renders every row it is given. This is the
  one place the cutover is a net loss rather than a redesign. It needs a
  paged API, not a front-end change.
- **No audit-log screen.** `/api/audit-logs` is live, authenticated and
  person-only, and the rows are written — CLAUDE.md §2.5 holds. But §7.13 as
  the founder approved it has four blocks and no audit list, so the old
  Profile modal's sixth block had nowhere to go. That is a product question,
  not a dropped guarantee.
- The sync watermark, the visual-viewport keyboard inset, and a single
  Escape-ordering handler all went with `app.js` and are not rebuilt.

---

## R-20 — The interface audit, and the five defects it found (31 October 2026)

The founder sent five screenshots and said the design "is not there yet", then
asked for something broader than the screenshots: a walk through **every screen
and every action in the web app**, each one captured and analysed, before any
discussion of what deviates from the plan.

### How it was done

A real server (`python -m app.cli dev`, seeded, with an ingestion run to
`ready` so the detail screens had content), driven by Playwright at 1440×900
and 390×844 on separate tenants:

1. **Navigation** — the 14 routes, at rest and with every disclosure open.
2. **Flows** — omnibar, ingestion, forms, themes, profile.
3. **An exhaustive control crawl** — every clickable element on every screen,
   clicked in isolation from a reloaded page: 26 screens, 149 actions on
   desktop and 137 on mobile, with detection of controls that do nothing.
4. **Sequenced journeys** — 40 steps: edit, archive, restore, confirm, undo,
   rename, revoke, export, sign out.

Every state was **measured in the DOM**, not just looked at. That distinction
earned its keep in both directions: four of the five defects had never shown up
in 1,418 passing tests, and five findings the crawl reported were artefacts of
the probes themselves — the toast (the real one is appended to `body`, not the
shell's `#toast`), the export (an `<a>`, not a `<button>`), the Archive filter
pills (they filter by visibility, which a presence check cannot see), the `[+]`
button (it opens a file chooser, which registers as no DOM change) and the
sticky sidebar (a full-page-screenshot artefact). Each was re-tested and
dropped. Verifying a detector's output is not optional.

### What was wrong

- **The guided steps collapsed into a 26px column.** `.steps-list > li` is a
  `26px 1fr` grid and its counter is a `::before`, so the counter is grid item
  #1 and auto-placement put every second real child into the 26px track. One
  declaration produced the whole of what two screenshots showed: command paths
  one word per line, the code block at 56px, the Copy button stretched to 806px
  above it. `/skills` escaped only because `steps-plain` re-declares
  `display: list-item`.
- **The phone's top bar grew with the page.** `.shell` is `min-height: 100svh`
  with two auto rows, and the default stretching `align-content` split the
  leftover height between them: 261px on `/rules`, 255px on
  `/library/transcripts`, against the 49px a long page gives it.
- **`ProviderError` reached the person as a class name.** `endpoint()` caught
  `ApiError` and `GatekeeperError` and let the rest fall into the net that
  answers `Something went wrong ({type(exc).__name__})`. Saving a rule against
  a live model said exactly that, while the exception carried a reason code and
  a sentence written to be read. Compounding it, `REMEDY` was keyed on the
  fetchers' reasons only, so all nine model and embedding failures fell through
  to "Make it reachable, then retry" — advice about a source, for a bad API
  key. The test meant to prevent this read `fetchers.py` alone.
- **The connection menu was clipped by 35px**, which is exactly where `Revoke`
  sits. `.conn-list` carried `overflow: hidden` only to round its corners.
- **The omnibar refused what its own backend could do.** The placeholder cycled
  "Ask Brain anything…" while `submit()` answered everything without an `http`
  with "Brain reads sources, not questions, for now." Asked directly,
  `/api/omnibar` answers "archive yesterday's video" with a resolved source, a
  confirmation and an undo — Checkpoint B's own worked example. The three
  endpoints had been mounted since Phase 4 and the page never called them.

Two more the phone showed: a 132px thumbnail that never shrinks left the source
title 162px and four lines; and a status pill alone in a `1fr` track stretched
to a 270px bar instead of reading `Live`.

### What was left alone

`.card-group` and `.result-table` carry the same `overflow: hidden` but host no
menu, so nothing is clipped and nothing was changed. The sticky sidebar, the
absence of horizontal overflow on every route, the Archive filters, the
`Archived.`/`Undo` toast, the export download, the confirm dialogs, inline
rename, Ctrl+K and the whole ingestion pipeline were all verified working and
not touched.

### Guards

`tests/browser/layout_regressions.js` and its wrapper measure eight facts, each
one a number that was wrong in the shipped app. Reverting any fix fails them:
the code block measures 56px, the step children report `grid-column: auto`.
The remedy test now reads `providers.py` as well, and removing one entry fails.

### Left undone, deliberately

- **The resting Rules row has no clickable `[1420s ↗]` badge.** Checkpoint B
  asks for one. The meta sits inside `button.exp-head`, and an anchor inside a
  button is invalid for the same reason the card already avoids nested buttons,
  so this needs the card restructured — a design decision, not a fix. The label
  itself is fixed: the row reads `23:40` and `p. 14` rather than `1420` and
  `14`, which is what the source detail already showed for the same claim.
- **An unknown URL still returns Starlette's plain-text `Not Found`**, with no
  shell and no designed screen. §7 names no 404 frame, so there is nothing to
  build to.
- **The work block does not collapse itself** to `Processed in 2m 14s · 6
  steps` as §4.4 specifies, and the "You can leave this page" line stays after
  all six steps are ticked.
- **`filter` mode has nowhere to go on Home.** The bar says it was not a
  command and names what it does take. The router's own "Showing matches"
  describes a search surface that does not exist on that page, and building one
  is a Checkpoint B decision.
- **LLM ↔ web-app parity.** The founder's standing principle is that anything
  doable in the web app should be doable by an LLM calling the tool. MCP
  exposes 8 tools; the web app performs roughly twenty more operations
  (rename, re-process, summarise, restore, permanent delete, roll and revoke a
  key, edit a rule, review and revert a skill, profile settings, export). That
  is its own piece of work — new tools, new permissions, new audit rows — and
  wiring the omnibar is only its first brick.
