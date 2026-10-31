"""The Web Control Plane's HTTP surface: §5's pages and `/api/*`.

Browser sessions are separate from MCP tokens on purpose: an agent gets an
audience-bound OAuth token, a person gets a signed, `SameSite=Strict` cookie.
Every mutating request additionally needs a double-submit CSRF header, so a
cross-site form post cannot act as the user even if the cookie rides along.

Hard deletion, archive recovery and export live here and **only** here — there
is no MCP tool that reaches them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app import auth, omnibar
from app.config import Settings, settings
from app.core import engine as E
from app.db import control as control_db
from app.ingestion.gatekeeper import GatekeeperError
from app.ingestion.providers import ProviderError
from app.security import ratelimit

LOGGER = logging.getLogger("brain.webapi")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "brain_session"
CSRF_COOKIE = "brain_csrf"
CSRF_HEADER = "x-brain-csrf"
#: Present on every mutating request from the app. A cross-origin HTML form
#: post cannot set it, and setting it forces a preflight we do not answer.
REQUEST_HEADER = "x-brain-request"
SESSION_TTL = 30 * 24 * 3600


# ---------------------------------------------------------------------------
# signed browser session
# ---------------------------------------------------------------------------
def _sign(payload: str, cfg: Settings) -> str:
    mac = hmac.new(auth.signing_key(cfg).encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("ascii").rstrip("=")


def issue_session_cookie(user_id: str, *, cfg: Settings | None = None) -> str:
    cfg = cfg or settings()
    body = base64.urlsafe_b64encode(
        json.dumps({"sub": user_id, "exp": int(time.time()) + SESSION_TTL}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{body}.{_sign(body, cfg)}"


def read_session_cookie(raw: str | None, *, cfg: Settings | None = None) -> str | None:
    cfg = cfg or settings()
    if not raw or "." not in raw:
        return None
    body, signature = raw.rsplit(".", 1)
    if not hmac.compare_digest(_sign(body, cfg), signature):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if int(claims.get("exp", 0)) <= time.time():
        return None
    return str(claims.get("sub") or "") or None


class ApiError(Exception):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message


def _json(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def _context(request: Request, *, surface: str = "web_ui") -> E.Context:
    cfg = settings()
    user_id = read_session_cookie(request.cookies.get(SESSION_COOKIE), cfg=cfg)
    if not user_id:
        raise ApiError(401, "not_signed_in", "Sign in to use the control plane.")
    try:
        return E.build_context(user_id, client_name="web", surface=surface, cfg=cfg)
    except ValueError as exc:
        raise ApiError(401, "unknown_user", "That session is no longer valid.") from exc


def _require_csrf(request: Request) -> None:
    """Two independent checks, because they fail to different attacks.

    The double-submit token stops a forged request that *can* read nothing but
    still rides the cookie. `X-Brain-Request` stops the one shape of request a
    token cannot: an HTML form post, which a browser will send cross-origin
    with the cookie attached and which cannot set a custom header at all. A
    custom header forces a preflight, and the preflight for `/api/*` is not
    answered — see `MCP_CORS_PREFIXES`.
    """
    if request.headers.get(REQUEST_HEADER) != "1":
        raise ApiError(
            403,
            "not_an_app_request",
            "This request did not come from the brain web app. Reload the page.",
        )
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not hmac.compare_digest(cookie, header):
        raise ApiError(403, "csrf_failed", "This request could not be verified. Reload the page.")


def _rate_limit(
    ctx: E.Context,
    name: str,
    arguments: dict[str, Any],
    *,
    ingestion: bool = False,
    repeatable: bool = False,
) -> None:
    """Screen one web call.

    `repeatable=True` is for an idempotent GET. The loop breaker refuses the
    same call with the same arguments inside 15 seconds — right for an agent
    stuck in a retry, wrong for a browser, where Back, a reload and a second
    visit to the same list all do exactly that. It is set per call site rather
    than inferred, so which reads dropped the guard is visible in one grep.
    """
    decision = ratelimit.limiter().check(
        ctx.user_id,
        f"web:{name}",
        arguments,
        is_ingestion=ingestion,
        loop_guard=not repeatable,
        # §2's 30/min is the ceiling for an agent in a runaway loop. A person
        # clicking through their own library is not that, and shared the
        # budget until this existed.
        bucket=ratelimit.WEB_BUCKET,
    )
    if not decision.allowed:
        raise ApiError(429, decision.reason, _for_a_person(decision))


#: The limiter's sentences are written for a host LLM — "pause for 3s before
#: calling brain again, and tell the user what you already have". Correct on
#: the MCP surface, and nonsense on a page a person is looking at.
_RATE_LIMIT_SAID = {
    "web_requests_rate_limited": (
        "That was a lot of requests at once. Give it a few seconds and try again."
    ),
    "ingestions_rate_limited": (
        "That is a lot of sources at once. The ones already queued are safe — "
        "add the next in a minute."
    ),
    "duplicate_call_circuit_breaker": (
        "That just happened. Give it a moment rather than sending it twice."
    ),
}


def _for_a_person(decision) -> str:
    said = _RATE_LIMIT_SAID.get(decision.reason)
    if said:
        return said
    # An unmapped reason is better said plainly than passed through: whatever
    # it is, it was written for a model.
    return "Brain is busy. Give it a few seconds and try again."


def endpoint(handler: Callable) -> Callable:
    """Uniform error envelope, so the UI never has to parse a stack trace."""

    async def wrapped(request: Request) -> Response:
        try:
            return await handler(request)
        except ApiError as exc:
            return _json({"ok": False, "reason": exc.reason, "message": exc.message}, exc.status)
        except GatekeeperError as exc:
            return _json({"ok": False, "reason": exc.reason, "message": exc.message}, 400)
        except ProviderError as exc:
            # A provider failure already carries a reason code and a sentence
            # written for the person. Falling through to the generic net below
            # replaced both with the name of a Python class: saving a rule
            # against a live embedding model answered "Something went wrong
            # (ProviderError)." — true, unactionable, and not what the message
            # on the exception says. 502, because what failed is upstream.
            return _json({"ok": False, "reason": exc.reason, "message": exc.message}, 502)
        except Exception as exc:  # never leak internals to a browser
            return _json(
                {
                    "ok": False,
                    "reason": "internal_error",
                    "message": f"Something went wrong ({type(exc).__name__}).",
                },
                500,
            )

    wrapped.__name__ = getattr(handler, "__name__", "endpoint")
    return wrapped


def _result(result: E.OpResult, status_on_error: int = 400) -> Response:
    if not result.ok:
        return _json(
            {"ok": False, "reason": result.error_reason, "message": result.error_message},
            status_on_error,
        )
    return _json({"ok": True, **result.data})


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------
@endpoint
async def api_login(request: Request) -> Response:
    """V1 sign-in: an email establishes the tenant. No password, by design —
    this is a single-person server, and the MCP side is where the real
    credentials live."""
    cfg = settings()
    payload = await _body(request)
    email = str(payload.get("email") or os.environ.get("BRAIN_OWNER_EMAIL") or "").strip()
    try:
        user = control_db.ensure_user(control_db.control(), email)
    except ValueError as exc:
        raise ApiError(400, "invalid_email", str(exc)) from exc

    # A first sign-in lands in an empty workspace, which teaches nothing about
    # what the product is. Three editable demo items (Rule 4c) mean the first
    # screen shows a claim card, a skill card and a disagreement — all of which
    # the user can edit or archive like any other.
    # Best-effort, and never fatal. Seeding embeds the demo claims, so a
    # misconfigured embedding host made the whole sign-in 502 and no new
    # account could be created at all — measured on production, where
    # `embedding-3` is rejected by `api.z.ai` with "Unknown Model". An empty
    # first screen is a worse welcome than a seeded one; it is not a reason to
    # refuse someone the door.
    try:
        seeded = E.seed_first_run_library(user["user_id"], cfg=cfg)
    except Exception:
        LOGGER.exception("could not seed the first-run library for %s", user["user_id"])
        seeded = False

    csrf = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    response = _json({
        "ok": True,
        "user_id": user["user_id"],
        "email": user["email"],
        "csrf": csrf,
        "seeded": seeded,
    })
    secure = not cfg.is_test
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_cookie(user["user_id"], cfg=cfg),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    # Readable by JS on purpose: the page echoes it back in the CSRF header.
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=SESSION_TTL, httponly=False, samesite="strict",
        secure=secure, path="/",
    )
    control_db.audit(
        control_db.control(),
        user_id=user["user_id"],
        action="web_login",
        status="ok",
        surface="web_ui",
        client_name="web",
    )
    return response


@endpoint
async def api_logout(request: Request) -> Response:
    response = _json({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@endpoint
async def api_me(request: Request) -> Response:
    cfg = settings()
    user_id = read_session_cookie(request.cookies.get(SESSION_COOKIE), cfg=cfg)
    if not user_id:
        return _json({"ok": True, "signed_in": False, "owner_hint": bool(os.environ.get("BRAIN_OWNER_EMAIL"))})
    ctx = _context(request)
    profile = E.op_profile(ctx)
    first_run = profile.data["library_counts"]["claims"] == 0 and (
        profile.data["library_counts"]["skills"] == 0
    )
    return _json({"ok": True, "signed_in": True, "first_run": first_run, **profile.data})


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------
@endpoint
async def api_cards(request: Request) -> Response:
    ctx = _context(request)
    view = request.query_params.get("view", "everything")
    if view not in ("everything", "skills", "library", "archived"):
        view = "everything"
    return _result(E.op_list_cards(ctx, view))


@endpoint
async def api_search(request: Request) -> Response:
    ctx = _context(request)
    query = request.query_params.get("q", "")
    scope = request.query_params.get("scope", "all")
    if scope not in ("all", "claims", "skills"):
        scope = "all"
    _rate_limit(ctx, "search", {"q": query, "scope": scope})
    return _result(E.op_search(ctx, query, scope))


@endpoint
async def api_item(request: Request) -> Response:
    ctx = _context(request)
    return _result(E.op_fetch(ctx, request.path_params["item_id"]), status_on_error=404)


@endpoint
async def api_export(request: Request) -> Response:
    ctx = _context(request)
    fmt = request.query_params.get("format", "json")
    result = E.op_export(ctx, "markdown" if fmt == "markdown" else "json")
    media = "text/markdown" if result.data["format"] == "markdown" else "application/json"
    return Response(
        result.data["content"],
        media_type=f"{media}; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{result.data["filename"]}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# the Omni-Bar
# ---------------------------------------------------------------------------
@endpoint
async def api_omnibar(request: Request) -> Response:
    """Plan, then (separately) execute. The bar never mutates on one call."""
    ctx = _context(request, surface="web_omnibar")
    payload = await _body(request)
    text = str(payload.get("text") or "")
    submitted = bool(payload.get("submitted"))
    if submitted:
        _require_csrf(request)
        _rate_limit(ctx, "omnibar", {"text": text})
    plan = omnibar.route(ctx, text, submitted=submitted, cfg=ctx.cfg)
    return _json({"ok": True, "plan": plan.as_dict()})


@endpoint
async def api_omnibar_run(request: Request) -> Response:
    ctx = _context(request, surface="web_omnibar")
    _require_csrf(request)
    payload = await _body(request)
    plan_data = payload.get("plan") or {}
    if not isinstance(plan_data, dict) or not plan_data.get("mode"):
        raise ApiError(400, "bad_plan", "No plan was supplied.")
    plan = omnibar.OmnibarPlan(
        mode=str(plan_data.get("mode")),
        text=str(plan_data.get("text") or ""),
        scope=str(plan_data.get("scope") or "all"),
        action=str(plan_data.get("action") or ""),
        target_kind=str(plan_data.get("target_kind") or ""),
        target_id=str(plan_data.get("target_id") or ""),
        target_label=str(plan_data.get("target_label") or ""),
        params=plan_data.get("params") or {},
    )
    _rate_limit(ctx, "omnibar_run", {"action": plan.action, "id": plan.target_id},
                ingestion=plan.mode == omnibar.MODE_INGEST)
    receipt, _result_obj = omnibar.execute(ctx, plan)
    return _json({"ok": receipt.ok, "receipt": receipt.as_dict()}, 200 if receipt.ok else 400)


@endpoint
async def api_omnibar_undo(request: Request) -> Response:
    ctx = _context(request, surface="web_omnibar")
    _require_csrf(request)
    payload = await _body(request)
    receipt = omnibar.undo(
        ctx, str(payload.get("action") or ""), payload.get("params") or {}
    )
    return _json({"ok": receipt.ok, "receipt": receipt.as_dict()}, 200 if receipt.ok else 400)


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------
@endpoint
async def api_ingest(request: Request) -> Response:
    ctx = _context(request, surface="web_ui")
    _require_csrf(request)
    payload = await _body(request)
    url = str(payload.get("url") or "")
    _rate_limit(ctx, "ingest", {"url": url}, ingestion=True)
    return _result(E.op_ingest_source(ctx, url, payload.get("user_note")))


@endpoint
async def api_upload(request: Request) -> Response:
    ctx = _context(request, surface="web_ui")
    _require_csrf(request)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise ApiError(400, "no_file", "No file was attached.")
    _rate_limit(ctx, "upload", {"name": upload.filename}, ingestion=True)

    # `R-17`: this is a staging folder, not storage. The bytes are kept for
    # `cfg.upload_retention_days` so a failed read can be looked at, then
    # swept by `app/storage/retention.py`.
    staging = ctx.cfg.uploads_dir_for(ctx.user_id)
    staging.mkdir(parents=True, exist_ok=True)
    safe_name = Path(str(upload.filename)).name  # strip any path the client sent
    target = staging / f"{control_db.new_id('up')}_{safe_name}"
    written = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 256):
            written += len(chunk)
            if written > ctx.cfg.max_upload_bytes:
                handle.close()
                target.unlink(missing_ok=True)
                raise ApiError(
                    413,
                    "file_too_large",
                    f"That file is over the {ctx.cfg.max_upload_bytes // (1024 * 1024)}MB cap.",
                )
            handle.write(chunk)
    return _result(E.op_ingest_file(ctx, target, filename=safe_name))


@endpoint
async def api_jobs(request: Request) -> Response:
    ctx = _context(request)
    return _result(E.op_get_ingest_status(ctx, request.query_params.get("job_id")))


@endpoint
async def api_job_run(request: Request) -> Response:
    """Push the queue forward from the browser.

    Cloud VMs keep no daemon alive between prompts, so the page drives the
    pipeline itself; in production `app/worker.py` does this instead.
    """
    ctx = _context(request)
    _require_csrf(request)
    processed = E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg, limit=3)
    return _json({"ok": True, "processed": processed})


@endpoint
async def api_counts(request: Request) -> Response:
    """§5: "Counts live in the navigation and nowhere else." One read, so the
    sidebar does not fetch six lists to draw six numbers."""
    ctx = _context(request)
    _rate_limit(ctx, "counts", {}, repeatable=True)
    from app.db import tenant as tenant_db

    return _json({"ok": True, "counts": tenant_db.counts(ctx.tenant)})


@endpoint
async def api_sources(request: Request) -> Response:
    ctx = _context(request)
    _rate_limit(ctx, "list_sources", {}, repeatable=True)
    return _result(E.op_list_sources(ctx))


@endpoint
async def api_source(request: Request) -> Response:
    """§7.3 in one read."""
    ctx = _context(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "get_source", {"source_id": source_id}, repeatable=True)
    return _result(E.op_get_source(ctx, source_id), status_on_error=404)


@endpoint
async def api_transcripts(request: Request) -> Response:
    ctx = _context(request)
    _rate_limit(ctx, "list_transcripts", {}, repeatable=True)
    return _result(E.op_list_transcripts(ctx))


@endpoint
async def api_transcript(request: Request) -> Response:
    ctx = _context(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "get_transcript", {"source_id": source_id}, repeatable=True)
    return _result(E.op_get_transcript(ctx, source_id), status_on_error=404)


@endpoint
async def api_archive_list(request: Request) -> Response:
    """§7.12's list. Web only — the engine refuses it to an agent.

    Named apart from `api_archive`, which archives ONE item by POST. Two
    functions of the same name in one module is not an error: Python keeps the
    second, and the route silently binds to whichever was defined last. Here
    that pointed a GET at a handler which demands a CSRF token, so the list
    answered 403 and was never reachable at all.
    """
    ctx = _context(request)
    _rate_limit(ctx, "list_archive", {}, repeatable=True)
    return _result(E.op_list_archive(ctx))


@endpoint
async def api_rules(request: Request) -> Response:
    ctx = _context(request)
    _rate_limit(ctx, "list_rules", {}, repeatable=True)
    return _result(E.op_list_rules(ctx))


@endpoint
async def api_skills(request: Request) -> Response:
    """§7.9's three sections and the review count."""
    ctx = _context(request)
    _rate_limit(ctx, "list_skills", {}, repeatable=True)
    return _result(E.op_list_skills(ctx))


