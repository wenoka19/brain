"""The three actions §7.3 and §7.4 put in a card's overflow menu.

`Rename`, `Re-process` and `Generate a summary` are the only menu items the
shipped backend had nothing behind. Archive, Activate/Deactivate, Delete and
Approve already existed; `View the source` is a link; `Export .md` is built
from a skill the frame already holds.

All three are the person's. An agent renaming someone's library, spending
their minutes on a re-read, or writing a summary they had switched off are
decisions §2's "zero destructive LLM tools" rules out by the same argument
that keeps `delete` off the MCP surface — so each refuses an agent, and each
refusal says which action it blocked rather than repeating the one sentence
the guard shipped with.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture()
def ingested(cfg, user):
    """One source, read all the way through."""
    ctx = E.build_context(user["user_id"], surface="web_ui")
    queued = E.op_ingest_source(ctx, YOUTUBE)
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
    return ctx, queued.data["source_id"]


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


def headers(client):
    return {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}


class TestRename:
    def test_it_changes_what_every_surface_shows(self, ingested, user):
        """An auto-extracted title is often a channel's boilerplate, and it is
        the one field of a source the person can correct."""
        ctx, source_id = ingested
        assert E.op_rename_source(ctx, source_id, "The outbound timing talk").ok

        conn = tenant_db.tenant(user["user_id"])
        assert tenant_db.get_source(conn, source_id)["title"] == "The outbound timing talk"
        # And the lists, which read it through a join rather than a copy.
        assert [r for r in tenant_db.list_sources(conn) if r["title"] == "The outbound timing talk"]
        assert E.op_get_transcript(ctx, source_id).data["source"]["title"] == (
            "The outbound timing talk"
        )

    def test_whitespace_is_collapsed_and_a_blank_title_is_refused(self, ingested):
        """A source with an empty title is a row nobody can find again."""
        ctx, source_id = ingested
        assert E.op_rename_source(ctx, source_id, "  two   spaces  ").ok
        assert E.op_get_source(ctx, source_id).data["source"]["title"] == "two spaces"

        for blank in ("", "   ", "\n\t"):
            refused = E.op_rename_source(ctx, source_id, blank)
            assert not refused.ok and refused.error_reason == "empty_title"

    def test_an_agent_cannot_rename_anything(self, ingested, user):
        ctx, source_id = ingested
        agent = E.build_context(user["user_id"], surface="mcp")
        refused = E.op_rename_source(agent, source_id, "mine now")
        assert not refused.ok and refused.error_reason == "person_only"
        assert "Renaming" in refused.error_message

    def test_an_unknown_id_is_not_found(self, ingested):
        ctx, _ = ingested
        assert E.op_rename_source(ctx, "src_nope", "x").error_reason == "not_found"

    def test_it_round_trips_over_http(self, ingested, client):
        _, source_id = ingested
        response = client.post(
            f"/api/sources/{source_id}/rename",
            json={"title": "Renamed from the card"},
            headers=headers(client),
        )
        assert response.status_code == 200
        assert response.json()["source"]["title"] == "Renamed from the card"


class TestReprocess:
    def test_it_queues_a_new_job_against_the_same_source(self, ingested, user, control):
        """§7.3's six steps are rewritten in place: the person asked for this
        source to be redone, not for a second copy of it."""
        ctx, source_id = ingested
        before = len(tenant_db.list_sources(tenant_db.tenant(user["user_id"])))

        result = E.op_reprocess_source(ctx, source_id)
        assert result.ok and result.data["job_id"]
        assert result.data["source_id"] == source_id

        after = tenant_db.list_sources(tenant_db.tenant(user["user_id"]))
        assert len(after) == before, "re-processing created a second source row"
        job = control_db.get_job(control, result.data["job_id"])
        assert job["source_id"] == source_id

    def test_the_row_says_processing_again_while_it_runs(self, ingested, user):
        """§7.7's `Processing` state, and §7.3's "Working on it" header."""
        ctx, source_id = ingested
        E.op_reprocess_source(ctx, source_id)
        conn = tenant_db.tenant(user["user_id"])
        assert tenant_db.get_source(conn, source_id)["status"] == "processing"
        assert E.op_source_progress(ctx, source_id).data["finished"] is False

    def test_the_six_steps_start_over_rather_than_accumulating(self, ingested, cfg, user, control):
        ctx, source_id = ingested
        job_id = E.op_reprocess_source(ctx, source_id).data["job_id"]
        steps = control_db.job_steps(control, job_id)
        assert len(steps) == 6
        assert {s["state"] for s in steps} == {"pending"}

        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        progress = E.op_source_progress(ctx, source_id).data
        assert progress["job_id"] == job_id, "the header read the old job"
        assert progress["finished"] is True

    def test_a_source_with_no_address_says_so_instead_of_queueing(self, cfg, user):
        """A pasted rescue or an uploaded file has nothing to go back to.
        §6 rule 11: say what is missing rather than fail at step 1."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        E.op_remember_this(ctx, claim="Written by hand.", when_it_applies="always")
        conn = tenant_db.tenant(user["user_id"])
        hand_written = conn.execute(
            "SELECT id FROM sources WHERE source_type = 'user_note'"
        ).fetchone()["id"]

        refused = E.op_reprocess_source(ctx, hand_written)
        assert not refused.ok and refused.error_reason == "nothing_to_reread"
        assert "add it again" in refused.error_message.lower()

    def test_it_refuses_while_one_is_already_running(self, ingested):
        """Two jobs on one source would write the same six rows from both
        sides, and the work block would flicker between them."""
        ctx, source_id = ingested
        assert E.op_reprocess_source(ctx, source_id).ok
        again = E.op_reprocess_source(ctx, source_id)
        assert not again.ok and again.error_reason == "already_running"

    def test_an_agent_cannot_spend_the_persons_credits(self, ingested, user):
        ctx, source_id = ingested
        agent = E.build_context(user["user_id"], surface="mcp")
        refused = E.op_reprocess_source(agent, source_id)
        assert not refused.ok and refused.error_reason == "person_only"
        assert "credits" in refused.error_message

    def test_it_round_trips_over_http(self, ingested, client):
        _, source_id = ingested
        response = client.post(f"/api/sources/{source_id}/reprocess", headers=headers(client))
        assert response.status_code == 200
        assert response.json()["job_id"]


