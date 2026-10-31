"""`R-20`: a capped list has to say what it is capped out of.

Every read on the web is capped server-side — Rules 200, Archive 200, Sources
100, Transcripts 100, Summaries 50 — and until now nothing on any page said
so. Measured on the shipped build: four documents produce 394 rules, the
sidebar renders `Rules 394`, the page renders 200, and no element anywhere
mentions the other 194. The page's own search box made it worse rather than
better, because it filtered the rows already in the DOM: a word that existed
in rule 250 returned "no results" from a library that contained it.

So the person was told a number they could not reach, and told "nothing
found" about something they had paid credits to store. That is a correctness
bug wearing a performance bug's clothes, and the fix is three things: say the
real total, let them ask for more, and send search to the database once the
list is short of the truth.

The first of those is the one that can rot. A total that does not match its
own list is a second lie on top of the first, so the invariant at the top of
this file — every `count_*` equals its own uncapped `list_*` — is the test
that matters most here. It is why the counts could not simply be read off
`counts()`, whose clauses answer the sidebar and are genuinely different.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.core import engine as E
from app.db import tenant as tenant_db

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
ARTICLE = "https://example.com/how-linear-builds-software"

UNCAPPED = 100_000


@pytest.fixture()
def library(cfg, user):
    """Two real sources through the real pipeline, plus a hand-written rule."""
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


class TestEveryTotalMatchesItsOwnList:
    """The invariant. Each `count_*` is paired with the `list_*` it describes,
    and the pair must agree on an uncapped read or the number on screen is
    wrong in a way nobody can see."""

    @pytest.mark.parametrize(
        "count_fn, list_fn",
        [
            (tenant_db.count_rules, tenant_db.list_rules),
            (tenant_db.count_skills, tenant_db.list_skills),
            (tenant_db.count_sources, tenant_db.list_sources),
            (tenant_db.count_transcripts, tenant_db.list_transcripts),
            (tenant_db.count_summaries, tenant_db.list_summaries),
            (tenant_db.count_archive, tenant_db.list_archive),
        ],
        ids=["rules", "skills", "sources", "transcripts", "summaries", "archive"],
    )
    def test_the_count_equals_the_uncapped_list(self, library, user, count_fn, list_fn):
        tdb = tenant_db.tenant(user["user_id"])
        assert count_fn(tdb) == len(list_fn(tdb, limit=UNCAPPED))

    def test_it_still_holds_once_things_are_archived(self, library, user):
        """Archiving moves rows between two lists at once. Both totals have to
        follow, or Archive grows a number that Rules never gave up."""
        tdb = tenant_db.tenant(user["user_id"])
        rules = tenant_db.list_rules(tdb, limit=UNCAPPED)
        assert rules, "the pipeline produced no rules to archive"

        before_rules = tenant_db.count_rules(tdb)
        before_archive = tenant_db.count_archive(tdb)
        tenant_db.set_claim_status(tdb, rules[0]["id"], "archived")

        assert tenant_db.count_rules(tdb) == before_rules - 1
        assert tenant_db.count_archive(tdb) == before_archive + 1
        assert tenant_db.count_rules(tdb) == len(tenant_db.list_rules(tdb, limit=UNCAPPED))
        assert tenant_db.count_archive(tdb) == len(tenant_db.list_archive(tdb, limit=UNCAPPED))


class TestWhyTheSidebarsCountsCouldNotBeReused:
    """`counts()` answers §5's navigation and its clauses are not these. These
    tests pin the two places they genuinely disagree, so that a later
    simplification to "just use counts()" fails loudly instead of quietly
    putting a wrong number under every toolbar."""

    def test_sources_counts_processing_rows_that_the_sidebar_does_not(self, library, user):
        """§7.7 shows `processing` and `blocked` rows — that is the whole
        point of the page — while `counts()['sources']` is active-only."""
        tdb = tenant_db.tenant(user["user_id"])
        rows = tenant_db.list_sources(tdb, limit=UNCAPPED)
        assert rows
        tdb.execute("UPDATE sources SET status = 'processing' WHERE id = ?", (rows[0]["id"],))
        tdb.commit()

        assert tenant_db.count_sources(tdb) == len(tenant_db.list_sources(tdb, limit=UNCAPPED))
        assert tenant_db.count_sources(tdb) > tenant_db.counts(tdb)["sources"]

    def test_skills_counts_proposed_rows_that_the_sidebar_does_not(self, library, user):
        """The Skills page lists proposed, active and inactive;
        `counts()['skills']` is active-only, so it reads low by exactly the
        drafts the page is showing."""
        tdb = tenant_db.tenant(user["user_id"])
        tenant_db.upsert_skill(
            tdb,
            slug="draft-one",
            name="A drafted skill",
            description="Proposed, not yet approved.",
            trigger_when="never",
            execution_steps=["one"],
            status="proposed",
        )

        assert tenant_db.count_skills(tdb) == len(tenant_db.list_skills(tdb, limit=UNCAPPED))
        assert tenant_db.count_skills(tdb) > tenant_db.counts(tdb)["skills"]


# ---------------------------------------------------------------------------
# The same thing, in a real browser.
#
# Everything above proves the server now reports the truth. None of it proves
# a person can see it, and the whole defect was invisible from Python: the
# page rendered 200 of 394 rules and answered "Nothing here matches" about
# one of the other 194, with every test green. So this part seeds a library
# past the cap, opens `/rules` on a phone-sized viewport, and reads what is
# actually on screen.
# ---------------------------------------------------------------------------

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "tests" / "browser" / "paging_audit.js"
JWT_SECRET = "paging-audit-secret-key-long-enough-for-hs256"
EMAIL = "founder@example.com"
SEEDED = 460  # two presses of a 200-row button, so the repeat is tested and not assumed

SEED = """
import os
from app.db import control as control_db
from app.db import tenant as tenant_db

