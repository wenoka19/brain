"""The 8 canonical brain operations, plus the synchronous job runner.

This module is the whole product's logic and returns **plain structured data**.
Markdown envelopes, tone and `[AGENT_HINT]` lines live in
`app/core/formatting.py`, which is co-designed with the founder at Checkpoint A —
so nothing here hard-codes how `brain` speaks.
"""

from __future__ import annotations

import contextlib
import logging
import re

import functools
import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from app.config import Settings, settings
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.db.search import SearchResults, hybrid_search
from app.ingestion import gatekeeper
from app.ingestion.extractor import chunk_content, extract_grounded_claims
from app.ingestion.gatekeeper import GatekeeperError
from app.ingestion.linker import link_claims
from app.ingestion.providers import FetchedContent, ProviderError, Providers, providers
from app.security.sanitizer import clean_text, lint_skill_safety
from app.storage import r2_sync

Scope = Literal["all", "claims", "skills"]
ItemType = Literal["source", "claim", "skill"]


# ===========================================================================
# result envelopes (data only — no formatting)
# ===========================================================================
@dataclass
class OpResult:
    ok: bool
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    error_reason: str = ""
    error_message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)



LOGGER = logging.getLogger("brain.engine")

def _fail(action: str, reason: str, message: str) -> OpResult:
    return OpResult(ok=False, action=action, error_reason=reason, error_message=message)


def _syncs_tenant(fn):
    """Flag the tenant database for snapshot after a successful write.

    Marking is a dict write behind a lock — it never blocks the request, and
    the actual `VACUUM INTO` + upload happens on the debounced background
    thread. With no object storage configured this is a no-op, so local runs
    and tests pay nothing for it.
    """

    @functools.wraps(fn)
    def wrapper(ctx: "Context", *args: Any, **kwargs: Any) -> OpResult:
        result = fn(ctx, *args, **kwargs)
        if getattr(result, "ok", False):
            r2_sync.mark_dirty(ctx.user_id)
        return result

    return wrapper


@dataclass
class Context:
    """Everything an operation needs, resolved from a *verified* identity."""

    user_id: str
    control: sqlite3.Connection
    tenant: sqlite3.Connection
    cfg: Settings
    prov: Providers
    client_name: str = "mcp_client"
    surface: str = "mcp"

    def audit(
        self,
        action: str,
        status: str,
        target_id: str | None = None,
        detail: Any = "",
        reasoning_trace: str = "",
    ) -> None:
        control_db.audit(
            self.control,
            user_id=self.user_id,
            action=action,
            status=status,
            surface=self.surface,
            client_name=self.client_name,
            target_id=target_id,
            detail=detail,
            reasoning_trace=reasoning_trace,
        )


def build_context(
    user_id: str,
    *,
    client_name: str = "mcp_client",
    surface: str = "mcp",
    cfg: Settings | None = None,
) -> Context:
    """Resolve a tenant context. ``user_id`` must already be verified.

    The tenant path is derived from ``control.db``'s record, never from input:
    an unknown user id raises before any filesystem access.
    """
    cfg = cfg or settings()
    control = control_db.control()
    if control_db.get_user(control, user_id) is None:
        raise ValueError(f"unknown user_id {user_id!r}")
    return Context(
        user_id=user_id,
        control=control,
        tenant=tenant_db.tenant(user_id, cfg=cfg),
        cfg=cfg,
        prov=providers(cfg),
        client_name=client_name,
        surface=surface,
    )


