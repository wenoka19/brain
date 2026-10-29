# STACK_AUDIT.md — `MASTER_PLAN.md` versus the shipped codebase

Audited 29 October 2026, against `a7b508b` on `claude/kind-ramanujan-kp27xd`
(phases 1 → 7f, see `PROGRESS.md`).

**Why this file exists.** `MASTER_PLAN.md` arrived as a greenfield specification.
The repository is not greenfield: it carries ~24,700 lines of working engine, MCP
server, OAuth layer, web control plane and tests. The plan and the code describe
the same product with **different names, a different data model and a different
MCP tool surface**. Building a frame without resolving the relevant row below
means silently overwriting shipped, tested behaviour.

Verdict column:
- `MATCH` — plan and code already agree.
- `DRIFT` — code is right, plan's description is stale; no work needed beyond the plan text.
- `GAP` — plan asks for something that does not exist yet; additive, no conflict.
- `CONFLICT` — plan and code disagree and **one must lose**. Needs a founder decision.

Every `CONFLICT` row carries a `D-n` id, listed with its decision in §7.

---

## 1. §1.1 STACK TABLE — line by line

| Plan §1.1 | Shipped | Verdict |
|---|---|---|
| Server: FastAPI + Uvicorn | `fastapi>=0.110`, `uvicorn>=0.27`; `app/main.py` composes the app and mounts the sub-apps | **MATCH** |
| Templating: Jinja2 → `HTMLResponse` | **No Jinja2 anywhere.** `app/webapi.py:598` serves `index.html`, `app.css`, `app.js` as `FileResponse`; all rendering is client-side in `app/static/app.js` (1,315 lines) | **CONFLICT `D-1`** |
| CSS: Tailwind via CDN | **No Tailwind.** Hand-written `app/static/app.css` (478 lines), fully tokenised. The shipped CSP is `default-src 'self'; script-src 'self'` — a CDN `<script>` is **blocked by the server's own header** (`app/main.py:36`) | **CONFLICT `D-2`** |
| JS: vanilla, no framework, no npm, no build step | Vanilla, no `package.json`, no build step | **MATCH** |
| MCP: Streamable HTTP, protocol `2026-07-28` | Official `mcp` SDK **2.3.0** installed; `mcp.types.LATEST_PROTOCOL_VERSION == "2026-07-28"` confirmed at runtime in this VM. `app/mcp_server.py` uses `mcp.server` + `TransportSecuritySettings` | **MATCH** |

### 1.1 Dependency drift — a real defect, independent of the plan

| Finding | Evidence |
|---|---|
| `mcp` is imported by `app/main.py:21` and `app/mcp_server.py:22` but is **absent from `pyproject.toml`** and from `.claude/setup.sh`. It appears only in `Dockerfile:15` as `"mcp>=1.2"` | `pip install .` yields an app that cannot import. The Cloud-VM setup script cannot run the MCP tests |
| `yt-dlp` — same: `Dockerfile:16` only | YouTube ingestion is undeclared outside the image |
| Every pin is `>=`, including `"mcp>=1.2"` while the code requires ≥2.x API shapes | Plan §10.14 ("pin every version, prefer a release at least two weeks old") is unmet across the board |
| Three dependency lists exist (`pyproject.toml`, `.claude/setup.sh`, `Dockerfile`) and none is authoritative | Any dependency change has to be made three times |

**This is a `GAP` to fix regardless of which way the `CONFLICT` rows resolve.**

---

## 2. §5 ROUTES AND TEMPLATE

| Plan | Shipped | Verdict |
|---|---|---|
| 14 routes, each a page | **One** page. `index.html` + four `data-view` buttons: `everything`, `skills`, `library`, `archived`, plus a Profile **modal** and a `/preview` design harness | **CONFLICT `D-1`** |
| URL per frame, deep-linkable | No `pushState`, no `location.hash` routing. No deep links, no browser Back | **CONFLICT `D-1`** |
| Sidebar 240px, `--bg-base`, nav sections `Library` / `Knowledge` / `System` | Sidebar 220px, `--bg-canvas` (same colour as the content area, not a distinct base), flat list of 4 + footer | **CONFLICT `D-3`** |
| Counts in the nav only | `data-count` spans on each nav item — already the rule | **MATCH** |
| `/signin` as a frame | Email-keyed login exists as `POST /api/login` (`app/webapi.py:172`), driven from inside the SPA | **GAP** |

---

## 3. §2 TOKENS — the name-collision hazard

