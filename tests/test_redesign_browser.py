"""`R-19` — the app, in a real browser.

The cutover deleted `index.html` and `app.js`, and with them the only two
Playwright suites pytest ever spawned: `TestHumanJourneyInABrowser` and
`TestMobileViewport`. Deleting those without replacing them would have left
the new app looking fully tested — 1,400 passing tests — while nothing had
ever opened it. Every defect in this redesign that mattered was found by
rendering it, not by reading it: a template literal ended by a backtick in
an HTML comment, which made every Python test pass and no page load; a meter
grid left a quarter empty; a card claiming "100% left" after a real spend.

So this file exists, and it is deliberately small. Three properties, over
every route the server claims:

1. **It renders.** No uncaught error, and something in `#content`.
2. **It does not scroll sideways on a phone.** The audit names the offending
   element, because "scrollWidth 577" sends someone hunting and "the
   `.toolbar` is 577 wide" is a fix. This found two broken routes the moment
   it was written, which is the argument for having it.
3. **Stored text reaches the DOM as text.** A malicious transcript is the
   attack CLAUDE.md §2.1 is written against, and a payload that got through
   would run with the viewer's session cookie and CSRF token in hand. The
   static guard in `test_redesign_frames.py` reads the source; this one
   plants `<img src=x onerror=...>` in a real rule and looks at a real page.

The measurements live in `tests/browser/app_audit.js` and the expectations
live here, which is how the deleted suites were built: the probe could report
a 577px overflow without anyone encoding "577 is too wide" in JavaScript.

Skipped rather than failed where node or playwright is missing, so the suite
still runs on a bare host.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "tests" / "browser" / "app_audit.js"
JWT_SECRET = "browser-audit-secret-key-long-enough-for-hs256"
PHONE_WIDTH = 390


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

    workdir = tmp_path_factory.mktemp("browser")
    env = {
        **os.environ,
        "BRAIN_ENV": "test",
        "BRAIN_DATA_DIR": str(workdir),
        "BRAIN_JWT_SECRET": JWT_SECRET,
        "BRAIN_ALLOWED_HOSTS": f"127.0.0.1:{port},localhost:{port}",
        # The audit signs in, which seeds the demo library — so every list has
        # something on it and an empty state is never mistaken for a pass.
        "BRAIN_INLINE_WORKER": "0",
    }
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
def report(served):
    result = subprocess.run(
        ["node", str(AUDIT)],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "BASE": served}, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parsed = json.loads(result.stdout)
    assert "fatal" not in parsed, parsed.get("fatal")
    return parsed


class TestEveryRouteRenders:
    def test_the_audit_visited_every_route_the_server_claims(self, report):
        """If the audit and the router drift apart, this tests nothing."""
        from app import webapi

        claimed = {p for p in webapi.UI_PATHS if "{" not in p}
        visited = {row["path"] for row in report["routes"]}
        assert claimed <= visited, claimed - visited

    def test_no_page_throws(self, report):
        """The whole module failing to parse looks exactly like this and
        nothing else catches it: every Python test passes, and the app
        renders its shell with an empty body."""
        broken = {row["path"]: row["errors"] for row in report["routes"] if row["errors"]}
        assert broken == {}, broken

    def test_every_page_puts_something_on_the_screen(self, report):
        """A 200 with an empty `#content` is what a dead router looks like."""
        empty = [row["path"] for row in report["routes"] if row["content"] == 0]
        assert empty == [], empty

    def test_every_page_answers_200(self, report):
        bad = {row["path"]: row["status"] for row in report["routes"] if row["status"] != 200}
        assert bad == {}, bad


class TestNothingScrollsSidewaysOnAPhone:
    """`R-19` found `/rules` at 577px and `/library/sources` at 489px against
    a 390px viewport, both from the same two rules: a toolbar that could not
    wrap and a search field that would not shrink below its placeholder."""

    def test_no_route_overflows_at_390(self, report):
        failures = [
            f"{row['path']}: scrollWidth={row['scrollWidth']} offenders={row['offenders']}"
            for row in report["routes"]
            if row["scrollWidth"] > PHONE_WIDTH or row["offenders"]
        ]
        assert failures == [], "\n".join(failures)

    def test_nor_at_a_desk(self, report):
        failures = [
            f"{row['path']}: offenders={row['offenders']}"
            for row in report.get("wide", [])
            if row["offenders"]
        ]
        assert failures == [], "\n".join(failures)


class TestStoredTextIsTextWhenItReachesTheDom:
    def test_the_payload_was_actually_stored(self, report):
        """Otherwise the next test passes by having tested nothing — the
        failure mode of every XSS test that was ever green for the wrong
        reason."""
        assert report["plantedPayload"] is True

    def test_no_page_executed_it(self, report):
        """`<img src=x onerror=...>` fires the moment it is parsed as markup,
        so a page that renders the rule and does not set the flag escaped it."""
        executed = [row["path"] for row in report["routes"] + report.get("wide", []) if row["xss"]]
        assert executed == [], executed

    def test_the_page_that_lists_rules_really_did_show_it(self, report):
        """The payload is only a test of escaping on a page that renders it.
        `/rules` is where a rule written through the API lands."""
        rules = next(row for row in report["routes"] if row["path"] == "/rules")
        assert rules["content"] > 0
        assert rules["errors"] == []
