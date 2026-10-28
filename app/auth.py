"""OAuth 2.1 Authorization Server + Resource Server for `brain`.

What is enforced here (OWASP MCP01 / MCP07, RFC 8707, RFC 7636):

* **PKCE is mandatory** — `S256` only. `plain` is refused outright, and a code
  cannot be exchanged without the matching verifier.
* **Audience binding** — every access token carries `aud` = this resource
  server's identifier. A token minted for a different audience is refused, so a
  token stolen from another MCP server cannot be replayed here.
* **No token passthrough** — we only ever accept tokens we signed *and* whose
  SHA-256 hash is present in `control.db`. An upstream bearer token is never
  forwarded anywhere.
* **Hashes only** — `control.db` never holds a plaintext access token, refresh
  token or authorization code.
* **1h access / absolute 30-day refresh** — rotation issues a new refresh token
  but never extends the original 30-day ceiling, and revocation is one UPDATE.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlencode, urlsplit

import jwt
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.config import Settings, settings
from app.db import control as control_db

JWT_ALGORITHM = "HS256"
TOKEN_TYPE = "Bearer"
SUPPORTED_SCOPES = ("brain:read", "brain:write")
DEFAULT_SCOPE = "brain:read brain:write"


# ---------------------------------------------------------------------------
# signing key
# ---------------------------------------------------------------------------
def signing_key(cfg: Settings | None = None) -> str:
    """HS256 secret.

    In prod `BRAIN_JWT_SECRET` is required — a process-random fallback would
    silently invalidate every live session on restart. In test mode a fixed
    value keeps runs deterministic.
    """
    cfg = cfg or settings()
    secret = os.environ.get("BRAIN_JWT_SECRET")
    if secret:
        return secret
    if cfg.is_test:
        return "brain-test-signing-key-not-for-production"
    raise RuntimeError(
        "BRAIN_JWT_SECRET must be set in production; refusing to sign tokens "
        "with an ephemeral key."
    )


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def resource_identifier(cfg: Settings | None = None) -> str:
    cfg = cfg or settings()
    return cfg.audience


def issuer(cfg: Settings | None = None) -> str:
    """The public origin, always as a client will actually reach it.

    Behind Railway's or Nginx's TLS terminator the app itself speaks plain
    HTTP, so an operator who sets `BRAIN_PUBLIC_URL=http://...` would have
    every metadata document advertise `http://` endpoints — which a browser
    client on an https page refuses as mixed content, and which would put an
    authorization code on the wire in the clear. The scheme is upgraded here, in
    the one place every URL in the discovery documents is built from.
    """
    cfg = cfg or settings()
    base = (os.environ.get("BRAIN_PUBLIC_URL") or "").strip()
    if not base:
        base = resource_identifier(cfg).rsplit("/mcp", 1)[0] or "https://brain.local"
    base = base.rstrip("/")
    if base.startswith("http://"):
        host = urlsplit(base).hostname or ""
        # Loopback and a bare `testserver` are genuinely plain HTTP.
        # `testserver` is Starlette's TestClient host, and loopback is
        # genuinely plain HTTP. Everything else is behind a terminator.
        if host not in LOOPBACK_HOSTS and host != "testserver":
            base = "https://" + base[len("http://") :]
    return base


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------
def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_pkce(verifier: str, challenge: str, method: str = "S256") -> bool:
    if method != "S256":
        return False  # OAuth 2.1 removed `plain`
    if not verifier or not 43 <= len(verifier) <= 128:
        return False
    return hmac.compare_digest(pkce_challenge(verifier), challenge)


# ---------------------------------------------------------------------------
# JWT mint / verify
# ---------------------------------------------------------------------------
def mint_access_token(
    *,
    user_id: str,
    session_id: str,
    client_id: str,
    scope: str,
    audience: str,
    expires_at: int,
    cfg: Settings | None = None,
) -> str:
    cfg = cfg or settings()
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer(cfg),
            "sub": user_id,
            "aud": audience,
            "sid": session_id,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "nbf": now,
            "exp": expires_at,
            "jti": secrets.token_urlsafe(16),
        },
        signing_key(cfg),
        algorithm=JWT_ALGORITHM,
    )


@dataclass(frozen=True)
class VerifiedToken:
    user_id: str
    session_id: str
    client_id: str
    client_name: str
    scope: str
    audience: str
    expires_at: int


class TokenRejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def verify_access_token(token: str, *, cfg: Settings | None = None) -> VerifiedToken:
    """Signature + audience + expiry, **then** the database.

    Both halves are required. The signature proves we minted it; the database
    row proves it has not been revoked. Checking only the JWT would make
    revocation take an hour instead of a millisecond.
    """
    cfg = cfg or settings()
    audience = resource_identifier(cfg)
    try:
        claims = jwt.decode(
            token,
            signing_key(cfg),
            algorithms=[JWT_ALGORITHM],
            audience=audience,
            issuer=issuer(cfg),
            options={"require": ["exp", "sub", "aud", "iss", "sid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenRejected("token_expired") from exc
    except jwt.InvalidAudienceError as exc:
        # RFC 8707: a token minted for another resource server is not ours.
        raise TokenRejected("audience_mismatch", audience) from exc
    except jwt.InvalidTokenError as exc:
        raise TokenRejected("invalid_token", str(exc)) from exc

    conn = control_db.control()
    record = control_db.session_by_access_token(conn, token)
    if record is None:
        raise TokenRejected("unknown_token", "not issued by this server")
    if record["revoked"]:
        raise TokenRejected("revoked")
    if record["expired"]:
        raise TokenRejected("token_expired")
    if record["user_id"] != claims["sub"] or record["session_id"] != claims["sid"]:
        raise TokenRejected("token_mismatch")
    if record["audience"] and record["audience"] != audience:
        raise TokenRejected("audience_mismatch", record["audience"])
    if control_db.get_user(conn, record["user_id"]) is None:
        raise TokenRejected("unknown_user")

    return VerifiedToken(
        user_id=record["user_id"],
        session_id=record["session_id"],
        client_id=str(claims.get("client_id") or ""),
        client_name=record["client_name"],
        scope=str(claims.get("scope") or ""),
        audience=audience,
        expires_at=int(claims["exp"]),
    )


def issue_grant(
    conn,
    *,
    user_id: str,
    client_name: str,
    client_id: str,
    scope: str,
    resource: str,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    """Create a session, then wrap its opaque access token in a bound JWT."""
    cfg = cfg or settings()
    audience = resource or resource_identifier(cfg)
    grant = control_db.create_oauth_session(
        conn, user_id, client_name=client_name, audience=audience, cfg=cfg
    )
    # The JWT *is* the access token the client sends; the row stores its hash.
    access_jwt = mint_access_token(
        user_id=user_id,
        session_id=grant["session_id"],
        client_id=client_id,
        scope=scope,
        audience=audience,
        expires_at=int(time.time()) + cfg.access_token_ttl_sec,
        cfg=cfg,
    )
    conn.execute(
        "UPDATE oauth_sessions SET access_token_hash = ? WHERE session_id = ?",
        (control_db.hash_token(access_jwt), grant["session_id"]),
    )
    return {
        "access_token": access_jwt,
        "token_type": TOKEN_TYPE,
        "expires_in": cfg.access_token_ttl_sec,
        "refresh_token": grant["refresh_token"],
        "scope": scope,
        "session_id": grant["session_id"],
    }


def rotate_grant(conn, refresh_token: str, *, cfg: Settings | None = None) -> dict[str, Any] | None:
    """Rotate a refresh token. The absolute 30-day ceiling never moves."""
    cfg = cfg or settings()
    rotated = control_db.rotate_refresh_token(conn, refresh_token, cfg=cfg)
    if rotated is None:
        return None
    audience = rotated["audience"] or resource_identifier(cfg)
    access_jwt = mint_access_token(
        user_id=rotated["user_id"],
        session_id=rotated["session_id"],
        client_id="",
        scope=DEFAULT_SCOPE,
        audience=audience,
        expires_at=int(time.time()) + cfg.access_token_ttl_sec,
        cfg=cfg,
    )
    conn.execute(
        "UPDATE oauth_sessions SET access_token_hash = ? WHERE session_id = ?",
        (control_db.hash_token(access_jwt), rotated["session_id"]),
    )
    return {
        "access_token": access_jwt,
        "token_type": TOKEN_TYPE,
        "expires_in": cfg.access_token_ttl_sec,
        "refresh_token": rotated["refresh_token"],
        "scope": DEFAULT_SCOPE,
        "session_id": rotated["session_id"],
    }


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def protected_resource_metadata(request: Request) -> Response:
    """RFC 9728 — tells an MCP client which server can authorise it."""
    cfg = settings()
    return JSONResponse(
        {
            "resource": resource_identifier(cfg),
            "authorization_servers": [issuer(cfg)],
            "scopes_supported": list(SUPPORTED_SCOPES),
            "bearer_methods_supported": ["header"],
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def authorization_server_metadata(request: Request) -> Response:
    """RFC 8414 — advertises PKCE-only, code-only OAuth 2.1."""
    cfg = settings()
    base = issuer(cfg)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "revocation_endpoint": f"{base}/revoke",
            "scopes_supported": list(SUPPORTED_SCOPES),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "resource_indicators_supported": True,
        },
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _split_redirect(uri: str) -> tuple[str, str, int | None, str]:
    parts = urlsplit(uri)
    return parts.scheme, (parts.hostname or "").lower(), parts.port, parts.path or "/"


def is_loopback_redirect(uri: str) -> bool:
    scheme, host, _port, _path = _split_redirect(uri)
    return scheme == "http" and host in ("127.0.0.1", "localhost", "::1")


def redirect_uri_acceptable(uri: str) -> bool:
    """What may be *registered*.

    RFC 8252: a native app redirects to a loopback address over plain http,
    because there is no transport to protect on the same machine. Anywhere else
    plain http would put an authorization code on the wire in the clear, so it
    is https only.
    """
    if not isinstance(uri, str) or not uri.startswith(("http://", "https://")):
        return False
    scheme, host, _port, _path = _split_redirect(uri)
    if not host:
        return False
    if scheme == "https":
        return True
    return is_loopback_redirect(uri)


def redirect_uri_matches(requested: str, registered: Sequence[str]) -> bool:
    """Whether a redirect_uri may be used, given what the client registered.

    Exact match, or — for loopback only — the same scheme, host and path on a
    **different port**. RFC 8252 §7.3 requires that: a desktop bridge binds an
    ephemeral port at launch, so the port it registered yesterday is not the
    port it is listening on now. Everything else stays exact, because a loose
    match anywhere else turns this endpoint into an open redirector.
    """
    if requested in registered:
        return True
    if not is_loopback_redirect(requested):
        return False
    scheme, host, _port, path = _split_redirect(requested)
    for candidate in registered:
        if not is_loopback_redirect(candidate):
            continue
        c_scheme, c_host, _c_port, c_path = _split_redirect(candidate)
        # `127.0.0.1` and `localhost` are not interchangeable: a client that
        # registered one must keep using it.
        if (scheme, host, path) == (c_scheme, c_host, c_path):
            return True
    return False


async def register(request: Request) -> Response:
    """RFC 7591 dynamic client registration (public clients, no secret)."""
    try:
        payload = await request.json()
    except Exception:
        return _oauth_error("invalid_request", "A JSON body is required.")
    redirect_uris = payload.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _oauth_error("invalid_redirect_uri", "At least one redirect_uri is required.")
    if not all(redirect_uri_acceptable(uri) for uri in redirect_uris):
        return _oauth_error(
            "invalid_redirect_uri",
            "Redirect URIs must be absolute https URLs, or http on a loopback "
            "address (127.0.0.1 / localhost) for a native client.",
        )

    conn = control_db.control()
    client = control_db.register_client(
        conn,
        client_name=str(payload.get("client_name") or "mcp_client")[:200],
        redirect_uris=redirect_uris[:10],
    )
    control_db.audit(
        conn,
        user_id=None,
        action="register_client",
        status="ok",
        surface="system",
        target_id=client["client_id"],
        detail={"client_name": client["client_name"]},
    )
    return JSONResponse(
        {
            **client,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


#: Linear-styled, and deliberately sharing `/app.css` rather than carrying its
#: own copy of the theme tokens — one definition of `--accent` for the whole
#: product. `/consent.js` applies the stored theme (and the 18:00 Warm Sunset
#: switch) because the CSP forbids inline script, which is the right trade.
CONSENT_PAGE = """<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Connect {client_name} to brain</title>
<link rel="stylesheet" href="/app.css">
<script src="/consent.js" defer></script></head>
<body class="consent-page">
<main class="consent-card">
  <header class="consent-head">
    <span class="consent-mark" aria-hidden="true">◆</span>
    <h1>Connect <strong>{client_name}</strong> to brain</h1>
    <p class="consent-sub">It is asking for access to your personal knowledge library.</p>
  </header>

  <ul class="consent-grants">
    <li><span class="consent-key">Can read</span>
        <span>Search your claims and skills, and fetch any one in full.</span></li>
    <li><span class="consent-key">Can write</span>
        <span>Save new claims and skills, and queue sources you paste into the chat.</span></li>
    <li><span class="consent-key">Safety</span>
        <span>Cannot permanently delete items &mdash; soft archive only. Deleting for good
        takes a human click in this web app, after 30 days in Trash.</span></li>
    <li><span class="consent-key">Duration</span>
        <span>30 days, revocable anytime in <strong>&#128100; Profile &rarr; MCP connection</strong>.</span></li>
  </ul>

  <form method="post" action="{action}" class="consent-form">
    <input type="hidden" name="state_blob" value="{state_blob}">
    {identity}
    <div class="consent-actions">
      <button type="submit" class="btn btn-primary">Authorize Connection</button>
      <button type="button" class="btn btn-quiet" id="consent-cancel">Cancel</button>
    </div>
  </form>
  <p class="consent-foot">Signing in here creates your library if you do not have one yet.</p>
