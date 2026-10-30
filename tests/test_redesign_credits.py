"""`R-18` — one allowance, counted in characters, and the person's say in
what Brain keeps.

The founder's two rulings, a week apart in effect but one change in the code:

  *"I think we should not use video minutes but some kind of credit thing… A
  character is a credit, kind of like how Eleven Labs does it… In usage we
  will replace that."*

  *"We should tell the people that we keep their thing there, somewhere in the
  profile… People should also be able to say, 'Okay I don't want you to keep
  it.' When we process it we delete it immediately."* And: *"the person should
  be able to delete an upload sooner than 30 days."*

The measurement that preceded this is worth keeping in view, because it is
what makes characters the right unit rather than a convenient one: there is
no transcription and no OCR anywhere in brain, so a minute of video is the
*cheapest* thing it reads. What costs money is characters of source text —
one extraction call per ~1,400 of them — and minutes metered YouTube alone,
so an article, a GitHub file or a PDF moved no meter at all.

What these tests hold, in order: the arithmetic is right and cannot drift
from what the extractor actually reads; the ledger cannot leak, double-charge
or lie; the month comes back; and the three retention controls do what their
copy says.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import engine as E
from app.db import control as control_db
from app.ingestion import gatekeeper
from app.ingestion.extractor import chunk_content

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
ARTICLE_URL = "https://example.com/report"


@pytest.fixture()
def ctx(cfg, user):
    return E.build_context(user["user_id"], surface="web_ui")


def _used(ctx) -> int:
    return control_db.credits_state(ctx.control, ctx.user_id, cfg=ctx.cfg)["used"]


class TestWhatACreditIs:
    def test_a_credit_is_a_character(self, cfg):
        """The founder's unit, and the scale is one constant. Set
        `credit_chars` to 1000 and every number in the product loses three
        zeros — the column, the ledger and the enforcement stay characters."""
        assert cfg.credit_chars == 1

    def test_the_price_is_the_text_the_extractor_reads(self, ctx):
        """Not `full_text`. `clean_text` truncates that at 2,000,000
        characters while applying itself to each cue *separately*, leaving
        the cues uncapped — and the chunker consumes the cues. Billing
        `full_text` would charge a 20MB text file for a tenth of what it
        costs to read."""
        E.op_ingest_source(ctx, YOUTUBE_URL)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        content = ctx.prov.fetcher.fetch_url(
            gatekeeper.normalize_url(YOUTUBE_URL, cfg=ctx.cfg), cfg=ctx.cfg
        )
        assert _used(ctx) == sum(len(c.text) for c in chunk_content(content))

    def test_every_source_type_costs_something(self, ctx):
        """The hole credits close. `quota_media_minutes` metered YouTube and
        nothing else, and `quota_files` was incremented by uploads alone — so
        three URL ingestions moved neither meter and the usage screen read
        zero."""
        E.op_ingest_source(ctx, ARTICLE_URL)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)
        assert _used(ctx) > 0, "an article used to cost nothing at all"

    def test_the_duration_ceiling_is_not_the_allowance(self, ctx):
        """`max_media_minutes` (120, per video) and the allowance are
        different things and must stay different. A video over the ceiling is
        out of V1 scope whatever the person's balance is."""
        assert ctx.cfg.max_media_minutes == 120
        assert ctx.cfg.quota_credits != ctx.cfg.max_media_minutes