@endpoint
async def api_review_skill(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    skill_id = request.path_params["skill_id"]
    _rate_limit(
        ctx, "review_skill", {"skill_id": skill_id, "decision": body.get("decision")}
    )
    return _result(E.op_review_skill(ctx, skill_id, str(body.get("decision") or "")))


@endpoint
async def api_skill_state(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    skill_id = request.path_params["skill_id"]
    # The state is what makes two of these different: without it, a person
    # switching a skill off and straight back on inside the loop breaker's
    # 15-second window was refused as a runaway agent.
    _rate_limit(ctx, "set_skill_state", {"skill_id": skill_id, "state": body.get("state")})
    return _result(E.op_set_skill_state(ctx, skill_id, str(body.get("state") or "")))


@endpoint
async def api_write_rule(request: Request) -> Response:
    """§7.8's `+ Write a rule`.

    The same operation the MCP tool calls, over the web surface — the engine
    records which surface it came from, which is how §7.8 can split
    `My rules` from `From your sources` without a second column.
    """
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    _rate_limit(ctx, "remember_this", {"claim": str(body.get("claim") or "")[:80]})
    tags = body.get("domain_tags") or []
    return _result(
        E.op_remember_this(
            ctx,
            claim=str(body.get("claim") or ""),
            when_it_applies=str(body.get("when_it_applies") or ""),
            when_it_fails=str(body.get("when_it_fails") or ""),
            metrics_and_quotes=str(body.get("metrics_and_quotes") or ""),
            domain_tags=[str(tag) for tag in tags] if isinstance(tags, list) else [],
        ),
        status_on_error=400,
    )


@endpoint
async def api_rename_source(request: Request) -> Response:
    """§7.4's overflow `Rename`."""
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    source_id = request.path_params["source_id"]
    _rate_limit(
        ctx, "rename_source", {"source_id": source_id, "title": body.get("title")}
    )
    return _result(
        E.op_rename_source(ctx, source_id, str(body.get("title") or "")),
        status_on_error=404,
    )


@endpoint
async def api_delete_upload(request: Request) -> Response:
    """§7.3's `Delete the file now` — `R-18`'s third retention control."""
    ctx = _context(request, surface="web_ui")
    _require_csrf(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "delete_upload", {"source_id": source_id})
    return _result(E.op_delete_upload(ctx, source_id), status_on_error=404)


@endpoint
async def api_reprocess_source(request: Request) -> Response:
    """§7.3's overflow `Re-process`: read the same address again."""
    ctx = _context(request)
    _require_csrf(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "reprocess_source", {"source_id": source_id})
    return _result(E.op_reprocess_source(ctx, source_id))


@endpoint
async def api_generate_summary(request: Request) -> Response:
    """§7.4's overflow `Generate a summary`, from the text already stored."""
    ctx = _context(request)
    _require_csrf(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "generate_summary", {"source_id": source_id})
    return _result(E.op_generate_summary(ctx, source_id))


@endpoint
async def api_summaries(request: Request) -> Response:
    """§7.6's list."""
    ctx = _context(request)
    _rate_limit(ctx, "list_summaries", {}, repeatable=True)
    return _result(E.op_list_summaries(ctx))


@endpoint
async def api_summary(request: Request) -> Response:
    ctx = _context(request)
    summary_id = request.path_params["summary_id"]
    _rate_limit(ctx, "get_summary", {"summary_id": summary_id}, repeatable=True)
    return _result(E.op_get_summary(ctx, summary_id=summary_id), status_on_error=404)


@endpoint
async def api_source_summary(request: Request) -> Response:
    """The same summary reached from §7.3's source detail."""
    ctx = _context(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "get_summary", {"source_id": source_id}, repeatable=True)
    return _result(E.op_get_summary(ctx, source_id=source_id), status_on_error=404)


@endpoint
async def api_source_progress(request: Request) -> Response:
    """§9.5 `GET /api/sources/:id/job` — step states, for the work block."""
    ctx = _context(request)
    source_id = request.path_params["source_id"]
    _rate_limit(ctx, "source_progress", {"source_id": source_id}, repeatable=True)
    return _result(E.op_source_progress(ctx, source_id), status_on_error=404)


@endpoint
async def api_job_rescue(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    return _result(
        E.op_rescue_job(ctx, request.path_params["job_id"], str(payload.get("text") or ""))
    )


# ---------------------------------------------------------------------------
# mutations on cards
# ---------------------------------------------------------------------------
@endpoint
async def api_archive(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    item_type = request.path_params["item_type"]
    # §7.12's list emits `rule` and `summary` as well — the words on the page.
    # The MCP tool's signature keeps its three; the engine maps both.
    if item_type not in E._ARCHIVABLE:
        raise ApiError(
            400, "bad_item_type", "item_type must be source, claim (or rule), skill or summary."
        )
    return _result(
        E.op_archive_item(
            ctx, item_type, request.path_params["item_id"], restore=bool(payload.get("restore"))
        ),
        status_on_error=404,
    )


@endpoint
async def api_delete(request: Request) -> Response:
    """The one destructive endpoint.

    Gated on the thing already being set aside, and unreachable over MCP —
    the engine refuses an agent surface outright, not merely by having no
    tool that wraps this.
    """
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    result = E.op_permanent_delete(
        ctx,
        request.path_params["item_id"],
        str(payload.get("item_type") or "") or None,
    )
    # A missing id is a 404; an item that exists but has not been set aside is
    # a 400, because the client asked for something it is not allowed to do yet.
    return _result(result, status_on_error=404 if result.error_reason == "not_found" else 400)


@endpoint
async def api_delete_library(request: Request) -> Response:
    """§7.13's `Delete library`, gated on the typed word by the engine too."""
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    result = E.op_delete_library(ctx, typed=str(payload.get("typed") or ""))
    return _result(result, status_on_error=403 if result.error_reason == "person_only" else 400)


@endpoint
async def api_delete_account(request: Request) -> Response:
    """§7.13's `Delete account`. The session it was called with dies with it."""
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    result = E.op_delete_account(ctx, typed=str(payload.get("typed") or ""))
    if not result.ok:
        return _result(
            result, status_on_error=403 if result.error_reason == "person_only" else 400
        )
    # The account is gone, so the cookie that named it is a cookie pointing at
    # nothing: cleared here rather than leaving the browser to discover a 401.
    response = _result(result)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@endpoint
async def api_update_claim(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    return _result(
        E.op_update_claim(
            ctx,
            request.path_params["claim_id"],
            claim=payload.get("claim"),
            when_it_applies=payload.get("when_it_applies"),
            when_it_fails=payload.get("when_it_fails"),
            metrics_and_quotes=payload.get("metrics_and_quotes"),
            domain_tags=payload.get("domain_tags"),
        ),
        status_on_error=404,
    )


@endpoint
async def api_save_skill(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    return _result(
        E.op_save_skill(
            ctx,
            slug=str(payload.get("slug") or ""),
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            trigger_when=str(payload.get("trigger_when") or ""),
            execution_steps=payload.get("execution_steps") or [],
            guardrails=str(payload.get("guardrails") or ""),
            output_template=str(payload.get("output_template") or ""),
            linked_claim_ids=payload.get("linked_claim_ids") or [],
        )
    )


@endpoint
async def api_revert_skill(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    payload = await _body(request)
    return _result(
        E.op_revert_skill(ctx, request.path_params["skill_id"], int(payload.get("version") or 1))
    )


@endpoint
async def api_synthesize(request: Request) -> Response:
    """The zero-match state's one-click draft."""
    ctx = _context(request)
    payload = await _body(request)
    return _result(
        E.op_get_or_synthesize_skill(ctx, str(payload.get("task") or ""), payload.get("slug"))
    )


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
@endpoint
async def api_sync_watermark(request: Request) -> Response:
    """A cheap "has anything changed?" probe for a tab that regains focus.

    An agent writing through `/mcp` is invisible to an open browser tab, so the
    tab would quietly show a stale library for as long as it stays open. This
    is deliberately the cheapest thing that answers the question: one count and
    one max() per content table, no rows, no rendering. The tab compares it to
    what it last saw and only re-fetches when it differs.
    """
    ctx = _context(request)
    return _json({"ok": True, **E.tenant_watermark(ctx)})


@endpoint
async def api_profile(request: Request) -> Response:
    ctx = _context(request)
    if request.method == "GET":
        return _result(E.op_profile(ctx))
    _require_csrf(request)
    payload = await _body(request)
    def flag(name: str) -> bool | None:
        value = payload.get(name)
        return None if value is None else bool(value)

    return _result(
        E.op_update_profile(
            ctx,
            about_you_context=payload.get("about_you_context"),
            theme_mode=payload.get("theme_mode"),
            display_name=payload.get("display_name"),
            locale=payload.get("locale"),
            want_summary=flag("want_summary"),
            want_rules=flag("want_rules"),
            want_skills=flag("want_skills"),
            keep_uploads=flag("keep_uploads"),
            write_in=payload.get("write_in"),
        )
    )


@endpoint
async def api_add_instruction(request: Request) -> Response:
    """§7.2's interjection. Appends to the instruction; cancels nothing."""
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    source_id = request.path_params["source_id"]
    text = str(body.get("text") or "")
    _rate_limit(ctx, "add_instruction", {"source_id": source_id, "text": text[:60]})
    return _result(E.op_add_instruction(ctx, source_id, text), status_on_error=400)


@endpoint
async def api_activity(request: Request) -> Response:
    """§7.10's two sections in one read — meters and the call log."""
    ctx = _context(request)
    _rate_limit(ctx, "activity", {}, repeatable=True)
    return _result(E.op_activity(ctx))


@endpoint
async def api_connections(request: Request) -> Response:
    """§7.11's list. One server, two ways in, one page."""
    ctx = _context(request)
    _rate_limit(ctx, "list_connections", {}, repeatable=True)
    return _result(E.op_list_connections(ctx))


@endpoint
async def api_create_connection(request: Request) -> Response:
    """§7.11's `Generate my link`. The key is in this response and nowhere
    else — only its digest is stored."""
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    _rate_limit(ctx, "create_connection", {"name": str(body.get("name") or "")})
    return _result(E.op_create_connection(ctx, str(body.get("name") or "My AI")))


@endpoint
async def api_connection_action(request: Request) -> Response:
    """§7.11's `⋯` — Rename, Roll the key, Revoke."""
    ctx = _context(request)
    _require_csrf(request)
    body = await _body(request)
    connection_id = request.path_params["connection_id"]
    action = request.path_params["action"]
    if action not in ("rename", "roll", "revoke"):
        raise ApiError(400, "bad_action", "action must be rename, roll or revoke.")
    _rate_limit(ctx, f"connection_{action}", {"connection_id": connection_id})
    if action == "rename":
        result = E.op_rename_connection(ctx, connection_id, str(body.get("name") or ""))
    elif action == "roll":
        result = E.op_roll_connection_key(ctx, connection_id)
    else:
        result = E.op_revoke_connection(ctx, connection_id)
    return _result(result, status_on_error=404)


@endpoint
async def api_audit_logs(request: Request) -> Response:
    """Read-only. The owner's own rows only — `_context` resolves the tenant."""
    ctx = _context(request)
    try:
        limit = int(request.query_params.get("limit", 40))
    except ValueError:
        limit = 40
    include_reads = request.query_params.get("include_reads") == "1"
    return _result(E.op_audit_log(ctx, limit=limit, include_reads=include_reads))


@endpoint
async def api_revoke_session(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    session_id = request.path_params["session_id"]
    owned = any(
        record["session_id"] == session_id
        for record in control_db.list_sessions(ctx.control, ctx.user_id)
    )
    if not owned:
        raise ApiError(404, "not_found", "No such connected client.")
    control_db.revoke_session(ctx.control, session_id)
    ctx.audit("revoke_session", "ok", target_id=session_id)
    return _result(E.op_profile(ctx))


# ---------------------------------------------------------------------------
# demo data
# ---------------------------------------------------------------------------
@endpoint
async def api_demo(request: Request) -> Response:
    ctx = _context(request)
    _require_csrf(request)
    action = request.path_params["action"]
    if action == "seed":
        return _result(E.op_seed_demo(ctx))
    if action == "clear":
        return _result(E.op_clear_demo(ctx))
    raise ApiError(400, "bad_action", "action must be seed or clear.")


# ---------------------------------------------------------------------------
# static
# ---------------------------------------------------------------------------
def _static(filename: str, media_type: str) -> Callable:
    async def serve(request: Request) -> Response:
        path = STATIC_DIR / filename
        if not path.is_file():
            return _json({"ok": False, "reason": "missing_asset", "message": filename}, 404)
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "no-cache" if settings().is_test else "max-age=300"},
        )

    serve.__name__ = f"static_{filename.replace('.', '_')}"
    return serve


async def ui(request: Request) -> Response:
    """Serve the shell for every one of §5's routes.

    A pushState router owns the path, so `/rules` must return the shell
    rather than a 404 — otherwise a deep link works until someone reloads it.

    `R-19` removed the `BRAIN_NEXT_UI` gate this sat behind. It was right
    while two apps were being served and the redesign was half-built; now
    there is one app, and a flag that can turn the whole product off is a
    way to ship an outage, not a safety feature.
    """
    path = STATIC_DIR / "shell.html"
    if not path.is_file():
        return _json({"ok": False, "reason": "missing_asset", "message": "shell.html"}, 404)
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


#: §5's fourteen routes, which `R-19` moved to the root.
#:
#: Declared one by one rather than as `{path:path}`. A catch-all would trip
#: the BOLA guard in `tests/test_phase6_security_and_live_providers.py` —
#: correctly, since a path parameter is the shape of a traversal bug — and it
#: would make every typo render a page instead of a 404. Server and router
#: agree on exactly which URLs exist.
UI_PATHS = (
    "/",
    "/library/transcripts",
    "/library/summaries",
    "/library/sources",
    "/transcripts/{id}",
    "/summaries/{id}",
    "/sources/{id}",
    "/rules",
    "/skills",
    "/activity",
    "/connections",
    "/archive",
    "/profile",
    "/signin",
)


async def moved_to_root(request: Request) -> Response:
    """`R-19`: `/next/...` is where this app lived while `/` was still the old
    one. The prefix is gone, but a bookmark made during that time should not
    land on a 404.

    302, not 301: a permanent redirect is cached by browsers indefinitely,
    and if this cutover ever has to be undone, an uncachable mistake is the
    cheaper one to have made.
    """
    rest = request.path_params.get("rest", "")
    target = "/" + rest.lstrip("/")
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=302)


ROUTES = [
    *[Route(path, ui, methods=["GET"]) for path in UI_PATHS],
    Route("/next", moved_to_root, methods=["GET"]),
    Route("/next/{rest:path}", moved_to_root, methods=["GET"]),
    Route("/tokens.css", _static("tokens.css", "text/css; charset=utf-8"), methods=["GET"]),
    Route("/atoms.css", _static("atoms.css", "text/css; charset=utf-8"), methods=["GET"]),
    Route("/molecules.css", _static("molecules.css", "text/css; charset=utf-8"), methods=["GET"]),
    Route("/frames.css", _static("frames.css", "text/css; charset=utf-8"), methods=["GET"]),
    Route("/frames.js", _static("frames.js", "application/javascript; charset=utf-8"), methods=["GET"]),
    #: The OAuth consent card's own sheet. It used to share `/app.css` with
    #: the control plane; that file is gone, so it carries the few rules it
    #: needs on top of the same tokens every other page uses.
    Route("/consent.css", _static("consent.css", "text/css; charset=utf-8"), methods=["GET"]),
    Route("/consent.js", _static("consent.js", "application/javascript; charset=utf-8"), methods=["GET"]),
    Route("/api/login", api_login, methods=["POST"]),
    Route("/api/logout", api_logout, methods=["POST"]),
    Route("/api/me", api_me, methods=["GET"]),
    Route("/api/cards", api_cards, methods=["GET"]),
    Route("/api/search", api_search, methods=["GET"]),
    Route("/api/item/{item_id}", api_item, methods=["GET"]),
    Route("/api/export", api_export, methods=["GET"]),
    Route("/api/omnibar", api_omnibar, methods=["POST"]),
    Route("/api/omnibar/run", api_omnibar_run, methods=["POST"]),
    Route("/api/omnibar/undo", api_omnibar_undo, methods=["POST"]),
    Route("/api/ingest", api_ingest, methods=["POST"]),
    Route("/api/upload", api_upload, methods=["POST"]),
    Route("/api/jobs", api_jobs, methods=["GET"]),
    Route("/api/jobs/run", api_job_run, methods=["POST"]),
    Route("/api/jobs/{job_id}/rescue", api_job_rescue, methods=["POST"]),
    Route("/api/sources/{source_id}/job", api_source_progress, methods=["GET"]),
    Route("/api/sources/{source_id}/summary", api_source_summary, methods=["GET"]),
    Route("/api/counts", api_counts, methods=["GET"]),
    Route("/api/sources", api_sources, methods=["GET"]),
    Route("/api/sources/{source_id}", api_source, methods=["GET"]),
    Route("/api/transcripts", api_transcripts, methods=["GET"]),
    Route("/api/transcripts/{source_id}", api_transcript, methods=["GET"]),
    Route("/api/rules", api_rules, methods=["GET"]),
    Route("/api/rule", api_write_rule, methods=["POST"]),
    Route("/api/archive", api_archive_list, methods=["GET"]),
    Route("/api/skills", api_skills, methods=["GET"]),
    Route("/api/skills/{skill_id}/review", api_review_skill, methods=["POST"]),
    Route("/api/skills/{skill_id}/state", api_skill_state, methods=["POST"]),
    Route("/api/summaries", api_summaries, methods=["GET"]),
    Route("/api/summaries/{summary_id}", api_summary, methods=["GET"]),
    Route("/api/sources/{source_id}/rename", api_rename_source, methods=["POST"]),
    Route("/api/sources/{source_id}/file", api_delete_upload, methods=["DELETE"]),
    Route("/api/sources/{source_id}/reprocess", api_reprocess_source, methods=["POST"]),
    Route("/api/sources/{source_id}/summarise", api_generate_summary, methods=["POST"]),
    Route("/api/archive/{item_type}/{item_id}", api_archive, methods=["POST"]),
    Route("/api/delete/{item_id}", api_delete, methods=["POST"]),
    Route("/api/delete-library", api_delete_library, methods=["POST"]),
    Route("/api/delete-account", api_delete_account, methods=["POST"]),
    Route("/api/claim/{claim_id}", api_update_claim, methods=["POST"]),
    Route("/api/skill", api_save_skill, methods=["POST"]),
    Route("/api/skill/{skill_id}/revert", api_revert_skill, methods=["POST"]),
    Route("/api/synthesize", api_synthesize, methods=["POST"]),
    Route("/api/profile", api_profile, methods=["GET", "POST"]),
    Route("/api/sync-watermark", api_sync_watermark, methods=["GET"]),
    Route("/api/sources/{source_id}/instruct", api_add_instruction, methods=["POST"]),
    Route("/api/activity", api_activity, methods=["GET"]),
    Route("/api/connections", api_connections, methods=["GET"]),
    Route("/api/connections", api_create_connection, methods=["POST"]),
    Route("/api/connections/{connection_id}/{action}", api_connection_action, methods=["POST"]),
    Route("/api/audit-logs", api_audit_logs, methods=["GET"]),
    Route("/api/sessions/{session_id}/revoke", api_revoke_session, methods=["POST"]),
    Route("/api/demo/{action}", api_demo, methods=["POST"]),
]
