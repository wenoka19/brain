# PROGRESS — `brain`

Status board for the build. Updated and committed after every phase and
checkpoint, per `CLAUDE.md` §0 Rule 2.

| Phase | Scope | State |
|---|---|---|
| 1 | Security primitives, dual SQLite DBs, hybrid search | ✅ complete |
| 2 | Gatekeeper, grounded extraction, contradiction linker | ✅ complete |
| **Checkpoint A** | In-chat Agentic UX (AX), tone & tool surface | 🛑 **options presented — awaiting founder** |
| 3 | MCP server + OAuth 2.1 / 30-day refresh | ⬜ blocked on Checkpoint A |
| **Checkpoint B** | Web app, Omni-Bar, theme feel | ⬜ not started |
| 4–5 | Web app, CLI, CI/CD, production packaging | ⬜ not started |

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
