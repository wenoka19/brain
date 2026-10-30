"""§7.10 Activity and §7.11 Connections — the last two rows of §9.2.

Both were approved as backend work the design asks for, and both were
genuinely missing rather than merely unwired.

**§7.10** needed four columns on `audit_logs`: the `180 ms`, the `1.2 kB`, the
`3 rules returned`, and which connection made the call. They are written at
`_run_tool`, the one gate every tool call passes through, because that is the
only place that knows all of it — the arguments going in, how long it took,
and how much came back. The operations keep writing their own rows (§2's
internal trail); a row with a `connection_id` is a *call*, and that is what
§7.10's log lists.

**§7.11** needed a credential that is not a session. "One server. Two ways to
authenticate. This is verified behaviour, not a choice": a chat app's
connector dialog has no header field, so the key rides in the URL; an editor
or an agent framework cannot run a browser sign-in, so it sends a bearer.
§7.11 requires both, and requires them to meet in one validation function.

Three security properties are the point of this file:

1. **The key is never stored.** Only its SHA-256 digest (§2 rule 4), so a
   stolen `control.db` yields nothing that works.
2. **The key never reaches a log.** `UrlKeyMiddleware` strips it from the ASGI
   scope, but that cannot reach uvicorn's access line, which is written from
   the raw request at the protocol layer *before* any middleware runs. A
   logging filter redacts it instead.
3. **One credential per connection.** Revoking one leaves the others working,
   and rolling one invalidates only its own key.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import auth
from app.core import engine as E
from app.db import control as control_db

MAIN_PY = Path(__file__).resolve().parents[1] / "app/main.py"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture()
def client(cfg, user):
    from app.main import create_app

    client = TestClient(create_app())
    client.post("/api/login", json={"email": "founder@example.com"})
    return client


def headers(client):
    return {"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")}


@pytest.fixture()
def web(cfg, user):
    return E.build_context(user["user_id"], surface="web_ui")


class TestTheKeyIsACredentialAndNotASession:
    def test_only_the_digest_is_stored(self, web, cfg, control):
        """§2 rule 4. The plaintext exists in the response that created it and
        nowhere else — so there is no second chance to read it, and a stolen
        database yields no working key."""
        key = E.op_create_connection(web, "Claude Desktop").data["key"]
        raw = (cfg.data_dir / "control.db").read_bytes()
        assert key.encode() not in raw

        row = control_db.list_connections(control, web.user_id)[0]
        assert row["key_hash"] == control_db.hash_token(key)
        assert row["key_hash"] != key
        # The prefix is kept so the list can say which key a row is without
        # being able to reconstruct it.
        assert row["key_prefix"] and row["key_prefix"] in key
        assert len(row["key_prefix"]) < len(key)

    def test_it_verifies_through_the_one_function(self, web):
        """§7.11: "two entry points, one validation function"."""
        key = E.op_create_connection(web, "Cursor").data["key"]
        verified = auth.verify_credential(key)
        assert verified.user_id == web.user_id
        assert verified.client_name == "Cursor"
        assert verified.connection_id

    def test_a_jwt_still_goes_down_the_jwt_path(self, web, cfg, control):
        """The prefix is what decides, so a key is never put through JWT
        decoding — where it would fail with a misleading `invalid_token` — and
        a JWT is never looked up as a key."""
        grant = auth.issue_grant(
            control,
            user_id=web.user_id,
            client_name="Claude",
            client_id="c",
            scope="brain.read",
            resource=auth.resource_identifier(web.cfg),
        )
        verified = auth.verify_credential(grant["access_token"])
        assert verified.user_id == web.user_id
        assert verified.connection_id == verified.session_id

    def test_a_made_up_key_is_refused(self, web):
        with pytest.raises(auth.TokenRejected) as refusal:
            auth.verify_credential("sk-brain-nothinglikethisexists")
        assert refusal.value.reason == "unknown_key"

    def test_revoking_stops_it_at_once(self, web):
        key = E.op_create_connection(web, "ChatGPT").data["key"]
        connection_id = auth.verify_credential(key).connection_id
        assert E.op_revoke_connection(web, connection_id).ok
        with pytest.raises(auth.TokenRejected) as refusal:
            auth.verify_credential(key)
        assert refusal.value.reason == "revoked"

    def test_revoking_one_leaves_the_others_working(self, web):
        """§7.11: "One credential per connection." """
        first = E.op_create_connection(web, "Claude").data
        second = E.op_create_connection(web, "Cursor").data
        E.op_revoke_connection(web, first["connection"]["id"])
        assert auth.verify_credential(second["key"]).user_id == web.user_id

    def test_rolling_replaces_the_key_and_keeps_the_connection(self, web):
        """§7.11's `Roll the key`. The connection keeps its name and its
        history, so §7.10's log reads continuously across the roll — which is
        why this is not "revoke and make a new one"."""
        made = E.op_create_connection(web, "Claude Code").data
        connection_id = made["connection"]["id"]
        rolled = E.op_roll_connection_key(web, connection_id)
        assert rolled.ok

        with pytest.raises(auth.TokenRejected):
            auth.verify_credential(made["key"])
        verified = auth.verify_credential(rolled.data["key"])
        assert verified.connection_id == connection_id
        assert verified.client_name == "Claude Code"

    def test_a_key_cannot_reach_another_persons_connection(self, cfg, user, control):
        """The one place a connection id arrives from input. §2's "tenant
        paths are resolved strictly from the verified `user_id`" is the same
        rule, and this is its equivalent here."""
        mine = E.build_context(user["user_id"], surface="web_ui")
        theirs_user = control_db.ensure_user(control, "someone-else@example.com")
        theirs = E.build_context(theirs_user["user_id"], surface="web_ui")
        target = E.op_create_connection(theirs, "Their Claude").data["connection"]["id"]

        for attempt in (
            E.op_revoke_connection(mine, target),
            E.op_rename_connection(mine, target, "mine now"),
            E.op_roll_connection_key(mine, target),
        ):
            assert not attempt.ok and attempt.error_reason == "not_found"
        # And theirs still works.
        assert not control_db.get_connection(control, target)["revoked"]

    def test_an_agent_cannot_mint_or_list_keys(self, cfg, user, web):
        agent = E.build_context(user["user_id"], surface="mcp")
        for refused in (
            E.op_create_connection(agent),
            E.op_list_connections(agent),
            E.op_revoke_connection(agent, "con_x"),
            E.op_activity(agent),
        ):
            assert not refused.ok and refused.error_reason == "person_only"


class TestTheKeyNeverReachesALog:
    def test_the_middleware_strips_it_from_the_scope(self):
        """Access logs, error reports and `request.url` all read the scope's
        query string."""
        source = MAIN_PY.read_text()
        middleware = source[source.index("class UrlKeyMiddleware") :]
        middleware = middleware[: middleware.index("def _has_authorization")]
        assert 'scope["query_string"] = urlencode(' in middleware
        assert 'if name != "key"' in middleware

    def test_a_real_authorization_header_wins(self):
        """A client that sends one is not guessing, and preferring a query
        parameter over it would let a pasted stale link override a correctly
        configured header."""
        source = MAIN_PY.read_text()
        assert "not _has_authorization(scope)" in source

    def test_the_access_line_is_redacted(self):
        """The scope rewrite cannot reach this one: uvicorn writes its line
        from the raw request at the protocol layer, before any ASGI
        middleware runs."""
        from app.main import RedactKeyFilter

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1,
            '%s - "%s %s HTTP/%s" %d', ("1.2.3.4", "POST", "/mcp?key=sk-brain-SECRETVALUE", "1.1", 200),
            None,
        )
        assert RedactKeyFilter().filter(record)
        rendered = record.getMessage()
        assert "sk-brain-SECRETVALUE" not in rendered
        # Still useful as a log: you can see a key was used, and §7.10 says
        # which connection used it.
        assert "key=[redacted]" in rendered

    def test_it_survives_a_message_with_no_args(self):
        from app.main import RedactKeyFilter

        record = logging.LogRecord(
            "uvicorn.error", logging.WARNING, __file__, 1,
            "failed to proxy /mcp?key=sk-brain-SECRETVALUE", None, None,
        )
        RedactKeyFilter().filter(record)
        assert "SECRETVALUE" not in record.getMessage()

    def test_it_is_installed_before_the_app_can_serve(self, cfg):
        from app.main import RedactKeyFilter, create_app

        create_app()
        access = logging.getLogger("uvicorn.access")
        assert any(isinstance(f, RedactKeyFilter) for f in access.filters)

    def test_installing_it_twice_does_not_stack_filters(self, cfg):
        from app.main import RedactKeyFilter, install_key_redaction

        for _ in range(3):
            install_key_redaction()
        access = logging.getLogger("uvicorn.access")
        assert sum(isinstance(f, RedactKeyFilter) for f in access.filters) == 1


class TestTheActivityLogIsALogOfCalls:
    def test_an_operations_own_audit_row_is_not_a_call(self, web, control):
        """§2 logs every operation; §7.10 lists calls. Listing both would make
        one `search` read as five lines, which is the opposite of a log you
        can scan."""
        E.op_search(web, "anything")
        assert control_db.recent_audit(control, web.user_id, limit=20)
        assert control_db.recent_calls(control, web.user_id) == []

    def test_a_call_carries_everything_7_10_shows(self, web, control):
        """Collapsed: time · connection · tool · target · latency · status.
        Expanded: `Asked for`, `Returned`, `Payload`, `Connection`."""
        control_db.audit(
            control,
            user_id=web.user_id,
            action="search",
            status="ok",
            surface="mcp",
            client_name="Claude Desktop",
            connection_id="con_abc",
            latency_ms=180,
            payload_bytes=1247,
            result_summary="3 rules returned",
            detail={"query": "planning cycle"},
        )
        row = E.op_activity(web).data["calls"][0]
        assert row["connection"] == "Claude Desktop"
        assert row["connection_id"] == "con_abc"
        assert row["tool"] == "search"
        assert row["latency_ms"] == 180
        assert row["payload_bytes"] == 1247
        assert row["returned"] == "3 rules returned"
        assert "planning cycle" in row["asked_for"]
        assert row["ok"] is True

    def test_a_refused_call_has_no_duration_rather_than_zero(self, web, control):
        """`0 ms` would read as "instant" on a page whose whole job is being
        readable. A call that never ran has no duration."""
        control_db.audit(
            control,
            user_id=web.user_id,
            action="search",
            status="token_expired",
            surface="mcp",
            client_name="ChatGPT",
            connection_id="con_abc",
        )
        row = E.op_activity(web).data["calls"][0]
        assert row["latency_ms"] is None
        assert row["payload_bytes"] is None
        assert row["ok"] is False
        # §7.10: "A failure says what to do."
        assert row["remedy"] == "Reconnect this app to resume."

    def test_every_failure_status_the_gate_writes_has_a_remedy(self):
        """A status with no sentence falls through to an empty one, which is
        the silent failure §7.10 exists to replace."""
        import inspect

        from app import mcp_server

        gate = inspect.getsource(mcp_server._run_tool)
        written = {"unknown_user", "error"}
        for reason in written:
            assert reason in E._REMEDY_FOR, reason
        assert "log(decision.reason" in gate  # the limiter's own reasons
        from app.security import ratelimit

        for reason in ("rate_limited", "loop_detected"):
            assert reason in E._REMEDY_FOR, reason

    def test_the_meters_read_the_numbers_the_product_enforces(self, web):
        """§7.10's capture shows `Calls 1,284 of 5,000` and `2.1 GB of 10 GB`;
        neither cap exists here. A meter against a number nothing checks is
        decoration, so every meter the page draws has a real ceiling behind
        it."""
        usage = E.op_activity(web).data["usage"]
        assert usage["sources"]["cap"] == web.cfg.quota_files
        assert usage["minutes"]["cap"] == web.cfg.quota_media_minutes
        assert usage["calls"]["cap"] == web.cfg.tool_calls_per_min
        for key, meter in usage.items():
            assert meter["cap"] is not None, f"{key} draws a meter with no ceiling"

    def test_the_strip_draws_no_storage_card(self):
        """`R-17`. The founder struck storage as a lever — "in the usage screen
        we shouldn't show the storage size" — so the frame does not ask for
        it. The engine side of the same ruling is in
        `test_redesign_retention.py`, with the sweep that makes it honest."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        activity = frames[frames.index("FRAMES.activity =") :]
        meters = activity[activity.index("const metersHtml") : activity.index(".join(")]
        assert "storage" not in meters

    def test_the_rate_meter_is_counted_from_the_log_not_the_limiter(self, web, control):
        """The limiter is per-process memory; the page has to be right behind
        more than one uvicorn worker."""
        for _ in range(3):
            control_db.audit(
                control,
                user_id=web.user_id,
                action="search",
                status="ok",
                surface="mcp",
                connection_id="con_abc",
            )
        assert E.op_activity(web).data["usage"]["calls"]["used"] == 3


