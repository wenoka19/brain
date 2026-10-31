"""§7.8's and §7.9's actions, and §7.1's two create links.

What the conformance review found here was the same shape as everywhere else,
with one difference: on these two frames the backend was already complete.

* §7.9's `⋯` had no handler, so `Activate`, `Deactivate`, `Export .md` and
  `Delete` were all unreachable — and `POST /api/skills/:id/state`, which the
  backend had served since the skill queue was built, was called by nothing in
  the web app. A skill could never enter or leave the `Inactive` section the
  frame draws.
* §7.9's `Review` button did not exist. §7.9 calls it the queue's entry point
  and says why: "a state the person cannot act on is a dead end." Approve,
  Edit and Dismiss appeared only once the person had worked out that the card
  expands.
* §7.8's `Edit` and `Archive` were plain buttons with no data attribute and no
  handler, although `POST /api/claim/:id` and `POST /api/archive/claim/:id`
  both existed.
* `?create=1` was a parameter no frame read, so §7.1's `+ Write a rule` and
  `+ Add a skill` opened the list the person was already looking at. Writing a
  rule from the web had no endpoint at all — `remember_this` was MCP-only.

`R-14` is recorded here too, because it is a conflict rather than a defect:
§7.9 lists `Delete (confirms)` in a live skill's menu and §6 rule 8 says "only
the person deletes, and only from Archive or Profile". Rule 8 wins — §2's
30-day recovery buffer is a security property, not a convenience — so the menu
item keeps its name, sets the skill aside, and the dialog says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import tenant as tenant_db

FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
MOLECULES = Path(__file__).resolve().parents[1] / "app/static/molecules.css"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


def headers(client):
    return {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}


class StepfulExtractor:
    """Yields a claim with `exact_steps`, which is what makes the pipeline
    draft a skill — without one, every draft-dependent test here would skip,
    and a skipped test proves nothing."""

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
def saved(cfg, user):
    from app.ingestion import providers as prov

    bundle = prov.build_providers(cfg)
    bundle.extractor = StepfulExtractor()
    prov.set_providers(bundle)
    ctx = E.build_context(user["user_id"], surface="web_ui")
    E.op_ingest_source(ctx, YOUTUBE)
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
    skill = E.op_save_skill(
        ctx,
        slug="weekly-review",
        name="Run a weekly review",
        description="The Friday pass over the week's commitments.",
        trigger_when="Every Friday afternoon",
        execution_steps=["Read the notes.", "Close what is done.", "Move the rest."],
    )
    rule = E.op_remember_this(
        ctx, claim="Never use em dashes.", when_it_applies="always"
    )
    return ctx, skill.data["skill"]["id"], rule.data["claim"]["id"]


class TestActivationLivesInTheMenu:
    """§7.9: "There is no toggle switch on a skill card. Activation lives in
    the menu." The endpoint existed; nothing called it."""

    def test_a_skill_can_leave_and_re_enter_the_inactive_section(self, saved, client):
        _, skill_id, _ = saved
        ids = lambda key: {s["id"] for s in client.get("/api/skills").json()[key]}
        assert skill_id in ids("active")

        off = client.post(
            f"/api/skills/{skill_id}/state", json={"state": "inactive"}, headers=headers(client)
        )
        assert off.status_code == 200
        assert skill_id not in ids("active")
        assert skill_id in ids("inactive")

        on = client.post(
            f"/api/skills/{skill_id}/state", json={"state": "active"}, headers=headers(client)
        )
        assert on.status_code == 200
        assert skill_id in ids("active")

    def test_changing_your_mind_is_not_a_runaway_agent(self, saved, client):
        """The loop breaker refuses the same call repeated inside 15 seconds.
        Its fingerprint for this endpoint was the skill id alone, so switching
        a skill off and straight back on — two different calls — read as one
        call made twice, and the second was refused.

        A person toggling a switch is exactly who the guard must not catch.
        Going back a *third* time repeats the first call and is caught, which
        is the guard doing its job — §2.5 defines it as identical repeated
        calls inside the window, and that one is identical.
        """
        _, skill_id, _ = saved
        for state in ("inactive", "active"):
            response = client.post(
                f"/api/skills/{skill_id}/state", json={"state": state}, headers=headers(client)
            )
            assert response.status_code == 200, state

    def test_the_guard_still_catches_the_same_call_twice(self, saved, client):
        """The other half: narrowing the fingerprint must not disarm it."""
        _, skill_id, _ = saved
        first = client.post(
            f"/api/skills/{skill_id}/state", json={"state": "inactive"}, headers=headers(client)
        )
        second = client.post(
            f"/api/skills/{skill_id}/state", json={"state": "inactive"}, headers=headers(client)
        )
        assert first.status_code == 200
        assert second.status_code == 429

    def test_an_inactive_skill_is_out_of_an_agents_reach(self, saved, user):
        """Which is what §7.9's section note promises: "Kept, but your AI
        cannot reach them"."""
        ctx, skill_id, _ = saved
        E.op_set_skill_state(ctx, skill_id, "inactive")
        agent = E.build_context(user["user_id"], surface="mcp")
        assert not E.op_fetch(agent, skill_id).ok

    def test_a_draft_cannot_be_activated_without_being_reviewed(self, saved, user):
        ctx, _, _ = saved
        draft = [s for s in tenant_db.list_skills(ctx.tenant) if s["status"] == "proposed"]
        assert draft, "the fixture drafted no skill, so this proves nothing"
        refused = E.op_set_skill_state(ctx, draft[0]["id"], "active")
        assert not refused.ok and refused.error_reason == "needs_review_first"


