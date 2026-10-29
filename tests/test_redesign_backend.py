"""The four defects the adversarial review surfaced, and their fixes.

Each was verified by hand before it was fixed and is pinned here, because all
four are the kind that come back: they are absences, and an absence does not
raise.

1. **The MCP `fetch` tool returned archived items.** `MASTER_PLAN.md` §9.2:
   *"Never exposed: anything archived … enforced server-side, not by prompt."*
   §7.12 makes the person the matching promise. The founder's instruction was
   that an archived item should be *"comme inexistante"* over MCP — not hidden,
   absent — so an archived id must answer exactly as an id that was never
   issued.

2. **Editing a skill promoted it.** `upsert_skill` hard-set `status =
   'active'` on every update, which would have left §9.2 row 5's review gate —
   a security control by §10.10, not a nicety — bypassable through Edit the
   day the `proposed` state arrives.

3. **Nothing drained the ingestion queue.** §7.2 tells the person "leaving the
   page does not cancel anything" and §7.3 says "Brain keeps working", while
   jobs only advanced when a browser called `/api/jobs/run`.

4. **No `archived_at` and no `archived_by`.** §7.12 orders by archive date and
   prints `archived by you` or `archived by Brain` beside every row — it calls
   that attribution the point of the page.
"""

from __future__ import annotations

import os

import pytest

from app.core import engine as E
from app.db import tenant as tenant_db


def mcp(user):
    return E.build_context(user["user_id"], client_name="probe", surface="mcp")


def web(user):
    return E.build_context(user["user_id"], client_name="browser", surface="web_ui")


def a_claim(ctx, text="Cut scope rather than move the date."):
    made = E.op_remember_this(ctx, claim=text, when_it_applies="planning a cycle")
    assert made.ok, made
    return made.data["claim"]["id"]


def a_skill(ctx, slug="run-a-cycle"):
    made = E.op_save_skill(
        ctx,
        slug=slug,
        name="Run a cycle planning session",
        description="Plan six weeks of work.",
        trigger_when="when planning a cycle",
        execution_steps=["List the candidates.", "Name an owner."],
    )
    assert made.ok, made
    return made.data["skill"]["id"]


# ---------------------------------------------------------------------------
# 1. archived is not hidden over MCP — it does not exist
# ---------------------------------------------------------------------------
class TestArchivedIsNonExistentOverMcp:
    def test_an_archived_claim_cannot_be_fetched(self, cfg, user):
        ctx = mcp(user)
        claim_id = a_claim(ctx)
        assert E.op_fetch(ctx, claim_id).ok

        E.op_archive_item(ctx, "claim", claim_id)
        assert not E.op_fetch(ctx, claim_id).ok

    def test_an_archived_skill_cannot_be_fetched_by_id_or_by_slug(self, cfg, user):
        """Two doors into the same room; §9.2 closes the room, not one door."""
        ctx = mcp(user)
        skill_id = a_skill(ctx)
        E.op_archive_item(ctx, "skill", skill_id)

        assert not E.op_fetch(ctx, skill_id).ok
        assert not E.op_fetch(ctx, "run-a-cycle").ok

    def test_it_answers_exactly_as_an_id_that_was_never_issued(self, cfg, user):
        """"Comme inexistante." A distinguishable refusal would leak the one
        bit the promise is about: that the thing is there."""
        ctx = mcp(user)
        claim_id = a_claim(ctx)
        E.op_archive_item(ctx, "claim", claim_id)

        archived = E.op_fetch(ctx, claim_id)
        never = E.op_fetch(ctx, "clm_0000000000000000never")
        assert archived.error_reason == never.error_reason == "not_found"
        assert archived.ok is never.ok is False

    def test_an_archived_claim_is_not_reachable_through_a_skills_links(self, cfg, user):
        """The side door: a skill that is still active, linking a claim that is
        not."""
        ctx = mcp(user)
        claim_id = a_claim(ctx)
        saved = E.op_save_skill(
            ctx,
            slug="cycle-rules",
            name="Cycle rules",
            description="What to hold to.",
            trigger_when="when planning",
            execution_steps=["Follow the linked rule."],
        )
        assert saved.ok
        tenant_db.upsert_skill(
            tenant_db.tenant(user["user_id"]),
            slug="cycle-rules",
            name="Cycle rules",
            description="What to hold to.",
            trigger_when="when planning",
            execution_steps=["Follow the linked rule."],
            linked_claim_ids=[claim_id],
        )
        E.op_archive_item(ctx, "claim", claim_id)

        got = E.op_fetch(ctx, saved.data["skill"]["id"])
        assert got.ok
        assert got.data["linked_claims"] == []

    def test_the_web_surface_still_sees_its_own_archive(self, cfg, user):
        """§7.12 is a whole page of them. The filter is about MCP, not about
        hiding the person's things from the person."""
        person = web(user)
        claim_id = a_claim(person)
        E.op_archive_item(person, "claim", claim_id)
        assert E.op_fetch(person, claim_id).ok

    def test_restore_still_works_after_the_filter(self, cfg, user):
        """`op_archive_item` reads the archived row deliberately unfiltered —
        otherwise nothing could ever be restored."""
        ctx = mcp(user)
        claim_id = a_claim(ctx)
        E.op_archive_item(ctx, "claim", claim_id)
        assert E.op_archive_item(ctx, "claim", claim_id, restore=True).ok
        assert E.op_fetch(ctx, claim_id).ok