Shipped `app/static/app.css` has a **semantic-only, single-layer** token set with
**all three themes already defined in hex**: `:root[data-theme="light"]`,
`[data-theme="dark"]`, `[data-theme="warm_sunset"]`.

| Plan token | Shipped token | Problem |
|---|---|---|
| `--bg-canvas` → `grey-150` `#f0f0f0` | `--bg-canvas` `#FCFCFD` | **Same name, different value.** Adding the plan's layer silently re-colours every shipped rule |
| `--bg-card` → `white` | `--bg-card` `#FFFFFF` | same name, same intent — safe |
| `--border-subtle` → ink @ 9% | `--border-subtle` `#E6E6EB` | same name, near-same intent — safe |
| `--text-primary` → `ink-900` `#343a40` | `--text-primary` `#16161A` | same name, different value |
| `--accent` → `blue-600` `#118ab2` | `--accent` `#4F46E5` (indigo) | same name, different hue |
| `--bg-base` (sidebar) | — | GAP |
| space / radius / type / weight / leading scales | only `--radius`, `--font-scale`, `--density` | GAP — the plan's whole §2.1 numeric layer is missing |
| `--duration-fast/base/slow`, `--ease-standard` | one `--duration-ui`, one `--ease-ui` (re-tuned per theme) | GAP + the shipped per-theme motion override has no equivalent in the plan |
| — | `--accent-conflict`, `--accent-conflict-quiet` | **The plan has no token for expert disagreement.** See §5 |
| — | `--shadow-pane`, `--density`, `--font-scale` | shipped-only, driven by the `/preview` tuner |

**Verdict: `CONFLICT D-4`.** Five token names collide; three of them —
`--bg-canvas`, `--text-primary`, `--accent` — carry materially different
values, and `--accent` changes hue (indigo → teal-blue). This is not a merge;
it is a migration with a visual-regression risk across 478 lines of CSS and
the 1,315 lines of JS that render against those class names.

### 3.1 `OPEN-1` is already answered by the code

The plan says dark and sunset palettes "are not yet specified. Do not invent them."
**They are specified and shipped** — and `control.db`.`users.theme_mode` persists
`'light'|'dark'|'warm_sunset'`. Note the plan names the third theme `sunset`,
the code names it `warm_sunset`; renaming means a data migration.

---

## 4. §8 DATA MODEL

| Plan entity | Shipped | Verdict |
|---|---|---|
| `user` | `control.db`.`users` — has `about_you_context`, `theme_mode`, `media_minutes_used`, `files_used`, `schema_version`. Missing `name`, `avatar_url`, `locale`, `theme_auto_evening` | **GAP** (additive) |
| `source` | `sources` — has `canonical_url`, `content_hash`, `title`, `author`, `published_at`, `language`, `source_type`, `duration_sec`, `status(active\|archived\|trash)`. Missing `kind`, `instruction`, `blocked_reason`, `archived_at`, `archived_by` | **GAP** + `CONFLICT D-5` on status enum |
| `source_type` | `youtube` · `article` · `github` · `pdf` · `docx` · `image` (+ txt/md) | Plan §7.7: "type is `Link`/`Document`/`Image`, **never a platform name**" → a presentation mapping, not a schema change. **DRIFT, resolvable in the formatter** |
| `transcript` | `source_content` — `full_text` + `cues_json` `[{start,end,text}]` or `[{page,text}]`. **No `speaker` field**, no `word_count`, no `speaker_count` | **GAP** — §7.5's speaker-change body and `Source · duration · word count · speakers` meta have no data behind them |
| `summary` | **does not exist** | **GAP** — a new entity, a new pipeline step, a new MCP tool, two new frames |
| `rule` (`short`, `full`, `origin`, `t_ms`) | `epistemic_claims` — a Toulmin card: `claim`, `speaker`, `evidence_basis`, `domain_tags`, `when_it_applies`, `when_it_fails`, `exact_steps_json`, `metrics_and_quotes`, `anchor_quote` (verbatim, rapidfuzz ≥ 88), `locator_type`/`locator_value`, `salience` | **CONFLICT D-6.** The plan's `rule` is a strict *subset*. Rendering a claim as a rule throws away the grounded quote, the evidence basis and `when_it_fails` — the three things `CLAUDE.md` §1.2 calls the product |
| — | **`tension_links`** (`contradicts`/`refines`/`supersedes` + `divergence_summary`) | **`MASTER_PLAN.md` never mentions expert disagreement.** It has no frame, no card, no token for it, while the shipped product has a DB table, an accent colour, an icon (`#i-conflict`) and a locked Checkpoint-A envelope for it. **CONFLICT D-7** |
| `skill` status `proposed\|active\|inactive\|archived` | `skills.status` is `active\|archived` only | **GAP** — §7.9's Review queue and the amber sidebar pill need two new states |
| `skill` fields | shipped also has `slug`, `guardrails`, `output_template`, `linked_claim_ids`, `origin_type`, `version` + a `skill_versions` history. The plan's model drops all of them | **DRIFT** — keep the shipped superset; they feed the locked Checkpoint-A "Contract vs Sketch" envelope |
| `connection` (`auth`, `key_hash`, `key_last4`, `last_used_at`, Roll the key) | `oauth_sessions` — `access_token_hash`, `refresh_token_hash`, `audience`, `revoked`, `client_name`. **No standalone API-key concept** | **GAP + CONFLICT D-8** |
| `call_log` (`connection_id`, `latency_ms`, `payload_bytes`, `args_digest`, `error_code`) | `audit_logs` — `user_id`, `client_name`, `surface`, `action`, `target_id`, `status` | **GAP** — §7.10's latency column and its connection link have no data |
| `job` — one row **per step**, 6 fixed steps, `detail`, `started_at`, `finished_at` | `ingestion_jobs` — **one row per job**, single `status` of `queued\|fetching\|extracting\|linking\|ready\|failed\|blocked_by_platform` | **GAP** — the §4.4 work block cannot be "a view of real state" without per-step rows |
| `blocked` → "Needs you" | `blocked_by_platform` already exists, with a rescue slot (`DESIGN_DECISIONS.md`) | **MATCH in substance** |