class TestGenerateSummary:
    @pytest.fixture()
    def without_a_summary(self, cfg, user, control):
        """The case the action exists for: summaries switched off at ingest."""
        control_db.update_profile(control, user["user_id"], want_summary=False)
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        source_id = queued.data["source_id"]
        assert tenant_db.get_summary(tenant_db.tenant(user["user_id"]), source_id=source_id) is None
        return ctx, source_id

    def test_it_writes_one_from_the_text_already_stored(self, without_a_summary, user):
        """Not a re-read: the whole point is a summary without spending the
        quota again."""
        ctx, source_id = without_a_summary
        result = E.op_generate_summary(ctx, source_id)
        assert result.ok and result.data["created"] is True
        assert result.data["summary"]["gist"]
        assert result.data["summary"]["points"]

        stored = tenant_db.get_summary(tenant_db.tenant(user["user_id"]), source_id=source_id)
        assert stored["id"] == result.data["summary"]["id"]

    def test_no_new_job_is_queued(self, without_a_summary, control, user):
        ctx, source_id = without_a_summary
        before = len(control_db.list_jobs(control, user["user_id"], limit=50))
        E.op_generate_summary(ctx, source_id)
        assert len(control_db.list_jobs(control, user["user_id"], limit=50)) == before

    def test_it_appears_in_the_list_and_on_the_source(self, without_a_summary, client):
        """The two frames that have to show it afterwards."""
        _, source_id = without_a_summary
        assert client.get("/api/summaries").json()["summaries"] == []
        assert client.post(
            f"/api/sources/{source_id}/summarise", headers=headers(client)
        ).status_code == 200
        assert len(client.get("/api/summaries").json()["summaries"]) == 1
        assert client.get(f"/api/sources/{source_id}").json()["summary"]["gist"]

    def test_asking_twice_returns_the_one_that_exists(self, without_a_summary):
        """Not an error, and not a second summary: the menu item is still
        there after the first click."""
        ctx, source_id = without_a_summary
        first = E.op_generate_summary(ctx, source_id)
        second = E.op_generate_summary(ctx, source_id)
        assert second.ok
        assert second.data["created"] is False
        assert second.data["summary"]["id"] == first.data["summary"]["id"]

    def test_a_source_with_no_stored_text_says_so(self, cfg, user):
        ctx = E.build_context(user["user_id"], surface="web_ui")
        conn = tenant_db.tenant(user["user_id"])
        tenant_db.upsert_source(conn, title="Nothing in it", source_type="article")
        empty = conn.execute("SELECT id FROM sources WHERE title = 'Nothing in it'").fetchone()["id"]
        refused = E.op_generate_summary(ctx, empty)
        assert not refused.ok and refused.error_reason == "no_text"

    def test_an_agent_cannot_ask_for_one(self, without_a_summary, user):
        _, source_id = without_a_summary
        agent = E.build_context(user["user_id"], surface="mcp")
        refused = E.op_generate_summary(agent, source_id)
        assert not refused.ok and refused.error_reason == "person_only"


