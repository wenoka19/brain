"""Phase 7 — production resilience and mobile / MCP hardening.

Four failure modes that only show up once something real is on the other end,
tested here without anything real on the other end:

1. **Direct-vendor rate limits.** Z.ai and DashScope enforce per-key
   concurrency and RPM, so a multi-claim ingestion burst earns a 429. The
   retry loop is tested with an injected sleep, so the suite stays fast and
   the waits themselves are asserted rather than endured.
2. **A redeploy mid-debounce.** Railway sends `SIGTERM`; anything marked dirty
   and not yet uploaded has to reach R2 before the process exits.
3. **An external MCP client.** Claude and Cursor connect cross-origin and
   discover everything from the challenge header outward, so the whole
   sequence is walked end to end on the real app.
4. **A phone.** The control plane is used on a 390x844 screen, where the one
   pane layouts and the bottom tuner bar have both broken before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from app import auth
from app import mcp_server as M
from app import webapi
from app.config import load_settings
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.ingestion import providers as P
from app.main import (
    CORS_ALLOW_HEADERS,
    CORS_EXPOSE_HEADERS,
    MCP_CORS_PREFIXES,
    create_app,
)
from app.storage import r2_sync

ROOT = Path(__file__).resolve().parents[1]
JWT_SECRET = "phase7-resilience-secret-key-long-enough"
VERIFIER = "phase7-pkce-verifier-value-that-is-long-enough-for-s256"


# ===========================================================================
# 1. direct-API rate limits and transient retries
# ===========================================================================
@pytest.fixture
def slept():
    """Collect the delays instead of serving them."""
    delays: list[float] = []
    P.set_sleeper(delays.append)
    yield delays
    P.set_sleeper(None)


class Host:
    """A fake endpoint that answers from a queued script."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.queue = list(responses)
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self.queue.pop(0) if self.queue else httpx.Response(200, json={"ok": True})
        if isinstance(item, Exception):
            raise item
        return item

    def install(self):
        P.set_http_client_factory(
            lambda: httpx.Client(transport=httpx.MockTransport(self.handler))
        )
        return self


@pytest.fixture
def host():
    yield Host
    P.set_http_client_factory(None)


def post(url: str = "https://api.z.ai/api/paas/v4/chat/completions", retries: int = 2):
    return P._post_json(
        url, api_key="sk-key", payload={"model": "glm-5.3-flash"}, timeout=5.0, retries=retries
    )


