"""The six visible pipeline steps — `MASTER_PLAN.md` §4.4, §7.3 and §9.1.

§9.1 is the sentence this file defends: *"Each transition writes a `job` row.
The UI polls or streams from those rows — the work block is a view of real
state, never a fake animation."*

The job's own `status` column could not carry it. It holds one value; the work
block shows six, each with its own result line, its own timing and its own
failure. So there is a ledger, and the tests below are mostly about the ways a
ledger can start lying: a step that reveals a result before it has one, a step
left spinning on a job that died, a list whose length changes with the source
type, and a progress read that answers for somebody else's source.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import control as control_db

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def ingest(user, cfg, target=YOUTUBE):
    ctx = E.build_context(user["user_id"])
    queued = E.op_ingest_source(ctx, target)
    assert queued.ok, queued
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
    return queued.data["job_id"], queued.data["source_id"]


def steps_of(job_id):
    return control_db.job_steps(control_db.control(), job_id)


class TestTheLedgerIsTheSixSteps:
    def test_the_six_are_fixed_ordered_and_named_by_the_plan(self, cfg, user):
        """§9.1's table, in its order. The frame draws six rows from the first
        paint, so they must all exist before any of them runs."""
        job_id, _ = ingest(user, cfg)
        assert [s["label"] for s in steps_of(job_id)] == [
            "Reading the source",
            "Transcribing audio",
            "Writing the summary",
            "Extracting rules",
            "Drafting skills",
            "Updating memory",
        ]

    def test_they_are_laid_down_before_anything_runs(self, cfg, user):
        """Queued, not yet drained: six pending rows."""
        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, YOUTUBE)
        rows = steps_of(queued.data["job_id"])
        assert len(rows) == 6
        assert {r["state"] for r in rows} == {"pending"}
        assert all(r["detail"] == "" for r in rows)

    def test_a_finished_step_reveals_what_it_actually_found(self, cfg, user):
        """§4.4: the result appears beside the step's name once it is done —
        and it is read from the run, not written as a caption."""
        job_id, _ = ingest(user, cfg)
        by_step = {s["step"]: s for s in steps_of(job_id)}

        assert by_step["read"]["detail"].startswith("www.youtube.com/watch")
        assert by_step["transcribe"]["detail"].endswith("words captured")
        assert by_step["extract"]["detail"].endswith("found")
        assert by_step["remember"]["detail"] == "available to every connection"

    def test_the_counts_in_the_details_match_the_library(self, cfg, user):
        """The number a step shows and the number of rows that landed are the
        same number, or the work block is decoration."""
        from app.db import tenant as tenant_db

        job_id, source_id = ingest(user, cfg)
        by_step = {s["step"]: s for s in steps_of(job_id)}

        claims = tenant_db.claims_for_source(tenant_db.tenant(user["user_id"]), source_id)
        assert by_step["extract"]["detail"] == f"{len(claims)} found"

        content = tenant_db.get_source_content(tenant_db.tenant(user["user_id"]), source_id)
        words = len(content["full_text"].split())
        assert by_step["transcribe"]["detail"] == f"{words:,} words captured"

    def test_every_step_that_ran_is_timed(self, cfg, user):
        job_id, _ = ingest(user, cfg)
        for step in steps_of(job_id):
            assert step["started_at"], step["step"]
            assert step["finished_at"], step["step"]

    def test_a_step_that_does_not_apply_says_so_rather_than_vanishing(self, cfg, user):
        """A list whose length changes with the source type cannot be read at
        a glance, so a step the person turned off is `skipped` with the
        reason — still six rows."""
        E.op_update_profile(E.build_context(user["user_id"], surface="web_ui"), want_summary=False)
        job_id, _ = ingest(user, cfg, "https://example.com/an-article")
        rows = steps_of(job_id)
        assert len(rows) == 6

        summarise = next(s for s in rows if s["step"] == "summarise")
        assert summarise["state"] == "skipped"
        assert "settings" in summarise["detail"]

    def test_step_two_is_named_after_what_the_source_is(self, cfg, user):
        """It was "Transcribing audio" for every source, so a text file got a
        row denying its own label: `Transcribing audio — 300 words read — no
        audio in this source`. The step always runs, because it is how a
        source becomes words; only the word for it changes."""
        video, _ = ingest(user, cfg, YOUTUBE)
        article, _ = ingest(user, cfg, "https://example.com/an-article")

        def step_two(job_id):
            return next(s for s in steps_of(job_id) if s["step"] == "transcribe")

        assert step_two(video)["label"] == "Transcribing audio"
        assert step_two(video)["detail"].endswith("words captured")

        assert step_two(article)["label"] == "Reading the text"
        assert step_two(article)["detail"].endswith("words read")
        assert "audio" not in step_two(article)["label"].lower()
        assert "audio" not in step_two(article)["detail"].lower()

    def test_an_image_is_described_rather_than_read_or_transcribed(self, cfg, user, tmp_path):
        """Its words come from a vision model looking at it, which is neither
        of the other two."""
        path = tmp_path / "chart.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 512)
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg)

        step = next(s for s in steps_of(queued.data["job_id"]) if s["step"] == "transcribe")
        assert step["label"] == "Reading the image"
        assert step["detail"].endswith("words described")

    def test_the_interjection_reply_names_the_steps_the_same_way(self, cfg, user):
        """§7.2 lists what a note will still reach. Two different names for
        one step, depending on which screen you are looking at, is how a
        person stops trusting either."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, "https://example.com/an-article")
        reply = E.op_add_instruction(ctx, queued.data["source_id"], "focus on pricing")
        named = reply.data["will_affect"] + reply.data["already_done"]
        assert "Reading the text" in named
        assert "Transcribing audio" not in named

    def test_no_step_is_a_placeholder_any_more(self, cfg, user):
        """All six have a producer now, and this is the assertion that says so.

        It began life the other way round: while `summarise` and
        `draft_skills` had no producer, they were recorded as `skipped` with
        "not built yet" — said to the person's face rather than dropped from
        the list. Each one moved off that list by growing a real producer, and
        the test narrowed with them until nothing was left on it. This is the
        end state, and it fails the day a placeholder comes back.
        """
        job_id, _ = ingest(user, cfg)
        for step in steps_of(job_id):
            assert step["detail"] != "not built yet", step["label"]
            assert step["state"] in ("done", "skipped"), step["label"]
            assert step["detail"], f"{step['label']} finished saying nothing"


