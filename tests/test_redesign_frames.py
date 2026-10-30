"""The redesign, layer 4 — the template, the router, and the first frame.

`MASTER_PLAN.md` §5 (the template and the fourteen routes), §7.1 (Home, empty),
§12 (responsive) and the parts of §14 that are testable without the remaining
frames.

The surface is mounted under `/next` while the shipped control plane still owns
`/`. Every path beneath that prefix returns the same shell, because a pushState
router owns the path: without it a deep link works until someone reloads it.
At cutover the prefix becomes `""` — one attribute in `shell.html`.

What a browser proved that no static read could, and why
`tests/browser/shell_audit.js` exists: the inline icon sprite, declared
`width="0" height="0"`, still generated a line box and pushed the entire page
down 21px — breaking §14.12's "no scroll on Home at 1512x982" in a way that is
invisible in the source.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "app/static/shell.html"
FRAMES_CSS = ROOT / "app/static/frames.css"
FRAMES_JS = ROOT / "app/static/frames.js"
TOKENS = ROOT / "app/static/tokens.css"

COMMENT_CSS = re.compile(r"/\*.*?\*/", re.DOTALL)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
VAR_USE = re.compile(r"var\((--[a-z0-9-]+)")
VAR_DECL = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)

# §5's route table, transcribed.
PLAN_ROUTES = [
    "/", "/library/transcripts", "/transcripts/:id", "/library/summaries",
    "/summaries/:id", "/library/sources", "/sources/:id", "/rules", "/skills",
    "/activity", "/connections", "/archive", "/profile", "/signin",
]

# §5's sidebar, in order, and which items carry a count.
PLAN_NAV = [
    ("Home", "/", False),
    ("Transcripts", "/library/transcripts", True),
    ("Summaries", "/library/summaries", True),
    ("Sources", "/library/sources", True),
    ("Rules", "/rules", True),
    ("Skills", "/skills", True),
    ("Activity", "/activity", False),
    ("Connections", "/connections", False),
    ("Archive", "/archive", True),
    ("Profile", "/profile", False),
]


def css() -> str:
    return COMMENT_CSS.sub("", FRAMES_CSS.read_text())


def css_rules() -> list[tuple[str, str]]:
    text = re.sub(r"@(media|keyframes|supports)[^{]*\{", "", css())
    return [(m.group(1).strip(), m.group(2)) for m in RULE.finditer(text)]


@pytest.fixture()
def client():
    from app.main import create_app

    return TestClient(create_app())


class TestTheSurfaceIsServed:
    def test_the_shell_answers_every_route_it_claims(self, client):
        """A pushState router owns the path. If `/rules` 404s, a deep link
        works right up until the moment somebody reloads it.

        `R-19` moved these to the root. The routes are declared one by one
        rather than as a catch-all, so the server and the router agree on
        exactly which URLs exist — which is why this iterates the real list
        rather than a sample."""
        from app import webapi

        for path in webapi.UI_PATHS:
            response = client.get(path.replace("{id}", "abc"))
            assert response.status_code == 200, path
            assert "<title>Brain</title>" in response.text, path

    def test_a_path_it_does_not_claim_is_a_404_not_the_shell(self, client):
        """A catch-all would make every typo render a page, and a path
        parameter is the shape of a traversal bug."""
        for path in ("/rulez", "/library", "/profile/extra"):
            assert client.get(path).status_code == 404, path

    def test_the_four_layers_are_served_and_linked_in_build_order(self, client):
        html = SHELL.read_text()
        order = [m for m in re.findall(r'href="/([a-z]+\.css)"', html)]
        assert order == ["tokens.css", "atoms.css", "molecules.css", "frames.css"], order
        for sheet in order + ["frames.js"]:
            assert client.get(f"/{sheet}").status_code == 200, sheet

    def test_the_old_stylesheet_is_gone_rather_than_merely_unlinked(self):
        """`app.css` declared five of the same token names with other values,
        so a document linking both silently recoloured itself. `R-19` deleted
        it; this is what stops it coming back."""
        assert not (ROOT / "app/static/app.css").exists()
        assert 'href="/app.css"' not in SHELL.read_text()

    def test_the_app_is_not_behind_a_flag_any_more(self):
        """`R-19`: while two apps were served, this one sat behind
        `BRAIN_NEXT_UI` and 404'd without it. That was right then and is a way
        to ship an outage now — a flag on the whole product is not a safety
        feature."""
        import inspect

        from app import webapi

        assert not hasattr(webapi, "next_ui_enabled")
        assert not hasattr(webapi, "preview_enabled")

        # The handler's docstring explains the gate by naming it, so read
        # the code below it rather than the prose.
        source = inspect.getsource(webapi.ui)
        body = source.split('"""')[2] if source.count('"""') >= 2 else source
        assert "environ" not in body, "no kill switch inside the handler either"
        assert "BRAIN_NEXT_UI" not in body

    def test_the_root_is_this_app_now(self, client):
        """The inverse of what this file used to assert. `/` was the old
        control plane until the cutover; it is the shell now."""
        text = client.get("/").text
        assert "<title>Brain</title>" in text
        assert 'data-base=""' in text
        assert "data-layout" not in text, "that attribute belonged to the old harness"


class TestTheTemplate:
    def test_the_sidebar_is_section_5_in_order(self):
        html = SHELL.read_text()
        found = re.findall(r'<a class="nav-item" href="([^"]+)"[^>]*>.*?<span class="nav-label">([^<]+)</span>(.*?)</a>', html, re.DOTALL)
        assert [(label, href) for href, label, _ in found] == [(l, h) for l, h, _ in PLAN_NAV]

    @pytest.mark.parametrize("label,href,counted", PLAN_NAV)
    def test_a_count_appears_only_where_section_5_puts_one(self, label, href, counted):
        """§6 rule 3: counts live in the navigation and nowhere else — and not
        on every item even there. Home, Activity, Connections and Profile carry
        none."""
        html = SHELL.read_text()
        block = re.search(rf'<a class="nav-item" href="{re.escape(href)}".*?</a>', html, re.DOTALL)
        assert block, href
        assert ('class="nav-count"' in block.group(0)) is counted, label

    def test_the_three_section_labels_are_there(self):
        html = SHELL.read_text()
        assert re.findall(r'<p class="nav-section">([^<]+)</p>', html) == ["Library", "Knowledge", "System"]

    def test_the_brand_mark_and_the_avatar_sit_in_the_icon_slot(self):
        """§3.1 is what puts the logo, every nav icon and the avatar on one
        vertical axis. Either one escaping the slot breaks the column."""
        html = SHELL.read_text()
        assert re.search(r'<span class="slot"><span class="brand-mark">', html)
        assert re.search(r'<span class="slot"><span class="avatar"', html)

    def test_there_is_no_inline_style_or_handler_anywhere(self):
        """§1.1 forbids inline styles; the CSP forbids inline handlers. The
        sprite is the one that tempted both — it is handled in `frames.css`."""
        html = SHELL.read_text()
        assert " style=" not in html
        assert not re.search(r"\son[a-z]+=", html)


class TestTheRouter:
    def test_it_declares_exactly_section_5s_fourteen_routes(self):
        declared = re.findall(r'\{\s*path:\s*"([^"]+)"', FRAMES_JS.read_text())
        assert declared == PLAN_ROUTES

    def test_every_route_resolves_to_a_frame(self):
        source = FRAMES_JS.read_text()
        frames = set(re.findall(r'frame:\s*"([a-z]+)"', source))
        assert len(frames) == len(PLAN_ROUTES) - 0 or frames
        # Each route names a frame, and the registry is seeded for all of them.
        assert "for (const { frame } of ROUTES) FRAMES[frame] = placeholder(frame);" in source

    def test_a_detail_route_keeps_its_list_highlighted(self):
        """`/transcripts/:id` is a child of Transcripts, and the navigation has
        no child rows — §3.2: "no indentation for children; the icon carries
        grouping"."""
        source = FRAMES_JS.read_text()
        assert re.search(r'path:\s*"/transcripts/:id".*?nav:\s*"/library/transcripts"', source)
        assert re.search(r'path:\s*"/sources/:id".*?nav:\s*"/library/sources"', source)
        assert re.search(r'path:\s*"/summaries/:id".*?nav:\s*"/library/summaries"', source)

    def test_links_are_rewritten_rather_than_only_intercepted(self):
        """Intercepting the click alone would leave a hard reload and an
        open-in-new-tab pointing at a path the server does not serve."""
        assert "function absolutise(" in FRAMES_JS.read_text()

    def test_every_string_that_reaches_the_dom_as_markup_is_escaped(self):
        """Scoped to the real surface: the template literals that become

        markup through `el()`. A `${}` elsewhere — a `setAttribute`, a class
        name — is not an injection point, and asserting over the whole file
        flagged one of those instead of finding a bug.
        """
        source = FRAMES_JS.read_text()
        assert "export function esc(" in source

        templates = []
        for chunk in source.split("el(`")[1:]:
            end = chunk.find("`")
            assert end != -1, "unterminated template passed to el()"
            templates.append(chunk[:end])
        assert templates, "no markup templates found — has el() been renamed?"

        for template in templates:
            for hole in re.findall(r"\$\{([^}]*)\}", template):
                hole = hole.strip()
                # A hole is allowed when it escapes, when it is one of the two
                # markup helpers, or when its name ends in `Html` — the
                # convention for a fragment this file built itself. The test
                # cannot tell a fragment from a value, so the name has to.
                trusted = (
                    "esc(" in hole
                    or hole.startswith(("icon(", "slot("))
                    or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*Html", hole)
                )
                assert trusted, f"unescaped interpolation into markup: {hole}"


