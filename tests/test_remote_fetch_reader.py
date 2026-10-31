"""`R-23`: the fetch that happens on another machine.

YouTube refuses anonymous requests from datacentre IPs. Measured three ways
on 31 October 2026 — yt-dlp's maintainers ("nothing that can be affected from
the yt-dlp side"), the bgutil PO-token provider ("a PO Token does not bypass
IP-based login restrictions"), and `youtube-transcript-api`, whose own error
text names the category and which ships a residential-proxy class as its
answer. The official `captions.download` needs authorisation from someone who
can edit the video, so there is no sanctioned path either.

So the fetch moves to a reader on a residential connection, and these tests
hold the four promises that move has to keep:

1. the server stops trying, and the reader is offered nothing else;
2. a video that comes back through the reader keeps its **timestamps**;
3. a failure the person cannot act on never reaches their screen;
4. a reader that cannot read takes no work.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import control as control_db
from app.db import tenant as tenant_db
from app.main import create_app

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
ARTICLE_URL = "https://example.com/post"
TOKEN = "reader-token-long-enough-to-be-a-secret"

#: What a reader posts back. Real cue shapes, because the whole point of the
#: payload contract is that `?t=1420` survives the trip.
PAYLOAD = {
    "title": "How to price a first offer",
    "full_text": (
        "We raised the price from twenty to eighty dollars and conversion barely moved, "
        "because the people who buy at twenty are not the people who buy at eighty. "
        "The lesson is that a price change is an audience change, and you should expect "
        "a different buyer rather than the same buyer paying more. "
        "When we tested this on a cold audience the pattern held for three months."
    ),
    "cues": [
        {"start": 1420, "end": 1427, "text": "We raised the price from twenty to eighty dollars and conversion barely moved"},
        {"start": 1428, "end": 1436, "text": "because the people who buy at twenty are not the people who buy at eighty"},
        {"start": 1437, "end": 1446, "text": "The lesson is that a price change is an audience change"},
        {"start": 1447, "end": 1455, "text": "and you should expect a different buyer rather than the same buyer paying more"},
        {"start": 1456, "end": 1464, "text": "When we tested this on a cold audience the pattern held for three months"},
    ],
    "author": "A Practitioner",
    "published_at": "2026-03-04",
    "language": "en",
    "duration_sec": 1800,
    "locator_type": "timestamp",
}


@pytest.fixture
def remote(cfg, monkeypatch):
    """`cfg`, with the reader's routing switched on.

    `BRAIN_ENV=test` deliberately leaves `remote_fetch_types` empty: its
    provider reads a video from a fixture, with no network and no IP to
    refuse, and the whole suite drains those jobs in-process. A test that
    wants the routing asks for it by name, through the same variable
    production uses.
    """
    from app import config as config_module

    monkeypatch.setenv("BRAIN_REMOTE_FETCH_TYPES", "youtube")
    monkeypatch.setenv("BRAIN_FETCHER_TOKEN", TOKEN)
    settings = config_module.reload_settings()
    settings.ensure_dirs()
    yield settings
    config_module.reload_settings()


@pytest.fixture
def ctx(remote, user):
    return E.build_context(user["user_id"], client_name="test", surface="web_ui")


@pytest.fixture
def queued_video(ctx):
    result = E.op_ingest_source(ctx, YOUTUBE_URL)
    assert result.ok, result.data
    return result.data["job_id"]


@pytest.fixture
def reader(remote, monkeypatch):
    """A TestClient that presents the reader's token and nothing else."""
    monkeypatch.setenv("BRAIN_JWT_SECRET", "r23-test-secret-key-long-enough-ok")
    app = create_app(remote)
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield client