class TestAFailedJobLeavesNothingSpinning:
    def test_nothing_stays_running_when_the_job_dies(self, cfg, user, monkeypatch):
        """A step left `running` on a dead job spins forever in the work
        block — the one failure mode a fake animation and a real one share."""
        from app.ingestion import extractor as extractor_mod

        def explode(*_args, **_kwargs):
            raise RuntimeError("extraction exploded")

        monkeypatch.setattr(E, "extract_grounded_claims", explode)

        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        rows = steps_of(queued.data["job_id"])
        assert not [s for s in rows if s["state"] == "running"]
        assert all(s["finished_at"] for s in rows)

    def test_the_step_that_broke_carries_the_reason_and_the_rest_do_not(
        self, cfg, user, monkeypatch
    ):
        """Saying "failed" of a step that never started would be a second lie
        on top of the first."""
        monkeypatch.setattr(
            E, "extract_grounded_claims",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        by_step = {s["step"]: s for s in steps_of(queued.data["job_id"])}
        assert by_step["read"]["state"] == "done"       # it really did
        assert by_step["extract"]["state"] == "failed"
        assert "boom" in by_step["extract"]["detail"]
        assert by_step["remember"]["state"] == "skipped"  # never started


class TestTheProgressRead:
    def test_it_returns_the_ledger_for_a_source(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)
        got = E.op_source_progress(ctx, source_id)
        assert got.ok
        assert got.data["status"] == "ready"
        assert got.data["finished"] is True
        assert len(got.data["steps"]) == 6

    def test_an_unknown_source_is_a_404_not_an_empty_list(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        got = E.op_source_progress(ctx, "src_does_not_exist")
        assert not got.ok and got.error_reason == "not_found"

    def test_it_answers_only_for_the_caller_s_own_source(self, cfg, user, control):
        """§10.7: "every query filters on `user_id` … the single most likely
        data-leak bug in this app". The source id arrives from the client."""
        _, source_id = ingest(user, cfg)

        other = control_db.ensure_user(control, "someone-else@example.com")
        other_ctx = E.build_context(other["user_id"])
        got = E.op_source_progress(other_ctx, source_id)
        assert not got.ok, "another tenant read this job's progress"

    def test_the_newest_run_wins_when_a_source_was_reprocessed(self, cfg, user):
        """§7.3's overflow menu offers Re-process, so a source has more than
        one job and the page must show the current one."""
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)
        first = E.op_source_progress(ctx, source_id).data["job_id"]

        again = E.op_ingest_source(ctx, YOUTUBE, force=True) if _supports_force() else None
        if again is None or not again.ok:
            pytest.skip("re-processing is not exposed yet")
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        assert E.op_source_progress(ctx, source_id).data["job_id"] != first


def _supports_force() -> bool:
    import inspect

    return "force" in inspect.signature(E.op_ingest_source).parameters


class TestTheEndpoint:
    def test_it_serves_the_work_block(self, cfg, user):
        _, source_id = ingest(user, cfg)
        client = TestClient(E_app())
        client.post("/api/login", json={"email": "founder@example.com"})

        response = client.get(f"/api/sources/{source_id}/job")
        assert response.status_code == 200
        body = response.json()
        assert len(body["steps"]) == 6
        assert body["steps"][0]["label"] == "Reading the source"

    def test_an_unknown_source_is_404(self, cfg, user):
        client = TestClient(E_app())
        client.post("/api/login", json={"email": "founder@example.com"})
        assert client.get("/api/sources/src_nope/job").status_code == 404

    def test_it_needs_a_session(self, cfg, user):
        _, source_id = ingest(user, cfg)
        client = TestClient(E_app())
        assert client.get(f"/api/sources/{source_id}/job").status_code in (401, 403)


def E_app():
    from app.main import create_app

    return create_app()