class TestTheLedgerCannotLie:
    def test_queueing_charges_nothing(self, ctx):
        """The fetch is free and happens first, so a queued job has spent
        nothing — which is what makes "a failure costs nothing" true for
        every failure before the gate."""
        E.op_ingest_source(ctx, YOUTUBE_URL)
        assert _used(ctx) == 0

    def test_a_debit_past_the_cap_takes_nothing(self, ctx, cfg):
        """The ceiling is inside the `WHERE` clause, so the check and the
        write are one statement: two workers racing the same account cannot
        both pass it."""
        assert control_db.debit_credits(ctx.control, ctx.user_id, cfg.quota_credits, cfg=cfg)
        assert not control_db.debit_credits(ctx.control, ctx.user_id, 1, cfg=cfg)
        assert _used(ctx) == cfg.quota_credits, "a refused debit spends nothing"

    def test_a_negative_debit_is_not_a_refund(self, ctx, cfg):
        """The function this replaced guarded with `if minutes and ...`,
        which a negative slipped straight through as a silent credit."""
        control_db.debit_credits(ctx.control, ctx.user_id, 1_000, cfg=cfg)
        control_db.debit_credits(ctx.control, ctx.user_id, -500, cfg=cfg)
        assert _used(ctx) == 1_000

    def test_a_refund_cannot_go_below_zero(self, ctx, cfg):
        control_db.refund_credits(ctx.control, ctx.user_id, credits=10_000)
        assert _used(ctx) == 0

    def test_the_charge_is_read_from_the_row_not_the_job_dict(self, ctx):
        """A worker holds `job` as a copy taken at claim time, and the charge
        is written after it. A refund computed from that copy would always
        give back zero."""
        E.op_ingest_source(ctx, YOUTUBE_URL)
        stale = control_db.list_jobs(ctx.control, ctx.user_id)[0]
        assert stale["charged_credits"] == 0

        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)
        assert control_db.job_charge(ctx.control, stale["job_id"]) > 0

    def test_an_over_budget_source_is_refused_at_the_gate_having_spent_nothing(self, ctx, cfg):
        """The refusal names both numbers, because "you are out" without a
        figure is not something a person can act on."""
        control_db.debit_credits(ctx.control, ctx.user_id, cfg.quota_credits - 5, cfg=cfg)
        E.op_ingest_source(ctx, YOUTUBE_URL)
        outcome = E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)[0]

        assert outcome["status"] == "failed"
        assert "credits" in outcome["message"]
        assert "5" in outcome["message"], "says what is left"
        assert _used(ctx) == cfg.quota_credits - 5, "the refusal spent nothing"

    def test_nothing_was_read_when_the_gate_refuses(self, ctx, cfg):
        """The gate sits above every LLM call, so a refusal leaves no claims,
        no summary and no embeddings — which is the difference between a
        refusal and a half-read source that looks whole."""
        control_db.debit_credits(ctx.control, ctx.user_id, cfg.quota_credits, cfg=cfg)
        E.op_ingest_source(ctx, YOUTUBE_URL)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        from app.db import tenant as tenant_db

        assert tenant_db.counts(ctx.tenant)["claims"] == 0


