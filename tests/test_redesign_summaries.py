"""Summaries — `MASTER_PLAN.md` §7.6, §8 and §9.1 step 3.

The audit's largest gap: no table, no column, no pipeline stage, no op, no
endpoint. §7.6 builds two frames and a sidebar count on top of it.

The sentence that shapes every test here is §7.6's: *"Every point carries its
timestamp, right-aligned, clickable through to the transcript. This is what
makes a generated summary trustworthy."* A summary whose points cannot be
traced back is exactly the thing a personal library must not produce — so a
point whose moment cannot be found carries **no** timestamp rather than a
plausible one, and that is asserted in both the deterministic and the live
path.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.ingestion import providers as prov

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def ingest(user, cfg, note=""):
    ctx = E.build_context(user["user_id"])
    queued = E.op_ingest_source(ctx, YOUTUBE, user_note=note)
    assert queued.ok, queued
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
    return queued.data["job_id"], queued.data["source_id"]


def app():
    from app.main import create_app

    return create_app()


def signed_in(cfg, user):
    client = TestClient(app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


class TestTheSummariserItself:
    def test_it_selects_sentences_and_never_writes_new_ones(self, cfg):
        """Extractive by design. Every sentence it returns appeared in the
        source, so §7.6's traceability holds by construction rather than by
        hope — which is the right property for a fallback nobody is watching."""
        text = (
            "Cycles run six weeks. The cooldown is never a buffer. "
            "Scope gets cut rather than dates moved. "
            "One owner writes the spec before any estimate exists."
        )
        out = prov.HeuristicSummariser(cfg=cfg).summarise(text, [], {})
        for point in out["points"]:
            assert point["text"] in text

    def test_a_point_whose_moment_is_unknown_gets_no_timestamp(self, cfg):
        """Not a guessed one. §7.6 calls the timestamp what makes the summary
        trustworthy, and a fabricated one is worse than none."""
        cues = [{"start": 42.0, "end": 50.0, "text": "Scope gets cut rather than dates moved."}]
        out = prov.HeuristicSummariser(cfg=cfg).summarise(
            "Cycles run six weeks here. Scope gets cut rather than dates moved. "
            "One owner writes the spec before any estimate exists.",
            cues,
            {},
        )
        by_text = {p["text"]: p["t_ms"] for p in out["points"]}
        assert by_text["Scope gets cut rather than dates moved."] == 42000
        assert all(v is None for k, v in by_text.items() if "Scope gets cut" not in k)

    def test_points_come_back_in_reading_order(self, cfg):
        """Selected by salience, returned in the order they were said. A
        summary that jumps around is not one."""
        text = (
            "An opening line that sets the scene for everyone listening. "
            "Scope gets cut rather than dates moved, always, without exception. "
            "One owner writes the spec before any estimate exists at all."
        )
        out = prov.HeuristicSummariser(cfg=cfg).summarise(text, [], {})
        positions = [text.index(p["text"]) for p in out["points"]]
        assert positions == sorted(positions)

    def test_chapters_need_a_timeline_and_say_nothing_without_one(self, cfg):
        """§7.6's Full view adds "a chapter walkthrough with time ranges". A
        document has no timeline, so it gets no chapters rather than four
        invented headings."""
        text = " ".join(f"Sentence number {i} about planning cycles." for i in range(12))
        assert prov.HeuristicSummariser(cfg=cfg).summarise(text, [], {})["chapters"] == []

        cues = [
            {"start": float(i * 10), "end": float(i * 10 + 9), "text": f"Part {i} of the talk."}
            for i in range(12)
        ]
        chapters = prov.HeuristicSummariser(cfg=cfg).summarise(text, cues, {})["chapters"]
        assert chapters
        for chapter in chapters:
            assert chapter["from_ms"] < chapter["to_ms"]
            assert chapter["title"]

    def test_the_live_one_falls_back_rather_than_losing_the_source(self, cfg):
        """A model outage must not cost the whole ingestion. `live_llm` is off
        in test, which is the same branch an outage takes."""
        live = prov.LiveSummariser(Settings(env="test"))
        out = live.summarise("Cycles run six weeks. Scope gets cut, never the date.", [], {})
        assert out["gist"]


class TestTheThirdStepProducesOne:
    def test_a_source_now_gets_a_summary(self, cfg, user):
        _, source_id = ingest(user, cfg)
        summary = tenant_db.get_summary(tenant_db.tenant(user["user_id"]), source_id=source_id)
        assert summary and summary["gist"]
        assert summary["points"]

    def test_the_step_reports_what_it_did(self, cfg, user):
        job_id, _ = ingest(user, cfg)
        step = next(
            s for s in control_db.job_steps(control_db.control(), job_id)
            if s["step"] == "summarise"
        )
        assert step["state"] == "done"
        assert step["detail"].endswith("key points")

    def test_an_instruction_changes_what_the_step_says(self, cfg, user):
        """§7.3's capture reads "following your instruction" — because steps 3
        to 5 honour it (§9.1), the step says so rather than reporting a count
        the person did not ask for."""
        job_id, source_id = ingest(user, cfg, note="Pull out how they run planning cycles.")
        step = next(
            s for s in control_db.job_steps(control_db.control(), job_id)
            if s["step"] == "summarise"
        )
        assert step["detail"] == "following your instruction"

        stored = tenant_db.get_summary(tenant_db.tenant(user["user_id"]), source_id=source_id)
        assert stored["instruction"] == "Pull out how they run planning cycles."

    def test_reprocessing_replaces_rather_than_duplicates(self, cfg, user):
        """§7.3's overflow menu offers Re-process. One source, one summary."""
        conn = tenant_db.tenant(user["user_id"])
        _, source_id = ingest(user, cfg)
        first = tenant_db.get_summary(conn, source_id=source_id)["id"]

        tenant_db.upsert_summary(
            conn, source_id=source_id, gist="A second pass.", points=[{"text": "x", "t_ms": None}]
        )
        rows = conn.execute("SELECT COUNT(*) AS n FROM summaries WHERE source_id = ?", (source_id,))
        assert rows.fetchone()["n"] == 1
        assert tenant_db.get_summary(conn, source_id=source_id)["id"] == first

    def test_a_failing_summariser_does_not_cost_the_source(self, cfg, user, monkeypatch):
        """The transcript and the rules are still worth having."""
        class Broken:
            def summarise(self, *_a, **_k):
                raise RuntimeError("model unreachable")

        bundle = prov.build_providers(cfg)
        monkeypatch.setattr(bundle, "summariser", Broken(), raising=False)
        prov.set_providers(bundle)
        try:
            job_id, source_id = ingest(user, cfg)
        finally:
            prov.set_providers(None)

        steps = {s["step"]: s for s in control_db.job_steps(control_db.control(), job_id)}
        assert steps["summarise"]["state"] in ("skipped", "failed")
        assert steps["extract"]["state"] == "done"
        conn = tenant_db.tenant(user["user_id"])
        assert tenant_db.claims_for_source(conn, source_id), "the source was lost with the summary"


