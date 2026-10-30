"""The review queue — `MASTER_PLAN.md` §7.9, §9.1 step 5 and §10.10.

§10.10 is the sentence that makes this more than a workflow:

    "Summaries, rules and skills derived from a source are proposals, never
    auto-executed. This is why skills require approval — it is a security
    control, not a UX nicety."

So the tests below are mostly about the ways that control can be hollowed out
while still looking present: a draft an agent can read, a draft an agent can
approve, an edit that promotes, a state change that skips the queue. Each one
would leave §7.9 drawing a review section over a gate that no longer closes.

The migration is tested against a database built on the **old** two-state
schema, because that is the only test of a table rebuild that means anything —
and it is what found the four bugs recorded in the commit.
"""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.ingestion import providers as prov

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class StepfulExtractor:
    """An extractor that returns a procedural claim, so step 5 has something
    to work with. The deterministic fixture source is prose with no steps —
    which is itself correct, and is why this stub exists."""

    name = "stepful"

    def extract(self, chunk, context):
        sentences = [s.strip() for s in chunk.split(".") if len(s.split()) >= 10]
        if not sentences:
            return []
        return [
            {
                "claim": "Run a cycle planning session before committing to dates",
                "evidence_basis": "case_study",
                "domain_tags": ["planning"],
                "when_it_applies": "when planning the next six weeks",
                "when_it_fails": "when the team is smaller than five people",
                "exact_steps": [
                    "List every candidate project with a one-line outcome.",
                    "Name a single owner for each, before any estimate.",
                    "Cut the list until it fits six weeks.",
                ],
                "anchor_quote": " ".join(sentences[0].split()[:14]),
                "locator_type": context.get("locator_type", "timestamp"),
                "locator_value": "144",
                "salience": 5,
            }
        ]


@pytest.fixture()
def with_steps(cfg):
    bundle = prov.build_providers(cfg)
    bundle.extractor = StepfulExtractor()
    prov.set_providers(bundle)
    yield
    prov.set_providers(None)


def ingest(user, cfg):
    ctx = E.build_context(user["user_id"])
    queued = E.op_ingest_source(ctx, YOUTUBE)
    assert queued.ok, queued
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
    return queued.data["job_id"], queued.data["source_id"]


def web(user):
    return E.build_context(user["user_id"], surface="web_ui")