class TestRetryBackoff:
    def test_a_429_is_retried_and_then_succeeds(self, host, slept):
        endpoint = host(
            httpx.Response(429, text="rate limited"),
            httpx.Response(200, json={"choices": []}),
        ).install()

        assert post() == {"choices": []}
        assert endpoint.calls == 2
        assert slept == [0.5]

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_every_transient_status_is_retried(self, host, slept, status):
        endpoint = host(
            httpx.Response(status), httpx.Response(status), httpx.Response(200, json={"ok": 1})
        ).install()
        assert post() == {"ok": 1}
        assert endpoint.calls == 3

    @pytest.mark.parametrize(
        "status,reason",
        [
            (400, "model_error"),
            (401, "bad_api_key"),
            (403, "model_forbidden"),
            (404, "model_not_found"),
        ],
    )
    def test_a_fatal_status_is_not_retried(self, host, slept, status, reason):
        """Each of these is about how the request was *built*. Sending it again
        is only a slower way to get the same answer."""
        endpoint = host(httpx.Response(status, text="no")).install()

        with pytest.raises(P.ProviderError) as raised:
            post()

        assert raised.value.reason == reason
        assert endpoint.calls == 1, "a fatal status must cost exactly one call"
        assert slept == []

    def test_the_404_message_names_what_to_check(self):
        """The likeliest 404 after the move off a marketplace is a model id, so
        the error says where to look rather than just the status."""
        endpoint = Host(httpx.Response(404, text='{"error":"model not found"}')).install()
        try:
            with pytest.raises(P.ProviderError) as raised:
                post()
            assert "LLM_MODEL" in raised.value.message
            assert "LLM_BASE_URL" in raised.value.message
        finally:
            P.set_http_client_factory(None)
            assert endpoint.calls == 1

    def test_the_backoff_is_exponential(self, host, slept):
        host(*(httpx.Response(503) for _ in range(4))).install()
        with pytest.raises(P.ProviderError):
            post(retries=3)
        assert slept == [0.5, 1.0, 2.0]

    def test_a_retry_after_header_wins_over_the_backoff(self, host, slept):
        """The host knows when its window resets and we do not."""
        host(
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"ok": 1}),
        ).install()
        assert post() == {"ok": 1}
        assert slept == [3.0]

    def test_a_retry_after_date_is_understood_too(self, host, slept):
        from email.utils import formatdate

        host(
            httpx.Response(429, headers={"Retry-After": formatdate(time.time() + 4, usegmt=True)}),
            httpx.Response(200, json={"ok": 1}),
        ).install()
        assert post() == {"ok": 1}
        assert 2.5 <= slept[0] <= 4.5

    def test_an_absurd_retry_after_is_capped(self, host, slept):
        """A worker parked for ten minutes is worse than a failed job: the
        queue has a lease and an attempt counter, and this does not."""
        host(httpx.Response(503, headers={"Retry-After": "600"}), httpx.Response(200, json={})).install()
        post()
        assert slept == [P.MAX_BACKOFF_SEC]

    def test_a_garbage_retry_after_falls_back_to_the_backoff(self, host, slept):
        host(httpx.Response(429, headers={"Retry-After": "soon"}), httpx.Response(200, json={})).install()
        post()
        assert slept == [0.5]

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("timed out"),
            httpx.ConnectTimeout("timed out"),
            httpx.RemoteProtocolError("reset"),
        ],
    )
    def test_a_transport_failure_is_retried(self, host, slept, error):
        """Nothing about the request is wrong, so the same bytes can go again."""
        endpoint = host(error, httpx.Response(200, json={"ok": 1})).install()
        assert post() == {"ok": 1}
        assert endpoint.calls == 2

    def test_retries_are_bounded(self, host, slept):
        endpoint = host(*(httpx.Response(429) for _ in range(9))).install()
        with pytest.raises(P.ProviderError) as raised:
            post()
        assert endpoint.calls == 3, "three attempts, not nine"
        assert raised.value.reason == "rate_limited"
        assert len(slept) == 2

    def test_an_exhausted_retry_reports_the_rate_limit_not_a_generic_error(self, host, slept):
        host(*(httpx.Response(429) for _ in range(3))).install()
        with pytest.raises(P.ProviderError) as raised:
            post()
        assert raised.value.reason == "rate_limited"

    def test_a_rate_limited_extraction_still_falls_back_rather_than_failing(
        self, cfg, monkeypatch, host, slept
    ):
        """A burst that outruns the quota must not lose the source: the
        heuristic extractor takes over for that chunk."""
        monkeypatch.setenv("BRAIN_ENV", "prod")
        monkeypatch.setenv("LLM_API_KEY", "sk-key")
        prod = load_settings()
        host(*(httpx.Response(429) for _ in range(3))).install()

        text = (
            "We batch every cold email into one block before ten in the morning, "
            "and our reply rate went up 34% that quarter."
        )
        claims = P.LiveLLMExtractor(prod).extract(text, {"title": "t"})
        assert claims, "the chunk still produced claims from the heuristic path"

    def test_an_embedding_rate_limit_is_retried_then_raised(self, cfg, monkeypatch, host, slept):
        """Embeddings must never fall back: a hashed vector in a neural index
        would corrupt every later search silently."""
        monkeypatch.setenv("BRAIN_ENV", "prod")
        monkeypatch.setenv("LLM_API_KEY", "sk-key")
        prod = load_settings()
        endpoint = host(*(httpx.Response(429) for _ in range(3))).install()

        with pytest.raises(P.ProviderError) as raised:
            P.LiveEmbedder(prod).embed(["one text"])
        assert raised.value.reason == "rate_limited"
        assert endpoint.calls == 3

    def test_the_sleeper_is_the_real_one_by_default(self):
        P.set_sleeper(None)
        assert P._sleeper is time.sleep


