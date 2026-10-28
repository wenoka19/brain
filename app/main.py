"""FastAPI/Starlette host: OAuth endpoints + the `/mcp` Streamable HTTP server.

Phase 3 scope. The web control plane (`app/static/index.html`, `app/omnibar.py`)
mounts here in Phases 4 & 5, once Co-Design Checkpoint B is settled — nothing
about its layout is decided in this file.
"""

from __future__ import annotations

import contextlib
import os
from urllib.parse import urlsplit
from typing import AsyncIterator

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import auth
from app.config import Settings, settings
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.mcp_server import server as mcp_server

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
        async with streamable.router.lifespan_context(streamable):
            yield
        tenant_db.close_all()
        control_db.close_all()

    app = FastAPI(
        title="brain",
        version="0.1.0",
        docs_url=None,  # no interactive docs on a personal server
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        routes=list(auth.ROUTES),
    )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok", "env": cfg.env, "version": "0.1.0"})

    # The MCP transport owns everything under /mcp.
    app.mount("/", streamable)
    return app


app = create_app()