# ===========================================================================
# 1. search
# ===========================================================================
def op_search(ctx: Context, query: str, scope: Scope = "all") -> OpResult:
    query = clean_text(query)[:500]
    if not query:
        ctx.audit("search", "rejected")
        return _fail("search", "empty_query", "A search needs at least one word.")

    embedding = ctx.prov.embedder.embed([query])[0]
    results: SearchResults = hybrid_search(
        ctx.tenant, query=query, embedding=embedding, scope=scope, cfg=ctx.cfg
    )
    ctx.audit("search", "ok", detail={"query": query, "scope": scope, "hits": results.total})
    return OpResult(
        ok=True,
        action="search",
        data={
            "query": query,
            "scope": results.scope,
            "claims": [_claim_view(hit) for hit in results.claims],
            "skills": [_skill_view(hit) for hit in results.skills],
            "total": results.total,
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


def _claim_view(hit) -> dict[str, Any]:
    record = hit.record
    return {
        "id": record["id"],
        "claim": record["claim"],
        "speaker": record["speaker"],
        "evidence_basis": record["evidence_basis"],
        "domain_tags": record["domain_tags"],
        "when_it_applies": record["when_it_applies"],
        "when_it_fails": record["when_it_fails"],
        "exact_steps": record.get("exact_steps", []),
        "metrics_and_quotes": record["metrics_and_quotes"],
        "anchor_quote": record["anchor_quote"],
        "locator_type": record["locator_type"],
        "locator_value": record["locator_value"],
        "salience": record["salience"],
        "source": {
            "id": record["source_id"],
            "title": record.get("source_title", ""),
            "url": record.get("source_url", ""),
            "type": record.get("source_type", ""),
            "author": record.get("source_author", ""),
        },
        "deep_link": deep_link(
            record.get("source_url", ""), record["locator_type"], record["locator_value"]
        ),
        "matched_by": hit.matched_by,
        "score": hit.rrf_score,
        "tensions": [
            {
                "link_id": tension["link_id"],
                "link_type": tension["link_type"],
                "divergence_summary": tension["divergence_summary"],
                "other_id": tension["other_id"],
                "other_claim": tension["other_claim"],
                "other_speaker": tension["other_speaker"],
                "other_evidence_basis": tension["other_evidence_basis"],
                "other_anchor_quote": tension["other_anchor_quote"],
                "other_when_it_applies": tension["other_when_it_applies"],
                "other_when_it_fails": tension["other_when_it_fails"],
                "other_metrics_and_quotes": tension["other_metrics_and_quotes"],
                "other_salience": tension["other_salience"],
                "other_source_title": tension["other_source_title"],
                "other_locator_type": tension["other_locator_type"],
                "other_locator_value": tension["other_locator_value"],
                "other_deep_link": deep_link(
                    tension["other_source_url"],
                    tension["other_locator_type"],
                    tension["other_locator_value"],
                ),
            }
            for tension in hit.tensions
        ],
    }


def _skill_view(hit) -> dict[str, Any]:
    record = hit.record
    return {
        "id": record["id"],
        "slug": record["slug"],
        "name": record["name"],
        "description": record["description"],
        "trigger_when": record["trigger_when"],
        "execution_steps": record["execution_steps"],
        "guardrails": record["guardrails"],
        "output_template": record["output_template"],
        "linked_claim_ids": record["linked_claim_ids"],
        "origin_type": record["origin_type"],
        "version": record["version"],
        "matched_by": hit.matched_by,
        "score": hit.rrf_score,
    }


def deep_link(source_url: str, locator_type: str, locator_value: str) -> str:
    """Build the clickable, verifiable link for a citation."""
    if not source_url:
        return ""
    if locator_type == "timestamp" and str(locator_value).isdigit():
        joiner = "&" if "?" in source_url else "?"
        return f"{source_url}{joiner}t={int(locator_value)}"
    if locator_type == "page" and str(locator_value).isdigit():
        return f"{source_url}#page={int(locator_value)}"
    return source_url


# ===========================================================================
# 2. fetch
# ===========================================================================
def _visible(ctx: Context, record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Over MCP, an archived item is not hidden — it does not exist.

    `MASTER_PLAN.md` §9.2: *"Never exposed: anything archived … This is
    enforced server-side, not by prompt."* §7.12 makes the person the matching
    promise: *"nothing here is reachable by your AI until you restore it."*

    So an archived id must be indistinguishable from an id that was never
    issued: the same `not_found`, the same wording, the same audit line. A
    "that exists but you may not have it" would leak the one bit the promise
    is about.

    The filter lives here rather than in `tenant_db` for two reasons:
    `op_archive_item` has to read an archived row in order to restore it, and
    the web surface has a whole page for them (§7.12).
    """
    if record is None:
        return None
    if ctx.surface == "mcp" and record.get("status") != "active":
        return None
    return record


def op_fetch(ctx: Context, item_id: str) -> OpResult:
    item_id = (item_id or "").strip()
    if not item_id:
        return _fail("fetch", "empty_id", "An id is required.")

    claim = _visible(ctx, tenant_db.get_claim(ctx.tenant, item_id))
    if claim:
        source = tenant_db.get_source(ctx.tenant, claim["source_id"]) or {}
        # Archiving a source cascades to its claims, so an active claim under
        # an archived source should not exist. Checked anyway: the cascade is
        # one `UPDATE` away from a future change, and this costs nothing.
        if _visible(ctx, source) is None and source:
            claim = None
    if claim:
        tensions = tenant_db.tensions_for_claims(ctx.tenant, [item_id]).get(item_id, [])
        ctx.audit("fetch", "ok", target_id=item_id, detail={"kind": "claim"})
        return OpResult(
            ok=True,
            action="fetch",
            data={
                "kind": "claim",
                "claim": claim,
                "source": source,
                "deep_link": deep_link(
                    source.get("canonical_url", ""), claim["locator_type"], claim["locator_value"]
                ),
                "tensions": [
                    {
                        **tension,
                        "other_deep_link": deep_link(
                            tension["other_source_url"],
                            tension["other_locator_type"],
                            tension["other_locator_value"],
                        ),
                    }
                    for tension in tensions
                ],
            },
        )

    skill = _visible(ctx, tenant_db.get_skill(ctx.tenant, item_id)) or _visible(
        ctx, tenant_db.get_skill_by_slug(ctx.tenant, item_id)
    )
    if skill:
        linked = [
            _visible(ctx, tenant_db.get_claim(ctx.tenant, cid))
            for cid in skill["linked_claim_ids"]
        ]
        ctx.audit("fetch", "ok", target_id=skill["id"], detail={"kind": "skill"})
        return OpResult(
            ok=True,
            action="fetch",
            data={
                "kind": "skill",
                "skill": skill,
                "linked_claims": [c for c in linked if c],
            },
        )

    source = tenant_db.get_source(ctx.tenant, item_id)
    if source:
        claims = tenant_db.claims_for_source(ctx.tenant, item_id)
        ctx.audit("fetch", "ok", target_id=item_id, detail={"kind": "source"})
        return OpResult(
            ok=True,
            action="fetch",
            data={"kind": "source", "source": source, "claims": claims},
        )

    ctx.audit("fetch", "not_found", target_id=item_id)
    return _fail("fetch", "not_found", f"Nothing in the library has the id `{item_id}`.")


# ===========================================================================
# 3. ingest_source
# ===========================================================================
def op_ingest_source(
    ctx: Context, url: str, user_note: str | None = None, *, run_now: bool = False
) -> OpResult:
    """Queue a URL. Returns in well under 100ms: no fetching happens here."""
    try:
        target = gatekeeper.normalize_url(url, cfg=ctx.cfg)
    except GatekeeperError as exc:
        ctx.audit("ingest_source", "rejected", detail={"url": url, "reason": exc.reason})
        return _fail("ingest_source", exc.reason, exc.message)
    except Exception as exc:  # SSRFBlocked and friends
        reason = getattr(exc, "reason", "blocked_url")
        ctx.audit("ingest_source", "rejected", detail={"url": url, "reason": reason})
        return _fail(
            "ingest_source",
            reason,
            "That link points at a private or unreachable address, so it was refused.",
        )

    existing = tenant_db.find_source_by_url(ctx.tenant, target.canonical_url)
    if existing:
        ctx.audit("ingest_source", "duplicate", target_id=existing["id"])
        return OpResult(
            ok=True,
            action="ingest_source",
            data={
                "duplicate": True,
                "source_id": existing["id"],
                "title": existing["title"],
                "canonical_url": existing["canonical_url"],
                "claim_count": len(tenant_db.claims_for_source(ctx.tenant, existing["id"])),
            },
        )

    # `R-18`: a URL can only be checked against an empty balance here, never
    # against a price. `normalize_url` knows no duration, no byte count and
    # no page count — `NormalizedTarget.estimated_minutes` is a field nothing
    # in the repo ever assigns — so the real charge waits for the gate in
    # `_ingest_content`, after the fetch. What this catches is the case worth
    # catching: a spent account queueing work that is certain to be refused.
    if control_db.credits_left(ctx.control, ctx.user_id, cfg=ctx.cfg) <= 0:
        ctx.audit("ingest_source", "out_of_credits")
        return _fail(
            "ingest_source",
            "out_of_credits",
            f"Your {ctx.cfg.quota_credits:,} credits for this month are spent. "
            f"They come back on the first.",
        )

    source_id = control_db.new_id("src")

    # The row exists from the moment the work is queued, not from the moment
    # the fetch succeeds. §7.7 has three states — none, `Processing`, `Needs
    # you` — and the last two have nothing to attach to if the row only
    # appears on success. It is also what makes §7.1's submit able to land on
    # the source's own page: before this, it landed on a 404.
    #
    # The title is provisional and the pipeline overwrites it with the real
    # one; until then it is the thing the person handed over, which is what
    # they would recognise it by anyway.
    tenant_db.upsert_source(
        ctx.tenant,
        source_id=source_id,
        title=_provisional_title(target),
        source_type=target.source_type,
        canonical_url=target.canonical_url,
        status="processing",
    )

    job = control_db.create_job(
        ctx.control,
        ctx.user_id,
        source_id=source_id,
        input_target=target.canonical_url,
        source_type=target.source_type,
        user_note=clean_text(user_note or "")[:2000],
    )
    ctx.audit("ingest_source", "queued", target_id=job["job_id"], detail={"url": target.canonical_url})

    result = OpResult(
        ok=True,
        action="ingest_source",
        data={
            "duplicate": False,
            "job_id": job["job_id"],
            "source_id": source_id,
            "source_type": target.source_type,
            "canonical_url": target.canonical_url,
            "user_note": job["user_note"],
            "status": job["status"],
            "in_flight": control_db.count_active_jobs(ctx.control, ctx.user_id),
        },
    )
    if run_now:
        processed = run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg, limit=1)
        result.data["processed"] = processed
    return result


def _upload_price(validated, *, cfg: Settings) -> int | None:
    """What this upload will cost, or None when it cannot be known yet.

    None for an image (the vision call *is* the fetch, so there are no
    characters to count until the money is spent — bounded at one response,
    so the overdraw is a few credits against the thousands a text file can
    cost), for a file over the pre-count ceiling, and for anything the local
    parsers cannot open. Never a guess: a number here is the same number the
    gate will charge, counted the same way, or there is no number.
    """
    from app.ingestion import fetchers

    if validated.source_type == "image" or validated.size_bytes > cfg.credit_precount_max_bytes:
        return None
    parse = {
        "text": fetchers.parse_text_file,
        "pdf": fetchers.parse_pdf_file,
        "docx": fetchers.parse_docx_file,
    }.get(validated.source_type)
    if parse is None:
        return None
    try:
        content = parse(validated, cfg=cfg)
    except Exception:
        # A file the parser chokes on is the fetch's problem to report, with
        # its own remedy. Pricing is not the place to turn that into a
        # different error.
        return None
    billable = sum(len(chunk.text) for chunk in chunk_content(content))
    return math.ceil(billable / max(1, cfg.credit_chars))


def op_ingest_file(
    ctx: Context, path: str | Path, *, filename: str | None = None, user_note: str | None = None
) -> OpResult:
    """Queue an uploaded file (web surface only — never exposed over MCP).

    `R-18`: every path out of here that does *not* queue a job has to let go
    of the staged bytes itself. Only a job row maps a source back to its
    file, so a refusal or a duplicate leaves one nothing in the product can
    reach — the 30-day sweep would eventually get it, but a person who asked
    Brain not to keep their files should not have to wait out a window they
    turned off.
    """
    def _drop_unqueued() -> None:
        _release_upload(
            ctx.control, ctx.user_id, {"input_target": str(path)}, cfg=ctx.cfg
        )

    try:
        validated = gatekeeper.validate_file(path, filename=filename, cfg=ctx.cfg)
    except GatekeeperError as exc:
        ctx.audit("ingest_file", "rejected", detail={"reason": exc.reason})
        _drop_unqueued()
        return _fail("ingest_file", exc.reason, exc.message)

    existing = tenant_db.find_source_by_hash(ctx.tenant, validated.content_hash)
    if existing:
        ctx.audit("ingest_file", "duplicate", target_id=existing["id"])
        _drop_unqueued()
        return OpResult(
            ok=True,
            action="ingest_file",
            data={"duplicate": True, "source_id": existing["id"], "title": existing["title"]},
        )

    # `R-18`: an upload is the one case where the price can be known at the
    # button. The bytes are already on local disk and parsing them is pure
    # Python, so a file that cannot be afforded is refused with the real
    # number instead of the person discovering it in a failed job minutes
    # later. Above `credit_precount_max_bytes` the parse would cost more
    # latency than the honesty is worth, and an image has no characters at
    # all until the vision call — both fall through to the empty-balance
    # check and let the gate decide.
    left = control_db.credits_left(ctx.control, ctx.user_id, cfg=ctx.cfg)
    price = _upload_price(validated, cfg=ctx.cfg)
    if (price is not None and price > left) or (price is None and left <= 0):
        ctx.audit("ingest_file", "out_of_credits", detail={"price": price})
        _drop_unqueued()
        return _fail(
            "ingest_file",
            "out_of_credits",
            (
                f"`{validated.filename}` needs {price:,} credits and you have "
                f"{left:,} left this month. They come back on the first."
                if price is not None
                else f"Your {ctx.cfg.quota_credits:,} credits for this month are spent. "
                f"They come back on the first."
            ),
        )

    source_id = control_db.new_id("src")
    job = control_db.create_job(
        ctx.control,
        ctx.user_id,
        source_id=source_id,
        input_target=str(validated.path),
        source_type=validated.source_type,
        user_note=clean_text(user_note or "")[:2000],
    )
    ctx.audit("ingest_file", "queued", target_id=job["job_id"], detail={"file": validated.filename})
    return OpResult(
        ok=True,
        action="ingest_file",
        data={
            "duplicate": False,
            "job_id": job["job_id"],
            "source_id": source_id,
            "source_type": validated.source_type,
            "filename": validated.filename,
            "user_note": job["user_note"],
            "status": job["status"],
            "in_flight": control_db.count_active_jobs(ctx.control, ctx.user_id),
        },
    )


# ===========================================================================
# 4. get_ingest_status
# ===========================================================================
def op_get_ingest_status(ctx: Context, job_id: str | None = None) -> OpResult:
    if job_id:
        job = control_db.get_job(ctx.control, job_id)
        if job is None or job["user_id"] != ctx.user_id:
            return _fail("get_ingest_status", "not_found", f"No job with id `{job_id}`.")
        jobs = [job]
    else:
        jobs = control_db.list_jobs(ctx.control, ctx.user_id, limit=5)

    views = []
    for job in jobs:
        source = tenant_db.get_source(ctx.tenant, job["source_id"]) or {}
        views.append(
            {
                "job_id": job["job_id"],
                "status": job["status"],
                "source_type": job["source_type"],
                "input_target": job["input_target"],
                "title": source.get("title", ""),
                "source_id": job["source_id"] if source else "",
                "claims_extracted": job["claims_extracted"],
                "error_message": job["error_message"],
                "attempts": job["attempts"],
                "created_at": job["created_at"],
            }
        )
    ctx.audit(
        "get_ingest_status",
        "ok",
        target_id=job_id,
        detail={"jobs": len(views)},
    )
    return OpResult(
        ok=True,
        action="get_ingest_status",
        data={"jobs": views, "library_counts": tenant_db.counts(ctx.tenant)},
    )


# ===========================================================================
# 5. remember_this
# ===========================================================================
USER_AXIOM_SOURCE_ID = "src_user_axioms"


@_syncs_tenant
def op_remember_this(
    ctx: Context,
    claim: str,
    when_it_applies: str,
    when_it_fails: str = "",
    metrics_and_quotes: str = "",
    domain_tags: Sequence[str] | None = None,
) -> OpResult:
    claim_text = clean_text(claim)[:600]
    applies = clean_text(when_it_applies)[:300]
    if not claim_text:
        return _fail("remember_this", "empty_claim", "There is nothing to remember.")
    if not applies:
        return _fail(
            "remember_this",
            "missing_when_it_applies",
            "A remembered rule needs its conditions — when does it apply?",
        )

    tenant_db.upsert_source(
        ctx.tenant,
        source_id=USER_AXIOM_SOURCE_ID,
        title="Your own rules",
        source_type="user_note",
        canonical_url="",
    )
    record = tenant_db.insert_claim(
        ctx.tenant,
        source_id=USER_AXIOM_SOURCE_ID,
        claim=claim_text,
        speaker="You",
        evidence_basis="user_axiom",
        domain_tags=domain_tags or [],
        when_it_applies=applies,
        when_it_fails=clean_text(when_it_fails)[:300],
        metrics_and_quotes=clean_text(metrics_and_quotes)[:400],
        # A rule you stated *is* its own verbatim source, so it is grounded by
        # definition and needs no locator.
        anchor_quote=claim_text,
        locator_type="none",
        locator_value="",
        salience=5,
        cfg=ctx.cfg,
    )
    vector = ctx.prov.embedder.embed([_claim_embedding_text(record)])[0]
    tenant_db.set_claim_embedding(ctx.tenant, record["id"], vector, cfg=ctx.cfg)

    link = link_claims(
        ctx.tenant,
        [record],
        embeddings={record["id"]: vector},
        classifier=ctx.prov.linker,
        cfg=ctx.cfg,
    )
    ctx.audit("remember_this", "ok", target_id=record["id"])
    return OpResult(
        ok=True,
        action="remember_this",
        data={
            "claim": record,
            "new_tensions": link.created,
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


def _claim_embedding_text(claim: dict[str, Any]) -> str:
    """One text per claim for the vector lane: the claim plus its conditions."""
    return " ".join(
        part
        for part in (
            claim.get("claim", ""),
            claim.get("when_it_applies", ""),
            claim.get("when_it_fails", ""),
            " ".join(claim.get("domain_tags") or []),
        )
        if part
    )


# ===========================================================================
# 6. get_or_synthesize_skill
# ===========================================================================
def op_get_or_synthesize_skill(
    ctx: Context, task_description: str, skill_slug: str | None = None
) -> OpResult:
    if skill_slug:
        saved = tenant_db.get_skill_by_slug(ctx.tenant, skill_slug)
        if saved and saved["status"] == "active":
            ctx.audit("get_or_synthesize_skill", "saved_hit", target_id=saved["id"])
            return OpResult(
                ok=True,
                action="get_or_synthesize_skill",
                data={"origin": "saved", "skill": saved, "linked_claims": _linked(ctx, saved)},
            )

    task = clean_text(task_description)[:500]
    if not task:
        return _fail(
            "get_or_synthesize_skill", "empty_task", "Describe the task you need a skill for."
        )

    embedding = ctx.prov.embedder.embed([task])[0]
    results = hybrid_search(
        ctx.tenant, query=task, embedding=embedding, scope="all", cfg=ctx.cfg
    )

    if results.skills:
        best = results.skills[0]
        ctx.audit("get_or_synthesize_skill", "saved_hit", target_id=best.id)
        return OpResult(
            ok=True,
            action="get_or_synthesize_skill",
            data={
                "origin": "saved",
                "skill": best.record,
                "linked_claims": _linked(ctx, best.record),
                "score": best.rrf_score,
                "also_matched": [_skill_view(hit) for hit in results.skills[1:3]],
            },
        )

    # No saved skill: synthesise a draft from the best matching claims so the
    # agent has something concrete to offer instead of an empty answer.
    supporting = [_claim_view(hit) for hit in results.claims[:5]]
    if not supporting:
        ctx.audit("get_or_synthesize_skill", "empty_library")
        return OpResult(
            ok=True,
            action="get_or_synthesize_skill",
            data={"origin": "none", "task_description": task, "supporting_claims": []},
        )

    draft = {
        "slug": tenant_db.slugify(task)[:48],
        "name": task[:80].strip().rstrip(".").capitalize(),
        "description": f"Draft workflow synthesised from {len(supporting)} claims in your library.",
        "trigger_when": _draft_trigger(task, supporting),
        "execution_steps": _draft_steps(supporting),
        "guardrails": _draft_guardrails(supporting),
        "output_template": "",
        "linked_claim_ids": [claim["id"] for claim in supporting],
        "origin_type": "synthesized",
        "version": 0,
    }
    ctx.audit("get_or_synthesize_skill", "synthesized", detail={"task": task})
    return OpResult(
        ok=True,
        action="get_or_synthesize_skill",
        data={
            "origin": "synthesized",
            "task_description": task,
            "draft_skill": draft,
            "supporting_claims": supporting,
        },
    )


#: Placeholders the extractor emits when a source stated no condition. They are
#: honest in a claim envelope but useless as a skill trigger.
_CONDITION_PLACEHOLDERS = ("not stated by the source", "working on")


def _draft_trigger(task: str, supporting: list[dict[str, Any]]) -> str:
    """Prefer a real condition from a backing claim; fall back to the task."""
    for claim in supporting:
        condition = (claim.get("when_it_applies") or "").strip()
        if condition and not condition.lower().startswith(_CONDITION_PLACEHOLDERS):
            return condition[:500]
    return task[:500]


def _draft_guardrails(supporting: list[dict[str, Any]]) -> str:
    """Collect distinct failure conditions — several claims often share one."""
    seen: set[str] = set()
    out: list[str] = []
    for claim in supporting:
        guardrail = (claim.get("when_it_fails") or "").strip()
        key = guardrail.lower().rstrip(".")
        if guardrail and key not in seen:
            seen.add(key)
            out.append(guardrail.rstrip(".") + ".")
    return " ".join(out)[:600]


def _draft_steps(supporting: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    for claim in supporting:
        if claim["exact_steps"]:
            steps.extend(str(s) for s in claim["exact_steps"])
        else:
            steps.append(claim["claim"])
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique = []
    for step in steps:
        key = step.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(step.strip())
    return unique[:8]


def _linked(ctx: Context, skill: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for claim_id in skill.get("linked_claim_ids", []):
        claim = tenant_db.get_claim(ctx.tenant, claim_id)
        if claim and claim["status"] == "active":
            out.append(claim)
    return out


# ===========================================================================
# 7. save_skill
# ===========================================================================
@_syncs_tenant
def op_save_skill(
    ctx: Context,
    slug: str,
    name: str,
    description: str,
    trigger_when: str,
    execution_steps: Sequence[str],
    guardrails: str = "",
    output_template: str = "",
    linked_claim_ids: Sequence[str] | None = None,
    origin_type: str = "distilled",
) -> OpResult:
    steps = [clean_text(str(step))[:500] for step in (execution_steps or []) if clean_text(str(step))]
    if not steps:
        return _fail("save_skill", "no_steps", "A skill needs at least one execution step.")
    if not clean_text(name):
        return _fail("save_skill", "no_name", "A skill needs a name.")
    if not clean_text(trigger_when):
        return _fail(
            "save_skill",
            "no_trigger",
            "A skill needs a trigger — when should it be reached for?",
        )

    # A saved skill is replayed to a model as *instructions*, unfenced. So it is
    # screened before it is stored, not spotlighted on the way out.
    report = lint_skill_safety(
        name, description, trigger_when, guardrails, output_template, execution_steps
    )
    if not report.safe:
        ctx.audit(
            "save_skill",
            "blocked_unsafe_content",
            detail=report.as_dict(),
            reasoning_trace=(
                "Blocked a skill before saving: the text "
                f"{report.findings[0].reason}."
            ),
        )
        return _fail("save_skill", "unsafe_skill_content", report.message)

    skill = tenant_db.upsert_skill(
        ctx.tenant,
        slug=slug or name,
        name=clean_text(name)[:200],
        description=clean_text(description)[:2000],
        trigger_when=clean_text(trigger_when)[:500],
        execution_steps=steps,
        guardrails=clean_text(guardrails)[:2000],
        output_template=clean_text(output_template)[:2000],
        linked_claim_ids=list(linked_claim_ids or []),
        origin_type=origin_type,
    )
    text = " ".join([skill["name"], skill["description"], skill["trigger_when"], *steps])
    tenant_db.set_skill_embedding(
        ctx.tenant, skill["id"], ctx.prov.embedder.embed([text])[0], cfg=ctx.cfg
    )
    ctx.audit("save_skill", "ok", target_id=skill["id"], detail={"version": skill["version"]})
    return OpResult(
        ok=True,
        action="save_skill",
        data={
            "skill": skill,
            "created": skill["version"] == 1,
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


# ===========================================================================
# 8. archive_item   (soft only — there is no delete over MCP)
# ===========================================================================
#: §7.12's list and §5's tool call the same thing by two names. The Archive
#: emits `rule`, because that is the word on the page; the MCP tool's signature
#: is `claim`, and a published tool signature does not change to suit a frame.
#: `summary` exists on the page and not in the tool — an agent has no business
#: setting aside a summary of a source it can still read.
_ARCHIVABLE = {
    "source": "source",
    "claim": "claim",
    "rule": "claim",      # §7.12's word for the same row
    "skill": "skill",
    "summary": "summary",  # web only; the MCP tool's Literal stays at three
}


@_syncs_tenant
def op_archive_item(ctx: Context, item_type: ItemType, item_id: str, *, restore: bool = False) -> OpResult:
    item_type = _ARCHIVABLE.get(item_type, "")  # type: ignore[arg-type]
    if not item_type:
        return _fail(
            "archive_item",
            "bad_item_type",
            "item_type must be source, claim (or rule), skill or summary.",
        )
    target_status = "active" if restore else "archived"
    # §7.12 prints `archived by you` or `archived by Brain` beside every row,
    # in amber for the second — "when something vanishes from the library
    # untouched, this page says who". An agent reaches the engine over MCP; a
    # person reaches it from the web.
    actor = "brain" if ctx.surface == "mcp" else "you"

    # Deliberately NOT filtered through `_visible`: restoring requires reading
    # the archived row, and archiving something twice should stay a no-op
    # rather than a lie about what exists.
    if item_type == "claim":
        record = tenant_db.get_claim(ctx.tenant, item_id)
        changed = (
            tenant_db.set_claim_status(ctx.tenant, item_id, target_status, actor=actor)
            if record else False
        )
        label = record["claim"][:120] if record else ""
    elif item_type == "skill":
        record = tenant_db.get_skill(ctx.tenant, item_id) or tenant_db.get_skill_by_slug(
            ctx.tenant, item_id
        )
        changed = (
            tenant_db.set_skill_status(ctx.tenant, record["id"], target_status, actor=actor)
            if record else False
        )
        label = record["name"] if record else ""
        item_id = record["id"] if record else item_id
    elif item_type == "summary":
        record = tenant_db.get_summary(ctx.tenant, summary_id=item_id)
        changed = (
            tenant_db.set_summary_status(ctx.tenant, item_id, target_status, actor=actor)
            if record else False
        )
        label = record["gist"][:120] if record else ""
    else:
        record = tenant_db.get_source(ctx.tenant, item_id)
        changed = (
            tenant_db.set_source_status(ctx.tenant, item_id, target_status, actor=actor)
            if record else False
        )
        label = record["title"] if record else ""

    if record is None:
        ctx.audit("archive_item", "not_found", target_id=item_id)
        return _fail("archive_item", "not_found", f"No {item_type} has the id `{item_id}`.")

    ctx.audit(
        "archive_item" if not restore else "restore_item",
        "ok" if changed else "no_change",
        target_id=item_id,
        detail={"item_type": item_type},
    )
    return OpResult(
        ok=True,
        action="archive_item",
        data={
            "item_type": item_type,
            "id": item_id,
            "label": label,
            "status": target_status,
            "changed": changed,
            "recoverable": True,
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


# ===========================================================================
# the ingestion pipeline + synchronous runner
# ===========================================================================
#: A claim earns a skill only if it reads like a procedure. `exact_steps` is
#: the extractor's own judgement that the source described how to do something
#: rather than that something is true — so this does not re-decide it, it
#: reuses it. Two steps is the floor: one step is an instruction, not a skill.
SKILL_DRAFT_MIN_STEPS = 2


def _draft_skills_from_claims(
    tdb: Any, source: dict[str, Any], claims: Sequence[dict[str, Any]], *, cfg: Settings
) -> list[dict[str, Any]]:
    """Turn the procedural claims of one source into skills awaiting review.

    Deliberately conservative. §7.9's queue is a thing a person has to empty by
    hand, and a pipeline that drafts six skills per video turns the review gate
    into a chore people click through — which is how a security control stops
    being one.
    """
    drafted: list[dict[str, Any]] = []
    for claim in claims:
        steps = claim.get("exact_steps") or []
        if len(steps) < SKILL_DRAFT_MIN_STEPS:
            continue

        slug = tenant_db.slugify(claim["claim"][:60]) or f"skill-{claim['id'][-8:]}"
        if tenant_db.get_skill_by_slug(tdb, slug):
            # A source re-processed, or two sources that said the same thing.
            # Never silently overwrite a skill the person may have approved.
            continue

        record = tenant_db.upsert_skill(
            tdb,
            slug=slug,
            name=claim["claim"][:120],
            description=claim.get("when_it_applies") or claim["claim"][:160],
            trigger_when=claim.get("when_it_applies") or "",
            execution_steps=steps,
            guardrails=claim.get("when_it_fails") or "",
            linked_claim_ids=[claim["id"]],
            origin_type="distilled",
            status="proposed",
            source_id=source["id"],
            source_locator=claim.get("locator_value") or "",
        )
        drafted.append(record)
    return drafted


def _step_detail_read(job: dict[str, Any], content: Any) -> str:
    """What step 1 reveals beside its name once it is done.

    The capture shows a URL for a link and a format for a file, elided rather
    than wrapped — the result sits inline next to the step name (§4.4), so a
    long one would push the row off the card.
    """
    target = job.get("input_target") or ""
    if job["source_type"] in ("pdf", "docx", "text", "image"):
        pages = (getattr(content, "metadata", None) or {}).get("pages")
        kind = job["source_type"].upper()
        return f"{kind} · {pages} pages" if pages else kind
    target = target.replace("https://", "").replace("http://", "")
    return target if len(target) <= 48 else target[:45] + "…"


def process_job(job: dict[str, Any], *, cfg: Settings | None = None) -> dict[str, Any]:
    """Run one leased job all the way through: fetch -> extract -> link."""
    cfg = cfg or settings()
    control = control_db.control()
    user_id = job["user_id"]
    tdb = tenant_db.tenant(user_id, cfg=cfg)
    prov = providers(cfg)
    job_id = job["job_id"]

    def finish(
        status: str, *, message: str = "", reason: str = "", claims: int = 0
    ) -> dict[str, Any]:
        # Even a failure can have written a source row before giving up, so
        # the tenant is flagged on every exit from the pipeline, not just on
        # `ready`.
        r2_sync.mark_dirty(user_id)
        control_db.set_job_status(
            control,
            job_id,
            status,
            error_message=message,
            # §7.7's `Fix this` tooltip states the remedy, and a sentence
            # written for the person cannot be matched on. The provider's own
            # reason code rides along so the frame can map it.
            error_reason=reason,
            claims_extracted=claims,
        )
        if status in ("failed", "blocked_by_platform"):
            # Nothing may stay `running` once the job is over, or the work
            # block spins a step forever on a job that died (§4.4, §9.1).
            control_db.abandon_steps(control, job_id, reason=message)
            # §7.7's third state. "Brain did not fail; Brain needs the
            # person" — so the row stays, carrying that, rather than
            # disappearing or pretending to be in memory.
            with contextlib.suppress(Exception):
                tenant_db.set_source_status(
                    tenant_db.tenant(user_id, cfg=cfg), job["source_id"], "blocked"
                )
            # `R-18`: a failure costs nothing. Read from the row, not from
            # `job`, which is the copy a worker took at claim time — the
            # charge was written after it.
            control_db.refund_credits(
                control, user_id, credits=control_db.job_charge(control, job_id)
            )
        # `R-18`'s opt-out, on every terminal exit — success and failure
        # alike. `finish` is the single one: `process_job` returns through it
        # from every fetch failure and hands the same closure to
        # `_ingest_content` for the success path, and `claim_next_job` never
        # re-leases a terminal job, so nothing can need the file afterwards.
        _release_upload(control, user_id, job, cfg=cfg)
        control_db.audit(
            control,
            user_id=user_id,
            action="process_job",
            status=status,
            surface="system",
            target_id=job_id,
            detail={"message": message, "claims": claims},
        )
        return {
            "job_id": job_id,
            "status": status,
            "claims": claims,
            "message": message,
            "source_id": job["source_id"],
        }

    # ---- step 1: reading the source ---------------------------------------
    # The ledger is seeded at queue time. This is the safety net for a job
    # queued before that was true, and it is `INSERT OR IGNORE`, so a retried
    # job keeps the history it already has.
    control_db.seed_job_steps(control, job_id)
    control_db.set_job_status(control, job_id, "fetching")
    control_db.start_step(control, job_id, "read")
    try:
        if job["source_type"] in ("pdf", "docx", "text", "image"):
            validated = gatekeeper.validate_file(job["input_target"], cfg=cfg)
            content = prov.fetcher.fetch_file(validated, cfg=cfg)
        else:
            target = gatekeeper.normalize_url(job["input_target"], cfg=cfg)
            content = prov.fetcher.fetch_url(target, cfg=cfg)
    except GatekeeperError as exc:
        return finish("failed", message=exc.message, reason=getattr(exc, "reason", "refused"))
    except ProviderError as exc:
        status = "blocked_by_platform" if exc.reason == "blocked_by_platform" else "failed"
        return finish(status, message=exc.message, reason=exc.reason)
    except Exception as exc:  # pragma: no cover - defensive
        return finish(
            "failed", message=f"Unexpected error while fetching: {exc}", reason="unexpected"
        )

    if not content.full_text:
        return finish(
            "failed",
            message="Nothing readable was found at that source.",
            reason="empty_content",
        )

    control_db.finish_step(
        control, job_id, "read", detail=_step_detail_read(job, content)
    )

    # ---- step 2: getting the words out of the source ----------------------
    # The same fetch produced them, so there is no second call to make: what
    # the person is told is what actually happened. The step runs for every
    # source — it is how a source becomes words — so it is the *word* for it
    # that changes, not whether it ran. It used to be "Transcribing audio"
    # for everything, and a text file got a row that denied its own label:
    # `Transcribing audio — 300 words read — no audio in this source`.
    control_db.start_step(control, job_id, "transcribe")
    words = len(content.full_text.split())
    captured = {
        "youtube": f"{words:,} words captured",
        "image": f"{words:,} words described",
    }.get(job["source_type"], f"{words:,} words read")
    control_db.finish_step(control, job_id, "transcribe", detail=captured)

    # A GitHub markdown file is usually a `SKILL.md` — procedural text the user
    # intends to *run*. Unlike a transcript, which is only ever quoted back
    # inside a fence, that will reach a model as instructions, so it is screened
    # at the door rather than spotlighted later.
    if job["source_type"] == "github":
        report = lint_skill_safety(content.title, content.full_text)
        if not report.safe:
            control_db.audit(
                control,
                user_id=user_id,
                action="ingest_source",
                status="blocked_unsafe_content",
                surface="system",
                target_id=job_id,
                detail=report.as_dict(),
                reasoning_trace=(
                    f"Refused {job['input_target']}: the file "
                    f"{report.findings[0].reason}."
                ),
            )
            return finish("failed", message=report.message)

    return _ingest_content(job, content, cfg=cfg, finish=finish)


def _release_upload(control, user_id: str, job: dict[str, Any], *, cfg: Settings) -> bool:
    """Delete the received file now, if the person asked not to keep it.

    `R-18`. The default is to keep it for `upload_retention_days`, because
    the reason for the window is that a failed read can be looked at. Turning
    the switch off is the person giving that up on purpose, so this runs on
    the failure path too — a file they asked not to keep is not kept because
    Brain had trouble with it.

    The path comes from `ingestion_jobs.input_target`, which is free text and
    holds a URL for link sources, so it is checked for containment inside the
    person's own uploads folder before anything is unlinked. That check is
    what stops a crafted or stale `input_target` from reaching a file that is
    not theirs — or not an upload at all.
    """
    target = str(job.get("input_target") or "")
    if not target:
        return False
    try:
        if control_db.ingestion_defaults(control, user_id).get("keep_uploads", 1):
            return False
        path = Path(target).resolve()
        if not path.is_relative_to(cfg.uploads_dir_for(user_id).resolve()):
            return False
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        # Housekeeping. A file that will not delete is swept in 30 days
        # anyway, and must not turn a finished job into a failed one.
        return False
    return True


def _ingest_content(job, content, *, cfg, finish):
    """Everything after a successful fetch: charge, store, extract, ground,
    link. Shared by the normal pipeline and by a rescue."""
    control = control_db.control()
    user_id = job["user_id"]
    tdb = tenant_db.tenant(user_id, cfg=cfg)
    prov = providers(cfg)
    job_id = job["job_id"]

    # The 120-minute admission ceiling — not the allowance. A video longer
    # than this is out of V1 scope whatever the person's balance is, and the
    # true duration is only knowable now.
    if content.duration_sec:
        try:
            gatekeeper.check_duration(content.duration_sec, cfg=cfg)
        except GatekeeperError as exc:
            return finish("failed", message=exc.message)

    # ---- `R-18`: THE GATE -------------------------------------------------
    # The one line in the product where the full text exists and nothing
    # expensive has run. Below it sit the summariser, the extractor, the
    # embedder and the linker; above it sits only the fetch, which costs no
    # credits. Both callers pass through here — the normal pipeline and
    # `op_rescue_job`'s pasted text — so one charge covers every source type
    # on both paths, images included.
    #
    # The billed number is the text the extractor will actually chunk, NOT
    # `content.full_text`. They are the same string for most sources, but
    # `clean_text` truncates `full_text` at 2,000,000 characters while
    # applying itself to each cue separately, leaving the cues uncapped — so
    # billing `full_text` would charge a 20MB text file for a tenth of what
    # it costs. Chunking is pure local Python with no LLM call.
    billable = sum(len(chunk.text) for chunk in chunk_content(content))
    credits = math.ceil(billable / max(1, cfg.credit_chars))
    # A retry charges once. `charged_credits` is read from the row, because
    # the job dict is a copy taken at claim time.
    already = control_db.job_charge(control, job_id)
    if not already and credits:
        if not control_db.debit_credits(control, user_id, credits, cfg=cfg):
            state = control_db.credits_state(control, user_id, cfg=cfg)
            return finish(
                "failed",
                reason="out_of_credits",
                message=(
                    f"Reading this needs {credits:,} credits and you have "
                    f"{state['left']:,} left this month. Nothing was read, and "
                    f"nothing was charged."
                ),
            )
        control_db.set_job_charge(control, job_id, credits)
        already = credits
    job["charged_credits"] = already

    source = tenant_db.upsert_source(
        tdb,
        source_id=job["source_id"],
        title=content.title,
        source_type=content.source_type or job["source_type"],
        canonical_url=job["input_target"] if job["source_type"] not in ("pdf", "docx", "text", "image") else "",
        content_hash=content.content_hash,
        author=content.author,
        published_at=content.published_at,
        language=content.language,
        duration_sec=content.duration_sec,
    )
    tenant_db.set_source_content(tdb, source["id"], content.full_text, content.cues)
    # The row was created `processing` when the work was queued. It becomes a
    # thing in the library only now that something has actually been read.
    tenant_db.set_source_status(tdb, source["id"], "active")

    # §9.1: "Steps 3-5 honour the person's ingestion defaults and their
    # per-source instruction." Read once, here, so a switch in §7.13 that is
    # off actually stops work rather than decorating the Profile page.
    wants = control_db.ingestion_defaults(control, user_id)

    # ---- step 3: writing the summary --------------------------------------
    if not wants["want_summary"]:
        control_db.finish_step(
            control, job_id, "summarise",
            detail="off in your settings",
            state="skipped",
        )
        written = {}
    else:
        control_db.start_step(control, job_id, "summarise")
        try:
            written = prov.summariser.summarise(
                content.full_text,
                content.cues,
                {
                    "title": source["title"],
                    "source_type": job["source_type"],
                    "author": content.author,
                    "user_note": job["user_note"],
                    "write_in": wants["write_in"],
                },
            ) if prov.summariser else {}
        except Exception as exc:  # pragma: no cover - defensive
            # §9.1 allows the summary step to fail without taking the source
            # with it: the transcript and the rules are still worth having.
            written = {}
            LOGGER.warning("summary failed for job %s: %s", job_id, exc)

        if written.get("gist"):
            summary = tenant_db.upsert_summary(
                tdb,
                source_id=source["id"],
                gist=written["gist"],
                points=written.get("points") or [],
                chapters=written.get("chapters") or [],
                instruction=job["user_note"],
            )
            control_db.finish_step(
                control, job_id, "summarise",
                detail=(
                    "following your instruction" if job["user_note"]
                    else f"{len(summary['points'])} key points"
                ),
            )
        else:
            control_db.finish_step(
                control, job_id, "summarise",
                detail="nothing in this source was long enough to summarise",
                state="skipped",
            )

    # ---- step 4: extracting rules -----------------------------------------
    control_db.set_job_status(control, job_id, "extracting")
    control_db.start_step(control, job_id, "extract")
    try:
        extraction = extract_grounded_claims(
            content, extractor=prov.extractor, user_note=job["user_note"], cfg=cfg
        )
    except Exception as exc:  # pragma: no cover - defensive
        return finish("failed", message=f"Extraction failed: {exc}")

    if not extraction.claims:
        return finish(
            "failed",
            message=(
                "No claim in that source could be grounded in a verbatim quote "
                f"({extraction.rejected} candidates were rejected)."
            ),
        )

    stored: list[dict[str, Any]] = []
    embeddings: dict[str, Sequence[float]] = {}
    texts = [_claim_embedding_text(draft) for draft in extraction.claims]
    vectors = prov.embedder.embed(texts)
    for draft, vector in zip(extraction.claims, vectors):
        record = tenant_db.insert_claim(
            tdb,
            source_id=source["id"],
            claim=draft["claim"],
            speaker=draft.get("speaker") or content.author,
            evidence_basis=draft["evidence_basis"],
            domain_tags=draft.get("domain_tags", []),
            when_it_applies=draft["when_it_applies"],
            when_it_fails=draft.get("when_it_fails", ""),
            exact_steps=draft.get("exact_steps", []),
            metrics_and_quotes=draft.get("metrics_and_quotes", ""),
            anchor_quote=draft["anchor_quote"],
            locator_type=draft["locator_type"],
            locator_value=draft["locator_value"],
            salience=draft.get("salience", 3),
            cfg=cfg,
        )
        tenant_db.set_claim_embedding(tdb, record["id"], vector, cfg=cfg)
        stored.append(record)
        embeddings[record["id"]] = vector

    control_db.finish_step(
        control, job_id, "extract",
        detail=f"{len(stored)} found" if stored else "nothing could be grounded",
    )

    # ---- step 5: drafting skills ------------------------------------------
    # §9.1: "skill rows, `status=proposed`". §10.10 says why that word matters:
    # "Summaries, rules and skills derived from a source are proposals, never
    # auto-executed. This is why skills require approval — it is a security
    # control, not a UX nicety." A drafted skill is unreachable over MCP until
    # a person approves it, which is enforced by its status and not by tone.
    drafted: list[dict[str, Any]] = []
    if not wants["want_skills"]:
        control_db.finish_step(
            control, job_id, "draft_skills", detail="off in your settings", state="skipped"
        )
    else:
        control_db.start_step(control, job_id, "draft_skills")
        drafted = _draft_skills_from_claims(tdb, source, stored, cfg=cfg)
        control_db.finish_step(
            control, job_id, "draft_skills",
            detail=(
                f"{len(drafted)} drafted, waiting for your review"
                if drafted else "no procedure in this source was worth keeping"
            ),
            state="done" if drafted else "skipped",
        )

    # ---- step 6: updating memory ------------------------------------------
    # The embeddings were written alongside each claim above and the FTS index
    # by its triggers; what is left is the cross-source link pass. This step is
    # what makes the library searchable, which is why §9.1 names it "updating
    # memory" rather than "linking".
    control_db.set_job_status(control, job_id, "linking")
    control_db.start_step(control, job_id, "remember")
    link_result = link_claims(
        tdb, stored, embeddings=embeddings, classifier=prov.linker, cfg=cfg
    )
    control_db.finish_step(
        control, job_id, "remember", detail="available to every connection"
    )

    outcome = finish("ready", claims=len(stored))
    outcome["tensions"] = len(link_result.created)
    outcome["rejected"] = extraction.rejected
    outcome["title"] = source["title"]
    return outcome


#: Deciding a skill's fate is a person's act, by §10.10's own argument:
#: approval is "a security control, not a UX nicety". An agent that could
#: approve its own drafts would be a gate that opens itself.
_PERSON_ONLY = ("web_ui", "web_omnibar", "system")


def _person_only(
    ctx: Context,
    action: str,
    because: str = "Only the person can decide a skill's state. Approve it in Brain's web app.",
) -> OpResult | None:
    """Refuse an action that belongs to the person, and say which one.

    The default sentence is about skills because that is where this guard
    started, but it also covers the archive and the two source actions — and
    an agent told its archive read was refused "because only the person can
    decide a skill's state" learns nothing it can act on.
    """
    if ctx.surface in _PERSON_ONLY:
        return None
    ctx.audit(action, "refused_surface", detail={"surface": ctx.surface})
    return _fail(action, "person_only", because)


def op_list_skills(ctx: Context) -> OpResult:
    """§7.9's three sections, plus the number the sidebar's amber pill shows."""
    rows = tenant_db.list_skills(ctx.tenant)
    sections = {"proposed": [], "active": [], "inactive": []}
    for row in rows:
        sections.setdefault(row["status"], []).append(row)

    ctx.audit("list_skills", "ok", detail={"count": len(rows)})
    return OpResult(
        ok=True,
        action="list_skills",
        data={
            "from_your_sources": sections["proposed"],
            "active": sections["active"],
            "inactive": sections["inactive"],
            "needs_review": tenant_db.count_skills_needing_review(ctx.tenant),
        },
    )


def op_review_skill(ctx: Context, skill_id: str, decision: str) -> OpResult:
    """§7.9's review flow: Approve moves it to Active, Dismiss archives it.

    `Dismiss` archives rather than deletes — §6 rule 8: "Nothing is deleted by
    an agent… only the person deletes, and only from Archive." A dismissed
    draft is recoverable from there like everything else.
    """
    refusal = _person_only(ctx, "review_skill")
    if refusal:
        return refusal
    if decision not in ("approve", "dismiss"):
        return _fail("review_skill", "bad_decision", "decision must be approve or dismiss.")

    skill = tenant_db.get_skill(ctx.tenant, skill_id) or tenant_db.get_skill_by_slug(
        ctx.tenant, skill_id
    )
    if skill is None:
        return _fail("review_skill", "not_found", f"No skill has the id `{skill_id}`.")
    if skill["status"] != "proposed":
        return _fail(
            "review_skill",
            "not_in_review",
            f"`{skill['name']}` is not waiting for review — it is {skill['status']}.",
        )

    target = "active" if decision == "approve" else "archived"
    tenant_db.set_skill_status(ctx.tenant, skill["id"], target, actor="you")
    ctx.audit("review_skill", decision, target_id=skill["id"])
    return OpResult(
        ok=True,
        action="review_skill",
        data={
            "skill": tenant_db.get_skill(ctx.tenant, skill["id"]),
            "decision": decision,
            "needs_review": tenant_db.count_skills_needing_review(ctx.tenant),
        },
    )


def op_set_skill_state(ctx: Context, skill_id: str, state: str) -> OpResult:
    """§7.9's overflow menu: Activate or Deactivate.

    Never reaches `proposed`: a skill cannot be pushed back into the review
    queue, because approval already happened and un-approving is not a thing
    the frame offers.
    """
    refusal = _person_only(ctx, "set_skill_state")
    if refusal:
        return refusal
    if state not in ("active", "inactive"):
        return _fail("set_skill_state", "bad_state", "state must be active or inactive.")

    skill = tenant_db.get_skill(ctx.tenant, skill_id) or tenant_db.get_skill_by_slug(
        ctx.tenant, skill_id
    )
    if skill is None:
        return _fail("set_skill_state", "not_found", f"No skill has the id `{skill_id}`.")
    if skill["status"] == "proposed":
        return _fail(
            "set_skill_state",
            "needs_review_first",
            f"`{skill['name']}` is still waiting for your review.",
        )

    tenant_db.set_skill_status(ctx.tenant, skill["id"], state, actor="you")
    ctx.audit("set_skill_state", state, target_id=skill["id"])
    return OpResult(
        ok=True,
        action="set_skill_state",
        data={"skill": tenant_db.get_skill(ctx.tenant, skill["id"])},
    )


def op_list_sources(ctx: Context, *, limit: int = 100) -> OpResult:
    """§7.7's list, type-first, with the yield in green.

    A blocked row carries why. The reason lives on the job in `control.db` and
    the row lives in the tenant database, so no query can join them — this is
    the one place that holds both connections, which is why the enrichment
    happens here and not in `tenant.list_sources`.
    """
    rows = tenant_db.list_sources(ctx.tenant, limit=limit)
    for row in rows:
        if row["status"] != "blocked":
            continue
        job = control_db.latest_job_for_source(ctx.control, ctx.user_id, row["id"])
        row["block_reason"] = (job or {}).get("error_reason", "")
        row["block_message"] = (job or {}).get("error_message", "")
    ctx.audit("list_sources", "ok", detail={"count": len(rows)})
    return OpResult(ok=True, action="list_sources", data={"sources": rows})


def op_get_source(ctx: Context, source_id: str) -> OpResult:
    """§7.3: one source and everything it produced, in one read.

    The frame is a single page — header, instruction, the six steps, then
    "What came out of it" — so it is one request. Fetching the summary, the
    rules and the skills separately would make four round trips to draw one
    screen, and a half-drawn page while they land.
    """
    source = _visible(ctx, tenant_db.get_source(ctx.tenant, (source_id or "").strip()))
    if source is None:
        return _fail("get_source", "not_found", f"No source has the id `{source_id}`.")

    rules = tenant_db.claims_for_source(ctx.tenant, source["id"], limit=50)
    skills = [
        dict(row)
        for row in ctx.tenant.execute(
            "SELECT * FROM skills WHERE source_id = ? AND status IN ('proposed','active') "
            "ORDER BY CASE status WHEN 'proposed' THEN 0 ELSE 1 END, updated_at DESC",
            (source["id"],),
        ).fetchall()
    ]
    summary = tenant_db.get_summary(ctx.tenant, source_id=source["id"])
    progress = op_source_progress(ctx, source["id"])

    ctx.audit("get_source", "ok", target_id=source["id"])
    return OpResult(
        ok=True,
        action="get_source",
        data={
            "source": source,
            "instruction": _instruction_for(ctx, source["id"]),
            "summary": summary,
            "rules": [
                {
                    **rule,
                    "deep_link": deep_link(
                        source.get("canonical_url", ""),
                        rule.get("locator_type", ""),
                        rule.get("locator_value", ""),
                    ),
                }
                for rule in rules
            ],
            "skills": [tenant_db.hydrate_skill(row) for row in skills],
            "has_transcript": tenant_db.get_source_content(ctx.tenant, source["id"]) is not None,
            "progress": progress.data if progress.ok else None,
            # `R-18`: whether the file the person sent is still on disk, so
            # §7.3 can offer to delete it now rather than wait out the 30
            # days. Never the path — that is a staging location inside the
            # server and the filename is the person's own; the page needs to
            # know only that there is something to delete.
            "file": _held_file(ctx, source["id"]),
        },
    )


#: The source types whose `input_target` is a file on disk rather than a URL.
_UPLOADED = ("pdf", "docx", "text", "image")


def _upload_path(ctx: Context, source_id: str) -> Path | None:
    """The received file for a source, or None — containment-checked.

    `ingestion_jobs.input_target` is free text that holds a URL for link
    sources and an arbitrary path in tests, so a path taken from it is only
    ever used after it is shown to live inside this person's own uploads
    folder. That check is what keeps a stale or crafted value from reaching
    a file that is not theirs, or not an upload at all.
    """
    job = control_db.latest_job_for_source(ctx.control, ctx.user_id, source_id)
    if not job or job["source_type"] not in _UPLOADED:
        return None
    try:
        path = Path(str(job["input_target"] or "")).resolve()
        if not path.is_relative_to(ctx.cfg.uploads_dir_for(ctx.user_id).resolve()):
            return None
    except (OSError, ValueError):
        return None
    return path


def _purge_uploads(ctx: Context) -> int:
    """Everything this person sent us, gone now. Returns how many files.

    Used by the two doors in §7.13 that end something — clearing the library
    and closing the account. Neither touched the uploads folder before
    `R-18`, so both left staged files behind for the sweeper to find weeks
    later, which is not what either button says it does.
    """
    folder = ctx.cfg.uploads_dir_for(ctx.user_id)
    if not folder.is_dir():
        return 0
    removed = 0
    for entry in folder.iterdir():
        try:
            if entry.is_file():
                entry.unlink()
                removed += 1
        except OSError:
            continue
    with contextlib.suppress(OSError):
        folder.rmdir()
    return removed


def _held_file(ctx: Context, source_id: str) -> dict[str, Any] | None:
    path = _upload_path(ctx, source_id)
    if path is None or not path.exists():
        return None
    # The staged name carries an `up_…_` prefix the person never chose.
    return {
        "held": True,
        "filename": gatekeeper.unstage(path.name),
        "days": ctx.cfg.upload_retention_days,
    }


@_syncs_tenant
def op_delete_upload(ctx: Context, source_id: str) -> OpResult:
    """§7.3's `Delete the file now` — `R-18`, the founder's third control.

    *"Yes the person should be able to delete an upload sooner than 30 days."*

    What goes is the file Brain was sent, not the source: the claims, the
    summary and the transcript stay, because those are the knowledge and the
    file was only ever the receipt. Nothing is refunded — the credits paid
    for reading the text, not for keeping the bytes — and the dialog says so
    rather than leaving the person to find out.

    No MCP tool, by §2: an agent archives, a person deletes.
    """
    refusal = _person_only(
        ctx,
        "delete_upload",
        "Deleting the file Brain was sent is the person's own call.",
    )
    if refusal:
        return refusal

    source = _visible(ctx, tenant_db.get_source(ctx.tenant, (source_id or "").strip()))
    if source is None:
        return _fail("delete_upload", "not_found", f"No source has the id `{source_id}`.")

    path = _upload_path(ctx, source["id"])
    if path is None:
        return _fail(
            "delete_upload",
            "nothing_held",
            f"`{source['title']}` came from a link, so there is no file to delete.",
        )
    if not path.exists():
        return _fail(
            "delete_upload",
            "nothing_held",
            f"Brain is no longer holding a file for `{source['title']}`.",
        )
    try:
        path.unlink()
    except OSError as exc:
        return _fail("delete_upload", "error", f"Brain could not delete it: {exc}")

    ctx.audit("delete_upload", "ok", target_id=source["id"])
    return OpResult(
        ok=True,
        action="delete_upload",
        data={
            "source_id": source["id"],
            "message": f"Deleted the file Brain was sent for `{source['title']}`. "
            f"Everything it read from it is still here.",
        },
    )


def _provisional_title(target: Any) -> str:
    """What to call a source before anything has read it.

    What the person pasted, minus the scheme. The last path segment was the
    obvious choice and it is wrong for exactly the commonest case: every
    YouTube link would have been titled "watch". The real title replaces this
    the moment step 1 finishes.
    """
    raw = (getattr(target, "canonical_url", "") or getattr(target, "identifier", "") or "").strip()
    if not raw:
        return "New source"
    shown = re.sub(r"^https?://(www\.)?", "", raw).rstrip("/")
    return shown[:120] or "New source"


def _instruction_for(ctx: Context, source_id: str) -> str:
    """What the person asked for when they handed this over (§7.3)."""
    job = control_db.latest_job_for_source(ctx.control, ctx.user_id, source_id)
    return (job or {}).get("user_note", "") or ""


def op_list_transcripts(ctx: Context, *, limit: int = 100) -> OpResult:
    """§7.4. A transcript is a source whose text was captured."""
    rows = tenant_db.list_transcripts(ctx.tenant, limit=limit)
    ctx.audit("list_transcripts", "ok", detail={"count": len(rows)})
    return OpResult(ok=True, action="list_transcripts", data={"transcripts": rows})


def op_get_transcript(ctx: Context, source_id: str) -> OpResult:
    """§7.5's reading surface.

    The cues come back as the body, grouped into turns, because the frame is a
    two-column grid of timestamp and text. `R-1` struck speakers, so a turn
    carries its moment and its words and nothing else.
    """
    record = tenant_db.get_transcript(ctx.tenant, (source_id or "").strip())
    if record is None or _visible(ctx, record["source"]) is None:
        return _fail("get_transcript", "not_found", "No transcript is on record for that id.")

    source = record["source"]
    turns = []
    for cue in record["cues"]:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        start = cue.get("start")
        turns.append(
            {
                "text": text,
                "t_ms": int(float(start) * 1000) if start is not None else None,
                "label": cue.get("section") or "",
                "deep_link": (
                    deep_link(source.get("canonical_url", ""), "timestamp", str(int(float(start))))
                    if start is not None else ""
                ),
            }
        )
    if not turns:
        # A document has no cues. The body is still readable, as one turn.
        turns = [{"text": record["full_text"], "t_ms": None, "label": "", "deep_link": ""}]

    # §7.5's derived strip names what this transcript produced, so it needs
    # the summary's id and the two counts rather than a boolean.
    summary = tenant_db.get_summary(ctx.tenant, source_id=source["id"])
    skill_count = ctx.tenant.execute(
        "SELECT COUNT(*) AS n FROM skills WHERE source_id = ? "
        "AND status IN ('active','proposed')",
        (source["id"],),
    ).fetchone()["n"]

    ctx.audit("get_transcript", "ok", target_id=source["id"])
    return OpResult(
        ok=True,
        action="get_transcript",
        data={
            "source": source,
            "word_count": record["word_count"],
            "turns": turns,
            "full_text": record["full_text"],
            "summary_id": summary["id"] if summary else "",
            "rule_count": len(tenant_db.claims_for_source(ctx.tenant, source["id"], limit=200)),
            "skill_count": skill_count,
        },
    )


def op_list_archive(ctx: Context, *, limit: int = 200) -> OpResult:
    """§7.12's one heterogeneous list, with who set each thing aside.

    Web only. §7.12's promise to the person is that nothing here is reachable
    by their AI, so the engine does not serve this list to an agent at all —
    not even filtered.
    """
    refusal = _person_only(
        ctx,
        "list_archive",
        "The archive is the person's alone. What they set aside is not reachable "
        "from here — treat it as though it does not exist.",
    )
    if refusal:
        return refusal

    rows = tenant_db.list_archive(ctx.tenant, limit=limit)
    ctx.audit("list_archive", "ok", detail={"count": len(rows)})
    return OpResult(
        ok=True,
        action="list_archive",
        data={
            "items": rows,
            # §7.12's filter pills, and the only place a type filter exists.
            "types": ["source", "transcript", "summary", "rule", "skill"],
        },
    )


def op_list_rules(ctx: Context, *, limit: int = 200) -> OpResult:
    """§7.8's two groups.

    `My rules` are the ones written here or carried from the person's own AI
    chats; `From your sources` are the ones Brain extracted. Provenance is the
    point of the page, so every row carries where it came from.
    """
    rows = tenant_db.list_rules(ctx.tenant, limit=limit)
    mine, extracted = [], []
    for row in rows:
        source = tenant_db.get_source(ctx.tenant, row["source_id"]) or {}
        row["provenance"] = {
            "title": source.get("title", ""),
            "locator": row.get("locator_value", ""),
            "deep_link": deep_link(
                source.get("canonical_url", ""), row.get("locator_type", ""), row.get("locator_value", "")
            ),
        }
        (mine if row.pop("written_by_hand", False) else extracted).append(row)

    ctx.audit("list_rules", "ok", detail={"count": len(rows)})
    return OpResult(
        ok=True,
        action="list_rules",
        data={"mine": mine, "from_your_sources": extracted},
    )


def op_list_summaries(ctx: Context, *, limit: int = 50) -> OpResult:
    """§7.6's list. Archived summaries never appear, on any surface: the
    Archive has its own view of them."""
    rows = tenant_db.list_summaries(ctx.tenant, limit=max(1, min(limit, 200)))
    ctx.audit("list_summaries", "ok", detail={"count": len(rows)})
    return OpResult(ok=True, action="list_summaries", data={"summaries": rows})


def op_get_summary(
    ctx: Context, *, summary_id: str | None = None, source_id: str | None = None
) -> OpResult:
    """One summary, by its own id or by the source it came from.

    Both doors exist because §7.6 is reached two ways: from the Summaries list
    by summary id, and from §7.3's source detail by source id.
    """
    if not summary_id and not source_id:
        return _fail("get_summary", "empty_id", "A summary id or a source id is required.")

    record = tenant_db.get_summary(ctx.tenant, summary_id=summary_id, source_id=source_id)
    record = _visible(ctx, record)
    if record is None:
        ctx.audit("get_summary", "not_found", target_id=summary_id or source_id or "")
        return _fail("get_summary", "not_found", "No summary is on record for that id.")

    source = _visible(ctx, tenant_db.get_source(ctx.tenant, record["source_id"])) or {}
    ctx.audit("get_summary", "ok", target_id=record["id"])
    return OpResult(
        ok=True,
        action="get_summary",
        data={
            "summary": record,
            "source": source,
            # §7.6: every point clicks through to its moment. The deep link is
            # built here, from the source's own URL, so no caller has to know
            # how a YouTube timestamp differs from a PDF page.
            "points": [
                {
                    **point,
                    "deep_link": (
                        deep_link(source.get("canonical_url", ""), "timestamp",
                                  str(int(point["t_ms"] / 1000)))
                        if point.get("t_ms") is not None else ""
                    ),
                }
                for point in record["points"]
            ],
        },
    )


def op_source_progress(ctx: Context, source_id: str) -> OpResult:
    """The six steps behind one source, for the work block of §4.4.

    §9.1: "The UI polls or streams from those rows — the work block is a view
    of real state, never a fake animation." So this returns exactly what the
    ledger holds, including a step that was skipped and why.
    """
    source_id = (source_id or "").strip()
    if not source_id:
        return _fail("source_progress", "empty_id", "A source id is required.")

    job = control_db.latest_job_for_source(ctx.control, ctx.user_id, source_id)
    if job is None:
        return _fail(
            "source_progress", "not_found", f"No ingestion is on record for `{source_id}`."
        )

    steps = control_db.job_steps(ctx.control, job["job_id"])
    done = [s for s in steps if s["finished_at"]]

    # §7.3's header reads "Done in 2m 14s". Measured from the first step that
    # started to the last that finished, which is the work the person waited
    # for — not the job row's age, which includes time spent queued.
    elapsed = None
    started = [s["started_at"] for s in steps if s["started_at"]]
    finished = [s["finished_at"] for s in steps if s["finished_at"]]
    if started and finished and len(done) == len(steps):
        from datetime import datetime

        def parse(value: str) -> datetime:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

        try:
            elapsed = max(0, int((parse(max(finished)) - parse(min(started))).total_seconds()))
        except (ValueError, TypeError):
            elapsed = None
    return OpResult(
        ok=True,
        action="source_progress",
        data={
            "source_id": source_id,
            "job_id": job["job_id"],
            "status": job["status"],
            "error_message": job["error_message"],
            "steps": steps,
            "finished": len(done) == len(steps) and len(steps) > 0,
            "elapsed_sec": elapsed,
            "started_at": job["created_at"],
        },
    )


# ---------------------------------------------------------------------------
# §7.3 and §7.4's overflow menus
# ---------------------------------------------------------------------------
# Three actions the card menus offer. All three are the person's: renaming
# their own library, spending quota on a re-read, and asking for a summary
# they turned off are decisions an agent does not get to make on their
# behalf — and §2's "zero destructive LLM tools" is the same argument.


@_syncs_tenant
def op_rename_source(ctx: Context, source_id: str, title: str) -> OpResult:
    """§7.4's overflow `Rename`, which §7.3 and §7.12 reach too.

    The title is what every list, card and reader shows, so it is the one
    field of a source the person can correct — an auto-extracted title is
    often a channel's boilerplate.
    """
    refusal = _person_only(
        ctx,
        "rename_source",
        "Renaming is the person's — their library's titles are not yours to change.",
    )
    if refusal:
        return refusal

    title = " ".join((title or "").split())[:500]
    if not title:
        return _fail("rename_source", "empty_title", "A source needs a title.")

    source = _visible(ctx, tenant_db.get_source(ctx.tenant, source_id))
    if source is None:
        return _fail("rename_source", "not_found", f"No source has the id `{source_id}`.")

    tenant_db.rename_source(ctx.tenant, source["id"], title)
    ctx.audit("rename_source", "ok", target_id=source["id"])
    return OpResult(
        ok=True,
        action="rename_source",
        data={"source": tenant_db.get_source(ctx.tenant, source["id"])},
    )


def op_reprocess_source(ctx: Context, source_id: str) -> OpResult:
    """§7.3's overflow `Re-process`: read the source again from its URL.

    A new job against the same source row, so the six steps of §4.4 are
    rewritten in place rather than accumulating — the person asked for this
    source to be redone, not for a second copy of it.

    Only a source that has a URL can be re-read. A pasted rescue or an
    uploaded file has nothing to go back to, and says so instead of queueing
    a job that would fail at step 1.
    """
    refusal = _person_only(
        ctx,
        "reprocess_source",
        "Re-reading a source spends the person's credits, so they ask for it themselves.",
    )
    if refusal:
        return refusal

    source = _visible(ctx, tenant_db.get_source(ctx.tenant, source_id))
    if source is None:
        return _fail("reprocess_source", "not_found", f"No source has the id `{source_id}`.")
    if not source["canonical_url"]:
        return _fail(
            "reprocess_source",
            "nothing_to_reread",
            f"`{source['title']}` came from a file or pasted text, so there is no "
            "address to read again. Add it again to replace it.",
        )

    running = control_db.latest_job_for_source(ctx.control, ctx.user_id, source["id"])
    if running and running["status"] in ("queued", "fetching", "extracting", "linking"):
        return _fail(
            "reprocess_source",
            "already_running",
            f"Brain is still working on `{source['title']}`.",
        )

    job = control_db.create_job(
        ctx.control,
        ctx.user_id,
        source_id=source["id"],
        input_target=source["canonical_url"],
        source_type=source["source_type"],
        user_note=running["user_note"] if running else "",
    )
    tenant_db.set_source_status(ctx.tenant, source["id"], "processing")
    ctx.audit("reprocess_source", "queued", target_id=source["id"])
    return OpResult(
        ok=True,
        action="reprocess_source",
        data={
            "job_id": job["job_id"],
            "source_id": source["id"],
            "message": f"Re-reading `{source['title']}`.",
        },
    )


@_syncs_tenant
def op_generate_summary(ctx: Context, source_id: str) -> OpResult:
    """§7.4's overflow `Generate a summary`, for a transcript that has none.

    The text is already stored, so this runs the summariser over it rather
    than re-reading the source — which is the whole reason the action exists
    on a transcript card: a person who had summaries switched off can still
    have one for this source without spending the quota again.
    """
    refusal = _person_only(
        ctx,
        "generate_summary",
        "A summary is written when the person asks for it.",
    )
    if refusal:
        return refusal

    source = _visible(ctx, tenant_db.get_source(ctx.tenant, source_id))
    if source is None:
        return _fail("generate_summary", "not_found", f"No source has the id `{source_id}`.")

    existing = tenant_db.get_summary(ctx.tenant, source_id=source["id"])
    if existing:
        return OpResult(
            ok=True,
            action="generate_summary",
            data={"summary": existing, "created": False},
        )

    content = tenant_db.get_source_content(ctx.tenant, source["id"])
    if not content or not (content["full_text"] or "").strip():
        return _fail(
            "generate_summary",
            "no_text",
            f"`{source['title']}` has no stored text to summarise.",
        )

    if ctx.prov.summariser is None:  # pragma: no cover - the bundle always has one
        return _fail("generate_summary", "unavailable", "No summariser is configured.")

    wants = control_db.ingestion_defaults(ctx.control, ctx.user_id)
    try:
        written = ctx.prov.summariser.summarise(
            content["full_text"],
            content["cues"],
            {
                "title": source["title"],
                "source_type": source["source_type"],
                "author": source["author"],
                "user_note": "",
                "write_in": wants["write_in"],
            },
        )
    except Exception as exc:
        LOGGER.warning("on-demand summary failed for %s: %s", source["id"], exc)
        return _fail(
            "generate_summary", "summariser_failed", "The summary could not be written."
        )

    if not written.get("gist"):
        return _fail(
            "generate_summary",
            "too_short",
            f"There is not enough in `{source['title']}` to summarise.",
        )

    summary = tenant_db.upsert_summary(
        ctx.tenant,
        source_id=source["id"],
        gist=written["gist"],
        points=written.get("points") or [],
        chapters=written.get("chapters") or [],
        instruction="",
    )
    ctx.audit("generate_summary", "ok", target_id=summary["id"])
    return OpResult(
        ok=True,
        action="generate_summary",
        data={"summary": summary, "created": True},
    )


def run_pending_jobs_sync(
    user_id: str | None = None, *, cfg: Settings | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Drain the queue in-process.

    Cloud Session VMs do not keep background daemons alive between prompts, so
    every surface (CLI, tests, selftest) can push the queue forward itself.
    """
    cfg = cfg or settings()
    control = control_db.control()
    control_db.reap_exhausted_jobs(control, user_id=user_id)
    processed: list[dict[str, Any]] = []
    for _ in range(max(1, limit)):
        job = control_db.claim_next_job(control, user_id=user_id)
        if job is None:
            break
        try:
            processed.append(process_job(job, cfg=cfg))
        except Exception as exc:
            # `R-18`: the same guard `Worker.drain_once` has had all along.
            # Without it a crash here leaves the job non-terminal with its
            # charge booked, and the third crash makes that permanent.
            LOGGER.exception("job %s crashed", job["job_id"])
            control_db.set_job_status(
                control, job["job_id"], "failed", error_message=f"Worker error: {exc}"
            )
            control_db.refund_credits(
                control, job["user_id"], credits=control_db.job_charge(control, job["job_id"])
            )
            processed.append(
                {"job_id": job["job_id"], "status": "failed", "claims": 0, "message": str(exc)}
            )
    return processed


# ===========================================================================
# web-surface operations (never exposed over MCP)
# ===========================================================================
# These back the Web Control Plane only. Hard deletion, trash recovery and
# export deliberately have no MCP tool: an agent can archive, a human deletes.
# ---------------------------------------------------------------------------
@_syncs_tenant
def op_rescue_job(ctx: Context, job_id: str, pasted_text: str) -> OpResult:
    """Rescue a blocked or failed job with text the user pastes in themselves.

    Checkpoint B, Rule 4a: a platform block must not dead-end. The pasted text
    enters the *existing* job's extraction and grounding pipeline, so the claims
    still land against the original source record and its canonical URL.
    """
    job = control_db.get_job(ctx.control, job_id)
    if job is None or job["user_id"] != ctx.user_id:
        return _fail("rescue_job", "not_found", f"No job with id `{job_id}`.")
    if job["status"] not in ("blocked_by_platform", "failed"):
        return _fail(
            "rescue_job",
            "not_rescuable",
            f"That job is `{job['status']}`, so there is nothing to rescue.",
        )

    text = clean_text(pasted_text)
    if len(text.split()) < 30:
        return _fail(
            "rescue_job",
            "text_too_short",
            "Paste a bit more — at least ~30 words, or no claim can be grounded in it.",
        )

    cues = [
        {"section": f"Pasted part {index + 1}", "text": clean_text(block)}
        for index, block in enumerate(text.split("\n\n"))
        if clean_text(block)
    ] or [{"section": "Pasted notes", "text": text}]

    content = FetchedContent(
        title=job["input_target"].rsplit("/", 1)[-1] or "Rescued source",
        full_text=text,
        cues=cues,
        author="",
        source_type=job["source_type"],
        locator_type="section",
    ).finalise()

    def finish(status: str, *, message: str = "", reason: str = "", claims: int = 0) -> dict[str, Any]:
        control_db.set_job_status(
            ctx.control,
            job_id,
            status,
            error_message=message,
            error_reason=reason,
            claims_extracted=claims,
        )
        if status in ("failed", "blocked_by_platform"):
            # `R-18`, same rule on this path: a rescue that fails costs
            # nothing. The gate it goes through is the same one.
            control_db.refund_credits(
                ctx.control, ctx.user_id, credits=control_db.job_charge(ctx.control, job_id)
            )
        _release_upload(ctx.control, ctx.user_id, dict(job), cfg=ctx.cfg)
        control_db.audit(
            ctx.control,
            user_id=ctx.user_id,
            action="rescue_job",
            status=status,
            surface=ctx.surface,
            target_id=job_id,
            detail={"claims": claims, "message": message},
        )
        return {"job_id": job_id, "status": status, "claims": claims, "message": message}

    outcome = _ingest_content(dict(job), content, cfg=ctx.cfg, finish=finish)
    if outcome["status"] != "ready":
        return _fail("rescue_job", "rescue_failed", outcome["message"])
    return OpResult(
        ok=True,
        action="rescue_job",
        data={**outcome, "rescued": True, "library_counts": tenant_db.counts(ctx.tenant)},
    )


@_syncs_tenant
def op_trash_item(ctx: Context, item_type: ItemType, item_id: str) -> OpResult:
    """Move a source to Trash — recoverable for 30 days, still not deleted."""
    if item_type != "source":
        # Claims and skills ride their source; Trash is a source-level concept.
        return op_archive_item(ctx, item_type, item_id)
    source = tenant_db.get_source(ctx.tenant, item_id)
    if source is None:
        return _fail("trash_item", "not_found", f"No source has the id `{item_id}`.")
    tenant_db.set_source_status(ctx.tenant, item_id, "trash")
    ctx.audit("trash_item", "ok", target_id=item_id)
    return OpResult(
        ok=True,
        action="trash_item",
        data={
            "id": item_id,
            "label": source["title"],
            "status": "trash",
            "recoverable_days": 30,
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


def op_list_trash(ctx: Context) -> OpResult:
    rows = ctx.tenant.execute(
        "SELECT id, title, source_type, created_at FROM sources WHERE status = 'trash' "
        "ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    return OpResult(
        ok=True,
        action="list_trash",
        data={"items": [dict(row) for row in rows], "recoverable_days": 30},
    )


#: Which table an id belongs to. The ids are prefixed at creation, so §7.12's
#: one heterogeneous list does not have to carry a type through the frame for
#: `Delete permanently` to know what it is deleting.
_ID_PREFIX = {"src": "source", "clm": "claim", "skl": "skill", "sum": "summary"}


@_syncs_tenant
def op_permanent_delete(
    ctx: Context, item_id: str, item_type: str | None = None
) -> OpResult:
    """Hard delete, of anything the person has already set aside.

    This is the single destructive path in the product, and an agent cannot
    reach it: there is no MCP tool that calls this function, and the surface
    guard below refuses one that tries.

    **Already set aside** is the precondition, not "in Trash". §9.4 puts
    `Delete permanently` on an archived card, so demanding Trash first — a
    source-only concept — meant the button on §7.12's cards could only ever
    answer `not_in_trash`, and for a rule, skill or summary `not_found`,
    because the id was resolved against `sources` alone.

    Archiving stays the first gate and §9.4's two-step dialog the second: the
    person sets a thing aside, finds it on the one page that lists what they
    set aside, and types the word.
    """
    refusal = _person_only(
        ctx,
        "permanent_delete",
        "Nothing is deleted by an agent. Archive it instead — the person deletes, "
        "from Archive, and confirms.",
    )
    if refusal:
        return refusal

    # The same map `op_archive_item` uses, so `rule` means here exactly what
    # it means there — the whole point of the defect this fixed.
    kind = _ARCHIVABLE.get(item_type, "") if item_type else _ID_PREFIX.get(item_id[:3], "")
    if kind not in ("source", "claim", "skill", "summary"):
        return _fail(
            "permanent_delete", "bad_item_type", f"`{item_id}` is not an id this can delete."
        )

    readers = {
        "source": lambda: tenant_db.get_source(ctx.tenant, item_id),
        "claim": lambda: tenant_db.get_claim(ctx.tenant, item_id),
        "skill": lambda: tenant_db.get_skill(ctx.tenant, item_id),
        "summary": lambda: tenant_db.get_summary(ctx.tenant, summary_id=item_id),
    }
    record = readers[kind]()
    if record is None:
        return _fail("permanent_delete", "not_found", f"No {kind} has the id `{item_id}`.")
    if record["status"] not in ("archived", "trash"):
        return _fail(
            "permanent_delete",
            "not_set_aside",
            "Only something you have already archived can be deleted for good. "
            "Archive it first.",
        )

    # A source takes its content, rules, skills and summary with it — that is
    # what `ON DELETE CASCADE` is for, and it is why the count is read first.
    cascaded = (
        len(tenant_db.claims_for_source(ctx.tenant, item_id, active_only=False))
        if kind == "source"
        else 0
    )
    table = {
        "source": "sources",
        "claim": "epistemic_claims",
        "skill": "skills",
        "summary": "summaries",
    }[kind]
    ctx.tenant.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))

    label = {
        "source": lambda: record["title"],
        "claim": lambda: record["claim"][:120],
        "skill": lambda: record["name"],
        "summary": lambda: record["gist"][:120],
    }[kind]()
    ctx.audit(
        "permanent_delete", "ok", target_id=item_id, detail={"kind": kind, "claims": cascaded}
    )
    return OpResult(
        ok=True,
        action="permanent_delete",
        data={
            "id": item_id,
            "item_type": kind,
            "label": label,
            "deleted_claims": cascaded,
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


@_syncs_tenant
def op_revert_skill(ctx: Context, skill_id: str, version: int) -> OpResult:
    """Restore a snapshotted skill version — the Omni-Bar's `Revert to vN`."""
    skill = tenant_db.get_skill(ctx.tenant, skill_id) or tenant_db.get_skill_by_slug(
        ctx.tenant, skill_id
    )
    if skill is None:
        return _fail("revert_skill", "not_found", f"No skill has the id `{skill_id}`.")
    row = ctx.tenant.execute(
        "SELECT snapshot_json FROM skill_versions WHERE skill_id = ? AND version = ?",
        (skill["id"], int(version)),
    ).fetchone()
    if row is None:
        return _fail(
            "revert_skill", "no_such_version", f"`{skill['slug']}` has no version {version}."
        )

    snapshot = json.loads(row["snapshot_json"])
    restored = tenant_db.upsert_skill(
        ctx.tenant,
        slug=snapshot["slug"],
        name=snapshot["name"],
        description=snapshot["description"],
        trigger_when=snapshot["trigger_when"],
        execution_steps=json.loads(snapshot.get("execution_steps_json") or "[]"),
        guardrails=snapshot.get("guardrails", ""),
        output_template=snapshot.get("output_template", ""),
        linked_claim_ids=json.loads(snapshot.get("linked_claim_ids") or "[]"),
        origin_type=snapshot.get("origin_type", "distilled"),
    )
    ctx.audit("revert_skill", "ok", target_id=restored["id"], detail={"to_version": version})
    return OpResult(
        ok=True,
        action="revert_skill",
        data={"skill": restored, "reverted_to": int(version)},
    )


@_syncs_tenant
def op_update_claim(
    ctx: Context,
    claim_id: str,
    *,
    claim: str | None = None,
    when_it_applies: str | None = None,
    when_it_fails: str | None = None,
    metrics_and_quotes: str | None = None,
    domain_tags: Sequence[str] | None = None,
) -> OpResult:
    """Inline click-to-edit from the Inner Doll.

    The `anchor_quote` and its locator are deliberately **not** editable: they
    are the grounded evidence, and letting the UI rewrite them would break the
    one guarantee the library makes.
    """
    record = tenant_db.get_claim(ctx.tenant, claim_id)
    if record is None:
        return _fail("update_claim", "not_found", f"No claim has the id `{claim_id}`.")

    fields: dict[str, Any] = {}
    if claim is not None and clean_text(claim):
        fields["claim"] = clean_text(claim)[:600]
    if when_it_applies is not None:
        fields["when_it_applies"] = clean_text(when_it_applies)[:300]
    if when_it_fails is not None:
        fields["when_it_fails"] = clean_text(when_it_fails)[:300]
    if metrics_and_quotes is not None:
        fields["metrics_and_quotes"] = clean_text(metrics_and_quotes)[:400]
    if domain_tags is not None:
        fields["domain_tags"] = json.dumps(
            sorted({clean_text(t).lower() for t in domain_tags if clean_text(t)}),
            ensure_ascii=False,
        )
    if not fields:
        return _fail("update_claim", "nothing_to_update", "No editable field was provided.")

    assignments = ", ".join(f"{key} = ?" for key in fields)
    ctx.tenant.execute(
        f"UPDATE epistemic_claims SET {assignments} WHERE id = ?",
        (*fields.values(), claim_id),
    )
    updated = tenant_db.get_claim(ctx.tenant, claim_id)
    # The claim text feeds the vector lane, so the embedding has to follow it.
    tenant_db.set_claim_embedding(
        ctx.tenant,
        claim_id,
        ctx.prov.embedder.embed([_claim_embedding_text(updated)])[0],
        cfg=ctx.cfg,
    )
    ctx.audit("update_claim", "ok", target_id=claim_id, detail={"fields": sorted(fields)})
    return OpResult(ok=True, action="update_claim", data={"claim": updated})


def seed_first_run_library(user_id: str, *, cfg: Settings | None = None) -> bool:
    """Put the three demo items in a brand-new library.

    Lives here rather than in `webapi` for one reason: the control plane must
    contain exactly one `build_context` call, which is the structural proof
    that no endpoint can resolve a tenant any other way. Tenant resolution
    belongs to the engine.

    Guarded on the counts rather than on a flag, so a user who cleared the demo
    on purpose never has it handed back, and a second sign-in never duplicates
    it.
    """
    cfg = cfg or settings()
    try:
        ctx = build_context(user_id, client_name="web", surface="web_ui", cfg=cfg)
    except ValueError:
        return False
    # Two conditions, because `counts` only sees *active* rows: clearing the
    # demo archives it, so counts alone would hand it straight back on the next
    # sign-in. The slug check is the same one `op_seed_demo` uses for
    # idempotence, and it sees archived rows too.
    if tenant_db.get_skill_by_slug(ctx.tenant, DEMO_SKILL_SLUG):
        return False
    counts = tenant_db.counts(ctx.tenant)
    if counts.get("claims") or counts.get("skills") or counts.get("sources"):
        return False
    return bool(op_seed_demo(ctx).ok)


#: The actions that change what a library *shows*. A read is audited too, and
#: including those would move the watermark on every poll — so a tab would
#: refresh itself forever. `export` and `profile` are reads of a different
#: shape; `update_profile` changes settings, not cards.
MUTATING_ACTIONS = frozenset(
    {
        "remember_this",
        "save_skill",
        "revert_skill",
        "update_claim",
        "archive_item",
        "restore_item",
        "trash_item",
        "permanent_delete",
        "rescue_job",
        "process_job",
        "seed_demo",
        "clear_demo",
    }
)


def tenant_watermark(ctx: Context) -> dict[str, Any]:
    """A change stamp for the tenant: `{counter, updated_at}`.

    Taken from `audit_logs`, because that table already *is* the mutation
    record: it is append-only, it has an autoincrementing id, and every write
    operation writes to it. The obvious alternative — counting tenant rows and
    taking the newest timestamp — was what this did first, and it silently
    missed an *edit*: editing a claim changes neither the row count nor any
    timestamp, because `epistemic_claims` has no `updated_at` column. A tab
    would have shown stale text indefinitely.

    `max(id)` alone would be enough for the counter, but the count is kept as
    well so that the stamp is still meaningful if the log is ever pruned.
    """
    row = ctx.control.execute(
        f"""
        SELECT count(*) AS writes, coalesce(max(id), 0) AS latest,
               coalesce(max(created_at), '') AS updated_at
          FROM audit_logs
         WHERE user_id = ?
           AND status = 'ok'
           AND action IN ({",".join("?" * len(MUTATING_ACTIONS))})
        """,
        (ctx.user_id, *sorted(MUTATING_ACTIONS)),
    ).fetchone()
    return {
        "counter": int(row["latest"] or 0),
        "updated_at": row["updated_at"] or "",
        "writes": int(row["writes"] or 0),
    }


def op_profile(ctx: Context) -> OpResult:
    """Everything the Profile modal's five blocks need, in one call."""
    user = control_db.get_user(ctx.control, ctx.user_id) or {}
    credits = control_db.credits_state(ctx.control, ctx.user_id, cfg=ctx.cfg)
    sessions = []
    for record in control_db.list_sessions(ctx.control, ctx.user_id):
        if record["revoked"]:
            continue
        expires = control_db.parse_iso(record["refresh_expires_at"])
        # Ceiling, not `.days`: a token minted a moment ago has 29 days and
        # some hours left, and `.days` truncates that to 29 — so a connection
        # the user just made would read "Expires in 29 days" on the screen
        # where they made it.
        seconds = (expires - control_db.now_utc()).total_seconds() if expires else 0
        remaining = math.ceil(seconds / 86400) if seconds > 0 else 0
        sessions.append(
            {
                "session_id": record["session_id"],
                "client_name": record["client_name"],
                "created_at": record["created_at"],
                "expires_in_days": max(0, remaining),
            }
        )
    return OpResult(
        ok=True,
        action="profile",
        data={
            "email": user.get("email", ""),
            "about_you_context": user.get("about_you_context", ""),
            "theme_mode": user.get("theme_mode", "warm_sunset"),
            "quotas": {
                "credits_used": credits["used"],
                "credits_cap": credits["cap"],
                "credits_left": credits["left"],
                "credits_period": credits["period"],
            },
            # `R-18`: what Brain does with the file it was sent, and the
            # person's say in it. The day count comes from the setting the
            # sweeper actually reads, so the sentence cannot drift from the
            # behaviour it describes.
            "retention": {
                "upload_days": ctx.cfg.upload_retention_days,
                "keep_uploads": bool(
                    control_db.ingestion_defaults(ctx.control, ctx.user_id)["keep_uploads"]
                ),
            },
            "sessions": sessions,
            "mcp_url": ctx.cfg.audience,
            "library_counts": tenant_db.counts(ctx.tenant),
            # §7.13's Account and Appearance blocks.
            "display_name": user.get("display_name", ""),
            "locale": user.get("locale", ""),
            # §7.13's "What Brain does with new sources". Returned as what the
            # pipeline actually reads, so the switches and the behaviour
            # cannot drift apart.
            "ingestion": control_db.ingestion_defaults(ctx.control, ctx.user_id),
        },
    )


#: Actions whose audit rows are noise in a human-readable activity list.
_AUDIT_NOISE = frozenset({"search", "fetch", "get_ingest_status", "list_cards", "web_login"})

#: A fallback trace for rows written before `reasoning_trace` existed, or by a
#: call site that had nothing useful to add.
_AUDIT_FALLBACK = {
    "ingest_source": "Queued a source for the library.",
    "ingest_file": "Queued an uploaded file.",
    "process_job": "Processed a queued source.",
    "rescue_job": "Rescued a blocked source from pasted text.",
    "remember_this": "Recorded one of your own rules.",
    "save_skill": "Saved or versioned a skill.",
    "archive_item": "Archived an item — reversible.",
    "restore_item": "Restored an archived item.",
    "trash_item": "Moved a source to Trash.",
    "permanent_delete": "Permanently deleted a source from Trash.",
    "revert_skill": "Reverted a skill to an earlier version.",
    "update_claim": "Edited a claim's conditions.",
    "update_profile": "Changed a profile setting.",
    "export": "Exported the library.",
    "revoke_session": "Revoked a connected client.",
    "token_issue": "Issued an access token to a client.",
    "token_refresh": "Rotated a client's refresh token.",
    "revoke": "Revoked a token.",
    "authorize": "Authorised a client to connect.",
    "register_client": "A new client registered itself.",
    "seed_demo": "Loaded the demo library.",
    "clear_demo": "Cleared the demo library.",
    "omnibar_edit": "Edited a skill from the Omni-Bar.",
}


# ---------------------------------------------------------------------------
# §7.2 HOME, the thread state
# ---------------------------------------------------------------------------


def op_add_instruction(ctx: Context, source_id: str, text: str) -> OpResult:
    """§7.2's mid-flight interjection: "Actually keep the hiring bits too."

    §7.2's hard requirements are the whole specification here: "The composer
    is never disabled while Brain works", "an interjection is acknowledged
    without restarting or stopping the pipeline", and "leaving the page does
    not cancel anything". So this appends to the instruction and cancels
    nothing.

    What it returns is what makes the acknowledgement honest. §7.2's example
    reply is *"Noted — I'll leave them in. Still on the summary, so nothing is
    lost."* — a sentence that is only true if the summary has not been written
    yet. So the steps that have not yet run come back, and the frame says what
    the note will actually reach rather than reassuring the person either way.
    """
    text = clean_text(text)[:600]
    if not text:
        return _fail("add_instruction", "empty_text", "Say what you want changed.")

    job = control_db.latest_job_for_source(ctx.control, ctx.user_id, source_id)
    if job is None:
        return _fail("add_instruction", "not_found", "Brain is not working on that one.")

    steps = control_db.job_steps(ctx.control, job["job_id"])
    pending = [step["step"] for step in steps if step["state"] == "pending"]
    done = [step["step"] for step in steps if step["state"] in ("done", "skipped", "failed")]

    if not pending:
        # Nothing is left to read it. Saying "noted" here would be a lie the
        # person finds out about when the result does not change.
        ctx.audit("add_instruction", "too_late", target_id=source_id)
        return _fail(
            "add_instruction",
            "too_late",
            "That one is finished, so this cannot change it. Add the source again "
            "with the new instruction, or edit what came out of it.",
        )

    instruction = control_db.append_user_note(ctx.control, job["job_id"], text)
    ctx.audit("add_instruction", "ok", target_id=source_id, detail={"pending": pending})
    return OpResult(
        ok=True,
        action="add_instruction",
        data={
            "source_id": source_id,
            "job_id": job["job_id"],
            "instruction": instruction,
            # What the note will reach, and what it is too late for.
            "will_affect": [
                control_db.step_label(step, job["source_type"]) for step in pending
            ],
            "already_done": [
                control_db.step_label(step, job["source_type"]) for step in done
            ],
        },
    )


# ---------------------------------------------------------------------------
# §7.10 ACTIVITY and §7.11 CONNECTIONS
# ---------------------------------------------------------------------------


def op_activity(ctx: Context, *, limit: int = 60) -> OpResult:
    """§7.10's two sections, because they answer two questions.

    **Usage** — meters read from the numbers this product actually enforces,
    not from the capture's illustrative ones. §7.10 shows `Calls 1,284 of
    5,000` and `Storage 2.1 GB of 10 GB`; neither cap exists here. What is
    enforced is `quota_files`, `quota_media_minutes`, and a per-minute call
    ceiling — so those three carry a meter. A meter against a number nothing
    checks is decoration, and §6 rule 11's whole argument is that a control
    which cannot keep its promise should say so instead.

    **`R-17`**: storage is not reported at all, not even as a capless figure.
    The founder ruled it out as a lever — "we shouldn't show storage" — and
    the reason holds on inspection: what the person sends us is a link, an
    image or a document, and once it is read there is nothing left worth
    charging for. The received file is swept after
    `cfg.upload_retention_days` (`app/storage/retention.py`), so stored bytes
    are bounded by that window rather than by the person's behaviour. What
    counts is what was *processed*, and that is the three meters above.

    **Calls** — the MCP log. A row is a call a *connection* made, which is
    what `connection_id` marks: the operations also write their own audit
    rows (§2's internal trail) and those are not calls. §7.10 calls the
    connection column "the link to §7.11", because "revoking a key is only
    meaningful once you can see what it did".
    """
    refusal = _person_only(
        ctx,
        "activity",
        "The activity log is the person's own view of what their agents did.",
    )
    if refusal:
        return refusal

    user = control_db.get_user(ctx.control, ctx.user_id) or {}
    credits = control_db.credits_state(ctx.control, ctx.user_id, cfg=ctx.cfg)
    counts = tenant_db.counts(ctx.tenant)

    # `R-18`: the spend list. Titles come from the tenant database, because
    # `ingestion_jobs.input_target` holds a staging path for an upload and
    # that path must not reach a page — §2's isolation rule, and the
    # filename would be the person's own.
    spend = []
    for row in control_db.charged_jobs(ctx.control, ctx.user_id, limit=10):
        source = tenant_db.get_source(ctx.tenant, row["source_id"])
        spend.append(
            {
                "source_id": row["source_id"],
                "title": (source or {}).get("title") or "A source that is gone",
                "source_type": row["source_type"],
                "credits": row["charged_credits"],
                "at": row["created_at"],
                "still_here": source is not None,
            }
        )

    calls = [
        {
            "id": row["id"],
            "at": row["created_at"],
            "connection": row["client_name"] or "Unknown",
            "connection_id": row["connection_id"],
            "tool": row["action"],
            "target": row["target_id"] or "",
            "latency_ms": row["latency_ms"],
            "payload_bytes": row["payload_bytes"],
            "status": row["status"],
            "ok": row["status"] == "ok",
            "asked_for": row["detail"],
            "returned": row["result_summary"],
            # §7.10: "A failure says what to do."
            "remedy": _REMEDY_FOR.get(row["status"], ""),
        }
        for row in control_db.recent_calls(ctx.control, ctx.user_id, limit=limit)
    ]

    ctx.audit("activity", "ok", detail={"calls": len(calls)})
    return OpResult(
        ok=True,
        action="activity",
        data={
            "usage": {
                "sources": {
                    "label": "Sources",
                    "used": counts["sources"],
                    # `R-18`: no cap. It used to be measured against
                    # `quota_files`, which counted uploads only — so this
                    # could read `3 of 50` while the number actually enforced
                    # was 0 of 50, two figures under one ceiling. Credits are
                    # the allowance now; this is a true count of the library.
                    "cap": None,
                    "unit": "count",
                },
                "credits": {
                    "label": "Credits",
                    "used": credits["used"],
                    "cap": credits["cap"],
                    "unit": "credits",
                    "period": credits["period"],
                },
                "calls": {
                    "label": "Calls a minute",
                    "used": control_db.calls_in_last_minute(ctx.control, ctx.user_id),
                    "cap": ctx.cfg.tool_calls_per_min,
                    "unit": "rate",
                },
                # No `storage` key. `R-17`, above.
            },
            # `R-18`: where the credits went. A meter says how much is left;
            # this says what it was spent on, which is the only form in which
            # the number is actionable.
            "spend": spend,
            "calls": calls,
        },
    )


#: §7.10: "A failure says what to do: `Token expired. Reconnect ChatGPT to
#: resume.`" Keyed on the status the gate recorded, so the sentence cannot
#: drift from the reason.
_REMEDY_FOR = {
    "token_expired": "Reconnect this app to resume.",
    "revoked": "This connection's key was revoked. Generate a new link.",
    "unknown_key": "That key is not one Brain issued. Generate a new link.",
    "unknown_user": "That account no longer exists.",
    "rate_limited": "Too many calls in a minute. It will resume on its own.",
    "loop_detected": "The same call was repeated — Brain stopped it. Nothing to fix.",
    "error": "Brain hit an internal error. Nothing you can fix.",
}


def op_list_connections(ctx: Context) -> OpResult:
    """§7.11's list: one server, two ways in, one page.

    An OAuth session and a key are both connections — "name · `Signed in` or
    `Key`" — so they arrive as one list rather than two sections the person
    has to reconcile.
    """
    refusal = _person_only(
        ctx,
        "list_connections",
        "Connections are the person's own keys. An agent cannot enumerate them.",
    )
    if refusal:
        return refusal

    items: list[dict[str, Any]] = []
    for row in control_db.list_connections(ctx.control, ctx.user_id):
        items.append(
            {
                "id": row["connection_id"],
                "name": row["name"],
                "how": "Key",
                "key_prefix": row["key_prefix"],
                "last_call_at": row["last_call_at"],
                "created_at": row["created_at"],
                "revoked": bool(row["revoked"]),
            }
        )
    for row in control_db.list_sessions(ctx.control, ctx.user_id):
        items.append(
            {
                "id": row["session_id"],
                "name": row["client_name"] or "An AI app",
                "how": "Signed in",
                "key_prefix": "",
                "last_call_at": None,
                "created_at": row["created_at"],
                "revoked": bool(row["revoked"]),
            }
        )
    items.sort(key=lambda item: item["created_at"], reverse=True)

    live = [item for item in items if not item["revoked"]]
    ctx.audit("list_connections", "ok", detail={"count": len(live)})
    return OpResult(
        ok=True,
        action="list_connections",
        data={
            "connections": items,
            # §7.11: the `Connected` section and its sentence about revocation
            # "do not render while the list is empty — that sentence means
            # nothing before there is anything to revoke".
            "connected": bool(live),
            "mcp_path": "/mcp",
        },
    )


def op_create_connection(ctx: Context, name: str = "My AI") -> OpResult:
    """§7.11's `Generate my link`.

    The key comes back in plaintext exactly once, in this response. Nothing
    stores it (§2 rule 4 — only the SHA-256 digest), so there is no second
    chance to read it, and the frame is the only place it ever exists.
    """
    refusal = _person_only(
        ctx,
        "create_connection",
        "Only the person issues a key to their own Brain.",
    )
    if refusal:
        return refusal

    row, key = control_db.create_connection(
        ctx.control, ctx.user_id, name=clean_text(name)[:120] or "My AI"
    )
    ctx.audit("create_connection", "ok", target_id=row["connection_id"])
    return OpResult(
        ok=True,
        action="create_connection",
        data={
            "connection": {
                "id": row["connection_id"],
                "name": row["name"],
                "how": "Key",
                "key_prefix": row["key_prefix"],
                "last_call_at": None,
                "created_at": row["created_at"],
                "revoked": False,
            },
            # §7.11: "One link, pasted once, nothing else to copy."
            "key": key,
            "link": f"{_public_base(ctx.cfg)}/mcp?key={key}",
        },
    )


def _public_base(cfg: Settings) -> str:
    """The address the person pastes into their AI, not the one this process
    happens to be bound to.

    Imported here rather than at module scope: `app.auth` imports the engine's
    database layer, and a top-level import would close the circle.
    """
    from app import auth

    return (os.environ.get("BRAIN_PUBLIC_URL") or auth.issuer(cfg)).rstrip("/")


def op_rename_connection(ctx: Context, connection_id: str, name: str) -> OpResult:
    refusal = _person_only(ctx, "rename_connection", "Only the person names their connections.")
    if refusal:
        return refusal
    name = clean_text(name)[:120]
    if not name:
        return _fail("rename_connection", "empty_name", "A connection needs a name.")
    if not _owns_connection(ctx, connection_id):
        return _fail("rename_connection", "not_found", "No such connection.")
    control_db.rename_connection(ctx.control, connection_id, name)
    ctx.audit("rename_connection", "ok", target_id=connection_id)
    return OpResult(ok=True, action="rename_connection", data={"id": connection_id, "name": name})


def op_roll_connection_key(ctx: Context, connection_id: str) -> OpResult:
    """§7.11's `Roll the key`. The old key stops working immediately.

    The connection keeps its name and its history, so §7.10's log still reads
    continuously across the roll — which is the whole reason this is not
    "revoke and make a new one".
    """
    refusal = _person_only(ctx, "roll_connection_key", "Only the person rolls their own key.")
    if refusal:
        return refusal
    if not _owns_connection(ctx, connection_id):
        return _fail("roll_connection_key", "not_found", "No such connection.")

    key = control_db.roll_connection_key(ctx.control, connection_id)
    ctx.audit("roll_connection_key", "ok", target_id=connection_id)
    return OpResult(
        ok=True,
        action="roll_connection_key",
        data={
            "id": connection_id,
            "key": key,
            "link": f"{_public_base(ctx.cfg)}/mcp?key={key}",
        },
    )


def op_revoke_connection(ctx: Context, connection_id: str) -> OpResult:
    """§7.11: "One credential per connection. Revoking one leaves the others
    working." An OAuth session is revoked through its own door."""
    refusal = _person_only(ctx, "revoke_connection", "Only the person revokes their own keys.")
    if refusal:
        return refusal

    if _owns_connection(ctx, connection_id):
        control_db.revoke_connection(ctx.control, connection_id)
    elif any(
        row["session_id"] == connection_id
        for row in control_db.list_sessions(ctx.control, ctx.user_id)
    ):
        control_db.revoke_session(ctx.control, connection_id)
    else:
        return _fail("revoke_connection", "not_found", "No such connection.")

    ctx.audit("revoke_connection", "ok", target_id=connection_id)
    return OpResult(ok=True, action="revoke_connection", data={"id": connection_id})


def _owns_connection(ctx: Context, connection_id: str) -> bool:
    """Tenant isolation, at the one place a connection id arrives from input.

    `get_connection` takes an id and would happily return another person's
    row; §2's "tenant SQLite paths are resolved strictly from the verified
    `user_id`" is the same rule, and this is its equivalent here.
    """
    record = control_db.get_connection(ctx.control, connection_id)
    return bool(record and record["user_id"] == ctx.user_id)


def op_audit_log(ctx: Context, *, limit: int = 40, include_reads: bool = False) -> OpResult:
    """The owner's own activity log — the anti-"SaaS blindness" surface.

    Everything an agent or the web app did on this library, with a plain-English
    reason, so nothing happens to the user's knowledge that they cannot see. It
    is read-only by construction: `audit_logs` is append-only and no operation
    updates or deletes a row.
    """
    rows = control_db.recent_audit(ctx.control, ctx.user_id, limit=max(1, min(limit, 200)) * 3)
    entries: list[dict[str, Any]] = []
    for row in rows:
        action = row["action"] or ""
        if not include_reads and action in _AUDIT_NOISE and row["status"] == "ok":
            continue
        blocked = str(row["status"] or "").startswith("blocked")
        entries.append(
            {
                "id": row["id"],
                "action": action,
                "status": row["status"],
                "surface": row["surface"],
                "client_name": row["client_name"],
                "target_id": row["target_id"],
                "created_at": row["created_at"],
                "blocked": blocked,
                "reasoning_trace": (
                    row["reasoning_trace"]
                    or _AUDIT_FALLBACK.get(action, f"{action.replace('_', ' ')} ({row['status']})")
                ),
            }
        )
        if len(entries) >= limit:
            break
    return OpResult(
        ok=True,
        action="audit_log",
        data={
            "entries": entries,
            "blocked_count": sum(1 for entry in entries if entry["blocked"]),
            "append_only": True,
        },
    )


def op_update_profile(
    ctx: Context,
    *,
    about_you_context: str | None = None,
    theme_mode: str | None = None,
    display_name: str | None = None,
    locale: str | None = None,
    want_summary: bool | None = None,
    want_rules: bool | None = None,
    want_skills: bool | None = None,
    keep_uploads: bool | None = None,
    write_in: str | None = None,
) -> OpResult:
    """§7.13's four sections write through here.

    A value outside its allowed set comes back as a refusal rather than being
    dropped: a switch that does not move, with no explanation, is worse than
    an error that says why.
    """
    try:
        control_db.update_profile(
            ctx.control,
            ctx.user_id,
            about_you_context=None if about_you_context is None else clean_text(about_you_context),
            theme_mode=theme_mode,
            display_name=None if display_name is None else clean_text(display_name),
            locale=locale,
            want_summary=want_summary,
            want_rules=want_rules,
            want_skills=want_skills,
            keep_uploads=keep_uploads,
            write_in=write_in,
        )
    except ValueError as exc:
        return _fail("update_profile", "invalid_value", str(exc))
    ctx.audit("update_profile", "ok")
    return op_profile(ctx)


def op_export(ctx: Context, fmt: str = "json") -> OpResult:
    """One-click library export. `json` round-trips; `markdown` is for reading."""
    sources = [
        dict(row)
        for row in ctx.tenant.execute(
            "SELECT * FROM sources WHERE status != 'trash' ORDER BY created_at"
        ).fetchall()
    ]
    claims = [
        tenant_db.hydrate_claim(row)
        for row in ctx.tenant.execute(
            "SELECT * FROM epistemic_claims ORDER BY source_id, rowid"
        ).fetchall()
    ]
    skills = [
        tenant_db.hydrate_skill(row)
        for row in ctx.tenant.execute("SELECT * FROM skills ORDER BY slug").fetchall()
    ]
    tensions = [
        dict(row) for row in ctx.tenant.execute("SELECT * FROM tension_links").fetchall()
    ]
    ctx.audit("export", "ok", detail={"format": fmt, "claims": len(claims)})

    if fmt == "markdown":
        return OpResult(
            ok=True,
            action="export",
            data={
                "format": "markdown",
                "filename": "brain-library.md",
                "content": _export_markdown(sources, claims, skills, tensions),
            },
        )
    return OpResult(
        ok=True,
        action="export",
        data={
            "format": "json",
            "filename": "brain-library.json",
            "content": json.dumps(
                {
                    "exported_at": control_db.iso(control_db.now_utc()),
                    "schema_version": 1,
                    "sources": sources,
                    "claims": claims,
                    "skills": skills,
                    "tension_links": tensions,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
        },
    )


def _export_markdown(sources, claims, skills, tensions) -> str:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        by_source.setdefault(claim["source_id"], []).append(claim)

    lines = ["# brain library export", ""]
    if skills:
        lines += ["## Skills", ""]
        for skill in skills:
            lines.append(f"### {skill['name']} (`{skill['slug']}` · v{skill['version']})")
            lines.append(f"**Trigger:** {skill['trigger_when']}")
            for index, step in enumerate(skill["execution_steps"], start=1):
                lines.append(f"{index}. {step}")
            if skill["guardrails"]:
                lines.append(f"**Guardrails:** {skill['guardrails']}")
            lines.append("")
    lines += ["## Library", ""]
    for source in sources:
        lines.append(f"### {source['title']}")
        if source["canonical_url"]:
            lines.append(f"<{source['canonical_url']}>")
        lines.append("")
        for claim in by_source.get(source["id"], []):
            lines.append(f"- **{claim['claim']}**")
            lines.append(f"  - Works when: {claim['when_it_applies']}")
            if claim["when_it_fails"]:
                lines.append(f"  - Breaks when: {claim['when_it_fails']}")
            lines.append(f'  - > "{claim["anchor_quote"]}"')
            lines.append(
                f"  - {claim['locator_type']}: {claim['locator_value']} · `{claim['id']}`"
            )
        lines.append("")
    if tensions:
        lines += ["## Disagreements", ""]
        for tension in tensions:
            lines.append(
                f"- `{tension['claim_a_id']}` {tension['link_type']} "
                f"`{tension['claim_b_id']}` — {tension['divergence_summary']}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# first-run demo data
# ---------------------------------------------------------------------------
DEMO_SOURCE_VIDEO = "src_demo_video"
DEMO_SOURCE_REPORT = "src_demo_report"
DEMO_SKILL_SLUG = "demo-cold-email-sprint"


@_syncs_tenant
def op_seed_demo(ctx: Context) -> OpResult:
    """Seed one skill and two genuinely contradicting claims.

    First-run onboarding (Checkpoint B, Rule 4c). The rows are ordinary rows —
    real ids, real embeddings, a real `tension_link` — so clicking into one
    exercises exactly the same code path as a real claim, and clearing the demo
    is an ordinary archive rather than a special case.
    """
    if tenant_db.get_skill_by_slug(ctx.tenant, DEMO_SKILL_SLUG):
        return OpResult(ok=True, action="seed_demo", data={"already_seeded": True})

    video = tenant_db.upsert_source(
        ctx.tenant,
        source_id=DEMO_SOURCE_VIDEO,
        title="Outbound Masterclass: the morning block (demo)",
        source_type="youtube",
        canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        author="Jane Expert",
        duration_sec=1860,
    )
    report = tenant_db.upsert_source(
        ctx.tenant,
        source_id=DEMO_SOURCE_REPORT,
        title="Enterprise Outbound Benchmark 2026 (demo)",
        source_type="pdf",
        canonical_url="https://example.com/report.pdf",
        author="Acme Research",
    )

    batching = tenant_db.insert_claim(
        ctx.tenant,
        source_id=video["id"],
        claim="Batch cold email into one morning block",
        speaker="Jane Expert",
        evidence_basis="case_study",
        domain_tags=["sales", "outbound", "demo"],
        when_it_applies="Small outbound teams without an SDR pod",
        when_it_fails="Past 500 sends a day, when inbox placement collapses",
        metrics_and_quotes="Reply rate up 34% the quarter they switched.",
        anchor_quote=(
            "we batch every single cold email into one block before ten in the morning, "
            "and our reply rate went up 34%"
        ),
        locator_type="timestamp",
        locator_value="1420",
        salience=5,
        cfg=ctx.cfg,
    )
    continuous = tenant_db.insert_claim(
        ctx.tenant,
        source_id=report["id"],
        claim="Send cold email continuously through the day, never batched",
        speaker="Acme Research",
        evidence_basis="data",
        domain_tags=["sales", "outbound", "demo"],
        when_it_applies="Teams sending more than 500 emails a day",
        when_it_fails="Under 200 sends a day, where timing makes no measurable difference",
        metrics_and_quotes="48M sends across 310 teams; 18% better reply rate.",
        anchor_quote=(
            "continuous sending beat batched sending on reply rate in every cohort above "
            "500 emails per day"
        ),
        locator_type="page",
        locator_value="14",
        salience=4,
        cfg=ctx.cfg,
    )
    for record in (batching, continuous):
        tenant_db.set_claim_embedding(
            ctx.tenant,
            record["id"],
            ctx.prov.embedder.embed([_claim_embedding_text(record)])[0],
            cfg=ctx.cfg,
        )
    tenant_db.add_tension_link(
        ctx.tenant,
        claim_a_id=batching["id"],
        claim_b_id=continuous["id"],
        link_type="contradicts",
        divergence_summary=(
            "Jane Expert batches; Acme Research's data says continuous wins above "
            "500 sends a day. The split is daily send volume."
        ),
    )

    skill = tenant_db.upsert_skill(
        ctx.tenant,
        slug=DEMO_SKILL_SLUG,
        name="Cold Email Sprint (demo)",
        description="Run a one-week cold email sprint.",
        trigger_when="Starting an outbound push",
        execution_steps=[
            "Build the list the night before",
            "Write the whole sequence in one sitting",
            "Send the batch before 10am",
        ],
        guardrails="Never send on a Friday. Stop batching past 500 sends a day.",
        linked_claim_ids=[batching["id"], continuous["id"]],
        origin_type="distilled",
    )
    text = " ".join(
        [skill["name"], skill["description"], skill["trigger_when"], *skill["execution_steps"]]
    )
    tenant_db.set_skill_embedding(
        ctx.tenant, skill["id"], ctx.prov.embedder.embed([text])[0], cfg=ctx.cfg
    )
    ctx.audit("seed_demo", "ok", detail={"claims": 2, "skills": 1})
    return OpResult(
        ok=True,
        action="seed_demo",
        data={
            "already_seeded": False,
            "skill_id": skill["id"],
            "claim_ids": [batching["id"], continuous["id"]],
            "library_counts": tenant_db.counts(ctx.tenant),
        },
    )


@_syncs_tenant
def op_clear_demo(ctx: Context) -> OpResult:
    """Clear the demo rows the ordinary way — a source archive, nothing special."""
    cleared = 0
    for source_id in (DEMO_SOURCE_VIDEO, DEMO_SOURCE_REPORT):
        if tenant_db.get_source(ctx.tenant, source_id):
            tenant_db.set_source_status(ctx.tenant, source_id, "trash")
            cleared += 1
    skill = tenant_db.get_skill_by_slug(ctx.tenant, DEMO_SKILL_SLUG)
    if skill:
        tenant_db.set_skill_status(ctx.tenant, skill["id"], "archived")
        cleared += 1
    ctx.audit("clear_demo", "ok", detail={"cleared": cleared})
    return OpResult(
        ok=True,
        action="clear_demo",
        data={"cleared": cleared, "library_counts": tenant_db.counts(ctx.tenant)},
    )


# ---------------------------------------------------------------------------
# §7.13's `Your data` — the two acts the product cannot take back
# ---------------------------------------------------------------------------
#: §9.4 gates both on typing `DELETE`. The word is checked **here** as well as
#: in the dialog: a dialog is a frame's promise, and a frame is one bug away
#: from firing a POST with an empty body. An op that destroys a library should
#: not be callable by accident from anywhere.
DELETE_WORD = "DELETE"


def _typed_the_word(action: str, typed: str) -> OpResult | None:
    if (typed or "").strip() != DELETE_WORD:
        return _fail(
            action,
            "not_confirmed",
            f"Type {DELETE_WORD} to confirm. Nothing was changed.",
        )
    return None


@_syncs_tenant
def op_delete_library(ctx: Context, *, typed: str = "") -> OpResult:
    """§7.13: "Removes every source and everything made from it. Your account
    stays."

    Every content table, emptied in one transaction — not row by row through
    the archive, which would leave the person watching a progress bar for
    something that should be instantaneous and atomic. The account, its
    settings, its connections and its sessions all survive: this is the person
    clearing their library, not leaving.
    """
    refusal = _person_only(
        ctx,
        "delete_library",
        "Nothing is deleted by an agent. This is the person's own door, in Brain's web app.",
    )
    if refusal:
        return refusal
    refusal = _typed_the_word("delete_library", typed)
    if refusal:
        return refusal

    before = tenant_db.counts(ctx.tenant)
    with ctx.tenant:  # one transaction: all of it goes, or none of it does
        # `sources` cascades to source_content, claims, tension_links and
        # summaries; skills hang off a source but survive its deletion when
        # they were written by hand, so they are cleared explicitly.
        ctx.tenant.execute("DELETE FROM sources")
        ctx.tenant.execute("DELETE FROM skills")
    # `R-18`: and the files Brain was sent. Without this, "removes every
    # source and everything made from it" left the person's uploads sitting
    # in staging for up to 30 days with nothing in the product able to reach
    # them — the one case where the sweep's window was not a promise but a
    # delay the person had not agreed to.
    files = _purge_uploads(ctx)
    ctx.audit("delete_library", "ok", detail={**before, "files": files})
    return OpResult(
        ok=True,
        action="delete_library",
        data={"deleted": before, "library_counts": tenant_db.counts(ctx.tenant)},
    )


def op_delete_account(ctx: Context, *, typed: str = "") -> OpResult:
    """§7.13: "Closes the account and everything in it."

    The tenant database file is unlinked, every session is revoked, and the
    `users` row goes — which cascades `oauth_sessions`, `ingestion_jobs` and
    their steps in `control.db`. The audit rows are deliberately left: they
    carry no content, and a deletion that erases its own record is not one the
    person can later prove happened.
    """
    refusal = _person_only(
        ctx,
        "delete_account",
        "An account is closed by the person who owns it, in Brain's web app.",
    )
    if refusal:
        return refusal
    refusal = _typed_the_word("delete_account", typed)
    if refusal:
        return refusal

    user_id = ctx.user_id
    counts = tenant_db.counts(ctx.tenant)
    sessions = control_db.revoke_all_sessions(ctx.control, user_id)

    # Audited BEFORE the row goes: the foreign key on `audit_logs.user_id` is
    # nullable by design, but the row has to be written while there is still a
    # user to attribute it to.
    files = _purge_uploads(ctx)
    ctx.audit("delete_account", "ok", detail={"sessions": sessions, "files": files, **counts})

    tenant_db.close_tenant(user_id, cfg=ctx.cfg)
    path = ctx.cfg.tenant_db_path(user_id)
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()

    control_db.delete_user(ctx.control, user_id)
    return OpResult(
        ok=True,
        action="delete_account",
        data={"user_id": user_id, "sessions_revoked": sessions, "deleted": counts},
    )


def op_list_cards(ctx: Context, view: str = "everything", limit: int = 60) -> OpResult:
    """The resting grid: newest-first rows for the sidebar's four views."""
    data: dict[str, Any] = {"view": view, "claims": [], "skills": []}

    if view in ("everything", "skills", "archived"):
        status = "archived" if view == "archived" else "active"
        rows = ctx.tenant.execute(
            "SELECT * FROM skills WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        data["skills"] = [tenant_db.hydrate_skill(row) for row in rows]

    if view in ("everything", "library", "archived"):
        status = "archived" if view == "archived" else "active"
        rows = ctx.tenant.execute(
            """
            SELECT c.*, s.title AS source_title, s.canonical_url AS source_url,
                   s.source_type AS source_type, s.author AS source_author
            FROM epistemic_claims c JOIN sources s ON s.id = c.source_id
            WHERE c.status = ? AND s.status != 'trash'
            ORDER BY c.salience DESC, c.rowid DESC LIMIT ?
            """,
            (status, limit),
        ).fetchall()
        claims = [tenant_db.hydrate_claim(row) for row in rows]
        tension_map = tenant_db.tensions_for_claims(ctx.tenant, [c["id"] for c in claims])
        for claim in claims:
            claim["deep_link"] = deep_link(
                claim.get("source_url", ""), claim["locator_type"], claim["locator_value"]
            )
            claim["tensions"] = [
                {
                    **tension,
                    "other_deep_link": deep_link(
                        tension["other_source_url"],
                        tension["other_locator_type"],
                        tension["other_locator_value"],
                    ),
                }
                for tension in tension_map.get(claim["id"], [])
            ]
        data["claims"] = claims

    data["library_counts"] = tenant_db.counts(ctx.tenant)
    data["active_jobs"] = control_db.list_jobs(ctx.control, ctx.user_id, limit=10)
    return OpResult(ok=True, action="list_cards", data=data)
