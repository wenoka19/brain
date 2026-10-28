"""Phase 7 — the human entry door, real-client interop, and the races.

Three audiences, each of which can only be checked one way:

* **A person arriving for the first time.** Driven in a real browser, because
  the question is whether the door works, not whether the handler returns 200.
* **A real MCP client** (Claude, Cursor, a desktop bridge). Driven over HTTP
  against the real app, discovering every endpoint from the metadata rather
  than from a hard-coded path.
* **Two requests arriving at once.** Driven with threads, because a singleflight
  lock is only interesting under contention.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from app import auth
from app import mcp_server as M
from app import webapi
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.main import CORS_ALLOW_HEADERS, CORS_EXPOSE_HEADERS, STREAM_HEADERS, create_app
from app.storage import r2_sync

ROOT = Path(__file__).resolve().parents[1]
JWT_SECRET = "phase7-polish-secret-key-long-enough"
VERIFIER = "phase7-polish-pkce-verifier-long-enough-for-s256-abcdef"
MOBILE_WIDTH = 390


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("BRAIN_JWT_SECRET", JWT_SECRET)
    M.reset_server()
    with TestClient(create_app(cfg)) as test_client:
        yield test_client
    M.reset_server()


@pytest.fixture
def app_client(client):
    """Signed in, sending exactly what the page sends."""
    response = client.post("/api/login", json={"email": "founder@example.com"})
    assert response.status_code == 200
    client.headers.update({
        "X-Brain-CSRF": response.json()["csrf"],
        "X-Brain-Request": "1",
    })
    return client


# ===========================================================================
# 1. the human entry door, as an HTTP contract
# ===========================================================================
class TestEntryDoor:
    def test_a_first_sign_in_lands_in_a_seeded_workspace(self, client):
        """Rule 4c. An empty canvas teaches nothing about what this is."""
        payload = client.post("/api/login", json={"email": "brand-new@example.com"}).json()
        assert payload["seeded"] is True
        counts = client.get("/api/cards").json()["library_counts"]
        assert counts == {
            "sources": 2, "claims": 2, "skills": 1, "tensions": 1, "archived_claims": 0
        }

    def test_signing_back_in_does_not_duplicate_the_demo(self, client):
        client.post("/api/login", json={"email": "returning@example.com"})
        again = client.post("/api/login", json={"email": "returning@example.com"}).json()
        assert again["seeded"] is False
        assert client.get("/api/cards").json()["library_counts"]["claims"] == 2

    def test_a_cleared_demo_is_never_handed_back(self, app_client):
        """Clearing it is a decision, not an accident to be corrected."""
        assert app_client.post("/api/demo/clear", json={}).status_code == 200
        assert E.seed_first_run_library("x" * 8) is False  # unknown user, no crash
        payload = app_client.post("/api/login", json={"email": "founder@example.com"}).json()
        assert payload["seeded"] is False

    def test_the_mcp_link_and_its_sessions_are_in_profile_block_two(self, app_client, cfg):
        profile = app_client.get("/api/profile").json()
        assert profile["mcp_url"].endswith("/mcp")
        assert profile["mcp_url"].startswith("http")

        grant = auth.issue_grant(
            control_db.control(),
            user_id=webapi.read_session_cookie(
                app_client.cookies.get(webapi.SESSION_COOKIE), cfg=cfg
            ),
            client_name="Claude Desktop",
            client_id="claude",
            scope="brain:read brain:write",
            resource=cfg.audience,
            cfg=cfg,
        )
        sessions = app_client.get("/api/profile").json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["client_name"] == "Claude Desktop"
        assert sessions[0]["expires_in_days"] == 30
        revoked = app_client.post(f"/api/sessions/{sessions[0]['session_id']}/revoke", json={})
        assert revoked.status_code == 200
        assert app_client.get("/api/profile").json()["sessions"] == []
        with pytest.raises(auth.TokenRejected):
            auth.verify_access_token(grant["access_token"], cfg=cfg)

    def test_the_sign_in_card_uses_the_theme_tokens(self):
        """Hard-coded colours are how a second theme silently breaks."""
        css = (ROOT / "app" / "static" / "app.css").read_text()
        block = css.split(".enter-card{")[1].split("}")[0]
        assert "var(--bg-card)" in block
        assert "var(--border-subtle)" in block
        assert "#" not in block, "no literal colours in the entry card"

    def test_the_card_measures_against_the_visual_viewport(self):
        css = (ROOT / "app" / "static" / "app.css").read_text()
        assert "--vvh" in css
        assert "100dvh" in css
        assert "env(safe-area-inset-bottom)" in css


# ===========================================================================
# 2. the five frontend safeguards, as source and route contracts
# ===========================================================================
class TestSyncWatermark:
    def test_the_watermark_moves_for_every_kind_of_change(self, app_client, cfg):
        first = app_client.get("/api/sync-watermark").json()
        assert first["ok"] and first["counter"] > 0

        # A read must not move it, or a tab would refresh itself forever.
        app_client.get("/api/cards")
        app_client.get("/api/search?q=cold")
        assert app_client.get("/api/sync-watermark").json()["counter"] == first["counter"]

        user_id = webapi.read_session_cookie(
            app_client.cookies.get(webapi.SESSION_COOKIE), cfg=cfg
        )
        ctx = E.build_context(user_id, cfg=cfg)
        assert E.op_remember_this(ctx, claim="A new claim entirely", when_it_applies="now").ok
        after_insert = app_client.get("/api/sync-watermark").json()
        # `remember_this` writes the claim *and* its `user_axiom` source, so the
        # counter moves by more than one. What matters is that it moves.
        assert after_insert["counter"] > first["counter"]

        claim_id = E.op_list_cards(ctx).data["claims"][0]["id"]
        # An *edit* is the case the obvious implementation missed: it changes
        # no row count and no timestamp, because claims have no `updated_at`.
        assert E.op_update_claim(ctx, claim_id, claim="Edited, not added").ok
        after_edit = app_client.get("/api/sync-watermark").json()
        assert after_edit["counter"] > after_insert["counter"]

        assert E.op_archive_item(ctx, "claim", claim_id).ok
        after_archive = app_client.get("/api/sync-watermark").json()
        assert after_archive["counter"] > after_edit["counter"]

    def test_the_probe_is_cheap(self, app_client):
        """It runs on every tab focus, so it must not render anything."""
        payload = app_client.get("/api/sync-watermark").json()
        assert set(payload) == {"ok", "counter", "updated_at", "writes"}

    def test_it_needs_a_session_like_every_other_endpoint(self, client):
        assert client.get("/api/sync-watermark").status_code == 401

    def test_the_page_restores_what_the_user_was_doing(self):
        """A refresh that loses your filter, your scroll position or the
        conflict you had open is worse than stale data."""
        js = (ROOT / "app" / "static" / "app.js").read_text()
        body = js.split("async function syncIfChanged()")[1].split("\n}")[0]
        assert "visibilityState" in body
        assert "/api/sync-watermark" in body
        for preserved in ("filter", "scroll", "inspector", "rendered", "expanded"):
            assert preserved in body, preserved
        assert "window.scrollTo" in body
        assert "reopenInspector" in body
        assert 'document.addEventListener("visibilitychange", syncIfChanged)' in js


class TestDomCap:
    def test_filtering_stays_over_the_full_arrays(self):
        """The cap is about layout cost, not about what is searchable: a term
        in claim 1,500 must still match."""
        js = (ROOT / "app" / "static" / "app.js").read_text()
        for fn in ("function visibleClaims()", "function visibleSkills()"):
            body = js.split(fn)[1].split("\n}")[0]
            assert "state.claims" in body or "state.skills" in body
            assert "rendered" not in body, "the filter must not know about paging"

    def test_only_a_bounded_number_of_cards_reaches_the_dom(self):
        js = (ROOT / "app" / "static" / "app.js").read_text()
        assert "const PAGE_SIZE = 100;" in js
        body = js.split("function renderCards()")[1].split("\n}")[0]
        assert "slice(0, state.rendered)" in body
        assert "Show more" in body and "remaining" in body

    def test_changing_the_filter_starts_the_list_over(self):
        js = (ROOT / "app" / "static" / "app.js").read_text()
        assert js.count("resetPaging()") >= 3
        assert "function resetPaging()" in js


class TestKeyboard:
    def test_slash_is_ignored_while_typing_but_the_chord_is_not(self):
        js = (ROOT / "app" / "static" / "app.js").read_text()
        body = js.split('document.addEventListener("keydown"')[1].split("\n});")[0]
        assert "isTyping()" in body
        assert "metaKey || event.ctrlKey" in body
        typing = js.split("function isTyping()")[1].split("\n}")[0]
        for tag in ("INPUT", "TEXTAREA", "SELECT"):
            assert tag in typing
        assert "isContentEditable" in typing

    def test_escape_closes_one_layer_at_a_time(self):
        """Escape in an open inspector means close this, not close this *and*
        throw away what I typed in the bar."""
        js = (ROOT / "app" / "static" / "app.js").read_text()
        body = js.split('if (event.key === "Escape")')[1].split("return;\n  }")[0]
        order = [
            body.index("closeProfile()"),
            body.index("closeInspector()"),
            body.index("state.plan = null"),
        ]
        assert order == sorted(order), "profile, then inspector, then the bar"


class TestRequestHeaderGuard:
    def test_a_mutation_without_the_header_is_refused(self, client):
        token = client.post("/api/login", json={"email": "founder@example.com"}).json()["csrf"]
        # A cross-origin HTML form post: the cookie rides along, and a form
        # cannot set a custom header at all.
        forged = client.post("/api/demo/clear", json={}, headers={"X-Brain-CSRF": token})
        assert forged.status_code == 403
        assert forged.json()["reason"] == "not_an_app_request"

    def test_the_header_alone_is_not_enough(self, client):
        client.post("/api/login", json={"email": "founder@example.com"})
        response = client.post(
            "/api/demo/clear", json={}, headers={"X-Brain-Request": "1", "X-Brain-CSRF": "wrong"}
        )
        assert response.status_code == 403
        assert response.json()["reason"] == "csrf_failed"

    def test_both_together_work(self, app_client):
        assert app_client.post("/api/demo/clear", json={}).status_code == 200

    def test_reads_are_unaffected(self, client):
        client.post("/api/login", json={"email": "founder@example.com"})
        assert client.get("/api/cards").status_code == 200

    def test_the_page_sends_it_on_every_mutation(self):
        js = (ROOT / "app" / "static" / "app.js").read_text()
        body = js.split("async function api(")[1].split("\n}")[0]
        assert '"X-Brain-Request"] = "1"' in body
        assert '!== "GET"' in body

    def test_the_api_surface_is_not_cross_origin_callable(self, client):
        """Which is what makes the header unforgeable: setting it forces a
        preflight, and the preflight for /api/* is not answered."""
        preflight = client.options(
            "/api/demo/clear",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-brain-request",
            },
        )
        assert preflight.status_code != 204
        assert "access-control-allow-origin" not in preflight.headers


# ===========================================================================
# 3. real-world MCP client interoperability
# ===========================================================================
class TestProxyAwareness:
    def test_uvicorn_is_launched_behind_a_proxy(self):
        source = inspect.getsource(__import__("app.cli", fromlist=["cmd_serve"]).cmd_serve)
        assert "proxy_headers=True" in source
        assert 'forwarded_allow_ips="*"' in source

    def test_every_launcher_goes_through_that_one_definition(self):
        """One definition instead of three that can drift."""
        entrypoint = (ROOT / "entrypoint.sh").read_text()
        assert entrypoint.count("app.cli serve") >= 2
        assert "--proxy-headers" in entrypoint  # named in the comment, set in cmd_serve
        assert "proxy" in json.loads((ROOT / "railway.json").read_text())["_notes"]

    @pytest.mark.parametrize(
        "public,expected",
        [
            ("http://brain.up.railway.app", "https://brain.up.railway.app"),
            ("https://brain.example", "https://brain.example"),
            ("https://brain.example/", "https://brain.example"),
        ],
    )
    def test_discovery_urls_are_always_https_in_prod(self, cfg, monkeypatch, public, expected):
        """Behind Railway's terminator the app speaks plain HTTP. An `http://`
        endpoint in a discovery document is refused as mixed content by a
        browser client, and would put an authorization code in the clear."""
        import dataclasses

        monkeypatch.setenv("BRAIN_PUBLIC_URL", public)
        prod = dataclasses.replace(cfg, env="prod")
        assert auth.issuer(prod) == expected

    def test_loopback_stays_plain_http(self, cfg, monkeypatch):
        """There is no transport to protect on the same machine, and upgrading
        it would break every desktop bridge."""
        import dataclasses

        monkeypatch.setenv("BRAIN_PUBLIC_URL", "http://127.0.0.1:8080")
        assert auth.issuer(dataclasses.replace(cfg, env="prod")) == "http://127.0.0.1:8080"

    def test_the_metadata_documents_emit_https(self, client, monkeypatch):
        monkeypatch.setenv("BRAIN_PUBLIC_URL", "http://brain.example")
        document = client.get("/.well-known/oauth-authorization-server").json()
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"):
            assert document[key].startswith("https://"), key


class TestLoopbackRedirects:
    def test_a_native_client_may_register_a_loopback_callback(self, client):
        for uri in ("http://127.0.0.1:1/callback", "http://localhost:0/cb", "http://[::1]:1/cb"):
            response = client.post(
                "/oauth/register", json={"client_name": "Cursor", "redirect_uris": [uri]}
            )
            assert response.status_code == 201, uri
            assert response.json()["token_endpoint_auth_method"] == "none"

    def test_plain_http_anywhere_else_is_refused(self, client):
        """A code on the wire in the clear is not a client's choice to make."""
        for uri in ("http://evil.example/cb", "http://10.0.0.5/cb", "not-a-url", ""):
            response = client.post("/register", json={"redirect_uris": [uri]})
            assert response.status_code == 400, uri

    def test_any_ephemeral_port_is_accepted_at_authorize_time(self, client):
        """RFC 8252 §7.3: a desktop bridge binds its port at launch, so the one
        it registered yesterday is not the one it is listening on now."""
        client_id = client.post(
            "/oauth/register",
            json={"client_name": "Claude Desktop", "redirect_uris": ["http://127.0.0.1:1/cb"]},
        ).json()["client_id"]

        for port in (1, 54321, 65535):
            page = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": f"http://127.0.0.1:{port}/cb",
                    "code_challenge": auth.pkce_challenge(VERIFIER),
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            assert page.status_code == 200, port

    @pytest.mark.parametrize(
        "requested",
        [
            "http://127.0.0.1:54321/other",     # a different path
            "http://localhost:54321/cb",        # a different host spelling
            "https://127.0.0.1:54321/cb",       # a different scheme
            "http://127.0.0.2:54321/cb",        # a different address
        ],
    )
    def test_only_the_port_may_vary(self, client, requested):
        """Anything looser than that turns this into an open redirector."""
        client_id = client.post(
            "/register",
            json={"client_name": "Claude Desktop", "redirect_uris": ["http://127.0.0.1:1/cb"]},
        ).json()["client_id"]
        page = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": requested,
                "code_challenge": auth.pkce_challenge(VERIFIER),
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert page.status_code == 400

    def test_an_https_callback_still_has_to_match_exactly(self, client):
        client_id = client.post(
            "/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            },
        ).json()["client_id"]
        for uri, status in (
            ("https://claude.ai/api/mcp/auth_callback", 200),
            ("https://claude.ai/api/mcp/auth_callback2", 400),
            ("https://evil.example/api/mcp/auth_callback", 400),
        ):
            page = client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": uri,
                    "code_challenge": auth.pkce_challenge(VERIFIER),
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            assert page.status_code == status, uri


class TestOauthPathAliases:
    @pytest.mark.parametrize("path", ["/register", "/oauth/register"])
    def test_registration_answers_on_both(self, client, path):
        response = client.post(
            path, json={"client_name": "c", "redirect_uris": ["https://c.example/cb"]}
        )
        assert response.status_code == 201

    def test_the_metadata_advertises_the_canonical_paths(self, client):
        """A conforming client reads these rather than guessing; the aliases
        are there for the ones that guess anyway."""
        document = client.get("/.well-known/oauth-authorization-server").json()
        assert document["authorization_endpoint"].endswith("/authorize")
        assert document["token_endpoint"].endswith("/token")

    def test_the_consent_form_posts_back_to_the_path_it_was_served_on(self, client):
        client_id = client.post(
            "/oauth/register", json={"client_name": "Claude", "redirect_uris": ["https://c.example/cb"]}
        ).json()["client_id"]
        page = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://c.example/cb",
                "code_challenge": auth.pkce_challenge(VERIFIER),
                "code_challenge_method": "S256",
            },
        )
        assert 'action="/oauth/authorize"' in page.text


class TestConsentCard:
    def _page(self, client, **overrides):
        client_id = client.post(
            "/register",
            json={"client_name": "Claude Desktop", "redirect_uris": ["https://c.example/cb"]},
        ).json()["client_id"]
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://c.example/cb",
            "code_challenge": auth.pkce_challenge(VERIFIER),
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        return client.get("/authorize", params=params)

    def test_it_states_what_the_agent_can_and_cannot_do(self, client):
        text = self._page(client).text
        assert "Connect <strong>Claude Desktop</strong> to brain" in text
        assert "Cannot permanently delete items" in text
        assert "soft archive only" in text
        assert "30 days, revocable anytime" in text
        assert "Authorize Connection" in text
        assert 'id="consent-cancel"' in text

    def test_it_shares_the_product_stylesheet(self, client):
        """One definition of `--accent` for the whole product."""
        text = self._page(client).text
        assert '<link rel="stylesheet" href="/app.css">' in text
        assert "<style" not in text

    def test_its_script_is_external_because_the_csp_forbids_inline(self, client):
        text = self._page(client).text
        assert '<script src="/consent.js" defer></script>' in text
        assert "onclick=" not in text
        assert client.get("/consent.js").status_code == 200
        assert "script-src 'self'" in client.get("/consent.js").headers["content-security-policy"]

    def test_it_asks_who_you_are_only_when_it_does_not_know(self, client):
        anonymous = self._page(client).text
        assert 'id="email"' in anonymous
        assert "consent-identity" not in anonymous

        client.post("/api/login", json={"email": "founder@example.com"})
        known = self._page(client).text
        assert "consent-identity" in known
        assert "founder@example.com" in known
        assert 'id="email"' not in known

    def test_the_client_name_is_escaped(self, client):
        """It comes from an unauthenticated registration call."""
        client_id = client.post(
            "/register",
            json={
                "client_name": '<img src=x onerror=alert(1)>',
                "redirect_uris": ["https://c.example/cb"],
            },
        ).json()["client_id"]
        page = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://c.example/cb",
                "code_challenge": auth.pkce_challenge(VERIFIER),
                "code_challenge_method": "S256",
            },
        )
        assert "<img src=x" not in page.text
        assert "&lt;img" in page.text

    def test_approving_still_issues_a_code_for_the_right_client(self, client):
        import re

        page = self._page(client)
        blob = re.search(r'name="state_blob" value="([^"]+)"', page.text).group(1)
        granted = client.post(
            "/authorize",
            data={"state_blob": blob.replace("&amp;", "&"), "email": "founder@example.com"},
            follow_redirects=False,
        )
        assert granted.status_code == 302
        assert granted.headers["location"].startswith("https://c.example/cb?code=")


class TestMcpWireHeaders:
    def test_options_is_answered_before_any_auth_check(self, client):
        """A browser preflight carries no Authorization header by definition,
        so a 401 here would stop every browser client before it starts."""
        response = client.options(
            "/mcp",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,mcp-session-id",
            },
        )
        assert response.status_code == 204
        assert "www-authenticate" not in response.headers

    @pytest.mark.parametrize(
        "header", ["Authorization", "Content-Type", "Mcp-Session-Id", "MCP-Protocol-Version"]
    )
    def test_the_allowed_request_headers_cover_the_transport(self, client, header):
        allowed = client.options(
            "/mcp", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"}
        ).headers["access-control-allow-headers"].lower()
        assert header.lower() in allowed

    @pytest.mark.parametrize("header", ["WWW-Authenticate", "Mcp-Session-Id"])
    def test_the_exposed_response_headers_cover_discovery_and_sessions(self, client, header):
        exposed = client.options(
            "/mcp", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"}
        ).headers["access-control-expose-headers"].lower()
        assert header.lower() in exposed

    def test_delete_is_advertised_as_allowed(self, client):
        """The transport closes a session with it."""
        allowed = client.options(
            "/mcp", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "DELETE"}
        ).headers["access-control-allow-methods"]
        assert "DELETE" in allowed

    def test_streams_are_marked_unbufferable(self, client):
        """Railway fronts the app with a proxy, and a buffered SSE stream looks
        to the client exactly like a hung tool call."""
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-cache, no-transform"

    def test_the_headers_are_only_on_the_mcp_surface(self, client):
        """`no-transform` on every asset would stop the proxy compressing them."""
        assert "x-accel-buffering" not in client.get("/app.css").headers

    def test_a_delete_without_a_session_is_handled_not_crashed(self, client):
        response = client.request(
            "DELETE", "/mcp", headers={"Accept": "application/json, text/event-stream"}
        )
        assert response.status_code < 500

    def test_the_header_constants_are_what_the_middleware_sends(self, client):
        assert "MCP-Protocol-Version".lower() in {h.lower() for h in CORS_ALLOW_HEADERS}
        assert set(CORS_EXPOSE_HEADERS) >= {"WWW-Authenticate", "Mcp-Session-Id"}
        assert STREAM_HEADERS["X-Accel-Buffering"] == "no"


# ===========================================================================
# 4. the races
# ===========================================================================
class FakeBucket:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.objects: dict[str, bytes] = {}
        self.gets = 0
        self.puts = 0
        self.delay = delay
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.split("/brain-tenants/", 1)[-1]
        if request.method == "PUT":
            with self.lock:
                self.puts += 1
                self.objects[key] = request.content
            return httpx.Response(200)
        with self.lock:
            present = key in self.objects
            if present:
                self.gets += 1
        if not present:
            return httpx.Response(404)
        if self.delay:
            time.sleep(self.delay)  # a slow link is where the race shows up
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


class TestSingleflightHydration:
    def _cold_tenant(self, cfg, bucket, email="cold@example.com") -> str:
        """A tenant that exists in R2 and not on this disk."""
        user_id = control_db.ensure_user(control_db.control(), email)["user_id"]
        ctx = E.build_context(user_id, cfg=cfg)
        assert E.op_remember_this(
            ctx, claim="Survived the move to another node", when_it_applies="always"
        ).ok
        assert r2_sync.snapshot_and_upload(user_id, cfg=cfg) is True
        tenant_db.close_tenant(user_id, cfg=cfg)
        for suffix in ("", "-wal", "-shm"):
            Path(str(cfg.tenant_db_path(user_id)) + suffix).unlink(missing_ok=True)
        assert not cfg.tenant_db_path(user_id).exists()
        return user_id

    def test_concurrent_requests_for_a_cold_tenant_share_one_download(self, cfg):
        """A page load fires /api/cards, /api/profile and /api/jobs together.
        Without the lock each starts its own GET and they race on the same
        `.hydrating` path — one gets renamed into place and then overwritten
        underneath an open connection."""
        bucket = FakeBucket(delay=0.25).install()
        user_id = self._cold_tenant(cfg, bucket)

        results: list[int] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(5)

        def open_it() -> None:
            try:
                barrier.wait()
                conn = tenant_db.tenant(user_id, cfg=cfg)
                results.append(conn.execute("SELECT count(*) FROM epistemic_claims").fetchone()[0])
            except Exception as exc:  # noqa: BLE001 - recorded and asserted on
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert errors == [], errors
        assert results == [1] * 5, "every caller saw the hydrated library"
        assert bucket.gets == 1, f"one GET, not {bucket.gets}"
        assert list(cfg.tenant_db_path(user_id).parent.glob("*.hydrating")) == []

    def test_a_second_open_after_hydration_costs_nothing(self, cfg):
        bucket = FakeBucket().install()
        user_id = self._cold_tenant(cfg, bucket)
        tenant_db.tenant(user_id, cfg=cfg)
        assert bucket.gets == 1
        tenant_db.close_tenant(user_id, cfg=cfg)
        tenant_db.tenant(user_id, cfg=cfg)
        assert bucket.gets == 1

    def test_the_lock_is_per_tenant_not_global(self, cfg):
        """Two different cold tenants must not serialise behind each other."""
        bucket = FakeBucket().install()
        first = self._cold_tenant(cfg, bucket, "one@example.com")
        second = self._cold_tenant(cfg, bucket, "two@example.com")
        assert r2_sync._hydration_lock(first) is not r2_sync._hydration_lock(second)
        assert r2_sync._hydration_lock(first) is r2_sync._hydration_lock(first)
        tenant_db.tenant(first, cfg=cfg)
        tenant_db.tenant(second, cfg=cfg)
        assert bucket.gets == 2

    def test_hydration_is_a_no_op_without_object_storage(self, cfg, user):
        r2_sync.set_config(r2_sync.R2Config())
        assert r2_sync.hydrate(user["user_id"], cfg.tenant_db_path(user["user_id"])) is False


class TestShutdownFlush:
    def test_the_lifespan_flushes_a_debounced_write(self, cfg, monkeypatch, user):
        monkeypatch.setenv("BRAIN_JWT_SECRET", JWT_SECRET)
        bucket = FakeBucket().install()
        user_id = user["user_id"]
        M.reset_server()
        with TestClient(create_app(cfg)) as client:
            assert client.get("/healthz").status_code == 200
            ctx = E.build_context(user_id, cfg=cfg)
            assert E.op_remember_this(ctx, claim="Written just before SIGTERM", when_it_applies="now").ok
            assert r2_sync.is_dirty(user_id) is True
            assert bucket.puts == 0
        assert bucket.puts >= 1
        assert r2_sync.is_dirty(user_id) is False
        M.reset_server()

    def test_the_flush_runs_from_a_finally_block(self):
        """SIGTERM arrives as a cancellation *at* the yield, so a flush written
        after it would never run."""
        source = (ROOT / "app" / "main.py").read_text()
        body = source.split("async def lifespan")[1].split("app = FastAPI")[0]
        assert body.index("yield") < body.index("finally:") < body.index("stop_background")

    def test_the_worker_flushes_too(self):
        source = (ROOT / "app" / "worker.py").read_text()
        assert "r2_sync.start_background" in source
        assert "r2_sync.stop_background" in source.split("finally:")[1]


class TestRetryBackoff:
    """Covered in depth by `tests/test_phase7_production_resilience.py`; this is
    the contract that must not regress."""

    def test_the_retry_and_fatal_sets_are_what_they_should_be(self):
        from app.ingestion import providers as P

        assert set(P.RETRY_STATUSES) >= {429, 502, 503, 504}
        assert set(P.FATAL_STATUSES) == {400, 401, 403, 404}
        assert P.MAX_BACKOFF_SEC <= 10

    def test_the_sleep_is_injectable(self):
        from app.ingestion import providers as P

        seen: list[float] = []
        P.set_sleeper(seen.append)
        try:
            P._sleeper(1.5)
            assert seen == [1.5]
        finally:
            P.set_sleeper(None)
        assert P._sleeper is time.sleep


# ===========================================================================
# 5. the browser journey
# ===========================================================================
@pytest.fixture(scope="class")
def journey_server(tmp_path_factory):
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

    workdir = tmp_path_factory.mktemp("journey")
    env = {
        **os.environ,
        "BRAIN_ENV": "test",
        "BRAIN_DATA_DIR": str(workdir),
        "BRAIN_JWT_SECRET": JWT_SECRET,
        "BRAIN_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
        "BRAIN_PUBLIC_URL": f"http://127.0.0.1:{port}",
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
def journey(journey_server):
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "browser" / "human_journey.js")],
        capture_output=True, text=True, timeout=900,
        env={
            **os.environ,
            "BASE": journey_server,
            "CHALLENGE": auth.pkce_challenge(VERIFIER),
            "VERIFIER": VERIFIER,
        },
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


class TestHumanJourneyInABrowser:
    def test_no_script_errors_anywhere_in_the_journey(self, journey):
        assert journey["errors"] == [], journey["errors"]

    def test_the_cold_door_is_a_card_and_nothing_else(self, journey):
        door = journey["door"]
        assert door["cardVisible"] is True
        assert door["heading"] == "Sign in to brain"
        assert door["sidebarHidden"] is True
        assert door["omniHidden"] is True
        assert door["points"] == 3
        assert door["inlineError"] is True, "a bad address is answered on the card"

    def test_the_card_is_painted_from_the_theme_token(self, journey):
        """Not a hard-coded white, or the dark theme would show a white card."""
        assert journey["door"]["cardBg"] == "rgb(255, 255, 255)"
        assert journey["door"]["tokenBg"].upper() == "#FFFFFF"

    def test_signing_up_lands_in_a_workspace_with_the_demo(self, journey):
        workspace = journey["workspace"]
        assert workspace["cards"] == 3
        assert workspace["skills"] == 1
        assert workspace["conflicts"] >= 1
        assert workspace["signInGone"] is True

    def test_connect_to_claude_shows_the_link_and_copies_it(self, journey):
        connect = journey["connect"]
        assert connect["endsWithMcp"] is True
        assert connect["clipboard"] == connect["mcpUrl"]
        assert connect["buttonText"] == "Copied ✓"
        assert connect["textRestored"] is True, "the copied state is transient"
        assert connect["closedByEscape"] is True

    def test_the_consent_card_states_the_bargain(self, journey):
        consent = journey["consent"]
        assert consent["registerStatus"] == 201
        assert consent["heading"] == "Connect Claude Desktop to brain"
        joined = " ".join(consent["grants"])
        assert "Cannot permanently delete" in joined
        assert "soft archive only" in joined
        assert "30 days" in joined
        assert consent["hasCancel"] is True
        # Already signed in here, so it does not ask again.
        assert consent["identity"] is True and consent["emailPrompt"] is False
        assert consent["theme"] in ("light", "dark", "warm_sunset")
        assert consent["cardBg"] != "rgba(0, 0, 0, 0)", "the shared stylesheet loaded"

    def test_approving_redirects_to_the_clients_ephemeral_port(self, journey):
        assert "127.0.0.1:54321" in journey["consent"]["redirected"]
        assert "code=" in journey["consent"]["redirected"]

    def test_the_client_exchanges_the_code_for_a_30_day_grant(self, journey):
        exchange = journey["consent"]["exchange"]
        assert exchange["status"] == 200
        assert exchange["hasToken"] is True
        assert exchange["expiresIn"] == 3600

    def test_the_new_session_is_visible_and_revocable_in_profile(self, journey):
        session = journey["session"]
        assert session["rows"] == 1
        assert "Claude Desktop" in session["text"]
        assert "Expires in 30 days" in session["text"]
        assert session["hasRevoke"] == 1

    def test_a_background_change_is_picked_up_without_losing_your_place(self, journey):
        sync = journey["sync"]
        assert sync["picked"] is True, sync
        assert sync["filterKept"] is True, "and kept what the user had typed"

    def test_the_xss_canary_never_fires(self, journey):
        xss = journey["xss"]
        assert xss["fired"] == "no", "an onerror payload executed"
        assert xss["imgTags"] == 0, "the payload became a real element"
        assert xss["visibleAsText"] is True, "it round-tripped and was escaped, not dropped"

    def test_nothing_overflows_at_390_wide(self, journey):
        failures = [
            f"{row['where']}: scrollWidth={row['scrollWidth']} offenders={row['offenders']}"
            for row in journey["mobile"]
            if row["scrollWidth"] > MOBILE_WIDTH or row["offenders"]
        ]
        assert failures == [], "\n".join(failures)

    def test_every_layout_and_state_was_measured_on_the_phone(self, journey):
        where = {row["where"] for row in journey["mobile"]}
        assert {"door", "workspace"} <= where
        for layout in ("B1", "B2", "B3", "merged"):
            for state in ("happy", "blocked", "zero-match", "first-run", "three-way",
                          "long-quote", "ingesting", "empty", "offline"):
                assert f"{layout}/{state}" in where

    def test_the_tuner_starts_collapsed_on_a_phone(self, journey):
        """160px of dev chrome over the bottom of the page is how a bottom
        action becomes unreachable."""
        tuner = journey["tuner"]
        assert tuner["collapsed"] == "true"
        assert tuner["handleVisible"] is True
        assert tuner["height"] <= 40, tuner
        assert tuner["expandedHeight"] > tuner["height"]

    def test_the_consent_card_is_usable_on_a_phone(self, journey):
        mobile = journey["consentMobile"]
        assert mobile["scrollWidth"] <= MOBILE_WIDTH
        assert mobile["offenders"] == []
        assert mobile["buttonInView"]["covered"] is False