# ===========================================================================
# 2. graceful shutdown flush
# ===========================================================================
class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/brain-tenants/", 1)[-1]
        if request.method == "PUT":
            self.puts += 1
            self.objects[key] = request.content
            return httpx.Response(200)
        if key not in self.objects:
            return httpx.Response(404)
        return httpx.Response(200, content=self.objects[key])

    def install(self):
        r2_sync.set_config(
            r2_sync.R2Config(
                endpoint_url="https://acct.r2.cloudflarestorage.com",
                bucket="brain-tenants",
                access_key_id="AKIA",
                secret_access_key="secret",
            )
        )
        r2_sync.set_http_client_factory(
            lambda: httpx.Client(transport=httpx.MockTransport(self.handler))
        )
        return self


@pytest.fixture
def bucket(cfg):
    return FakeBucket().install()


def remember(ctx: E.Context, text: str) -> E.OpResult:
    return E.op_remember_this(ctx, claim=text, when_it_applies="During a redeploy.")


class TestGracefulShutdown:
    def test_a_write_inside_the_debounce_window_survives_shutdown(self, cfg, bucket, user):
        """The exact redeploy case: a claim is saved, SIGTERM arrives before the
        debounce elapses, and the snapshot must still happen."""
        user_id = user["user_id"]
        assert remember(E.build_context(user_id), "A claim written seconds before SIGTERM.").ok
        assert r2_sync.is_dirty(user_id) is True
        # Nothing has gone out yet — the debounce has not elapsed.
        assert r2_sync.flush_due(cfg=cfg, now=time.monotonic()) == []
        assert bucket.puts == 0

        flushed = r2_sync.flush_all_dirty_tenants(cfg=cfg)

        assert flushed == [user_id]
        assert bucket.puts == 1
        assert r2_sync.is_dirty(user_id) is False

    def test_the_uploaded_snapshot_contains_the_last_write(self, cfg, bucket, user):
        import sqlite3

        user_id = user["user_id"]
        assert remember(E.build_context(user_id), "The very last thing anyone said.").ok
        r2_sync.flush_all_dirty_tenants(cfg=cfg)

        landed = Path(cfg.data_dir) / "landed.db"
        landed.write_bytes(bucket.objects[f"tenants/user_{user_id}.db"])
        conn = sqlite3.connect(str(landed))
        try:
            found = conn.execute(
                "SELECT count(*) FROM epistemic_claims WHERE claim LIKE '%last thing%'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert found == 1

    def test_stop_background_stops_then_flushes(self, cfg, bucket, user):
        user_id = user["user_id"]
        assert r2_sync.start_background(cfg=cfg) is True
        assert remember(E.build_context(user_id), "Written while the loop was running.").ok

        flushed = r2_sync.stop_background(cfg=cfg)

        assert flushed == [user_id]
        assert r2_sync.is_dirty(user_id) is False
        assert r2_sync._thread is None

    def test_flushing_twice_is_harmless(self, cfg, bucket, user):
        """Shutdown paths overlap — the worker's `finally` and the lifespan's
        can both run — so a second flush must be a no-op, not a second upload."""
        user_id = user["user_id"]
        assert remember(E.build_context(user_id), "Flushed once, asked twice.").ok
        assert r2_sync.flush_all_dirty_tenants(cfg=cfg) == [user_id]
        assert r2_sync.flush_all_dirty_tenants(cfg=cfg) == []
        assert bucket.puts == 1

    def test_the_shutdown_flush_is_safe_without_object_storage(self, cfg, user):
        r2_sync.set_config(r2_sync.R2Config())
        assert remember(E.build_context(user["user_id"]), "Local-only deployment.").ok
        assert r2_sync.flush_all_dirty_tenants(cfg=cfg) == []
        assert r2_sync.stop_background(cfg=cfg) == []

    def test_concurrent_flushes_of_one_tenant_serialise(self, cfg, bucket, user):
        """The loop and a shutdown flush can reach the same tenant at once;
        `VACUUM INTO` writing the same path twice would be a corrupt upload."""
        import threading

        user_id = user["user_id"]
        assert remember(E.build_context(user_id), "Raced by two flushers.").ok
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def flush() -> None:
            barrier.wait()
            results.append(r2_sync.snapshot_and_upload(user_id, cfg=cfg))

        threads = [threading.Thread(target=flush) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results) == [False, True] or results == [True, True]
        assert r2_sync.is_dirty(user_id) is False
        # Whatever landed is a whole, openable database.
        assert bucket.objects[f"tenants/user_{user_id}.db"][:16].startswith(b"SQLite format 3")

    def test_the_lifespan_flushes_on_shutdown(self, cfg, bucket, user, monkeypatch):
        """Through the real app: the ASGI lifespan's shutdown must do it, since
        that is what uvicorn runs when `SIGTERM` arrives."""
        monkeypatch.setenv("BRAIN_JWT_SECRET", JWT_SECRET)
        user_id = user["user_id"]
        M.reset_server()
        with TestClient(create_app(cfg)) as client:
            assert client.get("/healthz").status_code == 200
            assert remember(E.build_context(user_id), "Written while the server was up.").ok
            assert r2_sync.is_dirty(user_id) is True
        # Leaving the context runs the lifespan's shutdown.
        assert r2_sync.is_dirty(user_id) is False
        assert bucket.puts >= 1
        M.reset_server()

    def test_the_shutdown_path_is_in_a_finally_block(self):
        """A `SIGTERM` is delivered as a cancellation *at* the `yield`, so a
        flush written after it would simply never run. This is a source
        assertion because the condition cannot be provoked through TestClient,
        which closes the lifespan cleanly."""
        source = (ROOT / "app" / "main.py").read_text()
        body = source.split("async def lifespan")[1].split("app = FastAPI")[0]
        yield_at = body.index("yield")
        finally_at = body.index("finally:")
        stop_at = body.index("r2_sync.stop_background")
        assert yield_at < finally_at < stop_at

    def test_the_worker_flushes_too(self):
        """The worker writes tenant databases as well, so its own shutdown has
        to flush or an ingestion's claims sit dirty until something else
        touches that tenant."""
        source = (ROOT / "app" / "worker.py").read_text()
        assert "r2_sync.start_background" in source
        finally_block = source.split("finally:")[1]
        assert "r2_sync.stop_background" in finally_block
        assert finally_block.index("stop_background") < finally_block.index("close_all")


# ===========================================================================
# 3. the external MCP client wire protocol
# ===========================================================================
@pytest.fixture
def wire(cfg, monkeypatch):
    monkeypatch.setenv("BRAIN_JWT_SECRET", JWT_SECRET)
    M.reset_server()
    with TestClient(create_app(cfg)) as client:
        yield client
    M.reset_server()


ORIGIN = "https://claude.ai"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class TestCorsForExternalClients:
    def test_the_preflight_is_answered_not_routed(self, wire):
        """None of these endpoints declares OPTIONS, so without the middleware
        a browser's preflight gets a 405 and the client never even tries."""
        response = wire.options(
            "/mcp",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id",
            },
        )
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert "POST" in response.headers["access-control-allow-methods"]
        assert response.headers["vary"] == "Origin"

    @pytest.mark.parametrize("header", ["Authorization", "Content-Type", "Mcp-Session-Id"])
    def test_the_headers_a_client_must_send_are_allowed(self, wire, header):
        allowed = wire.options(
            "/mcp", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"}
        ).headers["access-control-allow-headers"]
        assert header.lower() in allowed.lower()

    @pytest.mark.parametrize("header", ["WWW-Authenticate", "Mcp-Session-Id"])
    def test_the_headers_a_client_must_read_are_exposed(self, wire, header):
        """Both are load bearing. Without `WWW-Authenticate` the client cannot
        discover where to authenticate, so the OAuth dance never starts; without
        `Mcp-Session-Id` it cannot continue the session it just opened."""
        exposed = wire.post(
            "/mcp",
            headers={**MCP_HEADERS, "Origin": ORIGIN},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ).headers["access-control-expose-headers"]
        assert header.lower() in exposed.lower()

    def test_discovery_is_reachable_cross_origin(self, wire):
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            response = wire.get(path, headers={"Origin": ORIGIN})
            assert response.status_code == 200, path
            assert response.headers["access-control-allow-origin"] == ORIGIN

    def test_the_control_plane_is_not_cross_origin_callable(self, wire):
        """`/api/*` authenticates with a SameSite=Strict cookie and a
        double-submit CSRF header. Advertising it as cross-origin-callable would
        undo that for no gain — an MCP client uses a bearer token and no cookie."""
        assert "/api" not in MCP_CORS_PREFIXES
        response = wire.get("/api/cards", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers
        preflight = wire.options(
            "/api/cards",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        assert preflight.status_code != 204

    def test_a_same_origin_request_gets_no_cors_headers(self, wire):
        """No `Origin`, nothing to negotiate."""
        response = wire.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    def test_the_security_headers_still_apply(self, wire):
        """CORS must not have quietly displaced the rest."""
        response = wire.get("/.well-known/oauth-protected-resource", headers={"Origin": ORIGIN})
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


class TestFullWireSequence:
    """What Claude or Cursor actually does, in order, with every step taken from
    the previous step's response rather than hard-coded."""

    def test_an_external_client_connects_from_nothing_but_the_url(self, wire, cfg):
        redirect_uri = "https://claude.ai/api/mcp/auth_callback"

        # 1. An unauthenticated call, and the challenge that starts everything.
        unauthorised = wire.post(
            "/mcp",
            headers={**MCP_HEADERS, "Origin": ORIGIN},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert unauthorised.status_code == 401
        challenge = unauthorised.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        metadata_url = re.search(r'resource_metadata="([^"]+)"', challenge).group(1)

        # 2. The resource metadata, fetched at the path the challenge named.
        resource = wire.get(metadata_url.replace(auth.issuer(cfg), ""))
        assert resource.status_code == 200
        assert resource.json()["resource"] == cfg.audience
        server_base = resource.json()["authorization_servers"][0].rstrip("/")

        # 3. The authorization server metadata (RFC 8414). Every endpoint below
        #    is read from here — a client never guesses a path.
        meta = wire.get("/.well-known/oauth-authorization-server")
        assert meta.status_code == 200
        document = meta.json()
        assert document["issuer"].rstrip("/") == server_base
        assert document["code_challenge_methods_supported"] == ["S256"]
        registration = document["registration_endpoint"].replace(server_base, "")
        authorization = document["authorization_endpoint"].replace(server_base, "")
        token_endpoint = document["token_endpoint"].replace(server_base, "")

        # 4. Dynamic registration (RFC 7591) — no pre-shared client id.
        registered = wire.post(
            registration,
            json={"client_name": "Claude", "redirect_uris": [redirect_uri]},
            headers={"Origin": ORIGIN},
        )
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]

        # 5. Authorize with S256 PKCE, then consent as the human would.
        challenge_value = auth.pkce_challenge(VERIFIER)
        page = wire.get(
            authorization,
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge_value,
                "code_challenge_method": "S256",
                "state": "opaque-client-state",
                "resource": cfg.audience,
            },
            follow_redirects=False,
        )
        assert page.status_code == 200
        blob = re.search(r'name="state_blob" value="([^"]+)"', page.text).group(1)
        granted = wire.post(
            authorization,
            data={"state_blob": blob.replace("&amp;", "&"), "email": "founder@example.com"},
            follow_redirects=False,
        )
        assert granted.status_code == 302
        location = granted.headers["location"]
        assert location.startswith(redirect_uri)
        assert "state=opaque-client-state" in location
        code = re.search(r"[?&]code=([^&]+)", location).group(1)

        # 6. Exchange the code. The verifier is what proves it is the same client.
        exchanged = wire.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": VERIFIER,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "resource": cfg.audience,
            },
            headers={"Origin": ORIGIN},
        )
        assert exchanged.status_code == 200
        grant = exchanged.json()
        assert grant["token_type"] == "Bearer"
        assert grant["expires_in"] == 3600
        assert grant["refresh_token"]
        assert exchanged.headers["cache-control"] == "no-store"
        assert auth.verify_access_token(grant["access_token"], cfg=cfg).audience == cfg.audience

        # 7. The same call that 401'd, now authorised, over the real transport.
        session = wire.post(
            "/mcp",
            headers={
                **MCP_HEADERS,
                "Origin": ORIGIN,
                "Authorization": f"Bearer {grant['access_token']}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "Claude", "version": "1.0"},
                },
            },
        )
        assert session.status_code == 200
        session_id = session.headers.get("mcp-session-id")
        assert session_id, "the client needs this header to continue the session"

        authorised_headers = {
            **MCP_HEADERS,
            "Origin": ORIGIN,
            "Authorization": f"Bearer {grant['access_token']}",
            "Mcp-Session-Id": session_id,
        }
        wire.post(
            "/mcp",
            headers=authorised_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        listed = wire.post(
            "/mcp",
            headers=authorised_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert listed.status_code == 200
        tools = {tool["name"] for tool in _sse(listed)["result"]["tools"]}
        assert tools == {
            "search", "fetch", "ingest_source", "get_ingest_status",
            "remember_this", "get_or_synthesize_skill", "save_skill", "archive_item",
        }

        # 8. And a real tool call, which is the thing the user will actually do.
        called = wire.post(
            "/mcp",
            headers=authorised_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "remember_this",
                    "arguments": {
                        "claim": "An external client can reach brain end to end.",
                        "when_it_applies": "On every new connector setup.",
                    },
                },
            },
        )
        assert called.status_code == 200
        text = _sse(called)["result"]["content"][0]["text"]
        assert "Saved" in text or "saved" in text

    def test_a_revoked_token_sends_the_client_back_to_the_start(self, wire, cfg):
        """Revocation has to produce the same challenge as having no token at
        all, or a client with a dead token has nothing to act on."""
        record = control_db.ensure_user(control_db.control(), "founder@example.com")
        grant = auth.issue_grant(
            control_db.control(),
            user_id=record["user_id"],
            client_name="Claude",
            client_id="claude-desktop",
            scope="brain:read brain:write",
            resource=cfg.audience,
            cfg=cfg,
        )
        headers = {**MCP_HEADERS, "Origin": ORIGIN, "Authorization": f"Bearer {grant['access_token']}"}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}},
        }
        assert wire.post("/mcp", headers=headers, json=body).status_code == 200

        revoked = wire.post("/revoke", data={"token": grant["refresh_token"]})
        assert revoked.status_code == 200

        again = wire.post("/mcp", headers=headers, json=body)
        assert again.status_code == 401
        assert "resource_metadata=" in again.headers["www-authenticate"]