---

## 5. §9.2 MCP SURFACE — the hardest conflict

| Plan (9 tools) | Shipped (8 tools) |
|---|---|
| `search_rules`, `search_sources`, `search_transcripts`, `get_transcript`, `get_summary`, `list_skills`, `get_skill`, `add_source`, `archive` | `search`, `fetch`, `ingest_source`, `get_ingest_status`, `remember_this`, `get_or_synthesize_skill`, `save_skill`, `archive_item` |

**Zero name overlap.** Both honour "no `delete` tool, archive only".

What switching costs:
1. Every already-connected client breaks — the URL survives, the tool names do not.
2. `DESIGN_DECISIONS.md` Checkpoint A (three locked envelopes, iterated with the
   founder) is written against the shipped tools and would be void.
3. `tests/test_phase3_mcp_and_auth.py` (1,524 lines) plus parts of phases 6 and 7
   are written against the shipped names.
4. `search` + `fetch` are the two names a **ChatGPT deep-research connector looks
   for by convention**; renaming them to `search_rules`/`search_sources` is a
   behavioural change in ChatGPT, not just a rename. *(Not asserted from the
   codebase — there is no comment recording this rationale; flagged as a risk to
   confirm before any rename.)*
5. The plan's set drops `remember_this` (write a claim straight from chat) and
   `get_or_synthesize_skill` (the synthesis draft flow) — two shipped
   capabilities with no replacement in the plan.

**`CONFLICT D-9`.**

---

## 6. §9.3 / §10 AUTH AND SECURITY