class TestTheConnectionsList:
    def test_a_session_and_a_key_are_both_connections(self, web, control):
        """§7.11: "name · `Signed in` or `Key`". One list, because two
        sections would leave the person reconciling them."""
        E.op_create_connection(web, "Cursor")
        auth.issue_grant(
            control,
            user_id=web.user_id,
            client_name="Claude",
            client_id="c",
            scope="brain.read",
            resource=auth.resource_identifier(web.cfg),
        )
        items = E.op_list_connections(web).data["connections"]
        assert {item["how"] for item in items} == {"Key", "Signed in"}
        assert {item["name"] for item in items} == {"Cursor", "Claude"}

    def test_the_connected_section_does_not_render_while_empty(self, web):
        """§7.11: that sentence about revocation "means nothing before there
        is anything to revoke"."""
        assert E.op_list_connections(web).data["connected"] is False
        E.op_create_connection(web, "Claude")
        assert E.op_list_connections(web).data["connected"] is True

    def test_a_revoked_connection_still_shows_but_does_not_count(self, web):
        """The row stays, because §7.10's log still refers to it."""
        made = E.op_create_connection(web, "Claude").data["connection"]["id"]
        E.op_revoke_connection(web, made)
        data = E.op_list_connections(web).data
        assert data["connected"] is False
        assert [item for item in data["connections"] if item["revoked"]]

    def test_the_last_call_column_is_written_by_a_real_call(self, web):
        """§7.11's `last call …`, and §7.10's reason for existing: "revoking a
        key is only meaningful once you can see what it did"."""
        made = E.op_create_connection(web, "Claude").data
        assert E.op_list_connections(web).data["connections"][0]["last_call_at"] is None
        auth.verify_credential(made["key"])
        assert E.op_list_connections(web).data["connections"][0]["last_call_at"]

    def test_the_link_is_the_whole_setup(self, web):
        """§7.11: "One link, pasted once, nothing else to copy." """
        data = E.op_create_connection(web, "Claude").data
        assert data["link"].endswith(data["key"])
        assert "/mcp?key=" in data["link"]

    def test_renaming_needs_a_name(self, web):
        made = E.op_create_connection(web, "Claude").data["connection"]["id"]
        refused = E.op_rename_connection(web, made, "   ")
        assert not refused.ok and refused.error_reason == "empty_name"