def _sse(response) -> dict:
    """The transport answers in SSE frames; pull the JSON-RPC body out."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(response.text)


# ===========================================================================
# 4. the mobile viewport audit
# ===========================================================================
MOBILE_WIDTH = 390
MOBILE_HEIGHT = 844


@pytest.fixture(scope="class")
def mobile_server(tmp_path_factory):
    """A real uvicorn on a real port, because Playwright needs a real socket.

    Skipped rather than failed when node or playwright is missing, so the suite
    still runs on a bare host.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    try:
        subprocess.run(
            ["node", "-e", "require('playwright')"], check=True, capture_output=True, timeout=30
        )
    except Exception:
        pytest.skip("playwright is not installed for node")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    workdir = tmp_path_factory.mktemp("mobile")
    env = {
        **os.environ,
        "BRAIN_ENV": "test",
        "BRAIN_DATA_DIR": str(workdir),
        "BRAIN_JWT_SECRET": JWT_SECRET,
        "BRAIN_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
    }
    process = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            if process.poll() is not None:
                pytest.skip("the server did not start")
            try:
                httpx.get(f"{base}/healthz", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.skip("the server did not become ready")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()


@pytest.fixture(scope="class")
def mobile_report(mobile_server):
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "browser" / "mobile_audit.js")],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "BASE": mobile_server}, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


