"""§7.3, finished — and one promise the frame could not keep.

§9.1 says the work block "is a view of real state, never a fake animation",
and §7.3 calls `processing` and `done` "two states of the same page". Nothing
polled. The block was a snapshot taken by the single page read, so the running
step's spinner kept turning after that step had finished, and `processing`
never became `done` without the person reloading. `GET /api/sources/:id/job`
had existed since the step ledger was built and was called by nothing.

The rest of this frame was missing its controls. §7.3 names five actions —
Open transcript, Copy summary, Download summary, Approve a skill, and the
overflow (Rename, Re-process, Archive). Three of the five did not exist: the
Summary card drew the gist with no controls at all, and the frame had no
overflow menu anywhere.

The browser half is in `tests/browser/source_probe.js` and
`tests/browser/poll_probe.js`. The second one is the only way to show the
polling: it loads the page with a job still queued, drains the queue from a
separate process, and checks the page notices on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import control as control_db

FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
FRAMES_CSS = Path(__file__).resolve().parents[1] / "app/static/frames.css"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
#: The demo library seeded at sign-in already holds `YOUTUBE`, so a test that
#: signs in and needs a source of its own uses this one.
OTHER = "https://www.youtube.com/watch?v=9bZkp7q19f0"


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


class TestTheWorkBlockIsAViewOfRealState:
    def test_the_six_steps_exist_before_the_worker_touches_the_job(self, cfg, user, control):
        """§7.3 draws six rows from the first paint, and a job sits `queued`
        for as long as the queue is busy."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        progress = E.op_source_progress(ctx, queued.data["source_id"]).data
        assert len(progress["steps"]) == 6
        assert {step["state"] for step in progress["steps"]} == {"pending"}
        assert progress["finished"] is False

    def test_the_same_read_shows_them_resolved_afterwards(self, cfg, user):
        """The whole point of polling: one endpoint, two answers."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        source_id = queued.data["source_id"]
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        progress = E.op_source_progress(ctx, source_id).data
        assert progress["finished"] is True
        assert "pending" not in {step["state"] for step in progress["steps"]}

    def test_a_step_reveals_its_finding_only_once_done(self, cfg, user):
        """§7.3: "A step reveals its finding only once done." A pending step
        with a detail already written would be the fake animation §9.1
        forbids."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        for step in E.op_source_progress(ctx, queued.data["source_id"]).data["steps"]:
            assert step["state"] == "pending"
            assert not step["detail"]

    def test_the_endpoint_the_poll_calls_is_served(self, cfg, user, client):
        # Not `YOUTUBE`: signing in seeds the demo library, whose video has
        # that URL, so ingesting it again is correctly deduplicated to a
        # source that never had a job.
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, OTHER)
        response = client.get(f"/api/sources/{queued.data['source_id']}/job")
        assert response.status_code == 200
        assert len(response.json()["steps"]) == 6

    def test_polling_it_repeatedly_is_not_a_runaway_agent(self, cfg, user, client):
        """A poll is the same call over and over by design — which is exactly
        what the loop breaker exists to stop. It is marked repeatable."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, OTHER)
        for _ in range(5):
            assert client.get(
                f"/api/sources/{queued.data['source_id']}/job"
            ).status_code == 200

    def test_a_failed_job_stops_the_poll_rather_than_spinning(self, cfg, user):
        """`abandon_steps` stamps every unfinished step, so `finished` is true
        on a job that died — which is what tells the frame to stop asking."""
        from app.ingestion.providers import ProviderError

        class Blocked:
            def fetch_url(self, target, *, cfg):
                raise ProviderError("blocked_by_platform", "That video is private.")

            def fetch_file(self, validated, *, cfg):  # pragma: no cover
                raise ProviderError("blocked_by_platform", "n/a")

        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        ctx.prov.fetcher = Blocked()
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        progress = E.op_source_progress(ctx, queued.data["source_id"]).data
        assert progress["status"] == "blocked_by_platform"
        assert progress["error_message"]


class TestTheSummaryIsCopyableAndDownloadable:
    @pytest.fixture()
    def ingested(self, cfg, user):
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        return ctx, queued.data["source_id"]

    def test_the_one_read_carries_everything_both_controls_need(self, ingested):
        """Neither control fetches anything: the gist and the points are on
        screen already, so a round trip to re-read them is a round trip for
        nothing."""
        ctx, source_id = ingested
        data = E.op_get_source(ctx, source_id).data
        assert data["summary"]["gist"]
        assert data["summary"]["points"]
        assert data["source"]["title"]

    def test_a_source_with_no_summary_draws_no_summary_card(self, cfg, user, control):
        """§6 rule 13, and §6 rule 11: a Copy button over an empty card is a
        promise the page cannot keep."""
        control_db.update_profile(control, user["user_id"], want_summary=False)
        ctx = E.build_context(user["user_id"], surface="web_ui")
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        assert E.op_get_source(ctx, queued.data["source_id"]).data["summary"] is None


class TestTheFrameSource:
    @staticmethod
    def frame() -> str:
        source = FRAMES_JS.read_text()
        return source[source.index("FRAMES.source =") : source.index("function watchTheWork")]

    def test_the_summary_card_carries_copy_and_download(self):
        frame = self.frame()
        card = frame[frame.index('<span>Summary</span>') :]
        card = card[: card.index("</article>")]
        assert 'act: "copy-summary"' in card
        assert 'act: "download-summary"' in card

    def test_and_neither_is_visible_at_rest(self):
        """§18.4: "the Summary card shows prose with no visible controls at
        rest" — §4.1's hover rule, on a card that is not an item card."""
        css = FRAMES_CSS.read_text()
        rest = css[css.index(".yield-actions {") : css.index("}", css.index(".yield-actions {"))]
        assert "opacity: 0;" in rest
        assert ".yield-card:hover .yield-actions" in css
        assert ".yield-card:focus-within .yield-actions" in css
        # A phone has no hover.
        assert css.index("@media (max-width: 719px)") > 0
        assert ".yield-actions { opacity: 1; }" in css

    def test_the_header_carries_the_overflow_menu_7_3_names(self):
        frame = self.frame()
        header = frame[frame.index('<header class="source-head">') : frame.index("</header>")]
        assert "cardMenu(" in header
        for label in ("Rename", "Re-process", "Archive"):
            assert f'"{label}"' in header, label
        # §4.7: a separator precedes destructive items.
        assert header.index("{ sep: true }") < header.index('"Archive"')

    def test_re_process_appears_only_for_a_source_with_an_address(self):
        """§6 rule 11: a menu item that can only answer with a refusal does
        not belong in the menu."""
        frame = self.frame()
        assert 'src.canonical_url && { act: "reprocess"' in frame

    def test_the_menu_sits_on_the_right(self):
        """§6 rule 1. The header is a flex row and the menu is its last
        child, so it needs the auto margin to reach the end."""
        css = FRAMES_CSS.read_text()
        assert ".source-head .menu-wrap { margin-left: auto;" in css

    def test_the_poll_stops_when_the_frame_is_replaced(self):
        """A frame that polls can outlive the page that owns it. Left running,
        it would keep reading, and its reload would land the person back on a
        page they navigated away from."""
        source = FRAMES_JS.read_text()
        watch = source[source.index("function watchTheWork") :]
        watch = watch[: watch.index("\n/** §7.3's `Rename`")]
        assert "if (stop || !node.isConnected) return" in watch
        # A failed poll is not a failed job.
        assert "setTimeout(tick, 4000)" in watch
        # And it stops once the job is over, either way.
        assert '["failed", "blocked_by_platform"].includes(progress.status)' in watch

    def test_the_header_does_not_say_done_in_0s(self):
        """Accurate, and it says nothing. Under a second it is just `Done` —
        the same answer the header gives when the duration is unknown."""
        frame = self.frame()
        assert "? `Done in ${humanDuration(progress.elapsed_sec)}`" in frame
        assert "progress.elapsed_sec !== null" not in frame

    def test_approve_goes_through_the_one_handler_map(self):
        """There were two Approve implementations — `wireApprovals` and the
        skills frame's own — and one of them has to be the survivor."""
        source = FRAMES_JS.read_text()
        assert "function wireApprovals" not in source
        assert "data-approve=" not in source
        assert len(re.findall(r"^    approve: async", source, re.M)) == 2  # §7.3 and §7.9