# ===========================================================================
# 1 — the server stops trying
# ===========================================================================
class TestTheServerStopsFetchingVideos:
    def test_the_in_process_drain_never_claims_a_video(self, ctx, queued_video):
        """The failure this prevents is the one production shipped: the server
        running `fetch_youtube` from the IP YouTube refuses, and spending the
        job's attempts proving it."""
        processed = E.run_pending_jobs_sync(ctx.user_id, limit=5)
        assert processed == []
        job = control_db.get_job(ctx.control, queued_video)
        assert job["status"] == "queued"
        assert job["attempts"] == 0

    def test_everything_else_is_still_fetched_in_place(self, ctx):
        """An article has no IP problem. Routing it away would make a working
        path depend on a machine being awake."""
        article = E.op_ingest_source(ctx, ARTICLE_URL)
        assert article.ok
        processed = E.run_pending_jobs_sync(ctx.user_id, limit=5)
        assert [row["status"] for row in processed] == ["ready"]

    def test_a_video_is_never_reaped_for_being_patient(self, ctx, queued_video):
        """`reap_exhausted_jobs` fails jobs nothing will claim again, on the
        in-process worker's lower ceiling. A video waiting for a reader is not
        that, and reaping it would fail a source that was never read once."""
        control_db.reap_exhausted_jobs(
            ctx.control, user_id=ctx.user_id, max_attempts=0,
            exclude_source_types=ctx.cfg.remote_fetch_types,
        )
        assert control_db.get_job(ctx.control, queued_video)["status"] == "queued"


# ===========================================================================
# 2 — what the reader is offered, and what it is not told
# ===========================================================================
class TestWhatTheReaderIsOffered:
    def test_only_videos_and_only_three_fields(self, ctx, queued_video):
        E.op_ingest_source(ctx, ARTICLE_URL)
        leased = E.lease_fetch_job(cfg=ctx.cfg)
        assert leased["job_id"] == queued_video
        # Least privilege, and it is cheaper to keep than to retrofit: not the
        # user id, not the note that steers extraction, not a database path.
        assert set(leased) == {"job_id", "url", "source_type"}
        assert leased["url"] == YOUTUBE_URL

    def test_the_lease_spends_no_attempt(self, ctx, queued_video):
        """A closed laptop must not be able to fail someone's video. Only a
        *reported* failure spends an attempt."""
        E.lease_fetch_job(cfg=ctx.cfg)
        job = control_db.get_job(ctx.control, queued_video)
        assert job["attempts"] == 0
        assert job["status"] == "fetching"
        assert job["locked_until"]

    def test_two_readers_do_not_get_the_same_job(self, ctx, queued_video):
        assert E.lease_fetch_job(cfg=ctx.cfg)["job_id"] == queued_video
        assert E.lease_fetch_job(cfg=ctx.cfg) is None

    def test_an_empty_queue_is_not_an_error(self, ctx):
        assert E.lease_fetch_job(cfg=ctx.cfg) is None


# ===========================================================================
# 3 — the timestamps survive, which is the whole product promise
# ===========================================================================
class TestTheTextComesBackWithItsTimestamps:
    def test_the_cues_keep_their_seconds(self, ctx, queued_video):
        E.lease_fetch_job(cfg=ctx.cfg)
        outcome = E.accept_fetched_content(queued_video, PAYLOAD, cfg=ctx.cfg)
        assert outcome["status"] == "ready", outcome

        source = tenant_db.get_source(ctx.tenant, outcome["source_id"])
        assert source["title"] == PAYLOAD["title"]
        assert source["duration_sec"] == 1800
        stored = tenant_db.get_source_content(ctx.tenant, outcome["source_id"])["cues"]
        assert [cue["start"] for cue in stored][:2] == [1420, 1428]

    def test_every_claim_carries_a_timestamp_locator(self, ctx, queued_video):
        E.lease_fetch_job(cfg=ctx.cfg)
        outcome = E.accept_fetched_content(queued_video, PAYLOAD, cfg=ctx.cfg)
        claims = tenant_db.claims_for_source(ctx.tenant, outcome["source_id"])
        assert claims, "a video that produced no claim proves nothing here"
        assert {claim["locator_type"] for claim in claims} == {"timestamp"}
        assert all(claim["locator_value"] for claim in claims)

    def test_this_is_why_the_rescue_path_could_not_be_reused(self, ctx):
        """`op_rescue_job` takes a job id and text, and would have been the
        shortcut. It rebuilds cues as `Pasted part N` and forces
        `locator_type="section"` — so a video rescued through it loses every
        deep link. The contrast is the reason a second endpoint exists."""
        job_id = E.op_ingest_source(ctx, YOUTUBE_URL).data["job_id"]
        control_db.set_job_status(ctx.control, job_id, "failed", error_message="x")
        rescued = E.op_rescue_job(ctx, job_id, PAYLOAD["full_text"])
        assert rescued.ok, rescued.data
        claims = tenant_db.claims_for_source(
            ctx.tenant, control_db.get_job(ctx.control, job_id)["source_id"]
        )
        assert claims and {claim["locator_type"] for claim in claims} == {"section"}

    def test_the_reader_cannot_change_what_kind_of_thing_this_is(self, ctx, queued_video):
        """`source_type` is read from the job row, never from the payload."""
        E.lease_fetch_job(cfg=ctx.cfg)
        outcome = E.accept_fetched_content(
            queued_video, {**PAYLOAD, "source_type": "user_note"}, cfg=ctx.cfg
        )
        source = tenant_db.get_source(ctx.tenant, outcome["source_id"])
        assert source["source_type"] == "youtube"

    def test_a_nonsense_locator_falls_back_to_what_the_cues_show(self, ctx, queued_video):
        E.lease_fetch_job(cfg=ctx.cfg)
        outcome = E.accept_fetched_content(
            queued_video, {**PAYLOAD, "locator_type": "../../etc/passwd"}, cfg=ctx.cfg
        )
        claims = tenant_db.claims_for_source(ctx.tenant, outcome["source_id"])
        assert {claim["locator_type"] for claim in claims} == {"timestamp"}

    def test_posting_twice_does_not_charge_twice(self, ctx, queued_video):
        E.lease_fetch_job(cfg=ctx.cfg)
        first = E.accept_fetched_content(queued_video, PAYLOAD, cfg=ctx.cfg)
        spent = control_db.credits_left(ctx.control, ctx.user_id, cfg=ctx.cfg)
        again = E.accept_fetched_content(queued_video, PAYLOAD, cfg=ctx.cfg)
        assert again.get("stale") is True
        assert again["status"] == first["status"] == "ready"
        assert control_db.credits_left(ctx.control, ctx.user_id, cfg=ctx.cfg) == spent


