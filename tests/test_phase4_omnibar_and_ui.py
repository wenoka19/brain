"""Phase 4 — the Hybrid Omni-Bar router, the web API, and the Checkpoint B UI."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import mcp_server as M
from app import omnibar, webapi
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.main import create_app

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def ctx(cfg, user):
    return E.build_context(user["user_id"], surface="web_omnibar")


@pytest.fixture
def seeded(ctx):
    E.op_seed_demo(ctx)
    return ctx


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("BRAIN_JWT_SECRET", "phase4-test-secret-key-long-enough")
    M.reset_server()
    app = create_app(cfg)
    with TestClient(app) as test_client:
        yield test_client
    M.reset_server()


@pytest.fixture
def signed_in(client):
    response = client.post("/api/login", json={"email": "founder@example.com"})
    assert response.status_code == 200
    client.headers.update({
        "X-Brain-CSRF": response.json()["csrf"],
        # The app sends this on every mutation; a cross-origin form cannot.
        "X-Brain-Request": "1",
    })
    return client


@pytest.fixture
def blank(signed_in):
    """A signed-in client with an *empty* library.

    Signing in now seeds the three demo items (Rule 4c), which is what a new
    user should land in — but a test about ingesting a source, or about the
    first-run state, needs the library those items would otherwise occupy.
    """
    assert signed_in.post("/api/demo/clear", json={}).status_code == 200
    return signed_in


# ===========================================================================
# the Omni-Bar router
# ===========================================================================
class TestScopeParsing:
    @pytest.mark.parametrize(
        "raw,text,scope",
        [
            ("@Skills cold email", "cold email", "skills"),
            ("@Library batching", "batching", "claims"),
            ("@Archived old stuff", "old stuff", "archived"),
            ("@Everything x", "x", "all"),
            ("no scope here", "no scope here", "all"),
        ],
    )
    def test_scope_tokens_are_lifted_out_of_the_text(self, cfg, raw, text, scope):
        parsed_text, parsed_scope, _slug = omnibar.split_scope(raw)
        assert parsed_text == text
        assert parsed_scope == scope

    def test_an_unknown_at_token_scopes_to_that_skill_slug(self, cfg):
        text, scope, slug = omnibar.split_scope("@cold-email-sprint step 2")
        assert text == "step 2" and scope == "skills" and slug == "cold-email-sprint"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://youtu.be/abc", True),
            ("www.example.com/post", True),
            ("example.com/post", True),
            ("cold email batching", False),
            ("archive the video", False),
            ("", False),
        ],
    )
    def test_url_detection(self, cfg, raw, expected):
        assert omnibar.looks_like_url(raw) is expected


class TestRouting:
    def test_typing_only_ever_filters(self, seeded):
        """Rule 1: the 0ms path must never become a command or a request."""
        for text in ("archive everything", "delete my skills", "cold email"):
            plan = omnibar.route(seeded, text, submitted=False)
            assert plan.mode == omnibar.MODE_FILTER
            assert plan.action == ""
            assert plan.requires_confirmation is False

    def test_a_url_routes_to_ingestion_without_submission(self, seeded):
        plan = omnibar.route(seeded, "https://youtu.be/dQw4w9WgXcQ", submitted=False)
        assert plan.mode == omnibar.MODE_INGEST

    @pytest.mark.parametrize(
        "text,action",
        [
            ("archive yesterdays video", "archive"),
            ("hide the cold email sprint", "archive"),
            ("restore the cold email sprint", "restore"),
            ("rename cold email sprint to Outbound Blitz", "rename"),
            ("edit step 2 of cold email sprint to Write on Sunday", "edit_step"),
            ("remember that I never send on Fridays", "remember"),
            ("I always warm up a new domain first", "remember"),
        ],
    )
    def test_commands_resolve_deterministically(self, seeded, text, action):
        plan = omnibar.route(seeded, text, submitted=True)
        assert plan.mode == omnibar.MODE_COMMAND, text
        assert plan.action == action
        assert plan.requires_confirmation is True
        assert plan.confirm_text.endswith("?")
        assert plan.router == "deterministic"

    def test_a_command_never_executes_during_routing(self, seeded):
        before = tenant_db.counts(seeded.tenant)
        omnibar.route(seeded, "archive yesterdays video", submitted=True)
        assert tenant_db.counts(seeded.tenant) == before

    def test_find_and_show_are_filters_not_commands(self, seeded):
        for text in ("find outbound claims", "show me the cold email skill", "search for batching"):
            assert omnibar.route(seeded, text, submitted=True).mode == omnibar.MODE_FILTER

    def test_an_unresolvable_target_asks_instead_of_guessing(self, seeded):
        plan = omnibar.route(seeded, "archive the quantum flux capacitor", submitted=True)
        assert plan.mode == omnibar.MODE_CLARIFY
        assert "clearly matches" in plan.message

    def test_an_unparsed_submission_degrades_to_a_filter_and_says_so(self, seeded):
        plan = omnibar.route(seeded, "what is the weather in Lagos", submitted=True)
        assert plan.mode == omnibar.MODE_FILTER
        assert plan.router == "none"
        assert "did not read as a command" in plan.message

    def test_typed_delete_can_never_hard_delete(self, seeded):
        """"delete" is routed to Trash, and the plan says so out loud."""
        plan = omnibar.route(seeded, "delete the Enterprise Outbound Benchmark", submitted=True)
        assert plan.action in ("trash", "archive")
        assert "Nothing is deleted outright" in plan.message
        assert "30 days" in plan.message

    def test_recency_words_resolve_to_the_newest_source(self, seeded):
        newest = tenant_db.upsert_source(
            seeded.tenant, title="Freshest Source", source_type="article",
            canonical_url="https://example.com/fresh",
        )
        plan = omnibar.route(seeded, "archive the latest article", submitted=True)
        assert plan.target_id == newest["id"]

    def test_the_llm_router_is_consulted_only_as_a_fallback(self, seeded):
        calls = []

        def router(text, context):
            calls.append(text)
            return {
                "action": "archive",
                "target_kind": "skill",
                "target_id": "skl_x",
                "target_label": "Something",
                "confirm_text": "Archive “Something”?",
            }

        omnibar.set_llm_router(router)
        try:
            # A pattern match must not reach the model.
            omnibar.route(seeded, "archive yesterdays video", submitted=True)
            assert calls == []
            plan = omnibar.route(seeded, "tidy up the thing from before", submitted=True)
            assert calls and plan.router == "llm"
            assert plan.requires_confirmation is True  # still only a proposal
        finally:
            omnibar.set_llm_router(None)

    def test_the_llm_router_is_never_asked_while_typing(self, seeded):
        calls = []
        omnibar.set_llm_router(lambda text, context: calls.append(text) or None)
        try:
            omnibar.route(seeded, "tidy up the thing", submitted=False)
            assert calls == []
        finally:
            omnibar.set_llm_router(None)


class TestExecution:
    def _run(self, ctx, text):
        plan = omnibar.route(ctx, text, submitted=True)
        return omnibar.execute(ctx, plan)

    def test_archive_then_undo_round_trips(self, seeded):
        receipt, _ = self._run(seeded, "archive yesterdays video")
        assert receipt.ok and receipt.undo_label == "Undo"
        assert tenant_db.counts(seeded.tenant)["claims"] == 1
        undone = omnibar.undo(seeded, receipt.undo_action, receipt.undo_params)
        assert undone.ok
        assert tenant_db.counts(seeded.tenant)["claims"] == 2

    def test_a_step_edit_offers_revert_to_the_previous_version(self, seeded):
        receipt, _ = self._run(
            seeded, "edit step 2 of cold email sprint to Write the sequence on Sunday"
        )
        assert receipt.ok
        assert receipt.undo_action == "revert_skill"
        assert receipt.undo_label == "Revert to v1"
        skill = tenant_db.get_skill_by_slug(seeded.tenant, E.DEMO_SKILL_SLUG)
        assert skill["execution_steps"][1] == "Write the sequence on Sunday"
        assert skill["version"] == 2

        omnibar.undo(seeded, receipt.undo_action, receipt.undo_params)
        restored = tenant_db.get_skill_by_slug(seeded.tenant, E.DEMO_SKILL_SLUG)
        assert restored["execution_steps"][1] == "Write the whole sequence in one sitting"

    def test_a_rename_is_versioned_too(self, seeded):
        receipt, _ = self._run(seeded, "rename cold email sprint to Outbound Blitz")
        assert receipt.ok and receipt.undo_action == "revert_skill"
        assert tenant_db.get_skill_by_slug(seeded.tenant, E.DEMO_SKILL_SLUG)["name"] == "Outbound Blitz"

    def test_an_out_of_range_step_is_refused_with_the_real_count(self, seeded):
        receipt, _ = self._run(seeded, "edit step 9 of cold email sprint to nope")
        assert not receipt.ok
        assert receipt.error_reason == "no_such_step"
        assert "has 3 steps" in receipt.text

    def test_remembering_a_rule_reports_the_conflicts_it_creates(self, seeded):
        receipt, _ = self._run(
            seeded, "remember that I never batch cold email, continuous always wins for my team"
        )
        assert receipt.ok
        assert "disagrees with" in receipt.text
        assert receipt.undo_action == "archive_claim"

    def test_trash_from_the_bar_is_recoverable(self, seeded):
        receipt, _ = self._run(seeded, "delete the Enterprise Outbound Benchmark")
        assert receipt.ok and "30 days" in receipt.text
        assert len(E.op_list_trash(seeded).data["items"]) == 1
        assert omnibar.undo(seeded, receipt.undo_action, receipt.undo_params).ok
        assert E.op_list_trash(seeded).data["items"] == []

    def test_ingest_through_the_bar_queues_without_processing(self, seeded):
        plan = omnibar.route(seeded, "https://example.com/a-new-article", submitted=True)
        receipt, result = omnibar.execute(seeded, plan)
        assert receipt.ok and "Queued" in receipt.text
        assert result.data["status"] == "queued"

    def test_an_unknown_undo_is_refused(self, seeded):
        assert omnibar.undo(seeded, "drop_database", {}).error_reason == "not_undoable"


# ===========================================================================
# the web API
# ===========================================================================
class TestWebApiAuth:
    def test_everything_needs_a_session(self, client):
        assert client.get("/api/cards").status_code == 401
        assert client.get("/api/profile").status_code == 401
        assert client.post("/api/ingest", json={"url": YOUTUBE_URL}).status_code in (401, 403)

    def test_login_issues_an_httponly_session_and_a_readable_csrf_token(self, client):
        response = client.post("/api/login", json={"email": "founder@example.com"})
        assert response.status_code == 200
        cookies = response.headers.get_list("set-cookie")
        session = next(c for c in cookies if c.startswith(webapi.SESSION_COOKIE))
        csrf = next(c for c in cookies if c.startswith(webapi.CSRF_COOKIE))
        assert "httponly" in session.lower()
        assert "samesite=strict" in session.lower()
        assert "httponly" not in csrf.lower()  # the page must be able to echo it
        assert response.json()["csrf"]

    def test_a_bad_email_is_refused(self, client):
        assert client.post("/api/login", json={"email": "not-an-email"}).status_code == 400

    def test_a_tampered_session_cookie_is_rejected(self, client, cfg):
        client.post("/api/login", json={"email": "founder@example.com"})
        good = client.cookies.get(webapi.SESSION_COOKIE)
        client.cookies.set(webapi.SESSION_COOKIE, good[:-4] + "AAAA")
        assert client.get("/api/cards").status_code == 401

    def test_mutations_require_the_csrf_header(self, client):
        response = client.post("/api/login", json={"email": "founder@example.com"})
        token = response.json()["csrf"]
        # Cookie present, header absent: a cross-site form post.
        assert client.post("/api/demo/seed", json={}).status_code == 403
        assert client.post(
            "/api/demo/seed", json={}, headers={"X-Brain-CSRF": "wrong", "X-Brain-Request": "1"}
        ).status_code == 403
        assert client.post(
            "/api/demo/seed", json={}, headers={"X-Brain-CSRF": token, "X-Brain-Request": "1"}
        ).status_code == 200

    def test_reads_do_not_require_csrf(self, signed_in):
        assert signed_in.get("/api/cards").status_code == 200

    def test_logout_clears_the_session(self, signed_in):
        assert signed_in.post("/api/logout", json={}).status_code == 200
        signed_in.cookies.clear()
        assert signed_in.get("/api/cards").status_code == 401

    def test_signing_in_lands_in_a_seeded_workspace(self, client):
        """Rule 4c: the first screen shows the product, not an empty canvas."""
        assert client.post("/api/login", json={"email": "new@example.com"}).json()["seeded"] is True
        counts = client.get("/api/cards").json()["library_counts"]
        assert counts["claims"] == 2 and counts["skills"] == 1 and counts["tensions"] == 1
        assert client.get("/api/me").json()["first_run"] is False

    def test_the_first_run_state_is_reachable_by_clearing_the_demo(self, blank):
        """Clearing it has to put the onboarding card back, or a user who
        clears the demo is left staring at a blank page with no next step."""
        payload = blank.get("/api/me").json()
        assert payload["signed_in"] is True and payload["first_run"] is True

    def test_seeding_an_already_seeded_library_is_a_no_op(self, signed_in):
        assert signed_in.post("/api/demo/seed", json={}).json()["already_seeded"] is True


class TestWebApiSurface:
    def test_cards_serve_the_four_sidebar_views(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        everything = signed_in.get("/api/cards?view=everything").json()
        assert everything["claims"] and everything["skills"]
        assert signed_in.get("/api/cards?view=skills").json()["claims"] == []
        assert signed_in.get("/api/cards?view=library").json()["skills"] == []
        assert signed_in.get("/api/cards?view=archived").json()["claims"] == []
        # An unknown view degrades rather than erroring.
        assert signed_in.get("/api/cards?view=nonsense").json()["view"] == "everything"

    def test_cards_carry_the_outer_doll_fields_and_the_deep_link(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        claim = signed_in.get("/api/cards?view=library").json()["claims"][0]
        for field in ("claim", "when_it_applies", "locator_type", "locator_value",
                      "deep_link", "anchor_quote", "tensions", "salience"):
            assert field in claim
        assert claim["deep_link"]
        assert claim["tensions"] and claim["tensions"][0]["other_anchor_quote"]

    def test_search_is_rate_limited_like_the_mcp_surface(self, signed_in, cfg):
        signed_in.post("/api/demo/seed", json={})
        statuses = {
            signed_in.get(f"/api/search?q=batch+{index}").status_code
            for index in range(cfg.tool_calls_per_min + 4)
        }
        assert 429 in statuses

    def test_the_omnibar_plans_and_runs_in_two_separate_calls(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        plan = signed_in.post(
            "/api/omnibar", json={"text": "archive yesterdays video", "submitted": True}
        ).json()["plan"]
        assert plan["requires_confirmation"] is True
        # Planning changed nothing.
        assert len(signed_in.get("/api/cards?view=library").json()["claims"]) == 2

        receipt = signed_in.post("/api/omnibar/run", json={"plan": plan}).json()["receipt"]
        assert receipt["ok"] and receipt["undo_action"]
        assert len(signed_in.get("/api/cards?view=library").json()["claims"]) == 1

        undone = signed_in.post(
            "/api/omnibar/undo",
            json={"action": receipt["undo_action"], "params": receipt["undo_params"]},
        ).json()
        assert undone["ok"]
        assert len(signed_in.get("/api/cards?view=library").json()["claims"]) == 2

    def test_running_a_plan_needs_csrf(self, client):
        token = client.post("/api/login", json={"email": "f@example.com"}).json()["csrf"]
        plan = client.post(
            "/api/omnibar",
            json={"text": "cold email", "submitted": False},
            headers={"X-Brain-CSRF": token, "X-Brain-Request": "1"},
        ).json()["plan"]
        assert client.post("/api/omnibar/run", json={"plan": plan}).status_code == 403

    def test_a_bogus_plan_is_refused(self, signed_in):
        assert signed_in.post("/api/omnibar/run", json={"plan": {}}).status_code == 400

    def test_the_ingestion_pipeline_runs_from_the_browser(self, blank):
        queued = blank.post("/api/ingest", json={"url": YOUTUBE_URL}).json()
        assert queued["ok"] and queued["status"] == "queued"
        processed = blank.post("/api/jobs/run", json={}).json()["processed"]
        assert processed and processed[0]["status"] == "ready"
        jobs = blank.get("/api/jobs").json()["jobs"]
        assert jobs[0]["status"] == "ready" and jobs[0]["claims_extracted"] >= 1

    def test_an_upload_is_stored_outside_the_project_and_sanitised(self, signed_in, cfg):
        payload = b"We always measure the baseline before changing anything, because a 20% lift means nothing without one."
        response = signed_in.post(
            "/api/upload", files={"file": ("../../etc/evil.txt", payload, "text/plain")}
        )
        assert response.status_code == 200
        assert response.json()["filename"] == "evil.txt"  # path stripped
        assert (cfg.data_dir / "uploads").is_dir()

    def test_an_oversized_upload_is_refused(self, signed_in, cfg):
        oversized = b"x" * (cfg.max_upload_bytes + 1024)
        response = signed_in.post(
            "/api/upload", files={"file": ("big.txt", oversized, "text/plain")}
        )
        assert response.status_code == 413
        assert response.json()["reason"] == "file_too_large"

    def test_a_blocked_job_can_be_rescued_with_pasted_text(self, blank, cfg):
        """Rule 4a, end to end over HTTP."""
        job_id = blank.post("/api/ingest", json={"url": YOUTUBE_URL}).json()["job_id"]
        control_db.set_job_status(
            control_db.control(), job_id, "blocked_by_platform",
            error_message="YouTube refused that video.",
        )
        thin = blank.post(f"/api/jobs/{job_id}/rescue", json={"text": "too short"})
        assert thin.status_code == 400 and thin.json()["reason"] == "text_too_short"

        notes = (
            "We always warm up a brand new sending domain for two full weeks before any real "
            "volume, because the reputation has not been established yet. It breaks down if you "
            "skip the warm up and start at full volume on day one."
        )
        rescued = blank.post(f"/api/jobs/{job_id}/rescue", json={"text": notes}).json()
        assert rescued["ok"] and rescued["claims"] >= 1
        assert blank.get("/api/jobs").json()["jobs"][0]["status"] == "ready"

    def test_claims_are_editable_but_their_evidence_is_not(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        claim = signed_in.get("/api/cards?view=library").json()["claims"][0]
        original_quote = claim["anchor_quote"]
        response = signed_in.post(
            f"/api/claim/{claim['id']}",
            json={
                "when_it_applies": "Teams of any size",
                "anchor_quote": "a quote I just made up",
                "locator_value": "9999",
            },
        ).json()
        assert response["ok"]
        assert response["claim"]["when_it_applies"] == "Teams of any size"
        # The grounded evidence is immutable from the UI.
        assert response["claim"]["anchor_quote"] == original_quote
        assert response["claim"]["locator_value"] == claim["locator_value"]

    def test_skills_save_and_revert_over_http(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        skill = signed_in.get("/api/cards?view=skills").json()["skills"][0]
        updated = signed_in.post(
            "/api/skill",
            json={
                "slug": skill["slug"],
                "name": skill["name"],
                "description": skill["description"],
                "trigger_when": skill["trigger_when"],
                "execution_steps": ["only one step now"],
            },
        ).json()
        assert updated["skill"]["version"] == 2
        reverted = signed_in.post(
            f"/api/skill/{skill['id']}/revert", json={"version": 1}
        ).json()
        assert reverted["ok"]
        assert len(reverted["skill"]["execution_steps"]) == 3

    def test_zero_match_synthesis_is_one_call(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        out = signed_in.post("/api/synthesize", json={"task": "run an outbound sprint"}).json()
        assert out["ok"] and out["origin"] in ("saved", "synthesized")

    def test_permanent_deletion_requires_trash_first(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        source_id = E.DEMO_SOURCE_VIDEO
        refused = signed_in.post(f"/api/delete/{source_id}", json={})
        assert refused.status_code == 400 and refused.json()["reason"] == "not_in_trash"

        assert signed_in.post(f"/api/trash/source/{source_id}", json={}).json()["status"] == "trash"
        assert signed_in.get("/api/trash").json()["items"][0]["id"] == source_id
        deleted = signed_in.post(f"/api/delete/{source_id}", json={}).json()
        assert deleted["ok"] and deleted["deleted_claims"] >= 1
        assert signed_in.get("/api/trash").json()["items"] == []

    def test_trashing_is_undone_by_restoring(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        signed_in.post(f"/api/trash/source/{E.DEMO_SOURCE_VIDEO}", json={})
        signed_in.post(f"/api/archive/source/{E.DEMO_SOURCE_VIDEO}", json={"restore": True})
        assert signed_in.get("/api/trash").json()["items"] == []
        assert signed_in.get("/api/cards?view=library").json()["claims"]

    def test_profile_serves_all_five_blocks(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        profile = signed_in.get("/api/profile").json()
        assert profile["about_you_context"] == ""           # block 1
        assert profile["mcp_url"].endswith("/mcp")          # block 2
        assert "sessions" in profile                        # block 2
        assert profile["theme_mode"] in ("light", "dark", "warm_sunset")  # block 3
        assert profile["quotas"]["media_minutes_cap"] == 300             # block 4
        assert profile["quotas"]["files_cap"] == 50                      # block 4
        assert signed_in.get("/api/trash").status_code == 200            # block 5

    def test_about_you_and_theme_persist(self, signed_in):
        signed_in.post("/api/profile", json={"about_you_context": "I run outbound for 6 people."})
        signed_in.post("/api/profile", json={"theme_mode": "warm_sunset"})
        profile = signed_in.get("/api/profile").json()
        assert profile["about_you_context"] == "I run outbound for 6 people."
        assert profile["theme_mode"] == "warm_sunset"
        assert signed_in.post("/api/profile", json={"theme_mode": "neon"}).status_code == 400

    def test_sessions_show_their_remaining_days_and_revoke_instantly(self, signed_in, cfg):
        user_id = signed_in.get("/api/profile").json() and control_db.control().execute(
            "SELECT user_id FROM users"
        ).fetchone()[0]
        control_db.create_oauth_session(control_db.control(), user_id, client_name="Claude", cfg=cfg)
        sessions = signed_in.get("/api/profile").json()["sessions"]
        assert sessions and sessions[0]["client_name"] == "Claude"
        assert 28 <= sessions[0]["expires_in_days"] <= 30

        after = signed_in.post(f"/api/sessions/{sessions[0]['session_id']}/revoke", json={}).json()
        assert after["sessions"] == []
        assert signed_in.post("/api/sessions/sess_nope/revoke", json={}).status_code == 404

    def test_export_downloads_both_formats(self, signed_in):
        signed_in.post("/api/demo/seed", json={})
        js = signed_in.get("/api/export?format=json")
        assert "attachment" in js.headers["content-disposition"]
        payload = json.loads(js.text)
        assert payload["claims"] and payload["skills"] and payload["tension_links"]

        md = signed_in.get("/api/export?format=markdown")
        assert md.text.startswith("# brain library export")
        assert "Disagreements" in md.text

    def test_demo_data_is_real_and_clearable(self, signed_in):
        # Sign-in already seeded it, so this asserts what the user actually has.
        counts = signed_in.get("/api/cards").json()["library_counts"]
        assert counts["claims"] == 2 and counts["tensions"] == 1
        # Seeding again is a no-op, not a duplicate.
        assert signed_in.post("/api/demo/seed", json={}).json()["already_seeded"] is True
        cleared = signed_in.post("/api/demo/clear", json={}).json()
        assert cleared["ok"] and cleared["library_counts"]["claims"] == 0
        assert signed_in.post("/api/demo/nonsense", json={}).status_code == 400

    def test_an_internal_error_never_reaches_the_browser(self, signed_in, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("secret path /data/users/user_x.db")

        monkeypatch.setattr(E, "op_list_cards", explode)
        response = signed_in.get("/api/cards")
        assert response.status_code == 500
        assert "secret path" not in response.text
        assert response.json()["reason"] == "internal_error"


class TestPreviewHarness:
    def test_the_harness_is_served_in_test_mode(self, signed_in):
        response = signed_in.get("/preview")
        assert response.status_code == 200
        assert "harness" in response.text

    def test_the_harness_is_off_unless_explicitly_enabled(self, cfg, monkeypatch):
        monkeypatch.setenv("BRAIN_ENV", "prod")
        monkeypatch.delenv("BRAIN_PREVIEW", raising=False)
        from app import config as config_module

        prod = config_module.load_settings()
        assert webapi.preview_enabled(prod) is False
        monkeypatch.setenv("BRAIN_PREVIEW", "1")
        assert webapi.preview_enabled(prod) is True

    def test_static_assets_are_served(self, client):
        assert client.get("/").status_code == 200
        assert "text/css" in client.get("/app.css").headers["content-type"]
        assert "javascript" in client.get("/app.js").headers["content-type"]

    def test_security_headers_cover_the_web_surface(self, client):
        for path in ("/", "/app.js", "/api/me"):
            headers = client.get(path).headers
            assert headers["X-Frame-Options"] == "DENY"
            assert "script-src 'self'" in headers["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


# ===========================================================================
# the static files themselves
# ===========================================================================
class TestStaticContract:
    @pytest.fixture(scope="class")
    def html(self):
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def css(self):
        return (STATIC / "app.css").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def js(self):
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_the_page_is_compatible_with_script_src_self(self, html):
        """No inline script and no inline handlers, so the CSP stays strict."""
        scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S)
        for attrs, body in scripts:
            assert "src=" in attrs, "an inline <script> would need 'unsafe-inline'"
            assert body.strip() == ""
        assert not re.search(r"\son[a-z]+\s*=", html)

    def test_nothing_is_loaded_from_a_third_party(self, html, css, js):
        for text in (html, css, js):
            assert "http://" not in text.replace("http://www.w3.org", "")
            assert "cdn." not in text
            assert "googleapis" not in text and "unpkg" not in text

    def test_the_icon_dictionary_is_complete_and_self_contained(self, html, js):
        ids = set(re.findall(r'<symbol id="i-([a-z]+)"', html))
        expected = {
            "search", "ingest", "skill", "quote", "conflict", "locator", "archive",
            "undo", "trash", "edit", "profile", "check", "copy",
        }
        assert ids == expected, ids ^ expected
        # Every icon referenced anywhere must exist in the sprite.
        used = set(re.findall(r'href="#i-([a-z]+)"', html)) | set(
            re.findall(r'icon\("([a-z]+)"', js)
        )
        assert used <= ids, used - ids

    def test_the_omnibar_is_not_a_chatbot(self, html, css, js):
        """Rule 1: no sparkle icon, no bubbles, no thread.

        Comments are stripped first — a comment documenting the ban is not a
        violation of it.
        """
        def strip_comments(text: str) -> str:
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            return re.sub(r"^\s*//.*$", "", text, flags=re.M)

        for text in (html, css, js):
            bare = strip_comments(text)
            for glyph in ("✨", "🪄", "💬", "🗨"):
                assert glyph not in bare
            for word in ("sparkle", "bubble", "chat-thread", "assistant-message"):
                assert word not in bare.lower(), word

    def test_every_theme_defines_every_semantic_token(self, css):
        required = [
            "--bg-canvas", "--bg-card", "--bg-card-hover", "--bg-inset",
            "--border-subtle", "--border-strong", "--text-primary", "--text-muted",
            "--accent", "--accent-quiet", "--accent-conflict", "--accent-ok",
            "--ease-ui", "--duration-ui",
        ]
        for theme in ("light", "dark", "warm_sunset"):
            block = re.search(
                r':root\[data-theme="' + theme + r'"\]\s*\{(.*?)\}', css, re.S
            )
            assert block, theme
            for token in required:
                assert token in block.group(1), f"{theme} is missing {token}"

    def test_warm_sunset_uses_the_gentler_motion(self, css):
        block = re.search(r':root\[data-theme="warm_sunset"\]\s*\{(.*?)\}', css, re.S).group(1)
        assert "--duration-ui:260ms" in block.replace(" ", "")

    def test_no_colour_is_hard_coded_outside_the_token_blocks(self, css):
        """Every rule must read a token, so the live tuner can rewrite anything."""
        without_tokens = re.sub(r':root(\[data-theme="[a-z_]+"\])?\s*\{.*?\}', "", css, flags=re.S)
        leaked = [
            hex_value
            for hex_value in re.findall(r"#[0-9A-Fa-f]{3,8}\b", without_tokens)
            if hex_value.lower() not in ("#fff", "#ffffff")  # button-on-accent text
        ]
        assert leaked == [], leaked

    def test_the_two_card_silhouettes_are_distinct(self, css, js):
        squashed = re.sub(r"\s+", "", css)
        assert ".card-skill{border-left:3pxsolidvar(--accent)}" in squashed
        assert "card-skill" in js and "card-claim" in js

    def test_the_outer_doll_shows_the_claim_and_the_inner_doll_the_quote(self, js):
        """Checkpoint B's correction to Rule 2."""
        outer = js[js.index("const head = `<button class=\"card-head\""):js.index("// Inner Doll")]
        assert "esc(claim.claim)" in outer
        assert "anchor_quote" not in outer
        inner = js[js.index("// Inner Doll"):js.index("return `<article class=\"card card-claim\"")]
        assert "anchor_quote" in inner
        assert "when_it_fails" in inner and "metrics_and_quotes" in inner

    def test_the_mobile_breakpoint_turns_the_inspector_into_a_sheet(self, css):
        mobile = css[css.index("@media (max-width:860px)"):]
        assert "translateY(100%)" in mobile
        assert ".mobile-bar" in mobile
        assert "border-top-left-radius" in mobile

    def test_reduced_motion_is_respected(self, css):
        assert "prefers-reduced-motion" in css

    def test_the_profile_modal_blocks_are_numbered_and_in_order(self, html):
        """Five locked at Checkpoint B, plus the audit log the founder added."""
        headings = re.findall(r"<h3>(\d) · ([^<]+)</h3>", html)
        assert [number for number, _ in headings] == ["1", "2", "3", "4", "5", "6"]
        labels = " ".join(label.lower() for _, label in headings)
        assert "about you" in labels
        assert "mcp connection" in labels and "sessions" in labels
        assert "appearance" in labels
        assert "quotas" in labels
        assert "recovery" in labels
        assert "audit log" in labels

    def test_the_tuner_exposes_the_contrast_readout_and_token_copy(self, html, js):
        assert 'id="contrast"' in html and 'id="copy-tokens"' in html
        assert "function updateContrast" in js
        assert "0.03928" in js  # the WCAG relative-luminance curve
        assert "function copyTokens" in js

    def test_all_four_layouts_stay_selectable_in_the_harness(self, html, js):
        assert set(re.findall(r'data-layout-pick="([A-Za-z0-9]+)"', html)) == {
            "B1", "B2", "B3", "merged"
        }
        assert '["B1", "B2", "B3", "merged"].includes(layout)' in js

    def test_every_edge_state_is_selectable(self, html):
        options = set(re.findall(r'<option value="([a-z-]+)">', html))
        assert {"blocked", "zero-match", "first-run", "ingesting"} <= options

    def test_user_content_is_escaped_everywhere_it_is_interpolated(self, js):
        """A claim is untrusted text; it must never reach innerHTML unescaped."""
        for field in ("claim.claim", "claim.anchor_quote", "skill.name", "skill.slug"):
            for match in re.finditer(re.escape("${" + field), js):
                window = js[max(0, match.start() - 40):match.start()]
                assert "esc(" in window, f"{field} interpolated without esc()"