</main>
</body></html>
"""

#: Already signed in to the web app: no reason to ask again.
CONSENT_KNOWN_IDENTITY = """<p class="consent-identity">Connecting as
<strong>{email}</strong></p>
<input type="hidden" name="email" value="{email}">"""

#: Not signed in: the email is the sign-in, inline, without leaving the flow.
CONSENT_SIGN_IN = """<label class="consent-label" for="email">Your email</label>
<input class="omni-input" id="email" name="email" type="email" required
       autocomplete="email" placeholder="you@example.com" value="{email}">"""


def consent_csp(redirect_uri: str) -> str:
    """The consent page's CSP: as tight as the rest, plus one form target."""
    parts = urlsplit(redirect_uri)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            f"form-action 'self' {origin}".strip(),
        ]
    )


def signed_in_email(request: Request, cfg: Settings) -> str:
    """The browser session's email, if this person is already signed in here.

    Imported lazily: `webapi` imports `auth` for the signing key, so importing
    it at module scope would be a cycle.
    """
    from app import webapi

    raw = request.cookies.get(webapi.SESSION_COOKIE)
    user_id = webapi.read_session_cookie(raw, cfg=cfg)
    if not user_id:
        return ""
    record = control_db.get_user(control_db.control(), user_id)
    return (record or {}).get("email", "") or ""