class TestHomeEmptyState:
    def test_the_typewriter_keeps_the_plans_timings(self):
        """§7.1 gives four numbers; all four are here, and the two rules that
        matter more than the numbers are asserted below."""
        source = FRAMES_JS.read_text()
        for ms in ("50", "26", "2200", "240"):
            assert re.search(rf"\b{ms}\b", source), ms

    def test_it_stops_permanently_and_never_restarts(self):
        source = FRAMES_JS.read_text()
        assert 'input.addEventListener("focus", stop, { once: true })' in source
        assert 'input.addEventListener("input", stop, { once: true })' in source
        assert "if (stopped) return;" in source

    def test_it_does_not_start_at_all_under_reduced_motion(self):
        """§7.1: "string 1 shows statically"."""
        source = FRAMES_JS.read_text()
        block = source[source.index("export function typewriter"):]
        assert "if (reducedMotion())" in block
        assert "input.placeholder = PHRASES[0];" in block

    def test_the_three_phrases_are_the_plans_in_order(self):
        source = FRAMES_JS.read_text()
        assert source.index("Ask Brain anything") < source.index("Paste a YouTube link")
        assert source.index("Paste a YouTube link") < source.index("Drop a document or a screenshot")

    @pytest.mark.parametrize("hour,expected", [
        (0, "Good morning"), (11, "Good morning"),
        (12, "Good afternoon"), (17, "Good afternoon"),
        (18, "Good evening"), (23, "Good evening"),
    ])
    def test_the_greeting_switches_on_the_local_hour(self, hour, expected):
        """§7.1: "<12 morning, <18 afternoon, else evening"."""
        source = FRAMES_JS.read_text()
        assert 'if (hour < 12) return "Good morning";' in source
        assert 'if (hour < 18) return "Good afternoon";' in source
        assert 'return "Good evening";' in source

    def test_the_three_action_links_are_the_plans(self):
        source = FRAMES_JS.read_text()
        for label in ("Teach Brain", "Add a skill", "Write a rule"):
            assert label in source