# ===========================================================================
# 4 — a failure the person cannot act on never reaches their screen
# ===========================================================================
class TestAFailureTheyCannotActOnIsNotShown:
    def test_a_platform_refusal_is_handed_back_not_reported(self, ctx, queued_video):
        """The founder's rule: do not dress up a failure state you can avoid.
        A refusal says nothing permanent about the video, so the job goes back
        to the queue and the work block keeps saying what it said."""
        E.lease_fetch_job(cfg=ctx.cfg)
        outcome = E.report_fetch_problem(
            queued_video, "platform_blocked_caller", "YouTube refused the reader", cfg=ctx.cfg
        )
        assert outcome["deferred"] is True
        job = control_db.get_job(ctx.control, queued_video)
        assert job["status"] == "queued"
        assert job["attempts"] == 1
        # Nothing terminal, and nothing for the person to read.
        assert not job["error_message"]
        assert job["locked_until"]

    def test_a_reader_with_stale_tooling_fails_nothing(self, ctx, queued_video):
        """The 31 October incident, as a state the product must absorb: deno
        one minor version below yt-dlp's minimum. Ours, not the video's."""
        E.lease_fetch_job(cfg=ctx.cfg)
        E.report_fetch_problem(queued_video, "js_runtime_missing", "deno 2.1.4", cfg=ctx.cfg)
        assert control_db.get_job(ctx.control, queued_video)["status"] == "queued"

    def test_one_sentence_once_the_attempts_are_spent(self, ctx, queued_video):
        for _ in range(ctx.cfg.fetch_max_attempts):
            control_db.defer_job(ctx.control, queued_video, seconds=-1)
            E.lease_fetch_job(cfg=ctx.cfg)
            E.report_fetch_problem(
                queued_video, "platform_blocked_caller", "refused again", cfg=ctx.cfg
            )
        job = control_db.get_job(ctx.control, queued_video)
        assert job["status"] == "failed"
        assert job["error_message"] == "Video unavailable right now, try later."
        # The cause is kept where the operator reads it, not on the screen.
        assert job["error_reason"] == "platform_blocked_caller"

    def test_the_credits_come_back_when_it_finally_gives_up(self, ctx, queued_video):
        before = control_db.credits_left(ctx.control, ctx.user_id, cfg=ctx.cfg)
        for _ in range(ctx.cfg.fetch_max_attempts):
            control_db.defer_job(ctx.control, queued_video, seconds=-1)
            E.lease_fetch_job(cfg=ctx.cfg)
            E.report_fetch_problem(queued_video, "fetch_failed", "", cfg=ctx.cfg)
        assert control_db.credits_left(ctx.control, ctx.user_id, cfg=ctx.cfg) == before

    @pytest.mark.parametrize(
        "reason,said",
        [
            ("no_transcript", "That video has no captions."),
            ("blocked_by_platform", "YouTube refused that video."),
        ],
    )
    def test_a_reason_where_waiting_cannot_help_ends_now_with_its_own_sentence(
        self, ctx, queued_video, reason, said
    ):
        """Flattening these into the one line would tell someone to try later
        when later cannot work, and would hide the thing they *can* do."""
        E.lease_fetch_job(cfg=ctx.cfg)
        outcome = E.report_fetch_problem(queued_video, reason, said, cfg=ctx.cfg)
        assert outcome.get("deferred") is not True
        job = control_db.get_job(ctx.control, queued_video)
        assert job["status"] in ("failed", "blocked_by_platform")
        assert job["error_message"] == said

    def test_a_report_on_a_finished_job_changes_nothing(self, ctx, queued_video):
        E.lease_fetch_job(cfg=ctx.cfg)
        E.accept_fetched_content(queued_video, PAYLOAD, cfg=ctx.cfg)
        outcome = E.report_fetch_problem(queued_video, "fetch_failed", "late", cfg=ctx.cfg)
        assert outcome.get("stale") is True
        assert control_db.get_job(ctx.control, queued_video)["status"] == "ready"


