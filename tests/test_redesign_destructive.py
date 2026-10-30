"""§7.12's Restore, and every path in §9.4's table.

Four defects, all of them the same shape: a control the design specifies, drawn
on the page, with nothing behind it.

1. **Archive had no `Restore`.** The page whose entire job is undoing an
   archive could not undo one. `R-9` puts it in the card header.

2. **The two vocabularies did not meet.** §7.12's list emits `source`, `rule`,
   `skill`, `summary` — the words on the page. `op_archive_item` accepted
   `source`, `claim`, `skill` — the words in §5's tool signature. So a rule
   could be archived (the frames send `claim` on the way in) and never
   restored, and a summary neither. The tool's `Literal` is a published
   contract and does not move; the engine maps both.

3. **`Delete permanently` had no endpoint that could act on it.** It resolved
   every id against `sources` and refused anything not already in Trash — a
   source-level concept — so on §7.12's cards it could only ever answer
   `not_in_trash`, or `not_found` for a rule, skill or summary.

4. **Nothing confirmed.** §9.4's dialog molecule was fully built in CSS and
   invoked by no frame, and `Delete library` and `Delete account` had neither
   a handler nor an endpoint.

The dialog is a frame's promise. The engine keeps its own: both account-level
deletions require the typed word on the way in, so neither is reachable by a
POST with an empty body.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
MOLECULES = Path(__file__).resolve().parents[1] / "app/static/molecules.css"


@pytest.fixture()
def set_aside(cfg, user):
    """One of each kind, archived — which is what the Archive lists."""
    web = E.build_context(user["user_id"], surface="web_ui")
    queued = E.op_ingest_source(web, YOUTUBE)
    E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)
    source_id = queued.data["source_id"]

    rule_id = E.op_remember_this(
        web, claim="A rule of my own.", when_it_applies="always"
    ).data["claim"]["id"]
    summary = tenant_db.get_summary(tenant_db.tenant(user["user_id"]), source_id=source_id)

    E.op_archive_item(web, "rule", rule_id)
    E.op_archive_item(web, "summary", summary["id"])
    return web, {"source": source_id, "rule": rule_id, "summary": summary["id"]}


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


def headers(client):
    return {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}


class TestTheTwoVocabulariesMeet:
    def test_the_word_on_the_page_reaches_the_engine(self, set_aside, user):
        """`rule` is what §7.12 renders; `claim` is what §5's tool declares.
        Both resolve to the same row, and `summary` resolves at all."""
        web, ids = set_aside
        kinds = {
            row["kind"] for row in tenant_db.list_archive(tenant_db.tenant(user["user_id"]))
        }
        assert {"rule", "summary"} <= kinds

        for kind in ("rule", "summary"):
            restored = E.op_archive_item(web, kind, ids[kind], restore=True)
            assert restored.ok, kind

    def test_a_rule_archived_as_a_claim_restores_as_a_rule(self, set_aside, user):
        """The asymmetry that made this a bug rather than a nit: the frames
        archive with one word and the Archive hands back the other."""
        web, ids = set_aside
        conn = tenant_db.tenant(user["user_id"])
        assert tenant_db.get_claim(conn, ids["rule"])["status"] == "archived"
        assert E.op_archive_item(web, "rule", ids["rule"], restore=True).ok
        assert tenant_db.get_claim(conn, ids["rule"])["status"] == "active"

    def test_the_tools_own_signature_is_unchanged(self):
        """§5's `archive_item` declares three types, and a published tool
        signature does not move to suit a frame. The engine maps the wider
        set; the tool keeps its `Literal`."""
        import inspect

        from app import mcp_server

        source = inspect.getsource(mcp_server)
        start = source.index("def archive_item")
        signature = source[start : start + 400]
        assert "'source', 'claim', 'skill'" in signature or (
            '"source", "claim", "skill"' in signature
        ), signature[:200]
        assert "summary" not in signature.split(")")[0]

    def test_an_unknown_type_is_still_refused(self, set_aside):
        web, ids = set_aside
        refused = E.op_archive_item(web, "transcript", ids["source"])
        assert not refused.ok and refused.error_reason == "bad_item_type"

    def test_the_endpoint_accepts_the_same_set(self, set_aside, client):
        _, ids = set_aside
        assert client.post(
            f"/api/archive/rule/{ids['rule']}", json={"restore": True}, headers=headers(client)
        ).status_code == 200
        assert client.post(
            f"/api/archive/summary/{ids['summary']}",
            json={"restore": True},
            headers=headers(client),
        ).status_code == 200
        bad = client.post(
            f"/api/archive/transcript/{ids['source']}", json={}, headers=headers(client)
        )
        assert bad.status_code == 400 and bad.json()["reason"] == "bad_item_type"


class TestDeletePermanently:
    def test_it_deletes_every_kind_the_archive_lists(self, set_aside, user):
        web, ids = set_aside
        conn = tenant_db.tenant(user["user_id"])

        assert E.op_permanent_delete(web, ids["rule"]).ok
        assert tenant_db.get_claim(conn, ids["rule"]) is None

        assert E.op_permanent_delete(web, ids["summary"]).ok
        assert tenant_db.get_summary(conn, summary_id=ids["summary"]) is None

    def test_the_kind_comes_from_the_id_so_the_frame_need_not_carry_it(self, set_aside):
        """§7.12 is one heterogeneous list; the ids are prefixed at creation,
        so `Delete permanently` already knows what it is deleting."""
        web, ids = set_aside
        assert E._ID_PREFIX[ids["rule"][:3]] == "claim"
        assert E._ID_PREFIX[ids["summary"][:3]] == "summary"
        assert E._ID_PREFIX[ids["source"][:3]] == "source"

    def test_something_still_active_is_refused(self, set_aside, user):
        """Archiving is the first gate. The dialog is the second."""
        web, ids = set_aside
        refused = E.op_permanent_delete(web, ids["source"])
        assert not refused.ok and refused.error_reason == "not_set_aside"
        assert tenant_db.get_source(tenant_db.tenant(user["user_id"]), ids["source"]) is not None

    def test_an_agent_is_refused_whatever_the_state(self, set_aside, user):
        web, ids = set_aside
        agent = E.build_context(user["user_id"], surface="mcp")
        refused = E.op_permanent_delete(agent, ids["rule"])
        assert not refused.ok and refused.error_reason == "person_only"
        assert tenant_db.get_claim(tenant_db.tenant(user["user_id"]), ids["rule"]) is not None

    def test_deleting_a_source_takes_what_came_out_of_it(self, set_aside, user):
        web, ids = set_aside
        conn = tenant_db.tenant(user["user_id"])
        E.op_archive_item(web, "source", ids["source"])
        result = E.op_permanent_delete(web, ids["source"])
        assert result.ok and result.data["deleted_claims"] >= 1
        assert tenant_db.get_source(conn, ids["source"]) is None
        assert tenant_db.get_source_content(conn, ids["source"]) is None

    def test_it_round_trips_over_http(self, set_aside, client):
        _, ids = set_aside
        response = client.post(
            f"/api/delete/{ids['rule']}", json={"item_type": "rule"}, headers=headers(client)
        )
        assert response.status_code == 200
        assert response.json()["item_type"] == "claim"


class TestDeleteTheLibrary:
    def test_it_empties_every_content_table_and_keeps_the_account(self, set_aside, user, control):
        web, _ = set_aside
        before = tenant_db.counts(web.tenant)
        assert before["claims"] >= 1

        result = E.op_delete_library(web, typed="DELETE")
        assert result.ok
        after = tenant_db.counts(web.tenant)
        assert after["claims"] == 0 and after["sources"] == 0
        assert not tenant_db.list_archive(web.tenant), "the archive outlived the library"

        # The account, its settings and its sessions are untouched.
        assert control_db.get_user(control, user["user_id"]) is not None

    def test_the_typed_word_is_required_by_the_engine_not_only_the_dialog(self, set_aside):
        """A dialog is a frame's promise, and a frame is one bug away from
        firing a POST with an empty body."""
        web, _ = set_aside
        for typed in ("", "delete", "DELETE ME", "yes"):
            refused = E.op_delete_library(web, typed=typed)
            assert not refused.ok and refused.error_reason == "not_confirmed", typed
        assert tenant_db.counts(web.tenant)["claims"] >= 1

    def test_an_agent_cannot_reach_it(self, set_aside, user):
        agent = E.build_context(user["user_id"], surface="mcp")
        refused = E.op_delete_library(agent, typed="DELETE")
        assert not refused.ok and refused.error_reason == "person_only"

    def test_it_round_trips_over_http(self, set_aside, client):
        empty = client.post("/api/delete-library", json={}, headers=headers(client))
        assert empty.status_code == 400 and empty.json()["reason"] == "not_confirmed"

        done = client.post("/api/delete-library", json={"typed": "DELETE"}, headers=headers(client))
        assert done.status_code == 200
        assert client.get("/api/sources").json()["sources"] == []


class TestDeleteTheAccount:
    def test_it_takes_the_tenant_file_the_sessions_and_the_row(self, set_aside, user, control, cfg):
        web, _ = set_aside
        path = cfg.tenant_db_path(user["user_id"])
        assert path.exists()

        result = E.op_delete_account(web, typed="DELETE")
        assert result.ok
        assert not path.exists(), "the tenant database outlived the account"
        assert control_db.get_user(control, user["user_id"]) is None
        assert control_db.list_sessions(control, user["user_id"]) == []

    def test_the_cascade_takes_the_jobs_and_their_steps(self, set_aside, user, control):
        """`job_steps` cascades from `ingestion_jobs`, which cascades from
        `users` — a two-hop cascade, which is why `PRAGMA foreign_keys = ON`
        is not optional."""
        web, _ = set_aside
        jobs = control_db.list_jobs(control, user["user_id"], limit=10)
        assert jobs
        job_id = jobs[0]["job_id"]
        assert control_db.job_steps(control, job_id)

        E.op_delete_account(web, typed="DELETE")
        assert control_db.list_jobs(control, user["user_id"], limit=10) == []
        assert control_db.job_steps(control, job_id) == []

    def test_the_audit_trail_survives_on_purpose(self, set_aside, user, control):
        """A deletion that erases its own record is not one the person can
        later show happened. The rows carry no content."""
        web, _ = set_aside
        E.op_delete_account(web, typed="DELETE")
        trail = control_db.recent_audit(control, user["user_id"], limit=50)
        assert [row for row in trail if row["action"] == "delete_account"]

    def test_the_typed_word_is_required(self, set_aside, cfg, user):
        web, _ = set_aside
        refused = E.op_delete_account(web, typed="")
        assert not refused.ok and refused.error_reason == "not_confirmed"
        assert cfg.tenant_db_path(user["user_id"]).exists()

    def test_an_agent_cannot_reach_it(self, set_aside, user):
        agent = E.build_context(user["user_id"], surface="mcp")
        refused = E.op_delete_account(agent, typed="DELETE")
        assert not refused.ok and refused.error_reason == "person_only"

    def test_the_session_it_was_called_with_is_cleared(self, set_aside, client):
        response = client.post(
            "/api/delete-account", json={"typed": "DELETE"}, headers=headers(client)
        )
        assert response.status_code == 200
        # The cookie that named the account is a cookie pointing at nothing.
        assert client.get("/api/sources").status_code in (401, 403)


class TestTheFrameSource:
    def test_restore_is_in_the_card_header_once(self):
        """`R-9`. The capture put a secondary `Restore` in the header AND a
        primary one in the body; §6 rule 2 forbids that."""
        source = FRAMES_JS.read_text()
        archive = source[source.index("FRAMES.archive ="): source.index("FRAMES.profile =")]
        assert "headHtml:" in archive
        assert "data-restore=" in archive.split("bodyHtml:")[0]
        assert "data-restore=" not in archive.split("bodyHtml:")[1].split("});")[0]

    def test_restore_gets_a_toast_and_no_dialog(self):
        """§9.4: "Restore | none, Undo toast". Undoing an undo is cheap, so
        the gate belongs after the act."""
        source = FRAMES_JS.read_text()
        archive = source[source.index("FRAMES.archive ="): source.index("FRAMES.profile =")]
        restore = archive[archive.index("[data-restore]"):archive.index("[data-delete]")]
        assert 'toast("Restored."' in restore
        assert 'action: "Undo"' in restore
        assert "confirmDialog" not in restore

    def test_every_destructive_control_goes_through_the_dialog(self):
        """The finding, as a rule: §6 rule 7 says destructive actions always
        confirm, and the dialog molecule was built and never invoked.

        Asserted as a property of each call site rather than as a count of
        them — a count is a number that goes stale the next time a frame
        gains a destructive control, which is exactly when this test should
        be checking something rather than failing.
        """
        source = FRAMES_JS.read_text()
        # Every handler that destroys or sets aside, by the name it is wired
        # under. Each must reach the dialog before it acts.
        for marker in ("[data-delete]", "[data-wipe]", "delete: async (id, button)"):
            block = source[source.index(marker):]
            block = block[: block.index("return node;")]
            assert "await confirmDialog({" in block, marker

        # And nothing calls the permanent door without having been through it.
        for match in re.finditer(r'post\(`/api/delete/', source):
            before = source[max(0, match.start() - 1200) : match.start()]
            assert "confirmDialog({" in before, "a delete with no dialog before it"

    def test_the_two_step_dialog_is_the_one_9_4_describes(self):
        source = FRAMES_JS.read_text()
        dialog = source[source.index("export function confirmDialog"):]
        dialog = dialog[: dialog.index("/** §6 rule 13")]
        # §9.4: "The confirm button is disabled until the field validates."
        assert "next.disabled = field.value.trim() !== word" in dialog
        # §18.8: step 1's Continue is OUTLINED danger — step 1 deletes nothing.
        assert 'data-variant="danger" data-act="next" disabled' in dialog
        assert 'data-variant="danger-solid" data-act="go" hidden disabled' in dialog
        # A Back control, an Escape and a scrim click all mean no.
        assert 'event.key === "Escape"' in dialog
        assert "event.target === scrim" in dialog

    def test_the_step_indicator_is_danger_and_not_accent(self):
        """§18.8: "the current step is red, not accent — the dialog is coloured
        by what it does"."""
        css = MOLECULES.read_text()
        current = css[css.index('.step-dot[data-state="current"]'):]
        current = current[: current.index("}")]
        assert "var(--danger)" in current
        assert "accent" not in current

    def test_the_caret_is_the_last_thing_in_the_row(self):
        """§18.6: "on the right, pointing right when closed and down when
        open". It was the first child, so it sat at the far left of every
        Rules, Skills and Archive card, against §6 rule 1."""
        source = FRAMES_JS.read_text()
        head = source[source.index("function expandable("):]
        head = head[: head.index("function wireExpanders")]
        assert head.index("item-text") < head.index("exp-caret")
        assert head.index("exp-actions") < head.index("exp-caret")
        # And the actions are a sibling of the toggle, not a child: a button
        # inside a button is invalid, and its click would also open the card.
        assert head.index("</button>") < head.index("exp-actions")

    def test_hidden_actually_hides(self):
        """A browser found this one, and it was latent in eleven atoms.

        `hidden` is a statement of fact, and any explicit `display` silently
        beats it. Nearly every atom sets one, so `.btn[hidden]` did nothing:
        at step 1 of §9.4's dialog both `Continue` and the final
        `Delete account` were on screen, the second merely disabled.

        `.menu[hidden]`, `.scrim[hidden]` and `.toast[hidden]` were each
        re-asserting it by hand, which is the tell that it belonged in the
        base block once.
        """
        atoms = (Path(__file__).resolve().parents[1] / "app/static/atoms.css").read_text()
        assert "[hidden] { display: none !important; }" in atoms
        # Declared before the first atom that sets a display, or it loses.
        assert atoms.index("[hidden] { display: none") < atoms.index(".btn {")

    def test_a_collapsed_card_shows_the_caret_alone(self):
        """§18.8, with §7.9's one exception: a `Review` button is the entry
        point to the queue, so it is visible before the card is opened."""
        css = MOLECULES.read_text()
        assert '.exp-card[data-open="false"] .exp-actions { display: none; }' in css
        assert ':has([data-act="review"])' in css