# ---------------------------------------------------------------------------
# 2. editing a skill must not promote it
# ---------------------------------------------------------------------------
class TestEditingASkillDoesNotPromoteIt:
    def test_an_archived_skill_stays_archived_through_an_edit(self, cfg, user):
        ctx = web(user)
        skill_id = a_skill(ctx)
        E.op_archive_item(ctx, "skill", skill_id)

        conn = tenant_db.tenant(user["user_id"])
        tenant_db.upsert_skill(
            conn,
            slug="run-a-cycle",
            name="Run a cycle planning session",
            description="Edited, not approved.",
            trigger_when="when planning a cycle",
            execution_steps=["List the candidates."],
        )
        assert tenant_db.get_skill(conn, skill_id)["status"] == "archived"

    def test_an_edit_still_bumps_the_version_and_keeps_a_snapshot(self, cfg, user):
        """The fix removes one clause; it must not remove the versioning §7.9
        and `skill_versions` depend on."""
        ctx = web(user)
        skill_id = a_skill(ctx)
        conn = tenant_db.tenant(user["user_id"])
        before = tenant_db.get_skill(conn, skill_id)["version"]

        tenant_db.upsert_skill(
            conn,
            slug="run-a-cycle",
            name="Run a cycle planning session",
            description="Second draft.",
            trigger_when="when planning a cycle",
            execution_steps=["List the candidates.", "Name an owner.", "Cut."],
        )
        after = tenant_db.get_skill(conn, skill_id)
        assert after["version"] == before + 1
        assert after["status"] == "active"
        assert after["description"] == "Second draft."


# ---------------------------------------------------------------------------
# 3. the queue drains without a browser
# ---------------------------------------------------------------------------
class TestTheQueueDrainsOnItsOwn:
    def test_the_gate_is_off_in_tests_and_under_the_worker_role(self, cfg, monkeypatch):
        from app.config import Settings
        from app.main import inline_worker_enabled

        monkeypatch.delenv("BRAIN_INLINE_WORKER", raising=False)
        monkeypatch.setenv("BRAIN_ROLE", "web")
        assert not inline_worker_enabled(cfg)          # tests drive it themselves
        assert inline_worker_enabled(Settings(env="prod"))

        monkeypatch.setenv("BRAIN_ROLE", "worker")
        assert not inline_worker_enabled(Settings(env="prod"))  # a process already does

        monkeypatch.setenv("BRAIN_INLINE_WORKER", "0")
        monkeypatch.setenv("BRAIN_ROLE", "web")
        assert not inline_worker_enabled(Settings(env="prod"))

    def test_a_queued_job_finishes_with_no_request_at_all(self, cfg, user, monkeypatch):
        """The promise, tested as the person experiences it: queue the work,
        then never touch the app again."""
        from starlette.testclient import TestClient

        from app.main import create_app

        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert queued.ok
        job_id = queued.data["job_id"]

        monkeypatch.setenv("BRAIN_INLINE_WORKER", "1")
        with TestClient(create_app()):
            # No request is made inside this block. The only thing running is
            # the drainer the lifespan started.
            deadline = 300
            while deadline:
                status = E.op_get_ingest_status(ctx, job_id)
                if status.ok and status.data["jobs"][0]["status"] in ("ready", "failed"):
                    break
                deadline -= 1
                import time

                time.sleep(0.02)

        final = E.op_get_ingest_status(ctx, job_id).data["jobs"][0]["status"]
        assert final == "ready", f"the queue did not drain on its own: {final}"