# ===========================================================================
# 5 — a reader that cannot read takes no work
# ===========================================================================
class TestAnUnfitReaderIsInvisible:
    def _worker(self, remote, posts):
        from app.fetch_worker import FetchWorker

        class Recorder:
            def post(self, url, json=None, headers=None):  # noqa: A002
                posts.append(url)
                raise AssertionError(f"an unfit reader called {url}")

        return FetchWorker("https://brain.test", TOKEN, cfg=remote, client=Recorder())

    def test_it_leases_nothing_while_its_tooling_is_unfit(self, remote, monkeypatch):
        """This is the whole lesson of 31 October, as code. A reader that
        cannot read must be invisible, not destructive: it took no job, so no
        job failed, so nobody's screen changed."""
        from app.ingestion import fetchers

        monkeypatch.setattr(
            fetchers,
            "youtube_readiness",
            lambda **_: fetchers.YoutubeReadiness(
                False, problem="deno 2.1.4 is too old", remedy="install it"
            ),
        )
        posts: list[str] = []
        assert self._worker(remote, posts).run(once=True) == 0
        assert posts == []

    def test_a_fit_reader_reads(self, remote, monkeypatch):
        from app.ingestion import fetchers

        monkeypatch.setattr(
            fetchers,
            "youtube_readiness",
            lambda **_: fetchers.YoutubeReadiness(True, runtimes="deno-2.9.8"),
        )
        from app.fetch_worker import FetchWorker

        calls: list[tuple[str, dict]] = []

        class Fake:
            def post(self, url, json=None, headers=None):  # noqa: A002
                calls.append((url, json or {}))

                class Response:
                    status_code = 200

                    @staticmethod
                    def json():
                        if url.endswith("/lease"):
                            return {
                                "ok": True,
                                "job": {
                                    "job_id": "job_x",
                                    "url": YOUTUBE_URL,
                                    "source_type": "youtube",
                                },
                            }
                        return {"ok": True, "status": "ready", "claims": 2}

                return Response()

        worker = FetchWorker("https://brain.test", TOKEN, cfg=remote, client=Fake())
        monkeypatch.setattr(worker, "_read", lambda url: dict(PAYLOAD))
        assert worker.run(once=True) == 1
        assert [url.rsplit("/", 2)[-1] for url, _ in calls] == ["lease", "content"]
        # The cues reach the server with their seconds intact.
        assert calls[1][1]["cues"][0]["start"] == 1420


