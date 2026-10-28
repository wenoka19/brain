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


#: The surface an external MCP client talks to. CORS is scoped to exactly these
#: prefixes, and deliberately not to `/api/*`: the control plane authenticates
#: with a `SameSite=Strict` cookie plus a double-submit CSRF header, and
#: advertising it as cross-origin-callable would undo that for no gain. An MCP
#: client authenticates with a bearer token and needs no cookie.
MCP_CORS_PREFIXES = (
    "/mcp",
    "/.well-known/",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
)

#: Request headers a client may send. `Mcp-Session-Id` is the Streamable HTTP
#: session handle; without it in this list a browser blocks the second request
#: of every session.
CORS_ALLOW_HEADERS = (
    "Authorization",
    "Content-Type",
    "Accept",
    "Mcp-Session-Id",
    "Mcp-Protocol-Version",
    "Last-Event-ID",
)

#: Response headers the browser must let the client *read*. Both are load
#: bearing: without `WWW-Authenticate` the client cannot discover where to
#: authenticate and the OAuth dance never starts, and without `Mcp-Session-Id`
#: it cannot continue the session it just opened.
CORS_EXPOSE_HEADERS = ("WWW-Authenticate", "Mcp-Session-Id")

CORS_METHODS = ("GET", "POST", "DELETE", "OPTIONS")

#: Headers that stop an intermediary from buffering or rewriting an SSE stream.
#: Railway fronts the app with a proxy, and a buffered stream looks to the
#: client exactly like a hung tool call — the response arrives only when the
#: whole thing is done, or never.
STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


class StreamPassthroughMiddleware(BaseHTTPMiddleware):
    """Keep `/mcp` responses unbuffered and unrewritten end to end."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/mcp"):
            for header, value in STREAM_HEADERS.items():
                response.headers[header] = value
        return response


def allowed_origins(cfg: Settings | None = None) -> list[str]:
    """The Origin policy for the MCP surface. `["*"]` means unrestricted.

    Default unrestricted, deliberately. `allowed_origins` exists to stop a
    drive-by page in someone's browser from driving a *local* MCP server, and
    that attack works because the browser attaches credentials by itself.
    Nothing on `/mcp` authenticates that way: it takes a bearer token the page
    does not have and cannot read. Meanwhile a strict list has to name every
    client shell that will ever connect — `https://claude.ai`, a Cursor
    webview, a desktop app's custom scheme — and gets each new one wrong as a
    403 nobody can diagnose.

    The real DNS-rebinding defence is the **Host** allow-list, which stays
    strict; see `transport_security`. Set `BRAIN_ALLOWED_ORIGINS` to a
    comma-separated list to restrict anyway.
    """
    cfg = cfg or settings()
    raw = (os.environ.get("BRAIN_ALLOWED_ORIGINS") or "*").strip()
    if raw == "*":
        return ["*"]
    listed = [item.strip() for item in raw.split(",") if item.strip()]
    public = urlsplit(os.environ.get("BRAIN_PUBLIC_URL") or auth.issuer(cfg))
    if public.scheme and public.netloc:
        listed.append(f"{public.scheme}://{public.netloc}")
    return sorted(set(listed))


class OriginPolicyMiddleware:
    """Apply *our* Origin policy before the transport applies its own.

    The SDK's check has no wildcard — an Origin it was not told about is a 403,
    and it treats an absent Origin as same-origin and allows it. So when the
    policy is unrestricted, the header is dropped here and the SDK's check
    becomes a no-op; when it is a list, the header is left alone and the SDK
    enforces it. Either way the Host check behind this is untouched, and the
    browser still gets its `Access-Control-Allow-Origin` from
    `McpCorsMiddleware`, which runs further out.

    Plain ASGI rather than `BaseHTTPMiddleware` because it has to mutate the
    request scope the transport will read.
    """

    def __init__(self, app, origins: list[str]) -> None:
        self.app = app
        self.unrestricted = origins == ["*"]

    async def __call__(self, scope, receive, send):
        if self.unrestricted and scope.get("type") == "http":
            headers = [
                (name, value)
                for name, value in scope.get("headers", [])
                if name.lower() != b"origin"
            ]
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


class McpCorsMiddleware(BaseHTTPMiddleware):
    """CORS for the MCP and OAuth endpoints only.

    `Access-Control-Allow-Origin` is echoed rather than `*` because a client
    may legitimately send `Origin: null` (a file:// desktop shell) and because
    `*` would be incompatible with ever allowing credentials. Credentials stay
    off: nothing on this surface authenticates with a cookie.
    """

    def __init__(self, app, cfg: Settings) -> None:
        super().__init__(app)
        self.cfg = cfg
        self.origins = allowed_origins(cfg)

    def _permitted(self, origin: str) -> bool:
        return self.origins == ["*"] or origin in self.origins

    def _headers(self, origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Methods": ", ".join(CORS_METHODS),
            "Access-Control-Allow-Headers": ", ".join(CORS_ALLOW_HEADERS),
            "Access-Control-Expose-Headers": ", ".join(CORS_EXPOSE_HEADERS),
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    async def dispatch(self, request: Request, call_next) -> Response:
        origin = request.headers.get("origin") or ""
        in_scope = request.url.path.startswith(MCP_CORS_PREFIXES) and self._permitted(origin)
        if request.method == "OPTIONS" and in_scope and origin:
            # Answer the preflight here. Routing it onward would 405, because
            # none of these endpoints declares OPTIONS.
            return Response(status_code=204, headers=self._headers(origin))
        response = await call_next(request)
        if in_scope and origin:
            for header, value in self._headers(origin).items():
                response.headers[header] = value
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

    policy = allowed_origins(cfg)
    if policy != ["*"]:
        origins.extend(policy)

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
    mounted = OriginPolicyMiddleware(streamable, allowed_origins(cfg))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        control_db.control()  # create/migrate control.db before serving
        r2_sync.start_background(cfg=cfg)  # no-op unless R2 is configured
        try:
            async with streamable.router.lifespan_context(streamable):
                yield
        finally:
            # `try/finally`, not statements after the `yield`: a redeploy's
            # SIGTERM is delivered as a cancellation *at* the yield, and
            # anything written after it would simply never run. Everything
            # marked dirty inside the debounce window is uploaded here,
            # synchronously, before the process is allowed to exit.
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
    app.add_middleware(StreamPassthroughMiddleware)
    app.add_middleware(McpCorsMiddleware, cfg=cfg)
    # Added last, so it runs first: a replayed request should not cost
    # this node a routing pass it is going to throw away.
    app.add_middleware(ShardRoutingMiddleware, cfg=cfg)

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok", "env": cfg.env, "version": "0.1.0"})

    # The MCP transport owns everything under /mcp.
    app.mount("/", mounted)
    return app


app = create_app()
