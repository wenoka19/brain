"""Every control the reading surfaces draw has to do its job — §6 rule 11.

This file exists because of one bug, and it is the worst kind the redesign
produced: **a frame that returns its node before its own wiring runs.**

    FRAMES.transcript = async function transcript(params) {
      ...
      return el(`... <input id="in-page"> <button id="copy"> ...`);

      wireReadBar(node);          // never reached
      node.querySelector("#copy") // never reached
        .addEventListener(...);
    };

`§7.5` and `§7.6` both had it. Nothing warns: the markup is right, the ids are
right, the handlers are written and correct, the page renders, no error reaches
the console. Every control is simply inert — which §6 rule 11 calls worse than
a control that was never drawn, because the person finds out by clicking.

JavaScript will not tell us, so these tests read the source: a frame that names
`node` after building it must bind it, and the code after must be reachable.
The behaviour itself was confirmed in a browser (`tests/browser/read_probe.js`)
— marks appear and clear, the match count counts, Copy fills the clipboard, the
menu opens and closes on Escape, the sticky border arrives only once stuck.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRAMES_JS = Path(__file__).resolve().parents[1] / "app/static/frames.js"
FRAMES_CSS = Path(__file__).resolve().parents[1] / "app/static/frames.css"


def source() -> str:
    return FRAMES_JS.read_text()


def frame_bodies() -> dict[str, str]:
    """Each `FRAMES.x = async function x(...) { … };`, by name.

    Taken to the frame's own closing `};` in column 0, which this file's
    formatting guarantees — splitting on the next assignment instead swept
    up the helper functions that sit between frames, and those helpers' own
    code reads as unreachable when it is not.
    """
    text = source()
    lines = text.splitlines(keepends=True)
    bodies: dict[str, str] = {}
    name: str | None = None
    collected: list[str] = []
    for line in lines:
        opening = re.match(r"^FRAMES\.(\w+) = ", line)
        if opening and name is None:
            name = opening.group(1)
            collected = [line]
            continue
        if name is None:
            continue
        collected.append(line)
        if line.rstrip("\n") == "};":
            bodies[name] = "".join(collected)
            name = None
    assert name is None, f"FRAMES.{name} never closes in column 0"
    return bodies


class TestNoFrameReturnsBeforeItIsWired:
    """The bug, stated as a rule: code after a frame's `return` is dead."""

    @pytest.mark.parametrize("name", sorted(frame_bodies()))
    def test_a_frame_that_builds_a_node_has_no_dead_code_after_its_return(self, name):
        """A `return` at the frame's own indentation ends the function. Any
        statement after it — at that same indentation — never runs.

        This is the whole bug, as a rule. The frame renders, the ids are
        there, the handlers are written, nothing throws, and every control
        is dead.
        """
        body = frame_bodies()[name]
        lines = body.splitlines()
        returned_at = next(
            (i for i, line in enumerate(lines) if re.match(r"^  return\b", line)), None
        )
        if returned_at is None:
            return
        # Find where that return statement ends: it may span lines.
        end = returned_at
        while end < len(lines) and not lines[end].rstrip().endswith(";"):
            end += 1

        unreachable = [
            line
            for line in lines[end + 1 :]
            if line.startswith("  ")
            and line.strip()
            and not line.lstrip().startswith(("//", "*", "/*"))
        ]
        assert not unreachable, (
            f"FRAMES.{name} returns on line {returned_at + 1} of its body and then "
            "runs nothing else. These statements are dead:\n  "
            + "\n  ".join(line.strip()[:90] for line in unreachable[:8])
        )

    def test_the_two_frames_that_had_it_now_bind_their_node(self):
        """Stated positively, so a future rewrite cannot quietly drop it."""
        bodies = frame_bodies()
        for name in ("transcript", "summary"):
            body = bodies[name]
            assert "const node = el(`" in body, name
            assert body.rstrip().endswith("};")
            assert re.search(r"^  return node;$", body, re.M), name

    def test_every_id_a_frame_hands_a_handler_is_an_id_it_draws(self):
        """The other half of the same failure: a handler attached to an id
        that the markup never writes throws on `.addEventListener` of null —
        loud, but only when the frame runs."""
        text = source()
        wired = set(re.findall(r'querySelector\("#([\w-]+)"\)\s*\n?\s*\.?addEventListener', text))
        wired |= set(re.findall(r'node\.querySelector\("#([\w-]+)"\)', text))
        drawn = set(re.findall(r'id="([\w-]+)"', text))
        drawn |= set(re.findall(r'id="\$\{esc\((\w+)\)\}"', text))  # downloadMenu's
        # `downloadMenu(id, …)` writes its own id from the argument.
        drawn |= set(re.findall(r'downloadMenu\("([\w-]+)"', text))
        missing = {name for name in wired if name not in drawn}
        assert not missing, f"wired to ids nothing draws: {sorted(missing)}"


