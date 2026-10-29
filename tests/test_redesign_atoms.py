"""The redesign, layer 2 — the atoms of `MASTER_PLAN.md` §3.

What this file guards is not "does the CSS parse" but the four rules that make
a design system hold together once more than one person touches it:

* §14.1 — no colour literal outside `:root`. Every colour resolves through §2.
* §6 rule 9 — a transition names its property. `transition: all` is how an
  unrelated layout change starts animating two releases later.
* §6 rule 10 — every interactive style is doubled with `[data-force~="..."]`,
  so a hover state can be held and screenshotted without a real pointer.
* §6 rule 15 — `prefers-reduced-motion` collapses everything.

Plus the one that catches typos no human will: every `var(--x)` used here must
be declared in `tokens.css`. A dangling custom property does not error, it just
renders as nothing, and nothing is very hard to see.

The sizes are measured in a real browser by `tests/browser/atoms_audit.js`,
because `height: 18px` on an inline element is valid CSS that silently does not
apply — which is exactly what it caught on `.v-rule`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ATOMS = ROOT / "app/static/atoms.css"
TOKENS = ROOT / "app/static/tokens.css"

COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
VAR_USE = re.compile(r"var\((--[a-z0-9-]+)")
VAR_DECL = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)


def source() -> str:
    return COMMENT.sub("", ATOMS.read_text())


def rules() -> list[tuple[str, str]]:
    """(selector list, declarations) for every rule, at-rules flattened out."""
    text = re.sub(r"@media[^{]*\{", "", source())
    return [(m.group(1).strip(), m.group(2)) for m in RULE.finditer(text)]


# The thirteen atoms §3 names, and the selector each is built on.
ATOM_SELECTORS = {
    "3.1 icon slot": ".slot",
    "3.2 nav item": ".nav-item",
    "3.3 button": ".btn",
    "3.4 icon button": ".icon-btn",
    "3.5 input": ".input",
    "3.6 select": ".select",
    "3.7 switch": ".switch",
    "3.8 status pill": ".pill",
    "3.9 meter": ".meter",
    "3.10 kbd": ".kbd",
    "3.11 divider": ".divider",
    "3.12 action link": ".action-link",
    "3.13 code block": ".code",
}


class TestTheAtomsExist:
    def test_the_file_is_served(self):
        from app import webapi

        assert ATOMS.is_file()
        assert "/atoms.css" in {getattr(r, "path", None) for r in webapi.ROUTES}

    @pytest.mark.parametrize("atom,selector", sorted(ATOM_SELECTORS.items()))
    def test_every_atom_in_section_3_is_built(self, atom, selector):
        assert re.search(rf"{re.escape(selector)}\s*[,{{]", source()), atom

    def test_every_button_variant_exists(self):
        """§3.3 names five. A missing one shows up as an unstyled button, which
        reads as "someone forgot a class" rather than "the variant is absent"."""
        for variant in ("primary", "secondary", "ghost", "danger", "danger-solid"):
            assert f'[data-variant="{variant}"]' in source(), variant

    def test_the_pill_has_three_states_and_red_is_not_one(self):
        """§3.8: three states and only three. "Red is never a pill." """
        states = set(re.findall(r'\.pill\[data-state="([a-z]+)"\]', source()))
        assert states == {"active", "pending", "archived"}
        pill_rules = [d for sel, d in rules() if sel.startswith(".pill[")]
        for decls in pill_rules:
            assert "--danger" not in decls and "--red-" not in decls


class TestTheRulesOfSection6:
    def test_no_colour_literal_escapes_the_token_layer(self):
        """§14.1. The data-URI chevron of §3.6 is the one exception: an SVG
        stroke cannot take a custom property from outside the document."""
        text = source()
        text = re.sub(r'url\("data:[^"]*"\)', "", text)
        assert not HEX.findall(text), f"colour literals in atoms.css: {HEX.findall(text)}"
        assert "rgb(" not in text and "hsl(" not in text

    def test_no_transition_names_all(self):
        """§6 rule 9."""
        for selector, decls in rules():
            for line in decls.splitlines():
                if "transition" in line:
                    assert not re.search(r"transition(-property)?\s*:\s*all", line), selector

    @pytest.mark.parametrize("state", ["hover", "focus"])
    def test_every_interactive_style_is_doubled(self, state):
        """§6 rule 10: `.x:hover, .x[data-force~="hover"]`.

        `:focus-visible` is exempt — it is the global keyboard treatment and
        cannot be forced on; `::placeholder` is not a state.
        """
        for selector, _ in rules():
            if f":{state}" not in selector:
                continue
            if state == "focus" and ":focus-visible" in selector and ":focus," not in selector:
                continue
            assert f'data-force~="{state}"' in selector, (
                f"{selector.strip()} has :{state} with no data-force twin"
            )

    def test_reduced_motion_collapses_everything(self):
        """§6 rule 15."""
        text = ATOMS.read_text()
        assert "prefers-reduced-motion: reduce" in text
        block = text[text.index("prefers-reduced-motion"):]
        assert "transition-duration: 0.01ms !important" in block
        assert "animation-duration: 0.01ms !important" in block


class TestTheAtomsRestOnTheTokens:
    def test_every_custom_property_used_here_is_declared_there(self):
        """A dangling `var(--x)` does not error. It renders as nothing."""
        declared = set(VAR_DECL.findall(TOKENS.read_text()))
        used = set(VAR_USE.findall(source()))
        missing = used - declared
        assert not missing, f"atoms.css uses undeclared tokens: {sorted(missing)}"

    def test_the_only_primitives_reached_for_are_the_three_that_are_justified(self):
        """§2 says rules name semantics, not primitives. Three exceptions are
        documented in the file header, and this pins the list so a fourth
        cannot slip in quietly."""
        primitives = {
            "--grey-100", "--grey-150", "--grey-200", "--white", "--ink-900",
            "--blue-600", "--blue-200", "--amber-100", "--amber-700",
            "--green-100", "--green-700", "--red-100", "--red-600", "--red-700",
        }
        reached = primitives & set(VAR_USE.findall(source()))
        assert reached == {"--ink-900", "--white", "--red-700"}, sorted(reached)

    def test_the_icon_contract_is_never_broken(self):
        """§3.1: "Never set an icon width directly." Only `.slot > svg` and
        `.icon` may size a glyph, and both take `--icon-size`."""
        # `.icon` as a whole class, not as a prefix: `.icon-btn` is a 32x32
        # button (§3.4), not a glyph, and matching it as one was a false
        # positive that made this test fail on correct CSS.
        glyph = re.compile(r"(\bsvg\b|\.icon(?![\w-]))")
        for selector, decls in rules():
            if not glyph.search(selector):
                continue
            for prop in ("width", "height"):
                for value in re.findall(rf"^\s*{prop}\s*:\s*([^;]+);", decls, re.MULTILINE):
                    assert "--icon-size" in value or "--slot-size" in value, (
                        f"{selector.strip()} sizes a glyph with {value.strip()}"
                    )

    def test_the_slot_is_what_guarantees_one_axis(self):
        decls = dict(rules())[".slot"]
        assert "place-items: center" in decls
        assert "var(--slot-size)" in decls