def consent_page(request: Request, parsed: dict[str, Any], cfg: Settings) -> str:
    email = signed_in_email(request, cfg)
    identity = (
        CONSENT_KNOWN_IDENTITY.format(email=_escape(email))
        if email
        else CONSENT_SIGN_IN.format(email="")
    )
    return CONSENT_PAGE.format(
        client_name=_escape(parsed["client_name"]),
        state_blob=_escape(_pack_state(parsed, cfg)),
        # Posting back to the path the browser is on keeps the `/oauth/*` alias
        # working for a client that discovered it that way.
        action=_escape(request.url.path),
        identity=identity,
    )


def _pack_state(payload: dict[str, Any], cfg: Settings) -> str:
    """Sign the in-flight authorization request so the form cannot be tampered with."""
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    mac = hmac.new(signing_key(cfg).encode("utf-8"), body.encode("ascii"), hashlib.sha256)
    return f"{body}.{base64.urlsafe_b64encode(mac.digest()).decode('ascii').rstrip('=')}"


def _unpack_state(blob: str, cfg: Settings) -> dict[str, Any] | None:
    try:
        body, signature = blob.split(".", 1)
    except ValueError:
        return None
    mac = hmac.new(signing_key(cfg).encode("utf-8"), body.encode("ascii"), hashlib.sha256)
    expected = base64.urlsafe_b64encode(mac.digest()).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected, signature):
        return None
    padding = "=" * (-len(body) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body + padding))
    except Exception:
        return None


