"""The 8 canonical brain operations, plus the synchronous job runner.

This module is the whole product's logic and returns **plain structured data**.
Markdown envelopes, tone and `[AGENT_HINT]` lines live in
`app/core/formatting.py`, which is co-designed with the founder at Checkpoint A —
so nothing here hard-codes how `brain` speaks.
"""

from __future__ import annotations

import logging

import functools
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from app.config import Settings, settings
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.db.search import SearchResults, hybrid_search
from app.ingestion import gatekeeper
from app.ingestion.extractor import extract_grounded_claims
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

    # Media minutes are only reserved once the real duration is known, so a
    # queued YouTube job books a provisional slot it can give back.
    reserved_minutes = 0
    if target.source_type == "youtube":
        reserved_minutes = min(ctx.cfg.max_media_minutes, 15)
        try:
            control_db.reserve_quota(ctx.control, ctx.user_id, minutes=reserved_minutes, cfg=ctx.cfg)
        except control_db.QuotaExceeded as exc:
            ctx.audit("ingest_source", "quota_exceeded", detail={"kind": exc.kind})
            return _fail(
                "ingest_source",
                "quota_exceeded",
                f"You have used {exc.used} of your {exc.cap} media minutes.",
            )

    source_id = control_db.new_id("src")
    job = control_db.create_job(
        ctx.control,
        ctx.user_id,
        source_id=source_id,
        input_target=target.canonical_url,
        source_type=target.source_type,
        user_note=clean_text(user_note or "")[:2000],
        reserved_minutes=reserved_minutes,
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


def op_ingest_file(
    ctx: Context, path: str | Path, *, filename: str | None = None, user_note: str | None = None
) -> OpResult:
    """Queue an uploaded file (web surface only — never exposed over MCP)."""
    try:
        validated = gatekeeper.validate_file(path, filename=filename, cfg=ctx.cfg)
    except GatekeeperError as exc:
        ctx.audit("ingest_file", "rejected", detail={"reason": exc.reason})
        return _fail("ingest_file", exc.reason, exc.message)

    existing = tenant_db.find_source_by_hash(ctx.tenant, validated.content_hash)
    if existing:
        ctx.audit("ingest_file", "duplicate", target_id=existing["id"])
        return OpResult(
            ok=True,
            action="ingest_file",
            data={"duplicate": True, "source_id": existing["id"], "title": existing["title"]},
        )

    try:
        control_db.reserve_quota(ctx.control, ctx.user_id, files=1, cfg=ctx.cfg)
    except control_db.QuotaExceeded as exc:
        ctx.audit("ingest_file", "quota_exceeded", detail={"kind": exc.kind})
        return _fail(
            "ingest_file",
            "quota_exceeded",
            f"You have used {exc.used} of your {exc.cap} file slots.",
        )

    source_id = control_db.new_id("src")
    job = control_db.create_job(
        ctx.control,
        ctx.user_id,
        source_id=source_id,
        input_target=str(validated.path),
        source_type=validated.source_type,
        user_note=clean_text(user_note or "")[:2000],
        reserved_files=1,
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
@_syncs_tenant
def op_archive_item(ctx: Context, item_type: ItemType, item_id: str, *, restore: bool = False) -> OpResult:
    if item_type not in ("source", "claim", "skill"):
        return _fail("archive_item", "bad_item_type", "item_type must be source, claim or skill.")
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

    def finish(status: str, *, message: str = "", claims: int = 0) -> dict[str, Any]:
        # Even a failure can have written a source row before giving up, so
        # the tenant is flagged on every exit from the pipeline, not just on
        # `ready`.
        r2_sync.mark_dirty(user_id)
        control_db.set_job_status(
            control, job_id, status, error_message=message, claims_extracted=claims
        )
        if status in ("failed", "blocked_by_platform"):
            # Nothing may stay `running` once the job is over, or the work
            # block spins a step forever on a job that died (§4.4, §9.1).
            control_db.abandon_steps(control, job_id, reason=message)
            # Give back whatever the queue booked, so a failure costs no quota.
            control_db.release_quota(
                control,
                user_id,
                minutes=job["reserved_minutes"],
                files=job["reserved_files"],
            )
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
        return finish("failed", message=exc.message)
    except ProviderError as exc:
        status = "blocked_by_platform" if exc.reason == "blocked_by_platform" else "failed"
        return finish(status, message=exc.message)
    except Exception as exc:  # pragma: no cover - defensive
        return finish("failed", message=f"Unexpected error while fetching: {exc}")

    if not content.full_text:
        return finish("failed", message="Nothing readable was found at that source.")

    control_db.finish_step(
        control, job_id, "read", detail=_step_detail_read(job, content)
    )

    # ---- step 2: transcribing audio ---------------------------------------
    # The same fetch produced it, so there is no second call to make: what the
    # person is told is what actually happened. A source with no spoken audio
    # says so rather than claiming a transcription that never ran.
    control_db.start_step(control, job_id, "transcribe")
    words = len(content.full_text.split())
    if job["source_type"] == "youtube":
        control_db.finish_step(
            control, job_id, "transcribe", detail=f"{words:,} words captured"
        )
    else:
        control_db.finish_step(
            control, job_id, "transcribe",
            detail=f"{words:,} words read — no audio in this source",
            state="skipped",
        )

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


def _ingest_content(job, content, *, cfg, finish):
    """Everything after a successful fetch: reconcile quota, store, extract,
    ground, link. Shared by the normal pipeline and by a rescue."""
    control = control_db.control()
    user_id = job["user_id"]
    tdb = tenant_db.tenant(user_id, cfg=cfg)
    prov = providers(cfg)
    job_id = job["job_id"]

    # True duration is only known now, so reconcile the provisional reservation.
    if content.duration_sec:
        try:
            actual_minutes = gatekeeper.check_duration(content.duration_sec, cfg=cfg)
        except GatekeeperError as exc:
            return finish("failed", message=exc.message)
        delta = actual_minutes - job["reserved_minutes"]
        if delta > 0:
            try:
                control_db.reserve_quota(control, user_id, minutes=delta, cfg=cfg)
            except control_db.QuotaExceeded as exc:
                return finish(
                    "failed",
                    message=f"You have used {exc.used} of your {exc.cap} media minutes.",
                )
        elif delta < 0:
            control_db.release_quota(control, user_id, minutes=-delta)
        job["reserved_minutes"] = actual_minutes

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

    # ---- step 3: writing the summary --------------------------------------
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
            },
        ) if prov.summariser else {}
    except Exception as exc:  # pragma: no cover - defensive
        # §9.1 allows the summary step to fail without taking the source with
        # it: the transcript and the rules are still worth having.
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
    control_db.finish_step(
        control, job_id, "draft_skills",
        detail="not built yet",
        state="skipped",
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
            "started_at": job["created_at"],
        },
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
    processed: list[dict[str, Any]] = []
    for _ in range(max(1, limit)):
        job = control_db.claim_next_job(control, user_id=user_id)
        if job is None:
            break
        processed.append(process_job(job, cfg=cfg))
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

    def finish(status: str, *, message: str = "", claims: int = 0) -> dict[str, Any]:
        control_db.set_job_status(
            ctx.control, job_id, status, error_message=message, claims_extracted=claims
        )
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