class TestTheMonthComesBack:
    def test_a_counter_from_last_month_is_zero_this_month(self, ctx, cfg):
        """The reset is lazy and needs no cron, no worker and no scheduled
        job to miss during a redeploy: the month a row names is either this
        one or it is not."""
        control_db.debit_credits(ctx.control, ctx.user_id, 400, cfg=cfg)
        assert _used(ctx) == 400

        ctx.control.execute(
            "UPDATE users SET credits_period = '2001-01' WHERE user_id = ?", (ctx.user_id,)
        )
        assert _used(ctx) == 0

    def test_the_period_is_utc_so_it_turns_over_once(self):
        moment = datetime(2026, 3, 1, 0, 30, tzinfo=timezone.utc)
        assert control_db.current_period(moment) == "2026-03"
        assert control_db.current_period(moment - timedelta(hours=1)) == "2026-02"

    def test_a_refund_into_a_fresh_month_does_not_gift_credits(self, ctx, cfg):
        """A job charged on the 31st and failed on the 1st. The roll happens
        first, so the refund lands on a counter that is already zero and
        `MAX(0, ...)` leaves it there — the person starts the new month whole
        either way, never ahead."""
        control_db.debit_credits(ctx.control, ctx.user_id, 900, cfg=cfg)
        ctx.control.execute(
            "UPDATE users SET credits_period = '2001-01' WHERE user_id = ?", (ctx.user_id,)
        )
        control_db.refund_credits(ctx.control, ctx.user_id, credits=900)
        assert _used(ctx) == 0

    def test_the_meter_says_when_it_comes_back(self):
        """A monthly allowance that does not say it returns reads as a wall,
        and a wall with no paid plan behind it is where a person stops being
        a user."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        card = frames[frames.index("function meterCard(") : frames.index("FRAMES.activity =")]
        assert "Back to full on the 1st." in card

    def test_the_credits_meter_counts_rather_than_rounding(self):
        """Found by rendering: against a 500,000 allowance a real month's use
        rounds to 0%, so the card read `1,919 of 500,000` and `100% left.` in
        the same breath. The count is also the useful figure — it answers
        "can I send this PDF"."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        card = frames[frames.index("function meterCard(") : frames.index("FRAMES.activity =")]
        credits = card[card.index('unit === "credits"') :]
        note = credits[: credits.index("`${100 - pct}% left.`")]
        # The comments explain the trap by quoting it, so read the code only.
        code = "\n".join(
            line for line in note.splitlines() if not line.strip().startswith("//")
        )
        assert "(cap - used).toLocaleString()" in code
        assert "% left" not in code, "a percentage here rounds a real spend to nothing"


class TestTheUsageScreen:
    def test_three_meters_credits_among_them_and_no_minutes(self, ctx):
        usage = E.op_activity(ctx).data["usage"]
        assert set(usage) == {"sources", "credits", "calls"}
        assert usage["credits"]["cap"] == ctx.cfg.quota_credits

    def test_sources_lost_its_cap_because_it_measured_the_wrong_number(self, ctx):
        """It read a tenant source count against `quota_files`, which only
        uploads ever incremented — so it could show `3 of 50` in the same
        minute the enforced figure was `0 of 50`. Two numbers under one
        ceiling is worse than no ceiling."""
        assert E.op_activity(ctx).data["usage"]["sources"]["cap"] is None

    def test_the_spend_list_says_where_the_credits_went(self, ctx):
        """A meter says how much is left, which is only actionable once you
        know what spent it."""
        E.op_ingest_source(ctx, YOUTUBE_URL)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        spend = E.op_activity(ctx).data["spend"]
        assert len(spend) == 1
        assert spend[0]["credits"] == _used(ctx)
        assert spend[0]["title"] and spend[0]["still_here"] is True

    def test_the_spend_list_never_carries_a_staging_path(self, ctx, tmp_path):
        """`ingestion_jobs.input_target` holds a server-side staging path for
        an upload, and the filename is the person's own. Titles come from the
        tenant database instead."""
        path = tmp_path / "quarterly plan.txt"
        path.write_text("We always measure the baseline before changing anything at all. " * 20)
        E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        blob = repr(E.op_activity(ctx).data["spend"])
        assert str(tmp_path) not in blob
        assert "uploads" not in blob

    def test_the_frame_asks_for_the_keys_the_engine_returns(self, ctx):
        """`meterCard` destructures `data.usage[key]` with no guard, so a key
        the engine stopped returning throws on render rather than degrading."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        activity = frames[frames.index("FRAMES.activity =") :]
        listed = activity[activity.index("const metersHtml") : activity.index(".join(")]
        asked = {part.strip().strip('"') for part in listed.split("[")[1].split("]")[0].split(",")}
        assert asked == set(E.op_activity(ctx).data["usage"])


class TestTheAgentIsToldTheTruth:
    def test_no_hint_suggests_archiving_to_free_room(self):
        """It never worked. Nothing in the product has ever returned an
        allowance for removed content, and credits pay for reading a source
        rather than keeping it — so the old hint sent the agent to spend the
        person's time on a remedy that does nothing."""
        from app.core import formatting

        for reason, hint in formatting.REFUSAL_HINTS.items():
            if "quota" in reason or "credit" in reason:
                assert "free room" not in hint, reason
                assert "Do not retry" in hint, reason
                assert "do not suggest archiving" in hint.lower(), reason

    def test_the_out_of_credits_hint_exists_and_names_the_reset(self):
        from app.core import formatting

        hint = formatting.REFUSAL_HINTS["out_of_credits"]
        assert "first of the month" in hint
        assert "archiv" in hint, "it must say explicitly not to suggest archiving"


class TestTellingThePersonWhatBrainKeeps:
    def test_the_profile_says_it_and_reads_the_day_count_from_the_setting(self, ctx):
        """Hardcoding 30 in the frame is how the sentence and the sweeper
        drift apart."""
        retention = E.op_profile(ctx).data["retention"]
        assert retention["upload_days"] == ctx.cfg.upload_retention_days
        assert retention["keep_uploads"] is True

        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        profile = frames[frames.index("FRAMES.profile =") : frames.index("FRAMES.signin =")]
        assert "data.retention.upload_days" in profile
        assert "Keep the file Brain receives" in profile

    def test_it_lives_in_the_existing_your_data_section(self, ctx):
        """The founder said to use a category that already exists if one
        fits, and one does — no new "Data" section was needed."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        profile = frames[frames.index("FRAMES.profile =") : frames.index("FRAMES.signin =")]
        section = profile[profile.index("Your data") :]
        assert section.index('switchHtml("keep_uploads"') < section.index("Export everything")

    def test_the_switch_needs_no_new_javascript(self):
        """The wiring posts the switch's own name, so a preference is six
        edits in the data path and zero in the page's behaviour."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        assert "[toggle.dataset.switch]: on" in frames

    def test_the_profile_still_has_exactly_two_danger_buttons(self):
        """A shipped test counts them, and this section gained a row."""
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        profile = frames[frames.index("FRAMES.profile =") : frames.index("FRAMES.signin =")]
        assert profile.count('data-variant="danger"') == 2


class TestNotKeepingItAtAll:
    def _upload(self, ctx, tmp_path, name="notes.txt"):
        staged = ctx.cfg.uploads_dir_for(ctx.user_id)
        staged.mkdir(parents=True, exist_ok=True)
        path = staged / f"{control_db.new_id('up')}_{name}"
        path.write_text("We always measure the baseline before changing anything at all. " * 20)
        return path

    def test_on_by_default_the_file_survives_the_job(self, ctx, tmp_path):
        path = self._upload(ctx, tmp_path)
        E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)
        assert path.exists()

    def test_off_means_gone_the_moment_the_job_finishes(self, ctx, tmp_path):
        path = self._upload(ctx, tmp_path)
        E.op_update_profile(ctx, keep_uploads=False)
        E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)
        assert not path.exists()

    def test_and_gone_when_the_job_fails_too(self, ctx, tmp_path):
        """A file the person asked not to keep is not kept because Brain had
        trouble with it."""
        from app.ingestion.providers import ProviderError

        class Failing:
            def fetch_url(self, target, *, cfg):
                raise ProviderError("unreadable", "n/a")

            def fetch_file(self, validated, *, cfg):
                raise ProviderError("unreadable", "Brain could not read that file.")

        path = self._upload(ctx, tmp_path)
        E.op_update_profile(ctx, keep_uploads=False)
        E.op_ingest_file(ctx, path)
        ctx.prov.fetcher = Failing()
        assert E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)[0]["status"] == "failed"
        assert not path.exists()

    def test_what_brain_read_from_it_stays(self, ctx, tmp_path):
        """The copy promises this, so it has to be true: the file goes, the
        knowledge does not."""
        from app.db import tenant as tenant_db

        path = self._upload(ctx, tmp_path)
        E.op_update_profile(ctx, keep_uploads=False)
        E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        assert not path.exists()
        assert tenant_db.counts(ctx.tenant)["sources"] == 1
        assert tenant_db.counts(ctx.tenant)["claims"] > 0

    def test_a_link_source_is_never_touched_by_the_switch(self, ctx):
        """`input_target` holds a URL for a link, and the containment check
        is what stops that from being treated as a path."""
        E.op_update_profile(ctx, keep_uploads=False)
        E.op_ingest_source(ctx, YOUTUBE_URL)
        assert E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)[0]["status"] == "ready"

    def test_a_path_outside_the_persons_own_folder_is_refused(self, ctx, tmp_path, control):
        """The containment check, directly. `input_target` is free text."""
        outsider = tmp_path / "someone_elses.txt"
        outsider.write_text("not yours")
        job = {"user_id": ctx.user_id, "input_target": str(outsider)}
        E.op_update_profile(ctx, keep_uploads=False)
        assert E._release_upload(control, ctx.user_id, job, cfg=ctx.cfg) is False
        assert outsider.exists()