class TestTheTranscriptReader:
    """§7.5. Each assertion names the control and what it has to do."""

    def test_the_in_page_search_is_bound_and_rebuilds_from_the_original_text(self):
        """A highlight applied over a previous highlight is how a search box
        starts eating its own markup, so each keystroke redraws from the
        text captured at mount."""
        body = frame_bodies()["transcript"]
        assert "wireReadBar(node)" in body
        read_bar = source()[source().index("function wireReadBar"):]
        read_bar = read_bar[: read_bar.index("\nfunction escapeRe")]
        assert "const originals = paragraphs.map" in read_bar
        assert 'field.addEventListener("input"' in read_bar
        assert "replaceChildren" in read_bar
        assert 'document.createElement("mark")' in read_bar
        # A needle in nothing says so rather than showing an empty count.
        assert '"none"' in read_bar

    def test_the_needle_is_escaped_before_it_becomes_a_regex(self):
        """Typing `(` into a search field is not a syntax error the person
        should have to see."""
        assert "escapeRe(needle)" in source()
        assert r"value.replace(/[.*+?^${}()|[\]\\]/g" in source()

    def test_copy_carries_the_timestamps_with_the_words(self):
        """What is on the clipboard is what the page shows, moments and all —
        a transcript pasted without its timestamps loses what makes it one."""
        body = frame_bodies()["transcript"]
        assert "`[${stamp(turn.t_ms)}] ${turn.text}`" in body
        assert "copyText(event.currentTarget, plain)" in body

    def test_the_download_menu_offers_only_formats_it_can_produce(self):
        assert 'downloadMenu("tx-download", ["txt", "md", "srt"])' in source()

    def test_a_source_with_no_moments_cannot_become_subtitles_and_says_so(self):
        srt = source()[source().index("function toSrt"):]
        srt = srt[: srt.index("\n/** §7.5")]
        assert "if (!timed.length) return" in srt
        assert "no timeline" in srt

    def test_the_sticky_border_arrives_only_once_stuck(self):
        """§7.5. A border drawn at rest is a divider the design does not have."""
        assert "IntersectionObserver" in source()
        assert "stick-sentinel" in source()
        css = FRAMES_CSS.read_text()
        assert '.read-bar[data-stuck="true"] {' in css
        rest = css[css.index(".read-bar {") : css.index("}", css.index(".read-bar {"))]
        assert "border-bottom" not in rest


class TestTheSummaryReader:
    """§7.6: "the control must change something real"."""

    def test_brief_and_full_are_the_same_record_rendered_twice(self):
        body = frame_bodies()["summary"]
        assert "const BRIEF_POINTS = 5" in body
        assert "data.points.slice(0, BRIEF_POINTS)" in body
        assert "await api(" in body.split("function show(")[0]
        assert "api(" not in body.split("function show(")[1].split("\n  }")[0]

    def test_the_read_time_is_recomputed_from_what_is_on_screen(self):
        body = frame_bodies()["summary"]
        assert "min read" in body
        assert "words / 220" in body

    def test_full_says_so_when_there_is_nothing_more_to_show(self):
        """§6 rule 11 applied to a control that is wired and still has nothing
        to do. A short article yields three points and no chapters, so Full
        and Brief are the same page; it answers instead of pretending."""
        body = frame_bodies()["summary"]
        assert "const nothingMore =" in body
        assert "data.points.length <= BRIEF_POINTS" in body
        assert 'answer(segment, "That is the whole summary")' in body
        # And it must not move the pressed state while refusing.
        refusal = body[body.index('if (segment.dataset.view === "full" && nothingMore)'):]
        assert refusal.index("return;") < refusal.index("aria-pressed")

    def test_a_source_with_no_timeline_says_why_it_has_no_chapters(self):
        assert "no timeline, so it has no chapters" in source()


class TestTheMenusCleanUpAfterThemselves:
    def test_the_document_listeners_are_scoped_to_the_frames_lifetime(self):
        """§4.7's outside-click and Escape are document-level, and a frame is
        replaced on every navigation. Left attached, each visit adds two more
        listeners holding a detached frame alive."""
        wire = source()[source().index("function wireMenus"):]
        wire = wire[: wire.index("\n/** Live filtering")]
        assert "new AbortController()" in wire
        assert "node.isConnected" in wire
        assert "scope.abort()" in wire
        assert wire.count("{ signal: scope.signal }") == 2
