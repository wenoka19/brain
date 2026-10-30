"""§7.2 — Home's thread state, and its four hard requirements.

"Ingestion is a conversation, not a progress bar. The person must be able to
interrupt while Brain works."

The requirements are the specification, and three of the four were already
broken by the frame that existed:

1. **"The composer is never disabled while Brain works."** It was disabled for
   the length of the submit request. Brief, but it is the same promise, and
   everything below depends on the field staying live.
2. **"An interjection is acknowledged without restarting or stopping the
   pipeline."** There was no way to say anything at all once a source was
   submitted — the frame navigated to the source's own page, where there is
   no composer.
3. **"Leaving the page does not cancel anything."** Already true: the inline
   worker drains the queue in the web process, which was fixed when the
   founder asked for *"quitter la page et Brain continue"* to be a promise
   the architecture could keep.
4. **"Reopening Home later returns to the empty state."** Which is why the
   thread is not persisted anywhere: the source it produced is the record.

The interjection is the part with teeth. §7.2's example reply is *"Noted —
I'll leave them in. Still on the summary, so nothing is lost"* — a sentence
that is only true if the summary has not been written yet. So the operation
returns which steps are still pending, and refuses outright once none are:
"noted" on a finished job is a reassurance the person finds out about when
the result does not change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import engine as E
from app.db import control as control_db

FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture()
def queued(cfg, user):
    """A job in the queue, not yet drained — which is the only state in which
    §7.2's interruption means anything."""
    ctx = E.build_context(user["user_id"], surface="web_ui")
    result = E.op_ingest_source(ctx, YOUTUBE, user_note="Pull out how they run planning cycles.")
    return ctx, result.data["source_id"], result.data["job_id"]


class TestTheInterjection:
    def test_it_reaches_the_instruction_the_pipeline_reads(self, queued, control):
        ctx, source_id, job_id = queued
        result = E.op_add_instruction(ctx, source_id, "Actually keep the hiring bits too.")
        assert result.ok

        job = control_db.get_job(control, job_id)
        assert job["user_note"] == (
            "Pull out how they run planning cycles. Actually keep the hiring bits too."
        )

    def test_it_changes_what_comes_out(self, queued, cfg, user, control):
        """The proof that it is not a decorative acknowledgement: the summary
        step reads the instruction, and the step's own detail says so."""
        ctx, source_id, job_id = queued
        E.op_add_instruction(ctx, source_id, "Keep the hiring bits.")
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        summary = E.op_get_source(ctx, source_id).data["summary"]
        assert "Keep the hiring bits." in summary["instruction"]

    def test_nothing_is_restarted_or_stopped(self, queued, control):
        """§7.2's second hard requirement, stated as what must NOT change."""
        ctx, source_id, job_id = queued
        before = control_db.get_job(control, job_id)
        steps_before = control_db.job_steps(control, job_id)

        E.op_add_instruction(ctx, source_id, "One more thing.")

        after = control_db.get_job(control, job_id)
        assert after["job_id"] == before["job_id"], "the job was replaced"
        assert after["status"] == before["status"], "the job's status moved"
        assert after["attempts"] == before["attempts"], "it was re-queued"
        assert control_db.job_steps(control, job_id) == steps_before, "a step was rewound"

    def test_it_says_what_the_note_will_actually_reach(self, queued):
        """§7.2's reply is "Still on the summary, so nothing is lost" — true
        only while the summary is still ahead."""
        ctx, source_id, _ = queued
        data = E.op_add_instruction(ctx, source_id, "Keep the hiring bits.").data
        assert "Writing the summary" in data["will_affect"]
        assert data["already_done"] == []

    def test_a_finished_job_is_refused_rather_than_reassured(self, queued, cfg, user):
        """"Noted" on a finished job is a reassurance the person finds out
        about when the result does not change."""
        ctx, source_id, _ = queued
        E.run_pending_jobs_sync(user["user_id"], cfg=cfg, limit=1)

        refused = E.op_add_instruction(ctx, source_id, "Too late.")
        assert not refused.ok and refused.error_reason == "too_late"
        assert "cannot change it" in refused.error_message

    def test_an_empty_interjection_is_refused(self, queued):
        ctx, source_id, _ = queued
        for blank in ("", "   ", "\n"):
            refused = E.op_add_instruction(ctx, source_id, blank)
            assert not refused.ok and refused.error_reason == "empty_text"

    def test_a_source_nobody_is_working_on_is_not_found(self, cfg, user):
        ctx = E.build_context(user["user_id"], surface="web_ui")
        refused = E.op_add_instruction(ctx, "src_nope", "anything")
        assert not refused.ok and refused.error_reason == "not_found"

    def test_it_round_trips_over_http(self, queued, cfg, user):
        from starlette.testclient import TestClient

        from app.main import create_app

        client = TestClient(create_app())
        client.post("/api/login", json={"email": "founder@example.com"})
        _, source_id, _ = queued
        response = client.post(
            f"/api/sources/{source_id}/instruct",
            json={"text": "Keep the hiring bits."},
            headers={"X-Brain-Request": "1", "X-Brain-CSRF": client.cookies.get("brain_csrf", "")},
        )
        assert response.status_code == 200
        assert response.json()["will_affect"]