def _validate_authorize_params(params) -> tuple[dict[str, Any] | None, Response | None]:
    cfg = settings()
    client_id = params.get("client_id") or ""
    redirect_uri = params.get("redirect_uri") or ""
    challenge = params.get("code_challenge") or ""
    method = params.get("code_challenge_method") or ""

    if params.get("response_type") != "code":
        return None, _oauth_error("unsupported_response_type", "Only `code` is supported.")
    if method != "S256":
        return None, _oauth_error(
            "invalid_request", "code_challenge_method must be S256; PKCE is mandatory."
        )
    if not challenge or len(challenge) < 43:
        return None, _oauth_error("invalid_request", "A valid S256 code_challenge is required.")

    conn = control_db.control()
    client = control_db.get_client(conn, client_id)
    if client is None:
        return None, _oauth_error("invalid_client", "Unknown client_id; register first.")
    if not redirect_uri_matches(redirect_uri, client["redirect_uris"]):
        # Never redirect to an unregistered URI — that is an open redirector.
        return None, _oauth_error("invalid_request", "redirect_uri is not registered for this client.")

    requested = params.get("resource") or resource_identifier(cfg)
    if requested != resource_identifier(cfg):
        return None, _oauth_error(
            "invalid_target",
            f"This server only issues tokens for {resource_identifier(cfg)}.",
        )

    scope = params.get("scope") or DEFAULT_SCOPE
    granted = " ".join(s for s in scope.split() if s in SUPPORTED_SCOPES) or DEFAULT_SCOPE
    return (
        {
            "client_id": client_id,
            "client_name": client["client_name"],
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "scope": granted,
            "resource": requested,
            "state": params.get("state") or "",
        },
        None,
    )