class TestWritingFromTheWeb:
    """§7.1's `+ Write a rule` had no endpoint behind it — `remember_this` was
    MCP-only, so the one thing §7.8's toolbar offers could not be done."""

    def test_a_rule_written_from_the_web_lands_in_my_rules(self, client):
        response = client.post(
            "/api/rule",
            json={"claim": "Answer in the language I wrote in.", "when_it_applies": "always"},
            headers=headers(client),
        )
        assert response.status_code == 200
        groups = client.get("/api/rules").json()
        assert len(groups["mine"]) == 1
        assert groups["mine"][0]["claim"].startswith("Answer in the language")

    def test_an_empty_rule_is_refused_rather_than_stored(self, client):
        refused = client.post(
            "/api/rule", json={"claim": "   ", "when_it_applies": "always"}, headers=headers(client)
        )
        assert refused.status_code == 400

    def test_a_skill_written_from_the_web_arrives_active(self, client):
        """Its author is the person, so there is nothing to review."""
        response = client.post(
            "/api/skill",
            json={
                "slug": "write-a-cold-open",
                "name": "Write a cold open",
                "description": "The first two lines.",
                "trigger_when": "When drafting outbound",
                "execution_steps": ["Name the trigger.", "Say the number."],
            },
            headers=headers(client),
        )
        assert response.status_code == 200
        active = client.get("/api/skills").json()["active"]
        assert "write-a-cold-open" in {skill["slug"] for skill in active}


class TestEditingARule:
    def test_the_short_form_and_its_scope_both_move(self, saved, client):
        """§7.8: "Always store both the short form and the full form." """
        _, _, rule_id = saved
        response = client.post(
            f"/api/claim/{rule_id}",
            json={"claim": "Never use em dashes, ever.", "when_it_applies": "every draft"},
            headers=headers(client),
        )
        assert response.status_code == 200
        mine = client.get("/api/rules").json()["mine"][0]
        assert mine["claim"] == "Never use em dashes, ever."
        assert mine["when_it_applies"] == "every draft"

    def test_archiving_one_takes_it_off_the_page(self, saved, client):
        _, _, rule_id = saved
        before = len(client.get("/api/rules").json()["mine"])
        assert client.post(
            f"/api/archive/claim/{rule_id}", json={}, headers=headers(client)
        ).status_code == 200
        assert len(client.get("/api/rules").json()["mine"]) == before - 1

    def test_and_undo_puts_it_back(self, saved, client):
        """§9.4 gives archiving an Undo toast and no dialog."""
        _, _, rule_id = saved
        client.post(f"/api/archive/claim/{rule_id}", json={}, headers=headers(client))
        assert client.post(
            f"/api/archive/claim/{rule_id}", json={"restore": True}, headers=headers(client)
        ).status_code == 200
        assert len(client.get("/api/rules").json()["mine"]) == 1


class TestProvenanceReachesTheCard:
    def test_a_skills_source_title_comes_with_it(self, saved, user):
        """§7.9's expanded body ends with provenance, and a drafted skill's
        provenance is the source it came from. Joined in the list query, so
        drawing the page costs one read rather than one per card."""
        ctx, _, _ = saved
        drafts = [s for s in tenant_db.list_skills(ctx.tenant) if s["source_id"]]
        assert drafts, "the fixture drafted no skill, so this proves nothing"
        assert drafts[0]["source_title"]

    def test_a_hand_written_skill_has_no_source_and_says_nothing(self, saved, user):
        ctx, skill_id, _ = saved
        skill = next(s for s in tenant_db.list_skills(ctx.tenant) if s["id"] == skill_id)
        assert not skill["source_id"]
        assert skill["source_title"] is None