| Plan | Shipped | Verdict |
|---|---|---|
| `?key=…` **or** `Authorization: Bearer`, one validation function | **Bearer only.** `app/mcp_server.py` verifies our own JWT via `BrainTokenVerifier`; `app/auth.py:797` returns `401` + `WWW-Authenticate` when absent. No query-string credential path | **CONFLICT D-8** |
| OAuth 2.1 + PKCE, RFC 9728 + RFC 8414 metadata, audience validation | All present: `/.well-known/*`, PKCE (`app/auth.py:106`), `validate_token_resource=True` (RFC 8707) | **MATCH** |
| Redirect URIs: `claude.ai/api/mcp/auth_callback`, `chatgpt.com/connector/oauth/{id}`, loopback | present (phase 7e, "real-client interop") | **MATCH — verify the exact list** |
| Keys hashed with **Argon2id or scrypt** | `hashlib.sha256` (`app/db/control.py:155`) | **DRIFT, defensible.** Argon2id exists to slow brute force on *low-entropy* secrets. These are 32-byte random tokens; SHA-256 is the correct choice and adding a KDF costs latency on every MCP call for no security gain. Keep SHA-256 and record the reason |
| Cookies `SameSite=Lax` | `SameSite=Strict` + double-submit CSRF + `X-Brain-Request` guard | **DRIFT** — shipped is stricter |
| `Referrer-Policy: strict-origin-when-cross-origin` | `no-referrer` | **DRIFT** — shipped is stricter, and §10.2 (never leak the key) wants it |
| strict CSP, `frame-ancestors 'none'` | present, plus COOP/CORP, HSTS, Permissions-Policy | **MATCH / stricter** |
| Uploads: sniff content, cap size, **strip EXIF**, serve from a separate origin | 20 MB cap, DOCX zip-bomb guard + ratio check, PDF page cap. **No EXIF stripping found**; images go to a vision provider (`fetchers.py:466`) and the bytes are not re-served | **GAP** (EXIF) — note the "separate origin" rule is moot while files are never served back |
| SSRF: DNS pre-resolution, redirect-hop validation, byte cap, timeout | `app/security/ssrf.py` (223 lines) | **MATCH** |
| Rate limits per user and per connection | `app/security/ratelimit.py` — per user; per-**connection** not separated | **GAP** |
| Prompt injection: fetched content is data; derived items are proposals | `app/security/sanitizer.py` + `UNTRUSTED_LIBRARY_DATA` wrapper. **But** shipped skills have no `proposed` state, so "skills require approval" is **not** currently enforced | **GAP — security-relevant.** Plan §10.10 calls approval a security control; the shipped flow writes skills active |
| Export (`zip` of Markdown) and real deletion | `GET /api/export` + trash/delete path with 30-day buffer | **MATCH — verify zip format** |

---

## 7. §11 I18N

**Zero i18n in the codebase.** No `data-i18n`, no dictionary, no
`Accept-Language` handling, no `?lang=`. Every string in `index.html` and
`app.js` is a hardcoded English literal.

The plan requires server-side resolution *before the first byte* — which only
exists if templating is server-side (`D-1`). A client-rendered SPA either ships
both dictionaries to the browser or fetches one, and the plan explicitly rejects
the second ("a visible flash of English"). **So `§11` and `D-1` are the same
decision.** **GAP, gated on `D-1`.**

---

## 8. §12 RESPONSIVE / §14 ACCEPTANCE

| Plan | Shipped | Verdict |
|---|---|---|
| `viewport-fit=cover`, safe-area insets | present in `index.html:5`; `--vvh`/`--kb` keyboard vars in `app.css:381` | **MATCH** |
| < 720: sidebar → top bar | a `.mobile-bar` exists (phase 7d/7e mobile hardening) | **MATCH in substance** |
| Token lint: zero hex outside `:root` | **passes today** — hexes live only in the three `:root` blocks | **MATCH** |
| Contrast ≥ 4.5:1 / borders ≥ 3:1 in all three themes | not measured for the plan's palette; the shipped palette was audited in phase 7 | **GAP** — re-measure after any token migration |
| `prefers-reduced-motion` collapses everything | present | **MATCH — verify it covers the new typewriter** |
| No scroll on Home at 1512×982 | no Home frame exists yet | **GAP** |

---

## 9. FOUNDER'S RULING — 29 OCTOBER 2026

Recorded verbatim in substance, because it governs every row above.

> The project was finished. The **web app design** was what the founder did not
> like, so the redesign was started with a second agent — **an agent that never
> saw this codebase.**
>
> So: **what `MASTER_PLAN.md` supplies is the web app design, and only that.**
> Everything in it that is not design — the data model (§8), the backend
> contract (§9), the MCP tool surface (§9.2), the stack choices in §1.1 — was
> written without knowledge of what already exists. Those parts are
> **proposals, not requirements.** The shipped engine, schema, MCP tools,
> OAuth layer and security headers stay.
>
> Where the new design genuinely cannot work on the shipped backend, the change
> it needs is **presented to the founder and validated before anything is
> modified or removed.** Nothing is silently replaced.

### 9.1 How that resolves the conflicts