async def authorize(request: Request) -> Response:
    """GET renders consent; POST issues the authorization code."""
    cfg = settings()

    if request.method == "GET":
        parsed, error = _validate_authorize_params(request.query_params)
        if error is not None:
            return error
        assert parsed is not None
        # A single-user server with a pre-declared owner can skip the form.
        owner = os.environ.get("BRAIN_OWNER_EMAIL")
        if owner:
            return _issue_code_redirect(parsed, owner, cfg)
        return HTMLResponse(
            consent_page(request, parsed, cfg),
            headers={
                "Cache-Control": "no-store",
                # The page's own CSP, which the global middleware then leaves
                # alone. Approving posts to `/authorize`, which answers 302 to
                # the client's `redirect_uri` — and a browser applies
                # `form-action` to the *redirect target* too, so the global
                # `form-action 'self'` blocks the last step of the flow. Rather
                # than loosen it everywhere, this one page names the one origin
                # it is allowed to end up at: the redirect URI this client
                # registered, which has already been validated above.
                "Content-Security-Policy": consent_csp(parsed["redirect_uri"]),
            },
        )

    form = await request.form()
    parsed = _unpack_state(str(form.get("state_blob") or ""), cfg)
    if parsed is None:
        return _oauth_error("invalid_request", "The authorization request could not be verified.")
    email = str(form.get("email") or "").strip()
    if not email or "@" not in email:
        return _oauth_error("invalid_request", "A valid email is required.")
    return _issue_code_redirect(parsed, email, cfg)