class TestOverHttp:
    def test_the_two_reads_and_the_three_writes(self, client):
        assert client.get("/api/connections").json()["connected"] is False
        made = client.post(
            "/api/connections", json={"name": "Claude Desktop"}, headers=headers(client)
        )
        assert made.status_code == 200
        connection_id = made.json()["connection"]["id"]

        assert client.get("/api/activity").status_code == 200
        assert client.post(
            f"/api/connections/{connection_id}/rename",
            json={"name": "Claude"},
            headers=headers(client),
        ).status_code == 200
        rolled = client.post(
            f"/api/connections/{connection_id}/roll", json={}, headers=headers(client)
        )
        assert rolled.status_code == 200 and rolled.json()["key"]
        assert client.post(
            f"/api/connections/{connection_id}/revoke", json={}, headers=headers(client)
        ).status_code == 200

    def test_an_unknown_action_is_refused(self, client):
        made = client.post("/api/connections", json={}, headers=headers(client)).json()
        bad = client.post(
            f"/api/connections/{made['connection']['id']}/destroy",
            json={},
            headers=headers(client),
        )
        assert bad.status_code == 400 and bad.json()["reason"] == "bad_action"

    def test_minting_a_key_needs_the_csrf_token(self, client):
        """A forged request that can read nothing can still ride the cookie —
        and this one would hand the attacker a working credential."""
        assert client.post("/api/connections", json={}).status_code == 403


