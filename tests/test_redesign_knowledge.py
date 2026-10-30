"""§7.3 source detail, §7.8 rules, §7.9 skills, §7.14 sign in.

Four frames, and one hole they closed: an unauthenticated visit used to fail
with a silent 401 and render the product's own error text as a page. §7.14 is
the door, and §6 rule 11 is the rule — "an unavailable control says what is
missing and offers to fix it".

The source detail is one read, deliberately. The frame is a single page —
header, instruction, six steps, then what came out of it — so fetching the
summary, the rules and the skills separately would be four round trips to draw
one screen and a page that assembles itself in front of the person.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import tenant as tenant_db
from app.ingestion import providers as prov

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
FRAMES_CSS = Path(__file__).resolve().parents[1] / "app/static/frames.css"


class StepfulExtractor:
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
                "when_it_fails": "the team is smaller than five people",
                "exact_steps": ["List the candidates.", "Name an owner.", "Cut to six weeks."],
                "anchor_quote": " ".join(sentences[0].split()[:14]),
                "locator_type": context.get("locator_type", "timestamp"),
                "locator_value": "144",
                "salience": 5,
            }
        ]


@pytest.fixture()
def library(cfg, user):
    bundle = prov.build_providers(cfg)
    bundle.extractor = StepfulExtractor()
    prov.set_providers(bundle)
    try:
        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(
            ctx, YOUTUBE, user_note="Pull out how they run planning cycles. Skip the hiring parts."
        )
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        E.op_remember_this(
            ctx, claim="Never use em dashes in anything you draft for me.", when_it_applies="always"
        )
        yield ctx, queued.data["source_id"]
    finally:
        prov.set_providers(None)


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


class TestTheSourceDetailIsOneRead:
    def test_it_carries_everything_the_frame_draws(self, library):
        ctx, source_id = library
        data = E.op_get_source(ctx, source_id).data
        assert data["source"]["title"]
        assert data["instruction"].startswith("Pull out how they run planning cycles")
        assert data["summary"] and data["summary"]["gist"]
        assert data["rules"] and data["skills"]
        assert data["has_transcript"] is True
        assert len(data["progress"]["steps"]) == 6

    def test_every_rule_carries_its_moment(self, library):
        """§7.3 prints a timestamp beside each rule, and it has to go
        somewhere when clicked."""
        ctx, source_id = library
        for rule in E.op_get_source(ctx, source_id).data["rules"]:
            assert rule["locator_value"]
            assert rule["deep_link"]

    def test_the_elapsed_time_is_measured_not_guessed(self, library):
        """§7.3's header reads "Done in 2m 14s". Measured from the first step
        that started to the last that finished — not the job row's age, which
        includes time spent waiting in the queue."""
        ctx, source_id = library
        progress = E.op_source_progress(ctx, source_id).data
        assert progress["finished"] is True
        assert progress["elapsed_sec"] is not None
        assert progress["elapsed_sec"] >= 0

    def test_a_job_still_running_has_no_elapsed_time(self, cfg, user):
        """Half a duration is worse than none: §4.4 says a running step
        reveals nothing, and the header is the same promise."""
        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, YOUTUBE)
        progress = E.op_source_progress(ctx, queued.data["source_id"]).data
        assert progress["finished"] is False
        assert progress["elapsed_sec"] is None

    def test_an_archived_source_has_no_detail_over_mcp(self, library, user):
        ctx, source_id = library
        agent = E.build_context(user["user_id"], surface="mcp")
        assert E.op_get_source(agent, source_id).ok
        E.op_archive_item(agent, "source", source_id)
        assert not E.op_get_source(agent, source_id).ok

    def test_the_endpoint_serves_it_and_refuses_an_unknown_id(self, library, client):
        _, source_id = library
        assert client.get(f"/api/sources/{source_id}").status_code == 200
        assert client.get("/api/sources/src_nope").status_code == 404


class TestTheKnowledgeFrames:
    def test_rules_arrive_in_the_two_groups(self, library, client):
        body = client.get("/api/rules").json()
        assert len(body["mine"]) == 1
        assert body["from_your_sources"]

    def test_skills_arrive_in_the_three_sections(self, library, client):
        body = client.get("/api/skills").json()
        assert body["needs_review"] == 1
        assert len(body["from_your_sources"]) == 1

    def test_approving_from_a_frame_moves_it(self, library, client):
        draft = client.get("/api/skills").json()["from_your_sources"][0]
        headers = {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}
        response = client.post(
            f"/api/skills/{draft['id']}/review", json={"decision": "approve"}, headers=headers
        )
        assert response.status_code == 200
        after = client.get("/api/skills").json()
        assert after["needs_review"] == 0
        assert len(after["active"]) == 1

    def test_dismissing_archives_it(self, library, client, user):
        draft = client.get("/api/skills").json()["from_your_sources"][0]
        headers = {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}
        client.post(
            f"/api/skills/{draft['id']}/review", json={"decision": "dismiss"}, headers=headers
        )
        row = tenant_db.get_skill(tenant_db.tenant(user["user_id"]), draft["id"])
        assert row["status"] == "archived"


class TestTheDoor:
    def test_an_unsigned_visit_is_refused_by_the_api(self, cfg, user):
        """The precondition for the redirect being worth anything."""
        from app.main import create_app

        anonymous = TestClient(create_app())
        assert anonymous.get("/api/sources").status_code in (401, 403)

    def test_the_frames_send_a_refused_read_to_the_door(self):
        """§6 rule 11. A 401 drawn as a page error tells the person nothing
        they can act on; §7.14 is the thing they can act on."""
        source = FRAMES_JS.read_text()
        assert 'error.reason = "needs_sign_in"' in source
        assert 'if (error?.reason === "needs_sign_in"' in source
        # Replaced, not pushed: otherwise Back bounces between the page they
        # cannot see and the form.
        assert 'go(href("/signin"), { replace: true })' in source

    def test_the_card_is_width_capped(self):
        """§7.14 names the bug by name: as a flex child with no `max-width`
        the column collapses to ~240px and the placeholder truncates
        mid-word."""
        css = FRAMES_CSS.read_text()
        block = css[css.index(".signin-card {") : css.index("}", css.index(".signin-card {"))]
        assert "max-width: 380px" in block
        assert "flex: 0 0 auto" in block

    def test_it_keeps_the_three_promises(self):
        source = FRAMES_JS.read_text()
        assert "three editable demo items" in source
        assert "with one link" in source
        assert "archive, never permanently delete" in source


class TestWhatOnlyARenderShows:
    """Three defects a browser found that the source did not."""

    def test_the_caret_points_down_at_rest(self):
        """§4.2 rotates it -90 degrees when CLOSED. With a right-pointing base
        the two states swap: open pointed right and closed pointed up."""
        assert 'icon("chevron-down")' in FRAMES_JS.read_text()
        assert 'rotate(-90deg)' in (
            Path(__file__).resolve().parents[1] / "app/static/molecules.css"
        ).read_text()

    def test_the_thumbnail_is_not_painted_the_colour_of_the_canvas(self):
        """`--bg-code` is grey-150, which is also `--bg-canvas`. A thumb
        painted with it is invisible on this page."""
        css = FRAMES_CSS.read_text()
        block = css[css.index(".source-thumb {") : css.index("}", css.index(".source-thumb {"))]
        assert "var(--bg-base)" in block
        assert "var(--bg-code)" not in block

    def test_a_skills_steps_carry_no_dividers(self):
        """The divider lives on `li + li`, so neutralising it needs the same
        shape and not a looser one."""
        assert ".steps-plain > li + li { border-top: 0; }" in FRAMES_CSS.read_text()

    def test_the_first_section_on_a_page_is_not_pushed_down(self):
        """§6 rule 4 puts `--space-8` BETWEEN sections. Written as
        `.section + .section` it also silently omits the gap above the first
        one that follows anything else."""
        css = FRAMES_CSS.read_text()
        assert ".section { margin-top: var(--space-8); }" in css
        assert ".section:first-child { margin-top: 0; }" in css