class TestReadingOne:
    def test_it_is_reachable_by_summary_id_and_by_source_id(self, cfg, user):
        """§7.6 is entered from the Summaries list and from §7.3's source
        detail, and both arrive at the same record."""
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)

        by_source = E.op_get_summary(ctx, source_id=source_id)
        assert by_source.ok
        by_id = E.op_get_summary(ctx, summary_id=by_source.data["summary"]["id"])
        assert by_id.ok
        assert by_id.data["summary"]["id"] == by_source.data["summary"]["id"]

    def test_every_point_that_has_a_moment_gets_a_deep_link(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)
        got = E.op_get_summary(ctx, source_id=source_id)

        timed = [p for p in got.data["points"] if p["t_ms"] is not None]
        assert timed, "no point resolved to a moment"
        for point in timed:
            assert point["deep_link"], point["text"]
            assert f"t={int(point['t_ms'] / 1000)}" in point["deep_link"]

    def test_a_point_with_no_moment_gets_no_link_either(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)
        for point in E.op_get_summary(ctx, source_id=source_id).data["points"]:
            if point["t_ms"] is None:
                assert point["deep_link"] == ""

    def test_the_read_time_is_derived_and_never_zero(self, cfg, user):
        """§7.6 puts it in the meta line, and the Brief/Full control changes
        it — so it is computed from the stored text, not typed in."""
        _, source_id = ingest(user, cfg)
        summary = tenant_db.get_summary(tenant_db.tenant(user["user_id"]), source_id=source_id)
        assert summary["read_minutes"] >= 1

    def test_an_unknown_id_is_a_refusal_not_an_empty_summary(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        assert not E.op_get_summary(ctx, summary_id="sum_nope").ok
        assert not E.op_get_summary(ctx).ok


class TestArchivingReachesTheSummary:
    def test_archiving_a_source_archives_its_summary(self, cfg, user):
        """§7.12's filter has a `Summary` pill, so a summary left active under
        an archived source would point at nothing."""
        conn = tenant_db.tenant(user["user_id"])
        ctx = E.build_context(user["user_id"], surface="mcp")
        _, source_id = ingest(user, cfg)

        E.op_archive_item(ctx, "source", source_id)
        summary = tenant_db.get_summary(conn, source_id=source_id)
        assert summary["status"] == "archived"
        assert summary["archived_by"] == "brain"
        assert summary["archived_at"]

    def test_restoring_the_source_restores_it(self, cfg, user):
        conn = tenant_db.tenant(user["user_id"])
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)

        E.op_archive_item(ctx, "source", source_id)
        E.op_archive_item(ctx, "source", source_id, restore=True)
        summary = tenant_db.get_summary(conn, source_id=source_id)
        assert summary["status"] == "active"
        assert summary["archived_at"] is None

    def test_an_archived_summary_does_not_exist_over_mcp(self, cfg, user):
        """The same rule as every other archived thing."""
        ctx = E.build_context(user["user_id"], surface="mcp")
        _, source_id = ingest(user, cfg)
        E.op_archive_item(ctx, "source", source_id)
        assert not E.op_get_summary(ctx, source_id=source_id).ok

    def test_the_list_never_shows_an_archived_one(self, cfg, user):
        ctx = E.build_context(user["user_id"])
        _, source_id = ingest(user, cfg)
        assert len(E.op_list_summaries(ctx).data["summaries"]) == 1
        E.op_archive_item(ctx, "source", source_id)
        assert E.op_list_summaries(ctx).data["summaries"] == []


class TestTheEndpoints:
    def test_the_list_and_both_detail_doors(self, cfg, user):
        _, source_id = ingest(user, cfg)
        client = signed_in(cfg, user)

        listed = client.get("/api/summaries")
        assert listed.status_code == 200
        assert len(listed.json()["summaries"]) == 1
        summary_id = listed.json()["summaries"][0]["id"]

        assert client.get(f"/api/summaries/{summary_id}").status_code == 200
        assert client.get(f"/api/sources/{source_id}/summary").status_code == 200

    def test_an_unknown_summary_is_404(self, cfg, user):
        assert signed_in(cfg, user).get("/api/summaries/sum_nope").status_code == 404

    def test_they_need_a_session(self, cfg, user):
        client = TestClient(app())
        assert client.get("/api/summaries").status_code in (401, 403)
