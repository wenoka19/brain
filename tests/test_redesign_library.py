"""The library frames — `MASTER_PLAN.md` §7.4, §7.5, §7.6 and §7.7.

Four frames that share one component (§4.1's item card) and differ, by design,
in exactly three things: the icon, the three metadata fragments, and the pill.

Two findings are pinned here because building the frames is what surfaced
them, and neither was visible from the engine side:

* **The loop breaker refused a browser.** It exists to stop an agent calling
  the same tool thirty times; a person pressing Back, reloading, or opening a
  second tab does the same thing and is not that. The frames hit it on their
  first navigation. Reads are now marked `repeatable`, per call site.
* **The Sources list included a source nobody can open.** `remember_this`
  hangs a hand-written rule off a synthetic `user_note` row, and §7.7 lists
  Link, Document and Image — so the container was a row with no type, no
  yield and nothing to fix.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import tenant as tenant_db

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
ARTICLE = "https://example.com/how-linear-builds-software"


@pytest.fixture()
def library(cfg, user):
    """One video, one article, and one rule written by hand."""
    ctx = E.build_context(user["user_id"])
    E.op_ingest_source(ctx, YOUTUBE, user_note="Pull out how they run planning cycles.")
    E.op_ingest_source(ctx, ARTICLE)
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=5)
    E.op_remember_this(
        ctx, claim="Never use em dashes in anything you draft for me.", when_it_applies="always"
    )
    return ctx


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


class TestTheReadsBehindTheFrames:
    def test_the_sources_list_leads_with_the_type_and_closes_with_the_yield(self, library, user):
        """§7.7: the type is `Link` / `Document` / `Image`, never a platform
        name — and "a source with nothing green produced nothing"."""
        rows = tenant_db.list_sources(tenant_db.tenant(user["user_id"]))
        assert rows
        for row in rows:
            assert row["source_type"] != "user_note"
            assert "rule_count" in row and "skill_count" in row

    def test_the_hand_written_container_is_not_a_source(self, library, user):
        """It has no type, no yield and nothing to open. §7.8 shows those rules
        as `Written here`; §7.7 must not show their container as a row."""
        conn = tenant_db.tenant(user["user_id"])
        assert conn.execute(
            "SELECT 1 FROM sources WHERE source_type = 'user_note'"
        ).fetchone(), "the fixture did not create one, so this proves nothing"
        assert not [r for r in tenant_db.list_sources(conn) if r["source_type"] == "user_note"]
        assert not [r for r in tenant_db.list_transcripts(conn) if r["source_type"] == "user_note"]

    def test_a_word_count_is_stored_rather_than_recomputed(self, library, user):
        """§7.4 prints one on every row. Computing it per render means reading
        every transcript in full to draw the list."""
        conn = tenant_db.tenant(user["user_id"])
        for row in tenant_db.list_transcripts(conn):
            assert row["word_count"] > 0
            stored = tenant_db.get_source_content(conn, row["id"])["full_text"]
            assert row["word_count"] == len(stored.split())

    def test_rules_split_into_the_two_groups_of_section_7_8(self, library, user):
        ctx = library
        data = E.op_list_rules(ctx).data
        assert len(data["mine"]) == 1
        assert data["from_your_sources"]
        assert data["mine"][0]["claim"].startswith("Never use em dashes")

    def test_every_extracted_rule_carries_where_it_was_said(self, library, user):
        """§7.8: "Provenance is the point of this page." """
        for rule in E.op_list_rules(library).data["from_your_sources"]:
            assert rule["provenance"]["title"]
            assert rule["provenance"]["locator"]
            assert rule["provenance"]["deep_link"]

    def test_a_transcript_comes_back_as_timed_turns(self, library, user):
        ctx = library
        source = tenant_db.list_transcripts(tenant_db.tenant(user["user_id"]))[0]
        data = E.op_get_transcript(ctx, source["id"]).data
        assert data["turns"]
        timed = [t for t in data["turns"] if t["t_ms"] is not None]
        assert timed, "a video transcript came back with no moments"
        assert all(t["deep_link"] for t in timed)

    def test_a_transcript_has_no_speaker_anywhere(self, library, user):
        """`R-1`. The capture shows `Host` / `Guest`; the founder struck them,
        and nothing in ingestion records one. A turn is a moment and words."""
        source = tenant_db.list_transcripts(tenant_db.tenant(user["user_id"]))[0]
        for turn in E.op_get_transcript(library, source["id"]).data["turns"]:
            assert "speaker" not in turn

    def test_an_archived_source_has_no_transcript_over_mcp(self, library, user):
        source = tenant_db.list_transcripts(tenant_db.tenant(user["user_id"]))[0]
        agent = E.build_context(user["user_id"], surface="mcp")
        assert E.op_get_transcript(agent, source["id"]).ok
        E.op_archive_item(agent, "source", source["id"])
        assert not E.op_get_transcript(agent, source["id"]).ok


class TestABrowserIsNotARunawayAgent:
    def test_reading_the_same_list_twice_is_allowed(self, library, client):
        """The finding that building the frames surfaced. Back, a reload and a
        second tab all repeat a read; the loop breaker refused the second one
        and the page rendered its own error."""
        for _ in range(3):
            assert client.get("/api/transcripts").status_code == 200
            assert client.get("/api/sources").status_code == 200
            assert client.get("/api/rules").status_code == 200

    def test_a_repeated_write_is_still_refused(self, library, client):
        """The guard is kept where it belongs: a double-submitted POST is
        exactly what it is for."""
        csrf = client.cookies.get("brain_csrf", "")
        headers = {"X-Brain-Request": "1", "X-Brain-CSRF": csrf}
        first = client.post("/api/ingest", json={"url": YOUTUBE}, headers=headers)
        second = client.post("/api/ingest", json={"url": YOUTUBE}, headers=headers)
        assert first.status_code in (200, 429)
        assert second.status_code == 429, "an identical write was not caught"

    def test_the_sliding_window_still_bounds_the_web(self, cfg, user):
        """Dropping the loop guard must not drop the ceiling."""
        from app.security import ratelimit

        limiter = ratelimit.RateLimiter(tool_calls_per_min=5)
        allowed = sum(
            1
            for _ in range(10)
            if limiter.check(user["user_id"], "web:list_sources", {}, loop_guard=False).allowed
        )
        assert allowed == 5


class TestTheFramesRender:
    @pytest.mark.parametrize(
        "path", ["/api/counts", "/api/sources", "/api/transcripts", "/api/rules", "/api/summaries"]
    )
    def test_every_read_a_frame_makes_is_served(self, library, client, path):
        assert client.get(path).status_code == 200

    def test_the_counts_are_what_the_navigation_shows(self, library, client):
        """§5: "Counts live in the navigation and nowhere else." One read, so
        the sidebar does not fetch six lists to draw six numbers."""
        counts = client.get("/api/counts").json()["counts"]
        assert counts["sources"] == 2          # the user_note container is not one
        assert counts["transcripts"] == 2
        assert counts["rules"] >= 3            # extracted, plus the hand-written one
        assert "needs_review" in counts and "archive" in counts

    def test_a_transcript_is_reachable_by_its_source_id(self, library, client):
        source_id = client.get("/api/sources").json()["sources"][0]["id"]
        response = client.get(f"/api/transcripts/{source_id}")
        assert response.status_code == 200
        assert response.json()["turns"]

    def test_an_unknown_transcript_is_404(self, library, client):
        assert client.get("/api/transcripts/src_nope").status_code == 404


class TestTheFrameSource:
    """What the static tests can check about the markup the frames build."""

    @staticmethod
    def source() -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()

    def test_the_type_word_is_scoped_to_the_sources_list(self):
        """§7.7's rule — "never a platform name as the type" — applies to that
        page and no other, which §18.4's Q-1 settled from the captures."""
        source = self.source()
        assert "TYPE_WORD" in source and "FORMAT_WORD" in source
        sources_frame = source[source.index("FRAMES.sources ="):]
        sources_frame = sources_frame[: sources_frame.index("FRAMES.transcript ")] if "FRAMES.transcript " in sources_frame else sources_frame
        assert "TYPE_WORD" in sources_frame

        transcripts_frame = source[source.index("FRAMES.transcripts ="): source.index("FRAMES.summaries =")]
        assert "FORMAT_WORD" in transcripts_frame
        assert "TYPE_WORD" not in transcripts_frame

    def test_the_yield_is_the_only_green_in_a_meta_line(self):
        assert 'cls: "item-yield"' in self.source()

    def test_a_slow_frame_cannot_paint_over_the_one_that_replaced_it(self):
        """A frame that fetches can resolve after the person has navigated
        away. The generation counter is what stops the stale one landing."""
        source = self.source()
        assert "const mine = ++generation" in source
        assert "if (mine !== generation) return" in source