class TestDeletingOneUploadByHand:
    def _ingest(self, ctx):
        staged = ctx.cfg.uploads_dir_for(ctx.user_id)
        staged.mkdir(parents=True, exist_ok=True)
        path = staged / f"{control_db.new_id('up')}_quarterly plan.txt"
        path.write_text("We always measure the baseline before changing anything at all. " * 20)
        result = E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)
        return result.data["source_id"], path

    def test_the_source_page_says_a_file_is_held_without_naming_the_path(self, ctx):
        source_id, path = self._ingest(ctx)
        held = E.op_get_source(ctx, source_id).data["file"]
        assert held["held"] is True
        assert held["filename"] == "quarterly plan.txt", "the staging prefix is not theirs"
        assert held["days"] == ctx.cfg.upload_retention_days
        assert str(path) not in repr(held)

    def test_deleting_it_removes_the_file_and_keeps_the_knowledge(self, ctx):
        from app.db import tenant as tenant_db

        source_id, path = self._ingest(ctx)
        claims_before = tenant_db.counts(ctx.tenant)["claims"]

        result = E.op_delete_upload(ctx, source_id)
        assert result.ok and not path.exists()
        assert tenant_db.counts(ctx.tenant)["claims"] == claims_before
        assert E.op_get_source(ctx, source_id).data["file"] is None

    def test_it_refunds_nothing_and_the_dialog_says_so(self, ctx):
        """The credits paid for reading the text, which is already done."""
        source_id, _ = self._ingest(ctx)
        spent = _used(ctx)
        E.op_delete_upload(ctx, source_id)
        assert _used(ctx) == spent

        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        assert "This returns no credits" in frames

    def test_a_link_source_has_nothing_to_delete_and_says_which(self, ctx):
        E.op_ingest_source(ctx, YOUTUBE_URL)
        jobs = control_db.list_jobs(ctx.control, ctx.user_id)
        result = E.op_delete_upload(ctx, jobs[0]["source_id"])
        assert not result.ok and result.error_reason == "nothing_held"
        assert "link" in result.error_message

    def test_an_agent_cannot_call_it(self, ctx, user):
        """§2: an agent archives, a person deletes. No MCP tool either."""
        agent = E.build_context(user["user_id"], surface="mcp")
        source_id, path = self._ingest(ctx)
        assert not E.op_delete_upload(agent, source_id).ok
        assert path.exists()

        from app import mcp_server

        assert "delete_upload" not in Path(mcp_server.__file__).read_text()

    def test_another_tenants_source_is_invisible(self, ctx, control, cfg):
        source_id, path = self._ingest(ctx)
        other = control_db.ensure_user(control, "someone@example.com")
        intruder = E.build_context(other["user_id"], surface="web_ui")
        assert not E.op_delete_upload(intruder, source_id).ok
        assert path.exists()