class TestMobileViewport:
    """iPhone portrait, every layout, every edge state.

    Both of the defects this guards against have happened: the fixed tuner bar
    covered the sidebar Profile button, and a long `anchor_quote` pushed a card
    past the viewport.
    """

    def test_the_audit_ran_at_the_right_size_with_no_script_errors(self, mobile_report):
        assert mobile_report["viewport"] == [MOBILE_WIDTH, MOBILE_HEIGHT]
        assert mobile_report["errors"] == [], mobile_report["errors"]
        # 4 layouts x 9 states, none skipped.
        assert len(mobile_report["states"]) == 36

    def test_no_state_overflows_horizontally(self, mobile_report):
        """A phone that scrolls sideways is the clearest possible signal that a
        layout was only ever looked at on a laptop."""
        failures = [
            f"{row['layout']}/{row['state']}: scrollWidth={row['scrollWidth']} "
            f"offenders={row['offenders']}"
            for row in mobile_report["states"]
            if row["scrollWidth"] > MOBILE_WIDTH or row["offenders"]
        ]
        assert failures == [], "\n".join(failures)

    @pytest.mark.parametrize("layout", ["B1", "B2", "B3", "merged"])
    def test_every_layout_was_measured_in_every_state(self, mobile_report, layout):
        states = {row["state"] for row in mobile_report["states"] if row["layout"] == layout}
        assert states == {
            "happy", "blocked", "zero-match", "first-run", "three-way",
            "long-quote", "ingesting", "empty", "offline",
        }

    def test_the_conflict_sheet_opens_and_closes_on_a_phone(self, mobile_report):
        sheet = mobile_report["sheet"]
        assert sheet["found"] is True, "the seeded library has no disagreement to inspect"
        assert sheet["open"] is True
        assert sheet["textLength"] > 100, "an empty sheet is not an open sheet"
        assert sheet["closed"] is True
        assert sheet["cardsAfterClose"] >= 1, "closing it must return you to the library"

    def test_the_conflict_sheet_is_not_under_the_tuner_bar(self, mobile_report):
        """The Phase 4 defect, in its other form: the bar is fixed to the
        bottom, so anything it covers cannot be tapped."""
        sheet = mobile_report["sheet"]
        assert sheet["closeButton"]["found"] is True
        assert sheet["closeButton"]["visible"] is True
        assert sheet["closeButton"]["covered"] is False, sheet["closeButton"].get("coveredBy")
        assert sheet["closeButton"]["inViewport"] is True

    def test_the_sheet_fits_the_screen(self, mobile_report):
        box = mobile_report["sheet"]["box"]
        assert box["left"] >= 0
        assert box["right"] <= MOBILE_WIDTH
        assert mobile_report["sheet"]["overflow"]["scrollWidth"] <= MOBILE_WIDTH

    def test_the_profile_modal_is_reachable_and_escapable_on_a_phone(self, mobile_report):
        profile = mobile_report["profile"]
        # Reached from the fixed bottom bar, since the sidebar is an off-canvas
        # drawer at this width — and the bar must not be under the tuner.
        assert profile["button"]["visible"] is True
        assert profile["button"]["covered"] is False, profile["button"].get("coveredBy")
        assert profile["button"]["inViewport"] is True
        assert profile["open"] is True
        # Five blocks from Checkpoint B, plus the audit log added in Phase 6.
        assert len(profile["blocks"]) == 6, profile["blocks"]
        assert profile["closeButton"]["covered"] is False, profile["closeButton"].get("coveredBy")
        assert profile["closeButton"]["inViewport"] is True
        # Two ways out: a modal you can only leave by reloading is a trap.
        assert profile["closedByButton"] is True
        assert profile["closedByEscape"] is True

    def test_the_bottom_bar_and_the_tuner_do_not_overlap(self, mobile_report):
        """Two bars both fixed to the bottom edge is how the Phase 4 defect
        happened. In the harness the tuner has to sit above the nav bar."""
        bar = mobile_report["profile"]["barBox"]
        tuner = mobile_report["profile"]["tunerBox"]
        assert tuner["bottom"] <= bar["top"] + 1, (
            f"the tuner (bottom {tuner['bottom']}) overlaps the nav bar "
            f"(top {bar['top']})"
        )

    def test_the_profile_modal_fits_the_screen(self, mobile_report):
        box = mobile_report["profile"]["box"]
        assert box["left"] >= 0 and box["right"] <= MOBILE_WIDTH
        assert mobile_report["profile"]["overflow"]["scrollWidth"] <= MOBILE_WIDTH
        assert mobile_report["profile"]["overflow"]["offenders"] == []

    def test_an_offline_page_says_so_instead_of_failing_silently(self, mobile_report):
        """`fetch` rejects when the network is gone. That used to skip every
        caller's `if (!data.ok)` branch, so the page half-rendered in silence."""
        offline = mobile_report["offline"]
        assert offline["strip"] >= 1
        assert "could not be reached" in offline["text"]
        assert offline["overflow"]["scrollWidth"] <= MOBILE_WIDTH


