"""FastAPI/Starlette host: the web control plane, the OAuth endpoints and the
`/mcp` Streamable HTTP server, behind one strict set of security headers.

Route ownership, outermost first:

* `/` `/app.css` `/app.js` `/preview` `/api/*` — the Web Control Plane
* `/authorize` `/token` `/register` `/revoke` `/.well-known/*` — OAuth 2.1
* everything under `/mcp` — the MCP Streamable HTTP transport
"""

from __future__ import annotations

import contextlib
import logging
import os
from urllib.parse import urlsplit
from typing import AsyncIterator

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import auth, webapi
from app.config import Settings, settings
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.mcp_server import server as mcp_server
from app.storage import r2_sync

LOGGER = logging.getLogger("brain.main")

#: A JSON/MCP API plus a self-contained control plane: no third-party scripts,
#: no framing, no inline event handlers.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


#: Paths that belong to no tenant: discovery, sign-in, health, static assets.
#: They are served by whichever node receives them.
SHARD_EXEMPT_PREFIXES = ("/healthz", "/.well-known/", "/authorize", "/token", "/register", "/revoke")


def request_tenant(request: Request, cfg: Settings) -> str | None:
    """The tenant a request acts on, or `None` if it cannot be told.

    Both identities are signature-verified before they are trusted, so a
    client cannot steer itself onto the wrong shard by inventing a header.
    """
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        try:
            return auth.verify_access_token(value.strip(), cfg=cfg).user_id
        except Exception:
            return None
    return webapi.read_session_cookie(request.cookies.get(webapi.SESSION_COOKIE), cfg=cfg)


class ShardRoutingMiddleware(BaseHTTPMiddleware):
    """Send a request to the node that owns its tenant.

    Every node computes the same owner from `crc32(user_id) % shard_total`,
    with no shared state and no lookup table — which is what makes adding a
    shard a redeploy rather than a migration. At `BRAIN_SHARD_TOTAL=1`
    (Stage 1, and every local run) `dispatch` falls straight through.
    """

    def __init__(self, app, cfg: Settings) -> None:
        super().__init__(app)
        self.cfg = cfg

    async def dispatch(self, request: Request, call_next) -> Response:
        cfg = self.cfg
        if not cfg.sharded or request.url.path.startswith(SHARD_EXEMPT_PREFIXES):
            return await call_next(request)

        user_id = request_tenant(request, cfg)
        if not user_id or cfg.owns_shard(user_id):
            return await call_next(request)

        target = cfg.shard_for(user_id)
        kind, address = cfg.shard_address(target)
        if kind == "instance":
            # Fly's proxy replays the request on the named machine. 204 so a
            # client that ignores the header gets nothing, rather than another
            # shard's answer.
            return Response(status_code=204, headers={"fly-replay": f"instance={address}"})
        if kind == "url":
            location = f"{address}{request.url.path}"
            if request.url.query:
                location = f"{location}?{request.url.query}"
            return Response(status_code=307, headers={"Location": location})

        # Sharding is on but shard `target` has no address configured. Serving
        # it here is still correct data — any node can hydrate any tenant from
        # R2 — but two nodes writing one tenant is last-snapshot-wins, so this
        # is a misconfiguration to fix, not a mode to run in.
        LOGGER.warning(
            "tenant %s belongs to shard %s, which has no BRAIN_SHARD_URLS/"
            "BRAIN_SHARD_INSTANCES entry; serving locally",
            user_id,
            target,
        )
        return await call_next(request)


def transport_security(cfg: Settings | None = None) -> TransportSecuritySettings:
    """Host/Origin allow-list for the `/mcp` transport (DNS-rebinding defence).

    The guard is on by default and answers `421` for any Host it was not told
    about, so the public hostname must be declared or a deployed `/mcp` refuses
    every request. Hosts come from `BRAIN_PUBLIC_URL` plus the optional
    `BRAIN_ALLOWED_HOSTS` (comma-separated).
    """
    cfg = cfg or settings()
    hosts: list[str] = ["127.0.0.1", "localhost"]
    origins: list[str] = []

    public = os.environ.get("BRAIN_PUBLIC_URL") or auth.issuer(cfg)
    parsed = urlsplit(public)
    if parsed.hostname:
        hosts.append(parsed.netloc)
        hosts.append(parsed.hostname)
        origins.append(f"{parsed.scheme}://{parsed.netloc}")

    for extra in (os.environ.get("BRAIN_ALLOWED_HOSTS") or "").split(","):
        extra = extra.strip()
        if extra:
            hosts.append(extra)

    if cfg.is_test:
        hosts.append("testserver")  # Starlette's TestClient Host header

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=sorted(set(origins)),
    )


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or settings()
    cfg.ensure_dirs()
    streamable = mcp_server(cfg).streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=transport_security(cfg),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        control_db.control()  # create/migrate control.db before serving
        r2_sync.start_background(cfg=cfg)  # no-op unless R2 is configured
        async with streamable.router.lifespan_context(streamable):
            yield
        # Flush before the connections go, so a rolling restart does not drop
        # the last debounce window of writes.
        r2_sync.stop_background(cfg=cfg)
        tenant_db.close_all()
        control_db.close_all()

    app = FastAPI(
        title="brain",
        version="0.1.0",
        docs_url=None,  # no interactive docs on a personal server
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        routes=list(auth.ROUTES) + list(webapi.ROUTES),
    )
    app.add_middleware(SecurityHeadersMiddleware)
    # Added last, so it runs first: a replayed request should not cost
    # this node a routing pass it is going to throw away.
    app.add_middleware(ShardRoutingMiddleware, cfg=cfg)

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok", "env": cfg.env, "version": "0.1.0"})

    # The MCP transport owns everything under /mcp.
    app.mount("/", streamable)
    return app


app = create_app()