class TestTheFrameSource:
    @staticmethod
    def skills_frame() -> str:
        source = FRAMES_JS.read_text()
        return source[source.index("FRAMES.skills =") : source.index("async function setSkillState")]

    def test_a_proposed_card_carries_a_review_button_not_a_pill(self):
        """§7.9: "Not a pill. A state the person cannot act on is a dead end."
        And it is visible on the COLLAPSED card — a review button the person
        has to expand the card to find is the same dead end."""
        frame = self.skills_frame()
        assert 'act: "review"' in frame
        assert 'label: "Review"' in frame
        css = MOLECULES.read_text()
        assert '.exp-card[data-open="false"] .exp-actions:has([data-act="review"])' in css

    def test_an_approved_card_carries_the_menu_alone(self):
        """`R-7`. The capture shows a pencil and a `⋯`; §7.9 also lists `Edit`
        in the menu, and §6 rule 2 forbids one action in two places."""
        frame = self.skills_frame()
        head = frame[frame.index("headHtml:") : frame.index("bodyHtml:")]
        approved = head.split("cardMenu(")[1]
        assert "cardButton(" not in approved
        # And `Edit` is the first item in that menu.
        assert approved.index('"Edit"') < approved.index('"Export .md"')

    def test_the_menu_carries_exactly_what_7_9_lists(self):
        frame = self.skills_frame()
        head = frame[frame.index("headHtml:") : frame.index("bodyHtml:")]
        for label in ("Edit", "Deactivate", "Activate", "Export .md", "Delete"):
            assert f'"{label}"' in head, label
        # §4.7: "a separator precedes destructive items."
        assert head.index("{ sep: true }") < head.index('"Delete"')

    def test_delete_sets_the_skill_aside_and_says_so(self):
        """`R-14`. The dialog names where it goes, because a control called
        `Delete` that archives would otherwise be a lie."""
        frame = self.skills_frame()
        block = frame[frame.index("delete: async") :]
        assert "confirmDialog({" in block
        assert "moves to your Archive" in block
        assert 'archiveRow("skill"' in block
        # Never the permanent door: §6 rule 8 keeps that in Archive/Profile.
        assert "/api/delete/" not in block

    def test_export_is_built_from_what_is_already_on_screen(self):
        """A round trip to fetch what the frame is rendering is a round trip
        for nothing."""
        frame = self.skills_frame()
        assert "skillMarkdown(skill)" in frame
        markdown = FRAMES_JS.read_text()
        body = markdown[markdown.index("function skillMarkdown") :]
        body = body[: body.index("\n}")]
        # §7.9: the trigger comes first, "without which a model never reaches
        # for the skill".
        assert body.index("When to use it") < body.index("What it does")

    def test_the_create_panel_opens_only_when_the_url_asks(self):
        source = FRAMES_JS.read_text()
        assert 'new URLSearchParams(window.location.search).get("create") !== "1"' in source
        for kind in ("rule", "skill"):
            assert f'openCreatePanel(node, "{kind}")' in source

    def test_both_create_links_have_a_panel_to_open(self):
        """They were the only two links in the app that went nowhere."""
        source = FRAMES_JS.read_text()
        assert '/rules?create=1' in source and '/skills?create=1' in source
        assert "CREATE_PANEL = {" in source
        panel = source[source.index("CREATE_PANEL = {") : source.index("/** Open the panel")]
        assert '"/api/rule"' in panel and '"/api/skill"' in panel

    def test_every_action_a_frame_names_has_a_handler(self):
        """The whole class of defect, as one rule: a `data-act` with no entry
        in any handler map is a control that renders and does nothing."""
        source = FRAMES_JS.read_text()
        declared = set(re.findall(r'(?:act|data-act)[:=]\s*"([a-z]+)"', source))
        declared |= set(re.findall(r'data-act="([a-z]+)"', source))
        handled = set(re.findall(r"^    (\w+): (?:async )?\(", source, re.M))
        handled |= set(re.findall(r"^    (\w+): async \(", source, re.M))
        # `R-20`: a handler map is not the only way to wire an act. A control
        # that belongs to the frame rather than to a card is wired straight by
        # selector — `wirePageFoot` reaches for `[data-act="more"]` itself.
        # Crediting the selector keeps the rule's teeth: an act that nothing
        # maps AND nothing queries still fails here.
        handled |= set(re.findall(r'\[data-act="([a-z]+)"\]', source))
        missing = declared - handled - {"cancel", "next", "go"}  # the dialog's own three
        assert not missing, f"rendered with no handler: {sorted(missing)}"