class TestTheTwoFrames:
    """What the static side can check about §7.10's and §7.11's markup.

    The behavioural half is `tests/browser/conn_probe.js` and
    `tests/browser/log_probe.js` — including the one defect only a render
    showed: `.meter` is a `<span>` and the atom set no `display`, so every
    meter rendered as a tall thin bar down its card. The atom had been
    written before anything used it, which is how it shipped unnoticed.
    """

    @staticmethod
    def frames() -> str:
        return (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()

    @staticmethod
    def css(name: str) -> str:
        return (Path(__file__).resolve().parents[1] / f"app/static/{name}").read_text()

    def test_the_meter_is_a_block(self):
        """An inline box ignores `width` and `height`. The same trap
        `.v-rule` fell into, and the second time it has cost a render."""
        atoms = self.css("atoms.css")
        block = atoms[atoms.index(".meter {") : atoms.index("}", atoms.index(".meter {"))]
        assert "display: block" in block
        assert "height: var(--meter-h)" in block

    def test_the_strip_has_a_column_per_meter(self):
        """`R-17` took the row from four meters to three and the grid still
        said `repeat(4, 1fr)`, so a quarter of the row sat empty on the right.
        Only a render showed it. The count is pinned to the frame's own list
        so the two cannot drift again."""
        import re

        frames = self.frames()
        activity = frames[frames.index("FRAMES.activity =") :]
        listed = re.search(r"const metersHtml = \[(.*?)\]", activity, re.S)
        meters = len(re.findall(r'"', listed.group(1))) // 2
        assert meters == 3, "the frame's own list of meters"

        css = self.css("frames.css")
        row = css[css.index(".meter-row {") : css.index("}", css.index(".meter-row {"))]
        assert f"repeat({meters}, 1fr)" in row, f"{meters} meters, grid says: {row.strip()}"

    def test_a_meter_with_no_cap_draws_no_bar(self):
        """No meter the page draws today is capless, but a figure can outlive
        its limit — and then the card should degrade to an honest number
        rather than a bar against something nothing checks."""
        frames = self.frames()
        meter = frames[frames.index("function meterCard(") : frames.index("FRAMES.activity =")]
        assert "cap === null || cap === undefined" in meter
        assert "No limit on this one." in meter

    def test_the_log_row_is_the_one_7_10_draws(self):
        """time · connection · tool · target · latency · status, and the tool
        in `--font-mono` on `--bg-canvas`."""
        frames = self.frames()
        row = frames[frames.index("FRAMES.activity =") : frames.index("function latencyLabel(")]
        for part in ("call-time", "call-conn", "call-tool", "call-target", "call-latency"):
            assert part in row, part
        css = self.css("frames.css")
        tool = css[css.index(".call-tool {") : css.index("}", css.index(".call-tool {"))]
        assert "var(--font-mono)" in tool and "var(--bg-canvas)" in tool

    def test_the_row_is_forty_pixels(self):
        """§7.10: "denser than an item card because a log is scanned, not
        browsed"."""
        css = self.css("frames.css")
        head = css[css.index(".call-head {") : css.index("}", css.index(".call-head {"))]
        assert "min-height: 40px" in head

    def test_the_expansion_carries_the_four_things(self):
        frames = self.frames()
        row = frames[frames.index("FRAMES.activity =") : frames.index("function latencyLabel(")]
        for part in ("Asked for", "Returned", "call.payload_bytes", "call.connection_id"):
            assert part in row, part
        # §7.10: "The connection column is the link to §7.11."
        assert 'href="/connections"' in row

    def test_a_latency_of_zero_is_not_printed_as_zero(self):
        """`0 ms` claims a precision the measurement does not have, and `—`
        means something different: the call never ran."""
        frames = self.frames()
        label = frames[frames.index("function latencyLabel(") :]
        label = label[: label.index("\n}")]
        assert 'return "—"' in label
        assert '"<1 ms"' in label

    def test_the_clock_is_twenty_four_hour(self):
        """§7.10's rows read `14:02`, and the column is 56px — `02:37 AM`
        does not fit it."""
        assert 'hourCycle: "h23"' in self.frames()

    def test_the_empty_connections_card_is_the_one_7_11_writes(self):
        frames = self.frames()
        frame = frames[frames.index("FRAMES.connections =") : frames.index("async function renameConnection")]
        assert "Brain is not reachable by anything yet" in frame
        assert "That link is the whole setup" in frame
        assert "Your link appears here" in frame
        # "Accent-bordered, first on the page."
        css = self.css("frames.css")
        card = css[css.index('.connect-card[data-state="empty"] {') :]
        card = card[: card.index("}")]
        assert "var(--accent)" in card

    def test_the_connected_section_is_conditional(self):
        """§7.11: that sentence about revocation "means nothing before there
        is anything to revoke"."""
        frames = self.frames()
        frame = frames[frames.index("FRAMES.connections =") : frames.index("async function renameConnection")]
        assert "One credential per connection. Revoking one" in frame
        section = frame[frame.index("live.length") :]
        assert section.index("Connected") > 0

    def test_copy_before_generating_says_so(self):
        """§7.11, word for word: "Clicking Copy before generating answers
        `Generate your link first` in amber for 2s. The button always reads
        `Copy`." """
        frames = self.frames()
        assert 'answer(event.currentTarget, "Generate your link first")' in frames
        assert frames.count('answer(event.currentTarget, "Generate your link first")') == 2

    def test_the_agent_block_is_an_instruction_not_json(self):
        frames = self.frames()
        block = frames[frames.index("function agentBlock(") : frames.index("FRAMES.connections =")]
        assert "Add this MCP server to this project." in block
        assert "transport: streamable HTTP" in block
        assert "needs no auth" in block
        # §7.11: "The placeholder is an amber `<mark>`."
        assert "<mark>PASTE_YOUR_GENERATED_LINK_HERE</mark>" in block

    def test_revoking_confirms_and_rolling_confirms(self):
        """§9.4: "Revoke a connection | confirm dialog". Rolling invalidates
        a link the person has already pasted somewhere, so it asks too."""
        frames = self.frames()
        frame = frames[frames.index("FRAMES.connections =") : frames.index("async function renameConnection")]
        for act in ("roll: async", "revoke: async"):
            block = frame[frame.index(act) :]
            assert "await confirmDialog({" in block[: block.index("},\n")], act


class TestTheWebsCeilingDidNotWeakenTheAgents:
    """§2: "Implement a per-user sliding-window rate limiter (max 30 tool
    calls/min, max 10 ingestions/min)". Giving the web its own window must
    not have raised the one that sentence is about."""

    def test_the_agent_still_stops_at_thirty(self, cfg, user):
        from app.security import ratelimit

        limiter = ratelimit.RateLimiter()
        allowed = sum(
            1 for _ in range(40) if limiter.check(user["user_id"], "search", {"q": "x"},
                                                  loop_guard=False).allowed
        )
        assert allowed == cfg.tool_calls_per_min == 30

    def test_the_two_windows_are_separate(self, cfg, user):
        """A person reading their library cannot lock out their agent, and an
        agent in a loop cannot lock the person out of their own pages."""
        from app.security import ratelimit

        limiter = ratelimit.RateLimiter()
        for _ in range(cfg.tool_calls_per_min):
            assert limiter.check(user["user_id"], "search", {}, loop_guard=False).allowed
        # The agent's window is spent.
        assert not limiter.check(user["user_id"], "search", {}, loop_guard=False).allowed
        # The person's is untouched.
        assert limiter.check(
            user["user_id"], "web:list_sources", {}, loop_guard=False,
            bucket=ratelimit.WEB_BUCKET,
        ).allowed

    def test_the_ingestion_window_is_still_shared_on_purpose(self, cfg, user):
        """Ingestion costs the same quota whoever asked for it, so §2's
        `max 10 ingestions/min` is about the work and not the caller."""
        from app.security import ratelimit

        limiter = ratelimit.RateLimiter()
        allowed = sum(
            1
            for _ in range(20)
            if limiter.check(
                user["user_id"], "web:ingest", {}, is_ingestion=True, loop_guard=False,
                bucket=ratelimit.WEB_BUCKET,
            ).allowed
        )
        assert allowed == cfg.ingestions_per_min == 10