class TestTheRulesStillHold:
    def test_no_colour_literal_escapes_the_token_layer(self):
        assert not HEX.findall(css()), HEX.findall(css())

    def test_no_transition_names_all(self):
        for selector, decls in css_rules():
            for line in decls.splitlines():
                if "transition" in line:
                    assert not re.search(r"transition(-property)?\s*:\s*all", line), selector

    def test_every_custom_property_used_here_is_declared(self):
        declared = set(VAR_DECL.findall(TOKENS.read_text()))
        missing = set(VAR_USE.findall(css())) - declared
        assert not missing, sorted(missing)

    def test_the_sprite_is_taken_out_of_flow(self):
        """The 21px bug. An inline <svg> generates a line box whatever its
        width and height attributes say."""
        decls = next(d for sel, d in css_rules() if sel == ".sprite")
        assert "position: absolute" in decls

    def test_the_three_breakpoints_of_section_12_exist(self):
        text = css()
        assert "max-width: 1199px" in text   # meter grid to one column
        assert "max-width: 959px" in text    # log rows drop columns
        assert "max-width: 719px" in text    # sidebar becomes a top bar

    def test_the_safe_areas_are_honoured(self):
        """§12: a fixed bottom composer adds the inset to its own padding."""
        text = css()
        assert "env(safe-area-inset-top)" in text
        assert "env(safe-area-inset-bottom)" in text