class TestTheFrameSource:
    @staticmethod
    def thread() -> str:
        source = FRAMES_JS.read_text()
        return source[source.index("function openThread(") : source.index("function watchThread(")]

    def test_the_composer_is_never_disabled(self):
        """§7.2's first hard requirement, as a grep: there is no code path
        that takes the field away."""
        source = FRAMES_JS.read_text()
        assert "input.disabled = true" not in source
        assert ".disabled = true" not in source.split("FRAMES.home =")[1].split("FRAMES.")[0]

    def test_the_greeting_and_the_pitch_go(self):
        """§7.2: "Greeting and pitch disappear. The thread fills the space"."""
        thread = self.thread()
        for gone in (".home-greeting", ".home-pitch", ".home-actions"):
            assert f'querySelector("{gone}")' in thread, gone
        assert 'input.placeholder = "Reply to Brain…"' in thread

    def test_the_typewriter_is_stopped_before_the_placeholder_is_set(self):
        """It owns the placeholder; a cycle still running would overwrite
        §7.2's a second later."""
        thread = self.thread()
        assert thread.index("cycling?.stop()") < thread.index('input.placeholder = "Reply')

    def test_the_sequence_is_the_one_7_2_lists(self):
        thread = self.thread()
        # source chip + instruction, then "On it", then the work block.
        assert thread.index("source-chip") < thread.index("On it.")
        assert thread.index("On it.") < thread.index('id="thread-work"')
        assert "Brain is working. You can keep typing" in thread

    def test_a_second_link_is_a_second_source_not_an_interjection(self):
        thread = self.thread()
        assert "if (!text || /\\bhttps?:\\/\\/\\S+/i.test(text)) return;" in thread

    def test_the_reply_does_not_claim_more_than_it_knows(self):
        """`will_affect` is empty when the engine could not say — the reply
        falls back to what is certainly true rather than naming a step."""
        thread = self.thread()
        assert "sent.will_affect" in thread
        assert "It applies from here." in thread

    def test_the_work_block_collapses_to_one_line(self):
        source = FRAMES_JS.read_text()
        watch = source[source.index("function watchThread(") :]
        watch = watch[: watch.index("/* ----")]
        assert 'dataset.collapsed = "true"' in watch
        assert "Processed in ${humanDuration(progress.elapsed_sec)}" in watch
        # "Processed in 0s" is accurate and says nothing.
        assert "`Processed · ${progress.steps.length} steps`" in watch

    def test_the_result_table_is_three_rows_of_pills(self):
        source = FRAMES_JS.read_text()
        table = source[source.index("function resultTable(") : source.index("/** Turn Home into")]
        assert '"Ready"' in table and '"Added"' in table and '"Need review"' in table
        # Amber only while they are waiting on the person.
        assert 'state: data.skills.some((s) => s.status === "proposed") ? "pending" : "active"' in table
        # §6 rule 11: a source can yield nothing, and the thread says so.
        assert "Nothing came out of this one" in table

    def test_leaving_the_page_stops_the_poll_and_not_the_work(self):
        """§7.2: "Leaving the page does not cancel anything." The poll is the
        page's; the pipeline is the server's."""
        source = FRAMES_JS.read_text()
        watch = source[source.index("function watchThread(") :]
        watch = watch[: watch.index("/* ----")]
        assert "if (!node.isConnected) return" in watch
        # Nothing in the frame cancels a job — there is no such endpoint.
        assert "cancel" not in watch.lower()

    def test_a_frames_root_lookup_goes_through_one_helper(self):
        """`querySelector` searches descendants, and a frame's root often IS
        the element a later lookup wants — `listFrame` returns the `.wrap`,
        Home returns the `.home`. Both returned null and threw on the next
        property access."""
        source = FRAMES_JS.read_text()
        assert "export function self_or_within(" in source
        assert 'self_or_within(node, ".home")' in source
        assert 'self_or_within(node, ".wrap")' in source