user = control_db.ensure_user(control_db.control(), %r)
conn = tenant_db.tenant(user["user_id"])
src = tenant_db.upsert_source(conn, title="Seeded", source_type="article")
for i in range(%d):
    tenant_db.insert_claim(
        conn,
        source_id=src["id"],
        claim=f"Marker zqx{i}: a seeded rule that exists to make the list longer.",
        when_it_applies="when the list is longer than one page",
        anchor_quote=f"Marker zqx{i} is quoted here verbatim for the grounding check.",
    )
conn.commit()
print("seeded")
""" % (EMAIL, SEEDED)


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    try:
        subprocess.run(
            ["node", "-e", "require('playwright')"], check=True, capture_output=True, timeout=30
        )
    except Exception:
        pytest.skip("playwright is not installed for node")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    workdir = tmp_path_factory.mktemp("paging")
    env = {
        **os.environ,
        "BRAIN_ENV": "test",
        "BRAIN_DATA_DIR": str(workdir),
        "BRAIN_JWT_SECRET": JWT_SECRET,
        "BRAIN_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
        "BRAIN_INLINE_WORKER": "0",
    }

    # Seeded before the server opens the files, so there is no second writer.
    seeded = subprocess.run(
        [sys.executable, "-c", SEED], cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    assert "seeded" in seeded.stdout, seeded.stdout + seeded.stderr

    process = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            if process.poll() is not None:
                pytest.skip("the server did not start")
            try:
                httpx.get(f"{base}/healthz", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.skip("the server did not become ready")
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()


@pytest.fixture(scope="module")
def seen(served):
    result = subprocess.run(
        ["node", str(AUDIT)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "BASE": served, "EMAIL": EMAIL}, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert "fatal" not in parsed, parsed.get("fatal")
    assert not parsed.get("errors"), parsed.get("errors")
    return parsed


class TestTheCappedListSaysSo:
    def test_the_server_still_caps_the_first_read(self, seen):
        """If it stopped capping, every assertion below would pass for the
        wrong reason — there would be nothing hidden to reveal."""
        assert seen["capped"] == 200
        assert seen["whole"] >= SEEDED
        assert seen["beyond"] > 0

    def test_the_page_says_how_many_it_is_showing_of_how_many(self, seen):
        assert "200" in seen["footText"]
        assert str(seen["whole"]) in seen["footText"].replace(",", "")
        assert "Showing" in seen["footText"]

    def test_there_is_a_button_to_see_more(self, seen):
        assert seen["hasButton"]
        assert "more" in seen["buttonText"].lower()


class TestShowMoreActuallyShowsMore:
    def test_it_loads_another_page_of_rows(self, seen):
        assert seen["firstPaintCards"] == 200
        assert seen["afterMoreCards"] == 400

    def test_it_keeps_going_on_the_second_press(self, seen):
        """The founder chose a button that repeats over one that loads
        everything, so one press proving the mechanism is not enough."""
        assert seen["afterTwoCards"] == seen["whole"]

    def test_and_then_the_footer_stops_saying_anything(self, seen):
        """A whole list owes no explanation. "Showing 460 of 460" under
        every page would be noise bought with the fix."""
        assert seen["footsWhenWhole"] == 0

    def test_it_does_not_throw_the_reader_back_to_the_top(self, seen):
        """Rows are only ever appended, so everything above the fold is
        unchanged and the position has to survive. A jump to the top is how
        a naive re-render fails, and it is worse than no button."""
        assert seen["scrollBefore"] > 0
        assert abs(seen["scrollAfter"] - seen["scrollBefore"]) < 150, (
            f'{seen["scrollBefore"]} -> {seen["scrollAfter"]}'
        )


class TestTheBugItself:
    """The regression this whole change exists to prevent: the page search
    answering "Nothing here matches" about a rule the library holds."""

    def test_a_word_that_exists_only_beyond_the_cap_is_found(self, seen):
        assert seen["token"], "the probe could not find a rule beyond the cap"
        assert seen["searchVisibleCards"] >= 1, (
            f"searching {seen['token']!r} showed {seen['searchVisibleCards']} cards; "
            f"the note said {seen['searchNote']!r}"
        )

    def test_and_the_page_does_not_claim_otherwise(self, seen):
        assert "Nothing here matches" not in seen["searchNote"]


class TestCompressionIsOn:
    def test_a_list_response_comes_back_gzipped(self, served):
        """There was no compression anywhere. On a slow link this one header
        was worth more than any amount of paging."""
        with httpx.Client(base_url=served) as client:
            client.post("/api/login", json={"email": EMAIL})
            raw = client.get(
                "/api/rules", headers={"Accept-Encoding": "gzip"}, extensions={"trace": None}
            )
            assert raw.headers.get("content-encoding") == "gzip"

    def test_the_mcp_transport_is_left_alone(self, served):
        """`/mcp` is deliberately unbuffered and unrewritten end to end."""
        with httpx.Client(base_url=served) as client:
            answer = client.get("/mcp", headers={"Accept-Encoding": "gzip"})
            assert answer.headers.get("content-encoding") != "gzip"
