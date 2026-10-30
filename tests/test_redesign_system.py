"""§7.12 Archive and §7.13 Profile.

Two frames and one principle each.

**Archive** is ordered by when something was set aside, not when it was made —
§7.12: "people come here to undo something they just did" — and every row says
who did it, in amber when it was Brain. That attribution is what §7.12 calls
the point of the page, and it is the reason `archived_at` and `archived_by`
had to exist before the frame could be honest.

**Profile** writes settings the pipeline actually reads. §9.1 says steps 3 to
5 honour the ingestion defaults, so a switch that is off has to stop work —
three switches that look wired and are not would be worse than no section.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
WEBAPI = Path(__file__).resolve().parents[1] / "app/webapi.py"
FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
ATOMS_CSS = Path(__file__).resolve().parents[1] / "app/static/atoms.css"


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


@pytest.fixture()
def set_aside(cfg, user):
    """One thing archived by the person, one by an agent."""
    web = E.build_context(user["user_id"], surface="web_ui")
    agent = E.build_context(user["user_id"], surface="mcp")
    queued = E.op_ingest_source(web, YOUTUBE)
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=2)

    mine = E.op_remember_this(
        web, claim="A rule I set aside myself.", when_it_applies="never"
    ).data["claim"]["id"]
    E.op_archive_item(web, "claim", mine)
    E.op_archive_item(agent, "source", queued.data["source_id"])
    return web, agent


class TestTheArchive:
    def test_it_is_ordered_by_when_it_was_set_aside(self, set_aside, user):
        """§7.12: "Not creation date — people come here to undo something they
        just did." Which is why `archived_at` had to exist first."""
        rows = tenant_db.list_archive(tenant_db.tenant(user["user_id"]))
        assert rows
        stamps = [r["archived_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)
        assert all(stamps), "a row reached the archive with no archive date"

    def test_every_row_says_who_set_it_aside(self, set_aside, user):
        """§7.12 calls the attribution the point of the page."""
        rows = tenant_db.list_archive(tenant_db.tenant(user["user_id"]))
        by_who = {r["archived_by"] for r in rows}
        assert by_who == {"you", "brain"}, by_who

    def test_the_cascade_is_attributed_to_whoever_caused_it(self, set_aside, user):
        """An agent archived a source; the rules that came out of it went with
        it, and each of those rows appears here too."""
        rows = tenant_db.list_archive(tenant_db.tenant(user["user_id"]))
        cascaded = [r for r in rows if r["kind"] == "rule" and r["archived_by"] == "brain"]
        assert cascaded, "the cascade left its rows unattributed"

    def test_it_is_one_heterogeneous_list(self, set_aside, user):
        """Which is why §7.12's type filter exists here and nowhere else."""
        kinds = {r["kind"] for r in tenant_db.list_archive(tenant_db.tenant(user["user_id"]))}
        assert len(kinds) > 1

    def test_an_agent_cannot_read_the_archive_at_all(self, set_aside):
        """§7.12 promises the person "nothing here is reachable by your AI".
        Not filtered for an agent — refused."""
        _, agent = set_aside
        refused = E.op_list_archive(agent)
        assert not refused.ok and refused.error_reason == "person_only"

    def test_the_endpoint_serves_the_list_and_not_the_mutation(self, set_aside, client):
        """A GET that lists, and a POST that archives one item, are two
        different handlers — see the name-collision test below."""
        response = client.get("/api/archive")
        assert response.status_code == 200
        body = response.json()
        assert body["items"] and body["types"]


class TestTheProfileWritesWhatThePipelineReads:
    def test_the_defaults_are_everything_on(self, cfg, user, control):
        wants = control_db.ingestion_defaults(control, user["user_id"])
        assert wants == {"want_summary": 1, "want_rules": 1, "want_skills": 1, "write_in": "source"}

    def test_a_switch_that_is_off_actually_stops_the_work(self, cfg, user, control):
        """§9.1: "Steps 3-5 honour the person's ingestion defaults." Three
        switches that look wired and are not would be worse than no section."""
        control_db.update_profile(control, user["user_id"], want_summary=False, want_skills=False)
        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        steps = {s["step"]: s for s in control_db.job_steps(control, queued.data["job_id"])}
        assert steps["summarise"]["state"] == "skipped"
        assert steps["summarise"]["detail"] == "off in your settings"
        assert steps["draft_skills"]["detail"] == "off in your settings"
        # And nothing was written.
        assert tenant_db.get_summary(
            tenant_db.tenant(user["user_id"]), source_id=queued.data["source_id"]
        ) is None

    def test_a_switch_that_is_on_still_does_the_work(self, cfg, user, control):
        """The other half: proving the switch is what changed the outcome."""
        control_db.update_profile(control, user["user_id"], want_summary=True)
        ctx = E.build_context(user["user_id"])
        queued = E.op_ingest_source(ctx, YOUTUBE)
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
        steps = {s["step"]: s for s in control_db.job_steps(control, queued.data["job_id"])}
        assert steps["summarise"]["state"] == "done"

    def test_an_invalid_value_is_refused_rather_than_dropped(self, cfg, user):
        """A switch that does not move, with no explanation, is worse than an
        error that says why."""
        ctx = E.build_context(user["user_id"], surface="web_ui")
        for field, value in (("theme_mode", "neon"), ("locale", "xx"), ("write_in", "klingon")):
            refused = E.op_update_profile(ctx, **{field: value})
            assert not refused.ok, field
            assert refused.error_reason == "invalid_value"

    def test_the_profile_read_returns_what_the_pipeline_reads(self, cfg, user, control):
        """Returned as the pipeline's own values, so the switches and the
        behaviour cannot drift apart."""
        control_db.update_profile(control, user["user_id"], want_rules=False, write_in="fr")
        data = E.op_profile(E.build_context(user["user_id"])).data
        assert data["ingestion"]["want_rules"] == 0
        assert data["ingestion"]["write_in"] == "fr"
        assert data["ingestion"] == control_db.ingestion_defaults(control, user["user_id"])

    def test_it_round_trips_over_http(self, cfg, user, client):
        headers = {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}
        response = client.post(
            "/api/profile",
            json={"display_name": "Expedit", "want_skills": False, "write_in": "en"},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["display_name"] == "Expedit"
        assert body["ingestion"]["want_skills"] == 0
        assert body["ingestion"]["write_in"] == "en"


class TestTwoDefectsThisBatchFound:
    def test_no_two_handlers_share_a_name(self):
        """`api_archive` was defined twice — once for the POST that archives an
        item and once for my GET that lists them. Python keeps the second, so
        the route bound to whichever came last: a GET answered by a handler
        demanding a CSRF token, 403, and a list that was never reachable.

        Not an error, not a warning — which is why it is a test.
        """
        import collections
        import re

        source = WEBAPI.read_text()
        names = re.findall(r"^async def (\w+)\(", source, re.M) + re.findall(
            r"^def (\w+)\(", source, re.M
        )
        duplicates = [n for n, count in collections.Counter(names).items() if count > 1]
        assert not duplicates, f"two handlers share a name: {duplicates}"

    def test_the_select_chevron_follows_the_theme(self):
        """A data URI cannot read a custom property, so the chevron's stroke
        is a literal — and the light one is invisible on a dark field. The one
        place in the system where a colour is written twice, because SVG
        cannot see CSS."""
        css = ATOMS_CSS.read_text()
        assert '%23343a40' in css          # ink, for light
        assert '%23ececf1' in css          # paper, for dark
        assert ':root[data-theme="dark"] .select' in css

    def test_the_identity_row_does_not_print_the_email_twice(self):
        """With no name set, the address IS the identity and the help line
        says nothing."""
        source = FRAMES_JS.read_text()
        assert 'esc(data.display_name ? data.email : "")' in source


class TestTheFrameSource:
    def test_the_type_filter_exists_on_archive_and_nowhere_else(self):
        """§7.12: "The filter exists here and nowhere else, because this is
        the only heterogeneous list in the app." """
        source = FRAMES_JS.read_text()
        assert source.count("filter-pill") >= 1
        archive = source[source.index("FRAMES.archive ="): source.index("FRAMES.profile =")]
        assert "filter-pill" in archive
        for other in ("FRAMES.sources", "FRAMES.transcripts", "FRAMES.rules"):
            start = source.index(other + " =")
            block = source[start : start + 3000]
            assert "filter-pill" not in block, other

    def test_filtering_hides_a_group_that_empties(self):
        """§6 rule 13: a section with zero items hides itself, title and all."""
        assert "group.hidden = !" in FRAMES_JS.read_text()

    def test_the_four_sections_of_7_13_are_all_there(self):
        source = FRAMES_JS.read_text()
        for title in (
            "Account",
            "Appearance",
            "What Brain does with new sources",
            "Your data",
        ):
            assert f'>{title}<' in source or f'"{title}"' in source or title in source

    def test_delete_is_a_danger_button_and_never_filled(self):
        """§7.13: the last two are `danger`, red-bordered. §3.8 keeps the
        filled red for a confirmed destructive act."""
        source = FRAMES_JS.read_text()
        profile = source[source.index("FRAMES.profile ="): source.index("FRAMES.signin =")]
        assert profile.count('data-variant="danger"') == 2
        assert 'data-variant="danger-solid"' not in profile