class TestTheBlockedRowCarriesItsRemedy:
    """§7.7: "`Needs you` ... carries a **Fix this** button whose tooltip
    states the remedy" — "Make it public, then retry", "No text layer —
    retype or re-scan".

    A sentence written for the person cannot be matched on, so the provider's
    own reason code is stored beside the message and the frame maps the code
    to the remedy. Matching on the prose is how a tooltip silently goes wrong
    the first time the wording is improved.
    """

    @pytest.fixture()
    def blocked(self, cfg, user):
        from app.ingestion.providers import ProviderError

        class Blocked:
            def fetch_url(self, target, *, cfg):
                raise ProviderError("blocked_by_platform", "That video is private.")

            def fetch_file(self, validated, *, cfg):  # pragma: no cover
                raise ProviderError("blocked_by_platform", "n/a")

        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        ctx.prov.fetcher = Blocked()
        assert E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)[0]["status"] == (
            "blocked_by_platform"
        )
        return ctx, queued.data["source_id"]

    def test_the_reason_code_is_stored_beside_the_message(self, blocked, control, user):
        _, source_id = blocked
        job = control_db.latest_job_for_source(control, user["user_id"], source_id)
        assert job["error_reason"] == "blocked_by_platform"
        assert job["error_message"] == "That video is private."

    def test_the_list_carries_it_across_the_two_databases(self, blocked):
        """The reason is on the job in `control.db`, the row is in the tenant
        database, and no query can join them — which is why the engine does
        it and `tenant.list_sources` does not."""
        ctx, source_id = blocked
        row = next(r for r in E.op_list_sources(ctx).data["sources"] if r["id"] == source_id)
        assert row["status"] == "blocked"
        assert row["block_reason"] == "blocked_by_platform"
        assert row["block_message"] == "That video is private."

    def test_a_healthy_row_carries_no_block_fields(self, ingested):
        """Enriching every row would mean a control.db read per source to
        draw a list in which nothing is blocked."""
        ctx, source_id = ingested
        row = next(r for r in E.op_list_sources(ctx).data["sources"] if r["id"] == source_id)
        assert "block_reason" not in row

    def test_every_reason_the_fetchers_raise_has_a_remedy(self):
        """A missing key falls through to `REMEDY.default`, which is a correct
        sentence but a vague one — so the mapping is checked against the codes
        the fetchers actually raise."""
        import re

        fetchers = (
            Path(__file__).resolve().parents[1] / "app/ingestion/fetchers.py"
        ).read_text()
        raised = set(re.findall(r'ProviderError\(\s*"([a-z_]+)"', fetchers))
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        block = frames[frames.index("const REMEDY = {") : frames.index("/** §7.7: \"Never")]
        mapped = set(re.findall(r"^  ([a-z_]+):", block, re.M))
        assert raised <= mapped, f"no remedy for: {sorted(raised - mapped)}"

    def test_the_two_the_plan_names_are_word_for_word(self):
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        assert '"Make it public, then retry"' in frames
        assert '"No text layer — retype or re-scan"' in frames

    def test_a_blocked_row_is_not_stamped_as_archived(self, blocked, user):
        """`_archival` stamped anything that was not `active`, so a source
        that had just failed to fetch carried an archive date and the words
        `archived by you`, having been archived by nobody.

        The Archive never showed it — that page selects on the status — but a
        column called `archived_at` has to mean it, and §7.3's blocked row is
        the one place a frame reads a source's own dates.
        """
        _, source_id = blocked
        row = tenant_db.get_source(tenant_db.tenant(user["user_id"]), source_id)
        assert row["status"] == "blocked"
        assert row["archived_at"] is None
        assert row["archived_by"] == ""

    def test_a_processing_row_is_not_either(self, cfg, user):
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        row = tenant_db.get_source(tenant_db.tenant(user["user_id"]), queued.data["source_id"])
        assert row["status"] == "processing"
        assert row["archived_at"] is None

    def test_but_an_archived_one_still_is(self, ingested, user):
        """The other half: narrowing the condition must not stop the stamp
        that §7.12 is built on."""
        ctx, source_id = ingested
        E.op_archive_item(ctx, "source", source_id)
        row = tenant_db.get_source(tenant_db.tenant(user["user_id"]), source_id)
        assert row["archived_at"] and row["archived_by"] == "you"

    def test_fix_this_retries_for_real(self, blocked, control, user):
        """Before `op_reprocess_source` existed, the button could only state
        the remedy and leave the person to find the retry themselves."""
        ctx, source_id = blocked
        result = E.op_reprocess_source(ctx, source_id)
        assert result.ok
        job = control_db.get_job(control, result.data["job_id"])
        assert job["status"] == "queued"
        assert job["source_id"] == source_id


class TestTheRefusalSaysWhatItBlocked:
    """The guard shipped with one sentence about skills, and `op_list_archive`
    used it too — so an agent whose archive read was refused was told "only
    the person can decide a skill's state", which it cannot act on."""

    def test_each_refusal_names_its_own_action(self, ingested, user):
        ctx, source_id = ingested
        agent = E.build_context(user["user_id"], surface="mcp")
        messages = {
            "list_archive": E.op_list_archive(agent).error_message,
            "rename": E.op_rename_source(agent, source_id, "x").error_message,
            "reprocess": E.op_reprocess_source(agent, source_id).error_message,
            "summary": E.op_generate_summary(agent, source_id).error_message,
        }
        assert len(set(messages.values())) == 4, messages
        assert "archive" in messages["list_archive"].lower()
        # And none of them is the skill sentence, which belongs to skills.
        for action, message in messages.items():
            assert "skill's state" not in message, action

    def test_the_skill_refusals_keep_the_sentence_that_is_theirs(self, cfg, user):
        agent = E.build_context(user["user_id"], surface="mcp")
        for result in (
            E.op_review_skill(agent, "skl_x", "approve"),
            E.op_set_skill_state(agent, "skl_x", "active"),
        ):
            assert result.error_reason == "person_only"
            assert "skill's state" in result.error_message