@_syncs_tenant
def op_permanent_delete(ctx: Context, item_id: str) -> OpResult:
    """Hard delete. Only reachable from the Web UI, only from Trash.

    This is the single destructive path in the product, and an agent cannot
    reach it: there is no MCP tool that calls this function.
    """
    source = tenant_db.get_source(ctx.tenant, item_id)
    if source is None:
        return _fail("permanent_delete", "not_found", f"No source has the id `{item_id}`.")
    if source["status"] != "trash":
        return _fail(
            "permanent_delete",
            "not_in_trash",
            "Only items in Trash can be permanently deleted. Move it to Trash first.",
        )
    claims = len(tenant_db.claims_for_source(ctx.tenant, item_id, active_only=False))
    ctx.tenant.execute("DELETE FROM sources WHERE id = ?", (item_id,))
    ctx.audit("permanent_delete", "ok", target_id=item_id, detail={"claims": claims})
    return OpResult(
        ok=True,
        action="permanent_delete",
        data={
            "id": item_id,
            "label": source["title"],
            "deleted_claims": claims,
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
                "media_minutes_used": user.get("media_minutes_used", 0),
                "media_minutes_cap": ctx.cfg.quota_media_minutes,
                "files_used": user.get("files_used", 0),
                "files_cap": ctx.cfg.quota_files,
            },
            "sessions": sessions,
            "mcp_url": ctx.cfg.audience,
            "library_counts": tenant_db.counts(ctx.tenant),
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
    ctx: Context, *, about_you_context: str | None = None, theme_mode: str | None = None
) -> OpResult:
    try:
        control_db.update_profile(
            ctx.control,
            ctx.user_id,
            about_you_context=None if about_you_context is None else clean_text(about_you_context),
            theme_mode=theme_mode,
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