def _issue_code_redirect(parsed: dict[str, Any], email: str, cfg: Settings) -> Response:
    conn = control_db.control()
    try:
        user = control_db.ensure_user(conn, email)
    except ValueError as exc:
        return _oauth_error("invalid_request", str(exc))

    control_db.purge_expired_auth_codes(conn)
    code = control_db.create_auth_code(
        conn,
        user_id=user["user_id"],
        client_id=parsed["client_id"],
        redirect_uri=parsed["redirect_uri"],
        code_challenge=parsed["code_challenge"],
        scope=parsed["scope"],
        resource=parsed["resource"],
    )
    control_db.audit(
        conn,
        user_id=user["user_id"],
        action="authorize",
        status="ok",
        surface="system",
        client_name=parsed.get("client_name", "mcp_client"),
    )
    query = {"code": code}
    if parsed.get("state"):
        query["state"] = parsed["state"]
    separator = "&" if "?" in parsed["redirect_uri"] else "?"
    return RedirectResponse(
        f"{parsed['redirect_uri']}{separator}{urlencode(query)}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


async def token(request: Request) -> Response:
    cfg = settings()
    try:
        form = await request.form()
    except Exception:
        return _oauth_error("invalid_request", "A form-encoded body is required.")

    grant_type = str(form.get("grant_type") or "")
    conn = control_db.control()

    if grant_type == "refresh_token":
        rotated = rotate_grant(conn, str(form.get("refresh_token") or ""), cfg=cfg)
        if rotated is None:
            return _oauth_error("invalid_grant", "That refresh token is expired or revoked.", 400)
        control_db.audit(
            conn,
            user_id=None,
            action="token_refresh",
            status="ok",
            surface="system",
            target_id=rotated["session_id"],
        )
        return JSONResponse(
            {k: v for k, v in rotated.items() if k != "session_id"},
            headers={"Cache-Control": "no-store"},
        )

    if grant_type != "authorization_code":
        return _oauth_error("unsupported_grant_type", f"`{grant_type}` is not supported.")

    code = str(form.get("code") or "")
    verifier = str(form.get("code_verifier") or "")
    record = control_db.consume_auth_code(conn, code)
    if record is None:
        # Covers unknown, expired and replayed codes alike.
        return _oauth_error("invalid_grant", "That authorization code is invalid or already used.")
    if record["redirect_uri"] != str(form.get("redirect_uri") or record["redirect_uri"]):
        return _oauth_error("invalid_grant", "redirect_uri does not match the authorization request.")
    if not verify_pkce(verifier, record["code_challenge"], record["challenge_method"]):
        return _oauth_error("invalid_grant", "PKCE verification failed.")

    requested_resource = str(form.get("resource") or record["resource"] or resource_identifier(cfg))
    if requested_resource != resource_identifier(cfg):
        return _oauth_error("invalid_target", "Resource indicator does not match this server.")

    client = control_db.get_client(conn, record["client_id"])
    grant = issue_grant(
        conn,
        user_id=record["user_id"],
        client_name=(client or {}).get("client_name", "mcp_client"),
        client_id=record["client_id"],
        scope=record["scope"] or DEFAULT_SCOPE,
        resource=requested_resource,
        cfg=cfg,
    )
    control_db.audit(
        conn,
        user_id=record["user_id"],
        action="token_issue",
        status="ok",
        surface="system",
        target_id=grant["session_id"],
        client_name=(client or {}).get("client_name", "mcp_client"),
    )
    return JSONResponse(
        {k: v for k, v in grant.items() if k != "session_id"},
        headers={"Cache-Control": "no-store"},
    )


async def revoke(request: Request) -> Response:
    """RFC 7009. Always 200, so a caller cannot probe which tokens exist."""
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"revoked": False}, headers={"Cache-Control": "no-store"})

    presented = str(form.get("token") or "")
    conn = control_db.control()
    record = control_db.session_by_refresh_token(
        conn, presented
    ) or control_db.session_by_access_token(conn, presented)
    if record:
        control_db.revoke_session(conn, record["session_id"])
        control_db.audit(
            conn,
            user_id=record["user_id"],
            action="revoke",
            status="ok",
            surface="system",
            target_id=record["session_id"],
        )
    return JSONResponse({"revoked": True}, headers={"Cache-Control": "no-store"})


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(
            status_code=401,
            detail="A Bearer access token is required.",
            headers=unauthorized_headers(),
        )
    return value.strip()


def unauthorized_headers(error: str = "invalid_token") -> dict[str, str]:
    """RFC 9728 challenge: point the client at our resource metadata."""
    cfg = settings()
    return {
        "WWW-Authenticate": (
            f'Bearer realm="brain", error="{error}", '
            f'resource_metadata="{issuer(cfg)}/.well-known/oauth-protected-resource"'
        )
    }


#: `/oauth/...` aliases alongside the canonical paths. The metadata document
#: advertises the canonical ones and a conforming client reads them from there,
#: but enough clients and docs assume an `/oauth` prefix that answering both
#: costs one line each and saves a confusing 404.
ROUTES = [
    Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"]),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize, methods=["GET", "POST"]),
    Route("/token", token, methods=["POST"]),
    Route("/revoke", revoke, methods=["POST"]),
    Route("/oauth/register", register, methods=["POST"]),
    Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
    Route("/oauth/token", token, methods=["POST"]),
    Route("/oauth/revoke", revoke, methods=["POST"]),
]
