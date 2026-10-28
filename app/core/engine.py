"""The 8 canonical brain operations, plus the synchronous job runner.

This module is the whole product's logic and returns **plain structured data**.
Markdown envelopes, tone and `[AGENT_HINT]` lines live in
`app/core/formatting.py`, which is co-designed with the founder at Checkpoint A —
so nothing here hard-codes how `brain` speaks.
"""

from __future__ import annotations

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
from app.ingestion.providers import ProviderError, Providers, providers
from app.security.sanitizer import clean_text

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


def _fail(action: str, reason: str, message: str) -> OpResult:
    return OpResult(ok=False, action=action, error_reason=reason, error_message=message)


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

    def audit(self, action: str, status: str, target_id: str | None = None, detail: Any = "") -> None:
        control_db.audit(
            self.control,
            user_id=self.user_id,
            action=action,
            status=status,
            surface=self.surface,
            client_name=self.client_name,
            target_id=target_id,
            detail=detail,
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
def op_fetch(ctx: Context, item_id: str) -> OpResult:
    item_id = (item_id or "").strip()
    if not item_id:
        return _fail("fetch", "empty_id", "An id is required.")

    claim = tenant_db.get_claim(ctx.tenant, item_id)
    if claim:
        source = tenant_db.get_source(ctx.tenant, claim["source_id"]) or {}
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

    skill = tenant_db.get_skill(ctx.tenant, item_id) or tenant_db.get_skill_by_slug(ctx.tenant, item_id)
    if skill:
        linked = [
            tenant_db.get_claim(ctx.tenant, cid)
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
def op_archive_item(ctx: Context, item_type: ItemType, item_id: str, *, restore: bool = False) -> OpResult:
    if item_type not in ("source", "claim", "skill"):
        return _fail("archive_item", "bad_item_type", "item_type must be source, claim or skill.")
    target_status = "active" if restore else "archived"

    if item_type == "claim":
        record = tenant_db.get_claim(ctx.tenant, item_id)
        changed = tenant_db.set_claim_status(ctx.tenant, item_id, target_status) if record else False
        label = record["claim"][:120] if record else ""
    elif item_type == "skill":
        record = tenant_db.get_skill(ctx.tenant, item_id) or tenant_db.get_skill_by_slug(
            ctx.tenant, item_id
        )
        changed = tenant_db.set_skill_status(ctx.tenant, record["id"], target_status) if record else False
        label = record["name"] if record else ""
        item_id = record["id"] if record else item_id
    else:
        record = tenant_db.get_source(ctx.tenant, item_id)
        changed = tenant_db.set_source_status(ctx.tenant, item_id, target_status) if record else False
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
def process_job(job: dict[str, Any], *, cfg: Settings | None = None) -> dict[str, Any]:
    """Run one leased job all the way through: fetch -> extract -> link."""
    cfg = cfg or settings()
    control = control_db.control()
    user_id = job["user_id"]
    tdb = tenant_db.tenant(user_id, cfg=cfg)
    prov = providers(cfg)
    job_id = job["job_id"]

    def finish(status: str, *, message: str = "", claims: int = 0) -> dict[str, Any]:
        control_db.set_job_status(
            control, job_id, status, error_message=message, claims_extracted=claims
        )
        if status in ("failed", "blocked_by_platform"):
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

    # ---- fetch ------------------------------------------------------------
    control_db.set_job_status(control, job_id, "fetching")
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

    # ---- extract + ground -------------------------------------------------
    control_db.set_job_status(control, job_id, "extracting")
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

    # ---- link -------------------------------------------------------------
    control_db.set_job_status(control, job_id, "linking")
    link_result = link_claims(
        tdb, stored, embeddings=embeddings, classifier=prov.linker, cfg=cfg
    )

    outcome = finish("ready", claims=len(stored))
    outcome["tensions"] = len(link_result.created)
    outcome["rejected"] = extraction.rejected
    outcome["title"] = source["title"]
    return outcome


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