class TestOfflineIsHandledInTheSource:
    """Asserted in the source too, so the contract holds on a host where
    Playwright is unavailable and the browser test skips."""

    def test_the_api_helper_cannot_reject(self):
        js = (ROOT / "app" / "static" / "app.js").read_text()
        body = js.split("async function api(")[1].split("\n}")[0]
        assert "try {" in body and "await fetch(" in body
        assert "catch" in body and "offlinePayload()" in body

    def test_the_offline_payload_is_an_ordinary_failed_response(self):
        js = (ROOT / "app" / "static" / "app.js").read_text()
        payload = js.split("function offlinePayload()")[1].split("\n}")[0]
        assert 'ok: false' in payload
        assert 'reason: "offline"' in payload
        assert "could not be reached" in payload

    def test_the_harness_exposes_the_offline_state(self):
        html = (ROOT / "app" / "static" / "index.html").read_text()
        assert '<option value="offline">' in html


# ===========================================================================
# the constants the middleware and the tests share
# ===========================================================================
class TestCorsContract:
    def test_the_exposed_headers_are_exactly_what_a_client_needs(self):
        assert set(CORS_EXPOSE_HEADERS) >= {"WWW-Authenticate", "Mcp-Session-Id"}

    def test_the_allowed_headers_cover_the_streamable_transport(self):
        lowered = {header.lower() for header in CORS_ALLOW_HEADERS}
        assert {"authorization", "content-type", "mcp-session-id"} <= lowered