class TestTheDoorsThatEndThings:
    def test_clearing_the_library_takes_the_files_too(self, ctx):
        """Before `R-18` it took the tenant tables only, so "removes every
        source and everything made from it" left the person's uploads in
        staging for up to 30 days with nothing able to reach them."""
        staged = ctx.cfg.uploads_dir_for(ctx.user_id)
        staged.mkdir(parents=True, exist_ok=True)
        path = staged / f"{control_db.new_id('up')}_notes.txt"
        path.write_text("We always measure the baseline before changing anything at all. " * 20)
        E.op_ingest_file(ctx, path)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        assert E.op_delete_library(ctx, typed="DELETE").ok
        assert not path.exists()

    def test_closing_the_account_does_the_same(self, ctx):
        staged = ctx.cfg.uploads_dir_for(ctx.user_id)
        staged.mkdir(parents=True, exist_ok=True)
        path = staged / f"{control_db.new_id('up')}_notes.txt"
        path.write_bytes(b"something")

        assert E.op_delete_account(ctx, typed="DELETE").ok
        assert not path.exists()

    def test_the_dialog_does_not_promise_credits_back(self):
        frames = (Path(__file__).resolve().parents[1] / "app/static/frames.js").read_text()
        assert "Your credits stay spent" in frames


class TestTheClassifierHasACeiling:
    def test_the_loop_is_bounded_and_says_when_it_stopped(self, cfg):
        """The one unbounded cost in the pipeline: up to `claims x
        link_top_k` LLM calls, nothing capping them. Not a second meter — a
        ceiling, so the price a person was quoted stays a loose bound on what
        their job can actually cost."""
        from app.ingestion import linker

        assert cfg.link_max_calls_per_job > 0
        source = Path(linker.__file__).read_text()
        assert "classified >= cfg.link_max_calls_per_job" in source
        assert "truncated" in source