# ---------------------------------------------------------------------------
# 4. archived_at and archived_by
# ---------------------------------------------------------------------------
class TestTheArchiveKnowsWhenAndWhoDidIt:
    @pytest.mark.parametrize("table", ["sources", "epistemic_claims", "skills"])
    @pytest.mark.parametrize("column", ["archived_at", "archived_by"])
    def test_the_columns_exist_on_every_archivable_table(self, cfg, user, table, column):
        conn = tenant_db.tenant(user["user_id"])
        names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert column in names

    def test_the_migration_is_idempotent(self, cfg, user):
        """It runs on every boot, so running it twice must be free."""
        conn = tenant_db.tenant(user["user_id"])
        assert tenant_db.migrate(conn) == []

    def test_an_agent_archiving_is_attributed_to_brain(self, cfg, user):
        """§7.12 in amber: "when something vanishes from the library
        untouched, this page says who"."""
        conn = tenant_db.tenant(user["user_id"])
        claim_id = a_claim(mcp(user))
        E.op_archive_item(mcp(user), "claim", claim_id)

        row = tenant_db.get_claim(conn, claim_id)
        assert row["archived_by"] == "brain"
        assert row["archived_at"]

    def test_a_person_archiving_is_attributed_to_them(self, cfg, user):
        conn = tenant_db.tenant(user["user_id"])
        claim_id = a_claim(web(user))
        E.op_archive_item(web(user), "claim", claim_id)
        assert tenant_db.get_claim(conn, claim_id)["archived_by"] == "you"

    def test_restoring_clears_the_stamp(self, cfg, user):
        """An item that is back in the library was never archived, as far as
        the Archive is concerned."""
        conn = tenant_db.tenant(user["user_id"])
        claim_id = a_claim(web(user))
        E.op_archive_item(web(user), "claim", claim_id)
        E.op_archive_item(web(user), "claim", claim_id, restore=True)

        row = tenant_db.get_claim(conn, claim_id)
        assert row["archived_at"] is None
        assert row["archived_by"] == ""

    def test_the_cascade_stamps_the_claims_it_archives(self, cfg, user):
        """Archiving a source archives its claims. Every one of those rows
        appears in the Archive, so every one needs its own date and author —
        not just the row that was clicked."""
        conn = tenant_db.tenant(user["user_id"])
        ctx = E.build_context(user["user_id"])
        E.op_ingest_source(ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        source = conn.execute("SELECT id FROM sources LIMIT 1").fetchone()
        assert source, "the fixture ingest produced no source"
        E.op_archive_item(mcp(user), "source", source["id"])

        rows = conn.execute(
            "SELECT archived_at, archived_by FROM epistemic_claims WHERE source_id = ?",
            (source["id"],),
        ).fetchall()
        assert rows, "the ingest produced no claims to cascade onto"
        for row in rows:
            assert row["archived_at"], "a cascaded claim has no archive date"
            assert row["archived_by"] == "brain"

    def test_the_timestamp_matches_sqlites_own_format(self, cfg, user):
        """`archived_at` is ordered against `created_at`. A column that mixes
        two formats sorts wrong and never raises."""
        import re

        conn = tenant_db.tenant(user["user_id"])
        claim_id = a_claim(web(user))
        E.op_archive_item(web(user), "claim", claim_id)
        row = tenant_db.get_claim(conn, claim_id)
        shape = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        assert shape.match(row["archived_at"]), row["archived_at"]
        assert shape.match(row["created_at"]), row["created_at"]