| id | Resolution | Reason |
|---|---|---|
| `D-1` | **Keep the static SPA.** Build the plan's 14 screens as real URLs behind a `history.pushState` router in `app.js` | Jinja2 is a stack proposal, not design. The *screens, their names and Back working* are design and are honoured; how bytes reach the browser is not. Keeps the CSP, the `/preview` harness and every shipped test |
| `D-2` | **Keep the hand-written tokenised CSS.** No Tailwind, no CDN | Stack proposal, written without knowing the app serves `script-src 'self'` and blocks CDN scripts by its own header. The plan's own prohibitions leave Tailwind nearly nothing to do. Every design *value* is implemented either way |
| `D-3` | **Follow the plan** — 240px sidebar on `--bg-base` | Design |
| `D-4` | **Follow the plan** — two token layers, primitives then semantics, the plan's light palette | Design. The five colliding names are migrated, not aliased: `app.css` is being replaced by the redesign anyway |
| `D-5` | **No schema change attempted first.** A source's `Processing` state is derivable from `ingestion_jobs`, and `blocked_by_platform` already *is* the plan's `Needs you` | Prefer zero backend change where the design can be served by what exists |
| `D-6` | **A Rule is a rendering of the shipped Epistemic Claim**, never a new table | The claim is a superset. What of it shows on the card is a design question — co-designed against the reference image, not guessed |
| `D-7` | **RULED: out of the web app, kept on the MCP side.** Expert disagreement stays in the engine, in `tension_links`, in hybrid search and in the Checkpoint-A envelope — it surfaces where it is useful, through the host LLM or agent. The redesigned web surface shows **no** conflict card, no conflict pill, no conflict accent | The founder's reasoning: the person reading the web app does not need it; the agent working the knowledge does. **Nothing is deleted.** Removing the table or the MCP formatting would be a different and unauthorised change |
| `D-8` | **RULED: approved.** `?key=…` is implemented as a second entry point into the one existing validation function, with §10.2's mitigations | Part of the blanket approval of §9.2: "on va implémenter ce que le design réclame, backend" |
| `D-9` | **Keep the shipped 8 MCP tools.** `search`, `fetch`, `ingest_source`, `get_ingest_status`, `remember_this`, `get_or_synthesize_skill`, `save_skill`, `archive_item` | Not design. Renaming breaks live clients, voids Checkpoint A and rewrites ~1,500 test lines for no capability gain. Consequence: §7.10's call log shows the real tool names, not the plan's illustrative `search_rules` |

### 9.2 What the new design asks of the backend — **APPROVED 29 October 2026**

The founder approved this list in full: *"On va implémenter ce que le design
réclame, backend."* Two rows were settled individually at the same time and are
struck below. Ordered by cost.

| # | The design needs | What exists | Smallest change | Cost |
|---|---|---|---|---|
| 1 | A URL per screen, Back, deep links (§5) | one page, four `data-view` buttons | a router in `app.js` | small, front-end only |
| 2 | `audit_logs` latency + which connection (§7.10) | neither column | two columns | small |
| 3 | Word count on a transcript (§7.5) | `source_content.full_text` | computed on read | free |
| 4 | ~~Speaker names in the transcript body (§7.5)~~ | `cues_json` has no speaker | **DROPPED by the founder.** Knowing which speaker is talking is not important. `speakers` leaves the §7.5 meta line; the transcript body renders turns without attribution | none — removed from scope |
| 5 | A skill review queue: `proposed` → `active`, and `inactive` (§7.9) | `skills.status` is `active\|archived` only | two enum values, and the MCP tools must refuse both | small schema — and it closes a real hole: §10.10 calls approval a security control, and the shipped flow writes skills straight to active |
| 6 | Six visible pipeline steps, each with its own result (§4.4, §7.3) | one `status` per job | a `job_steps` table the engine writes | medium |
| 7 | Connections: a pasteable link carrying a key, Roll the key (§7.11) | bearer-only, no standalone key | a `connections` table + accept `?key=` in the same validation function | medium — and it is what makes the product usable without a terminal |
| 8 | Summaries: a list, a detail, Brief/Full, a sidebar count (§7.6) | **nothing** | a `summaries` table, a pipeline step, a formatter | large — a new product capability, not a design change |
| 9 | French (§11) | no i18n at all | a dictionary + `data-i18n` on every string | large, and not needed for the design pass |

---

## 10. BASELINE

| Run | Result |
|---|---|
| `BRAIN_ENV=test python3 -m pytest` @ `0674eb0` | **808 passed, 0 failed**, 25 warnings, 109s |

Warnings are all one kind: `InsecureKeyLengthWarning` from PyJWT, raised by tests that
sign with deliberately short throwaway keys. Not a product finding.

`.claude/setup.sh` does **not** install `mcp` or `yt-dlp` (§1.1) — both are
needed before the suite can run in a fresh Cloud VM.