class TestTheModuleActuallyParses:
    """The guard that was missing.

    `R-18` shipped a frames.js that every Python test passed and no page
    could load: an HTML comment inside a frame's template literal contained
    a backtick, which ended the string. The whole module failed to parse, so
    the app rendered its shell and nothing else. Reading the file as text
    cannot see this; only a parser can.
    """

    def _frames(self) -> Path:
        return Path(__file__).resolve().parents[1] / "app/static/frames.js"

    def test_frames_js_is_valid_javascript(self):
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed on this host")
        # `--check` infers the goal from the extension, and this is a module.
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as handle:
            handle.write(self._frames().read_text())
            copy = handle.name
        try:
            done = subprocess.run([node, "--check", copy], capture_output=True, text=True)
        finally:
            Path(copy).unlink(missing_ok=True)
        assert done.returncode == 0, done.stderr

    def test_no_html_comment_carries_a_backtick(self):
        """The specific trap, held separately so it names itself when it
        trips — and so it still holds on a host with no node."""
        import re

        for comment in re.findall(r"<!--.*?-->", self._frames().read_text(), re.S):
            assert "`" not in comment, (
                "a backtick inside an HTML comment ends the template literal "
                f"the frame is built from: {comment[:80]}"
            )


class TestTheFileKeepsThePersonsOwnName:
    """`R-18` found this by rendering, not by reading. The worker re-validates
    from `ingestion_jobs.input_target`, which is the staged path — so a source
    was titled `up_0228df82e6454712ad38_board notes.txt`, a machine id the
    person never chose, shown back to them as the name of their own document
    and carried into §7.10's spend list.
    """

    def test_the_staging_prefix_is_stripped_from_the_title(self, ctx):
        staged = ctx.cfg.uploads_dir_for(ctx.user_id)
        staged.mkdir(parents=True, exist_ok=True)
        path = staged / f"{control_db.new_id('up')}_board notes.txt"
        path.write_text("We always measure the baseline before changing anything at all. " * 30)

        # No `filename=`, exactly as the worker re-validates it.
        assert gatekeeper.validate_file(path, cfg=ctx.cfg).filename == "board notes.txt"

    def test_a_persons_own_up_prefixed_name_survives(self, ctx):
        """The pattern is pinned to `new_id`'s exact 20 hex characters. A
        looser one would quietly rename someone's file."""
        assert gatekeeper.unstage("up_abc_notes.txt") == "up_abc_notes.txt"
        assert gatekeeper.unstage("update_notes.txt") == "update_notes.txt"
        assert gatekeeper.unstage(f"{control_db.new_id('up')}_x.txt") == "x.txt"


class TestAnUploadThatNeverQueuesIsNotLeftBehind:
    """Only a job row maps a source back to its file, so a refusal or a
    duplicate leaves bytes nothing in the product can reach. The sweep would
    get them in 30 days — but a person who turned the window off should not
    have to wait out a window they turned off.
    """

    def _staged(self, ctx, body: str, name: str = "notes.txt"):
        folder = ctx.cfg.uploads_dir_for(ctx.user_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{control_db.new_id('up')}_{name}"
        path.write_text(body)
        return path

    def test_a_duplicate_upload_drops_its_copy(self, ctx):
        body = "We always measure the baseline before changing anything at all. " * 30
        first = self._staged(ctx, body)
        E.op_ingest_file(ctx, first)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)
        E.op_update_profile(ctx, keep_uploads=False)

        again = self._staged(ctx, body)
        assert E.op_ingest_file(ctx, again).data["duplicate"] is True
        assert not again.exists(), "the second copy was never queued, so nothing would reach it"

    def test_a_refused_upload_drops_its_copy(self, ctx, cfg):
        E.op_update_profile(ctx, keep_uploads=False)
        path = self._staged(ctx, "x" * 400, name="clip.mp4")
        result = E.op_ingest_file(ctx, path)
        assert not result.ok
        assert not path.exists()

    def test_an_unaffordable_upload_drops_its_copy(self, ctx, cfg):
        E.op_update_profile(ctx, keep_uploads=False)
        control_db.debit_credits(ctx.control, ctx.user_id, cfg.quota_credits, cfg=cfg)
        path = self._staged(ctx, "We always measure the baseline before changing. " * 30)
        assert E.op_ingest_file(ctx, path).error_reason == "out_of_credits"
        assert not path.exists()

    def test_with_the_switch_on_they_are_kept_for_the_sweep(self, ctx):
        """The default is still to keep: the window exists so a failed read
        can be looked at."""
        body = "We always measure the baseline before changing anything at all. " * 30
        first = self._staged(ctx, body)
        E.op_ingest_file(ctx, first)
        E.run_pending_jobs_sync(ctx.user_id, cfg=ctx.cfg)

        again = self._staged(ctx, body)
        E.op_ingest_file(ctx, again)
        assert again.exists()