def mcp(user):
    return E.build_context(user["user_id"], client_name="agent", surface="mcp")


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------
class TestTheTableRebuild:
    @staticmethod
    def _old_schema_db(tmp_path, cfg):
        """A tenant database exactly as it was before the four states."""
        import sqlite_vec

        conn = sqlite3.connect(tmp_path / "old.db")
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for pragma in tenant_db.PRAGMAS:
            conn.execute(pragma)

        old = (
            tenant_db.SCHEMA.replace(
                "CHECK (status IN ('proposed','active','inactive','archived'))",
                "CHECK (status IN ('active','archived'))",
            )
            .replace("    source_id            TEXT REFERENCES sources(id) ON DELETE SET NULL,\n", "")
            .replace("    source_locator       TEXT NOT NULL DEFAULT '',\n", "")
        )
        conn.executescript(old)
        conn.executescript(tenant_db.FTS_SCHEMA)
        for name, key in (("skills_vec", "skill_rowid"), ("claims_vec", "claim_rowid")):
            extra = "slug TEXT" if name == "skills_vec" else "source_id TEXT"
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING vec0("
                f"{key} INTEGER PRIMARY KEY, status TEXT, {extra}, "
                f"embedding float[{cfg.embedding_dim}] distance_metric=cosine)"
            )
        conn.executescript(tenant_db.TRIGGERS)
        return conn

    @staticmethod
    def _old_insert(conn, slug, name="S", description="d"):
        """Raw insert, because `upsert_skill` writes columns this old schema
        does not have — in production `migrate()` adds them before any write."""
        conn.execute(
            "INSERT INTO skills (id, slug, name, description, trigger_when, "
            "execution_steps_json) VALUES (?, ?, ?, ?, 't', '[\"a\"]')",
            (f"skl_{slug}", slug, name, description),
        )

    def test_the_old_schema_really_did_reject_the_new_states(self, tmp_path, cfg):
        """Proving the premise before testing the fix."""
        conn = self._old_schema_db(tmp_path, cfg)
        self._old_insert(conn, "s")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE skills SET status = 'proposed'")

    def test_it_preserves_rowids_exactly(self, tmp_path, cfg):
        """`skills_fts` and `skills_vec` are keyed on `skills.rowid`. Letting
        AUTOINCREMENT reassign them would point every index entry at the wrong
        skill — silently, and only noticeably through search."""
        conn = self._old_schema_db(tmp_path, cfg)
        for i in range(3):
            self._old_insert(conn, f"s-{i}", name=f"S{i}", description=f"d{i}")
        before = [(r["rowid"], r["id"]) for r in conn.execute("SELECT rowid, id FROM skills ORDER BY rowid")]

        assert "skills.status(proposed|inactive)" in tenant_db.migrate(conn)

        after = [(r["rowid"], r["id"]) for r in conn.execute("SELECT rowid, id FROM skills ORDER BY rowid")]
        assert before == after

    def test_the_indexes_and_triggers_survive(self, tmp_path, cfg):
        conn = self._old_schema_db(tmp_path, cfg)
        self._old_insert(conn, "s")
        triggers_before = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()["n"]
        fts_before = conn.execute("SELECT COUNT(*) AS n FROM skills_fts").fetchone()["n"]

        tenant_db.migrate(conn)

        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()["n"] == triggers_before
        assert conn.execute("SELECT COUNT(*) AS n FROM skills_fts").fetchone()["n"] == fts_before

        # And they still fire afterwards, which a count alone would not show.
        tenant_db.upsert_skill(
            conn, slug="s", name="Renamed after the rebuild", description="d",
            trigger_when="t", execution_steps=["a"],
        )
        assert conn.execute(
            "SELECT rowid FROM skills_fts WHERE skills_fts MATCH 'Renamed'"
        ).fetchone()

    def test_afterwards_the_four_states_are_allowed_and_nothing_else(self, tmp_path, cfg):
        conn = self._old_schema_db(tmp_path, cfg)
        self._old_insert(conn, "s")
        tenant_db.migrate(conn)

        for state in tenant_db.SKILL_STATUSES:
            conn.execute("UPDATE skills SET status = ?", (state,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE skills SET status = 'nonsense'")

    def test_it_is_idempotent_and_leaves_foreign_keys_on(self, tmp_path, cfg):
        conn = self._old_schema_db(tmp_path, cfg)
        tenant_db.migrate(conn)
        assert tenant_db.migrate(conn) == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_the_rebuild_target_matches_the_real_table(self, cfg, user):
        """The rebuild schema is a duplicated literal. This is what keeps it
        from drifting from `SCHEMA` without anyone noticing.

        Compared as a SET, not a sequence: an additive `ALTER` appends its
        column, so a database that was migrated and one created fresh hold the
        same columns in a different order. SQLite binds by name, so that is
        harmless — but a column that exists in one and not the other is not,
        and that is what this catches.
        """
        conn = tenant_db.tenant(user["user_id"])
        live = {r["name"] for r in conn.execute("PRAGMA table_info(skills)")}
        conn.execute(tenant_db.SCHEMA_SKILLS_REBUILD.replace("skills_rebuild", "probe_skills"))
        probe = {r["name"] for r in conn.execute("PRAGMA table_info(probe_skills)")}
        conn.execute("DROP TABLE probe_skills")
        assert live == probe, f"missing from rebuild: {live - probe}; extra: {probe - live}"

    def test_detection_reads_the_constraint_not_the_comment(self, tmp_path, cfg):
        """`sqlite_master.sql` keeps comments, and this table's own comment
        explains what `proposed` means. Searching the raw text for that word
        reported the migration as applied on a database that had never seen
        it — found by running it, not by reading it.

        Exercised rather than read: the check moved into a shared helper when
        `sources` needed the same widening, and a test that greps the old
        function's body would have called that a failure.
        """
        assert "proposed" in tenant_db.SCHEMA, "the comment that caused it is gone"

        conn = self._old_schema_db(tmp_path, cfg)
        assert not tenant_db._check_allows(conn, "skills", "proposed")
        assert tenant_db._check_allows(conn, "skills", "archived")

        tenant_db.migrate(conn)
        assert tenant_db._check_allows(conn, "skills", "proposed")
        assert tenant_db._check_allows(conn, "skills", "inactive")

    def test_a_table_that_does_not_exist_is_not_migrated(self, tmp_path, cfg):
        """The detector answers "already fine" for a missing table, so a
        migration never runs against nothing."""
        import sqlite3

        conn = sqlite3.connect(tmp_path / "empty.db")
        conn.row_factory = sqlite3.Row
        assert tenant_db._check_allows(conn, "skills", "proposed")


# ---------------------------------------------------------------------------
# step 5
# ---------------------------------------------------------------------------
class TestDraftingSkills:
    def test_a_procedural_claim_becomes_a_proposed_skill(self, cfg, user, with_steps):
        _, source_id = ingest(user, cfg)
        conn = tenant_db.tenant(user["user_id"])
        drafted = tenant_db.list_skills(conn, statuses=("proposed",))
        assert drafted
        assert drafted[0]["source_id"] == source_id
        # Resolved by the grounder from the verbatim quote, not taken from the
        # extractor's hint — which is the point of grounding.
        assert drafted[0]["source_locator"]

    def test_the_step_says_what_it_drafted(self, cfg, user, with_steps):
        job_id, _ = ingest(user, cfg)
        step = next(
            s for s in control_db.job_steps(control_db.control(), job_id)
            if s["step"] == "draft_skills"
        )
        assert step["state"] == "done"
        assert step["detail"].endswith("waiting for your review")

    def test_a_source_with_no_procedure_drafts_nothing_and_says_so(self, cfg, user):
        """The deterministic fixture is prose. Drafting a skill from it anyway
        would be the pipeline inventing a procedure."""
        job_id, _ = ingest(user, cfg)
        step = next(
            s for s in control_db.job_steps(control_db.control(), job_id)
            if s["step"] == "draft_skills"
        )
        assert step["state"] == "skipped"
        assert tenant_db.list_skills(tenant_db.tenant(user["user_id"]), statuses=("proposed",)) == []

    def test_reprocessing_never_overwrites_an_approved_skill(self, cfg, user, with_steps):
        """A person approved it and may have edited it. A second pass over the
        same source must not quietly replace their version."""
        conn = tenant_db.tenant(user["user_id"])
        ingest(user, cfg)
        draft = tenant_db.list_skills(conn, statuses=("proposed",))[0]
        E.op_review_skill(web(user), draft["id"], "approve")

        ctx = E.build_context(user["user_id"])
        second = E.op_ingest_source(ctx, YOUTUBE)
        assert second.ok and second.data.get("duplicate"), "the second pass was not recognised"
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        again = tenant_db.get_skill(conn, draft["id"])
        assert again["status"] == "active"
        assert again["version"] == draft["version"]


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------
class TestTheApprovalGate:
    @pytest.fixture()
    def a_draft(self, cfg, user, with_steps):
        ingest(user, cfg)
        conn = tenant_db.tenant(user["user_id"])
        return tenant_db.list_skills(conn, statuses=("proposed",))[0]

    def test_a_draft_is_invisible_to_an_agent(self, cfg, user, a_draft):
        """§9.2: "Never exposed: … any proposed skill." By id and by slug."""
        agent = mcp(user)
        assert not E.op_fetch(agent, a_draft["id"]).ok
        assert not E.op_fetch(agent, a_draft["slug"]).ok

    def test_a_draft_never_comes_back_from_search(self, cfg, user, a_draft):
        agent = mcp(user)
        found = E.op_search(agent, a_draft["name"][:30], scope="skills")
        assert a_draft["id"] not in str(found.data)

    def test_a_draft_is_not_synthesised_into_an_answer_either(self, cfg, user, a_draft):
        agent = mcp(user)
        got = E.op_get_or_synthesize_skill(agent, "planning a cycle", skill_slug=a_draft["slug"])
        assert got.data.get("origin") != "saved" if got.ok else True

    def test_an_agent_cannot_approve_its_own_draft(self, cfg, user, a_draft):
        """The whole control, in one test. A gate an agent can open is not one."""
        refused = E.op_review_skill(mcp(user), a_draft["id"], "approve")
        assert not refused.ok
        assert refused.error_reason == "person_only"
        assert tenant_db.get_skill(tenant_db.tenant(user["user_id"]), a_draft["id"])["status"] == "proposed"

    def test_an_agent_cannot_activate_one_either(self, cfg, user, a_draft):
        assert not E.op_set_skill_state(mcp(user), a_draft["id"], "active").ok

    def test_a_person_approving_makes_it_reachable(self, cfg, user, a_draft):
        assert E.op_review_skill(web(user), a_draft["id"], "approve").ok
        assert E.op_fetch(mcp(user), a_draft["id"]).ok

    def test_dismissing_archives_rather_than_deletes(self, cfg, user, a_draft):
        """§6 rule 8: nothing is deleted by an agent, and a dismissed draft is
        recoverable from the Archive like anything else."""
        assert E.op_review_skill(web(user), a_draft["id"], "dismiss").ok
        row = tenant_db.get_skill(tenant_db.tenant(user["user_id"]), a_draft["id"])
        assert row["status"] == "archived"
        assert row["archived_at"]

    def test_reviewing_something_that_is_not_in_review_is_refused(self, cfg, user, a_draft):
        E.op_review_skill(web(user), a_draft["id"], "approve")
        again = E.op_review_skill(web(user), a_draft["id"], "approve")
        assert not again.ok and again.error_reason == "not_in_review"

    def test_a_draft_cannot_skip_the_queue_through_the_state_change(self, cfg, user, a_draft):
        """`Activate` is the overflow menu of an approved skill. Pointing it at
        a draft would be approval by another name."""
        refused = E.op_set_skill_state(web(user), a_draft["id"], "active")
        assert not refused.ok and refused.error_reason == "needs_review_first"

    def test_an_inactive_skill_is_invisible_to_an_agent(self, cfg, user, a_draft):
        """§9.2: "any inactive skill" — the other half of the same sentence."""
        E.op_review_skill(web(user), a_draft["id"], "approve")
        E.op_set_skill_state(web(user), a_draft["id"], "inactive")
        assert not E.op_fetch(mcp(user), a_draft["id"]).ok

    def test_editing_a_draft_still_does_not_promote_it(self, cfg, user, a_draft):
        """§7.9 offers Edit before Approve. The edit must leave it in the
        queue, or the queue is decoration."""
        conn = tenant_db.tenant(user["user_id"])
        tenant_db.upsert_skill(
            conn, slug=a_draft["slug"], name=a_draft["name"],
            description="Corrected before approving.", trigger_when=a_draft["trigger_when"],
            execution_steps=a_draft["execution_steps"],
        )
        assert tenant_db.get_skill(conn, a_draft["id"])["status"] == "proposed"


class TestTheSidebarCount:
    def test_it_counts_the_queue_and_not_the_library(self, cfg, user, with_steps):
        """§18.5: the amber pill shows the review queue; when it empties the
        pill becomes a plain total. So this counts `proposed`, not skills."""
        conn = tenant_db.tenant(user["user_id"])
        ingest(user, cfg)
        assert tenant_db.count_skills_needing_review(conn) == 1

        draft = tenant_db.list_skills(conn, statuses=("proposed",))[0]
        E.op_review_skill(web(user), draft["id"], "approve")
        assert tenant_db.count_skills_needing_review(conn) == 0
        assert tenant_db.list_skills(conn, statuses=("active",))


class TestTheEndpoints:
    def _client(self, cfg, user):
        from app.main import create_app

        client = TestClient(create_app())
        client.post("/api/login", json={"email": "founder@example.com"})
        return client

    def test_the_three_sections_and_the_count(self, cfg, user, with_steps):
        ingest(user, cfg)
        body = self._client(cfg, user).get("/api/skills").json()
        assert body["needs_review"] == 1
        assert len(body["from_your_sources"]) == 1
        assert body["active"] == [] and body["inactive"] == []

    def test_approving_over_http(self, cfg, user, with_steps):
        ingest(user, cfg)
        client = self._client(cfg, user)
        draft = client.get("/api/skills").json()["from_your_sources"][0]

        response = client.post(
            f"/api/skills/{draft['id']}/review",
            json={"decision": "approve"},
            headers={
                "X-Brain-Request": "1",
                "X-Brain-CSRF": client.cookies.get("brain_csrf", ""),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["needs_review"] == 0

    def test_they_need_a_session(self, cfg, user):
        from app.main import create_app

        assert TestClient(create_app()).get("/api/skills").status_code in (401, 403)