# ===========================================================================
# 6 — the endpoints
# ===========================================================================
class TestTheReaderEndpoints:
    def test_the_whole_round_trip_over_http(self, reader, ctx, queued_video):
        leased = reader.post("/api/fetch-queue/lease").json()
        assert leased["job"]["job_id"] == queued_video
        posted = reader.post(
            f"/api/fetch-queue/{queued_video}/content", json=PAYLOAD
        ).json()
        assert posted["ok"] is True and posted["status"] == "ready"

    def test_a_problem_can_be_reported_over_http(self, reader, queued_video):
        reader.post("/api/fetch-queue/lease")
        answer = reader.post(
            f"/api/fetch-queue/{queued_video}/problem",
            json={"reason": "platform_blocked_caller", "message": "refused"},
        ).json()
        assert answer["deferred"] is True

    def test_no_token_and_a_wrong_token_are_indistinguishable(self, reader, queued_video):
        """A reader with the wrong token must not learn whether the feature is
        on, let alone whether a job is waiting."""
        blank = dict(reader.headers)
        blank.pop("authorization", None)
        missing = reader.post("/api/fetch-queue/lease", headers={"Authorization": ""})
        wrong = reader.post(
            "/api/fetch-queue/lease", headers={"Authorization": "Bearer not-the-token"}
        )
        assert missing.status_code == wrong.status_code == 401
        assert missing.json()["message"] == wrong.json()["message"]
        assert missing.json()["reason"] == wrong.json()["reason"]

    def test_an_unauthorised_caller_cannot_post_content_either(self, reader, queued_video):
        refused = reader.post(
            f"/api/fetch-queue/{queued_video}/content",
            json=PAYLOAD,
            headers={"Authorization": "Bearer wrong"},
        )
        assert refused.status_code == 401
        assert control_db.get_job(control_db.control(), queued_video)["status"] == "queued"

    def test_an_unknown_job_is_a_404_not_a_crash(self, reader):
        assert reader.post("/api/fetch-queue/job_nope/content", json=PAYLOAD).status_code == 404

    def test_the_reader_cannot_push_an_upload_through_this_door(self, reader, ctx, tmp_path):
        """Only the source types the server does not fetch itself. A reader
        that could post content for a PDF would be posting over a file the
        person uploaded."""
        path = tmp_path / "note.txt"
        path.write_text(PAYLOAD["full_text"], encoding="utf-8")
        job_id = E.op_ingest_file(ctx, path).data["job_id"]
        refused = reader.post(f"/api/fetch-queue/{job_id}/content", json=PAYLOAD)
        assert refused.status_code == 409


# ===========================================================================
# 7 — the silent queue, said out loud
# ===========================================================================
class TestDoctorNoticesWhatNobodyElseWould:
    def _doctor(self, monkeypatch, capsys, **env):
        from app import cli
        from app import config as config_module

        monkeypatch.setenv("BRAIN_ENV", "prod")
        monkeypatch.setenv("BRAIN_PUBLIC_URL", "https://example.test")
        monkeypatch.setenv("BRAIN_JWT_SECRET", "doctor-secret-key-long-enough-for-hs256")
        monkeypatch.setenv("BRAIN_REMOTE_FETCH_TYPES", "youtube")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        config_module.reload_settings()
        from app.ingestion import fetchers

        monkeypatch.setattr(
            fetchers,
            "youtube_readiness",
            lambda **_: fetchers.YoutubeReadiness(True, runtimes="deno-2.9.8"),
        )
        cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
        return capsys.readouterr().out

    def test_a_missing_reader_token_is_a_failure_not_a_footnote(
        self, cfg, monkeypatch, capsys
    ):
        """Without it no reader can authenticate, so every video would wait
        forever — and a queue that waits looks exactly like a queue that is
        being read. This is the one configuration mistake that produces no
        error anywhere until a person is already waiting."""
        monkeypatch.delenv("BRAIN_FETCHER_TOKEN", raising=False)
        out = self._doctor(monkeypatch, capsys)
        assert "video reader" in out and "BRAIN_FETCHER_TOKEN" in out
        assert "0 failure(s)" not in out

    def test_a_configured_reader_passes(self, cfg, monkeypatch, capsys):
        out = self._doctor(monkeypatch, capsys, BRAIN_FETCHER_TOKEN=TOKEN)
        assert "video reader" in out and "token set" in out
        assert "video queue" in out

    def test_an_old_waiting_video_is_reported(self, cfg, monkeypatch, capsys, user):
        """The reader has been offline long enough to matter."""
        control = control_db.control()
        control_db.create_job(
            control,
            user["user_id"],
            source_id="src_waiting",
            input_target=YOUTUBE_URL,
            source_type="youtube",
        )
        control.execute(
            "UPDATE ingestion_jobs SET created_at = datetime('now', '-40 minutes') "
            "WHERE source_id = 'src_waiting'"
        )
        control.commit()
        out = self._doctor(monkeypatch, capsys, BRAIN_FETCHER_TOKEN=TOKEN)
        assert "video queue" in out and "is a reader running?" in out
