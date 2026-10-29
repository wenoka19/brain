"""The redesign, layer 1 — the token contract of `MASTER_PLAN.md` §2.

`§0` rule 1 is the rule this file enforces: *never invent a design value*. The
plan lists every primitive and every semantic; `app/static/tokens.css` must
contain exactly those, with exactly those values, and nothing else. A literal
that appears outside the primitives block is the failure mode this guards —
it is how a design system quietly stops being one.

The contrast tests at the bottom assert §14.4 directly. They can, because
`OPEN-6` has been ruled on: four tokens moved to the smallest values that clear
the bar, and two were deliberately left below it with their reasons. Those two
are asserted as exceptions, so neither can spread.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "app/static/tokens.css"

DECL = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*(.+?);", re.MULTILINE)
COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def declarations() -> dict[str, str]:
    """The LIGHT layer only.

    The dark theme redeclares a dozen of the same names, and a flat dict would
    let its values silently overwrite the ones under test — which is exactly
    what happened the first time this ran.
    """
    text = TOKENS.read_text()
    light = text[: text.index('[data-theme="dark"]')]
    return {m.group(1): m.group(2).strip() for m in DECL.finditer(light)}


# ---------------------------------------------------------------------------
# §2.1 — the primitives, transcribed from the plan
#
# The dark theme forced the set to GROW. §2.3 says "primitives never change",
# which is kept literally: every value below is byte-for-byte §2.1's. But §2.3
# also says the dark theme redefines the semantic layer *only*, and that cannot
# be done — §2.1 holds no dark surface at all, its darkest value being a mid
# grey-blue. So `DARK_PRIMITIVES` is additive, pinned here, and asserted to be
# exactly this set so it cannot grow quietly.
# ---------------------------------------------------------------------------
PRIMITIVES = {
    # colour
    "--grey-100": "#f5f5f5", "--grey-150": "#f0f0f0", "--grey-200": "#d9d9d9",
    "--white": "#ffffff", "--ink-900": "#343a40",
    "--blue-600": "#1080a6", "--blue-200": "#a2d2ff",
    "--amber-100": "#fbeec9", "--amber-700": "#7a5500",
    "--green-100": "#d9f2e4", "--green-700": "#14663f",
    "--red-100": "#fbe4e1", "--red-600": "#b3372b", "--red-700": "#8a2a22",
    # space
    "--space-1": "4px", "--space-2": "8px", "--space-3": "12px", "--space-4": "16px",
    "--space-5": "20px", "--space-6": "24px", "--space-8": "32px", "--space-10": "40px",
    # radius
    "--radius-sm": "6px", "--radius-md": "8px", "--radius-lg": "24px", "--radius-full": "999px",
    # border
    "--border-width": "1px",
    # type
    "--text-xs": "11px", "--text-sm": "13px", "--text-base": "14px", "--text-lg": "15px",
    "--text-code": "13px", "--text-read": "16px", "--text-lead": "19px",
    "--text-title": "20px", "--text-stat": "22px", "--text-display": "30px",
    "--weight-regular": "400", "--weight-medium": "500", "--weight-semibold": "600",
    "--leading-tight": "1.3", "--leading-normal": "1.5", "--leading-read": "1.65",
    # motion
    "--duration-fast": "120ms", "--duration-base": "160ms", "--duration-slow": "280ms",
    # dimension
    "--sidebar-width": "240px", "--content-max": "880px", "--read-max": "680px",
    "--nav-item-height": "34px", "--card-height": "64px", "--icon-size": "16px",
    "--slot-size": "20px", "--switch-w": "36px", "--switch-h": "21px", "--meter-h": "4px",
}

DARK_PRIMITIVES = {
    # surfaces, from the palette shipped and audited in phase 7, re-stacked so
    # the order mirrors light: sidebar below canvas, card above both.
    "--ink-950": "#0b0b0e", "--ink-925": "#0e0e11",
    "--ink-900-surface": "#16161a", "--ink-915": "#111115", "--ink-890": "#1c1c21",
    # the dark theme's ink
    "--paper-50": "#ececf1",
    # accent and danger, lifted so they read as text on a dark surface
    "--blue-400": "#1bb5e9", "--red-400": "#e3938b",
    # status pairs: a dark tint carrying light text
    "--green-400": "#3dd68c", "--green-900": "#12271e",
    "--amber-400": "#f0c168", "--amber-900": "#2e2413",
    "--grey-400": "#b8b8c2",
}

# §2.2 — each semantic and the primitive it must reference, with its opacity.
SEMANTICS = {
    "--bg-base": ("--grey-200", None), "--bg-canvas": ("--grey-150", None),
    "--bg-card": ("--white", None), "--bg-code": ("--grey-150", None),
    "--bg-hover": ("--grey-100", None), "--bg-selected": ("--white", None),
    "--bg-field": ("--white", None), "--bg-overlay": ("--ink-900", 32),
    "--text-primary": ("--ink-900", None),
    "--text-secondary": ("--ink-900", 77), "--text-muted": ("--ink-900", 45),
    "--border-subtle": ("--ink-900", 9), "--border-control": ("--ink-900", 56),
    "--icon-default": ("--ink-900", None), "--icon-muted": ("--ink-900", 59),
    "--on-accent": ("--white", None), "--on-danger": ("--white", None),
    "--accent": ("--blue-600", None), "--accent-soft": ("--blue-200", None),
    "--focus-ring": ("--blue-600", None),
    "--status-active-bg": ("--green-100", None), "--status-active-text": ("--green-700", None),
    "--status-pending-bg": ("--amber-100", None), "--status-pending-text": ("--amber-700", None),
    "--danger": ("--red-600", None), "--danger-soft": ("--red-100", None),
    "--switch-off": ("--ink-900", 22), "--meter-track": ("--ink-900", 10),
}


class TestTheTokenContract:
    def test_the_file_exists_and_is_served(self):
        assert TOKENS.is_file()
        from app import webapi

        paths = {getattr(r, "path", None) for r in webapi.ROUTES}
        assert "/tokens.css" in paths

    @pytest.mark.parametrize("name,value", sorted(PRIMITIVES.items()))
    def test_every_primitive_matches_the_plan(self, name, value):
        found = declarations().get(name)
        assert found is not None, f"§2.1 declares {name} and tokens.css does not"
        assert found == value, f"{name}: plan says {value}, tokens.css says {found}"

    @pytest.mark.parametrize("name,expected", sorted(SEMANTICS.items()))
    def test_every_semantic_references_the_primitive_the_plan_assigns(self, name, expected):
        primitive, opacity = expected
        found = declarations().get(name)
        assert found is not None, f"§2.2 declares {name} and tokens.css does not"
        assert f"var({primitive})" in found, f"{name} must reference {primitive}, got {found}"
        if opacity is None:
            assert "color-mix" not in found, f"{name} is a plain reference in §2.2"
        else:
            assert found.startswith("color-mix(in srgb,"), f"{name} must use color-mix (§2.2)"
            assert f"{opacity}%" in found, f"{name} must be {opacity}%, got {found}"
            assert "transparent" in found

    def test_no_literal_escapes_the_primitive_block(self):
        """The whole point of two layers: only primitives may hold a value."""
        text = TOKENS.read_text()
        marker = "2.2 SEMANTICS"
        assert marker in text, "the semantics block is not marked"
        body = text[text.index(marker):]
        # strip comments before looking for literals
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        assert not HEX.findall(body), f"literal colour in the semantics layer: {HEX.findall(body)}"
        assert "rgb(" not in body and "hsl(" not in body

    def test_it_declares_nothing_the_plan_did_not_ask_for(self):
        declared = set(declarations())
        allowed = (
            set(PRIMITIVES) | set(SEMANTICS) | set(DARK_PRIMITIVES)
            | {"--font-sans", "--font-mono", "--ease-standard"}
        )
        extra = declared - allowed
        assert not extra, f"tokens.css invents {sorted(extra)}; §0 rule 1 forbids it"

    @pytest.mark.parametrize("name,value", sorted(DARK_PRIMITIVES.items()))
    def test_every_added_dark_primitive_is_the_one_that_was_pinned(self, name, value):
        """The dark set is additive and closed. A fourteenth value appearing
        here is someone inventing a colour."""
        assert declarations().get(name) == value, name

    def test_the_two_fonts_and_the_easing_are_the_plan_s(self):
        d = declarations()
        assert d["--font-sans"].startswith('"Inter"')
        assert "system-ui" in d["--font-sans"] and "sans-serif" in d["--font-sans"]
        assert d["--font-mono"].startswith("ui-monospace")
        assert d["--ease-standard"] == "cubic-bezier(.2, 0, .2, 1)"

    def test_there_are_two_themes_and_warm_sunset_is_gone(self):
        """`OPEN-1`, closed 29 October 2026. The founder ruled: add dark, and
        no warm sunset. So exactly two themes exist, and the third must not
        reappear — it is still in the shipped `app.css` and still persisted in
        `users.theme_mode`, which is what makes asserting its absence here
        worth doing."""
        text = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(), flags=re.DOTALL)
        assert '[data-theme="dark"]' in text
        assert '[data-theme="sunset"]' not in text
        assert '[data-theme="warm_sunset"]' not in text

    def test_the_dark_theme_redefines_semantics_and_never_a_primitive(self):
        """The half of §2.3's rule that can be kept: a theme may repoint a
        semantic, never restate a value."""
        text = COMMENTS.sub("", TOKENS.read_text())
        block = text[text.index('[data-theme="dark"]'):]
        for name in DECL.findall(block):
            assert name[0] in SEMANTICS, f"the dark theme redeclares {name[0]}"

    def test_tokens_and_the_old_stylesheet_are_never_loaded_together(self):
        """Five names collide across the two files with different values, so a
        document that links both silently recolours itself."""
        html = (ROOT / "app/static/index.html").read_text()
        links = set(re.findall(r'href="/(app\.css|tokens\.css)"', html))
        assert links != {"app.css", "tokens.css"}, (
            "index.html links both stylesheets; --bg-canvas, --text-primary and "
            "--accent are declared in each with different values"
        )


# ---------------------------------------------------------------------------
# §14.4 — measured, not asserted. See OPEN-6.
# ---------------------------------------------------------------------------
def _ratio(fg: str, bg: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def luminance(h: str) -> float:
        h = h.lstrip("#")
        r, g, b = (lin(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _over(ink: str, bg: str, alpha: float) -> str:
    def ch(h, i):
        return int(h.lstrip("#")[i:i + 2], 16)

    return "#%02x%02x%02x" % tuple(
        round(ch(ink, i) * alpha + ch(bg, i) * (1 - alpha)) for i in (0, 2, 4)
    )


class TestAcceptanceCriterion14_4:
    """Text at 4.5:1, control borders at 3:1, in the light theme.

    `OPEN-6`: the plan specified values that could not meet the criterion it
    also specified. The founder ruled on 29 October 2026 — raise what harms
    someone, leave the quiet metadata alone — and these tests hold that line.
    """

    INK, CANVAS, CARD, BASE = "#343a40", "#f0f0f0", "#ffffff", "#d9d9d9"
    ACCENT = "#1080a6"

    @property
    def surfaces(self):
        return {"canvas": self.CANVAS, "card": self.CARD, "sidebar": self.BASE}

    def test_primary_text_clears_4_5_on_every_surface(self):
        for where, bg in self.surfaces.items():
            assert _ratio(self.INK, bg) >= 4.5, where

    def test_secondary_text_clears_4_5_on_every_surface(self):
        """Raised 62% -> 77%. It carries every explanatory sentence, so it is
        body text and the full bar applies — the sidebar is the tight one."""
        for where, bg in self.surfaces.items():
            assert _ratio(_over(self.INK, bg, 0.77), bg) >= 4.5, where

    def test_control_borders_clear_3_1(self):
        """Raised 16% -> 56%. A white field on a near-white canvas is a 1.07:1
        edge, so the border is the only thing locating the control."""
        for where in ("card", "canvas"):
            bg = self.surfaces[where]
            assert _ratio(_over(self.INK, bg, 0.56), bg) >= 3.0, where

    def test_icons_clear_3_1_on_every_surface(self):
        """Raised 55% -> 59%, set by the sidebar, the darkest surface."""
        for where, bg in self.surfaces.items():
            assert _ratio(_over(self.INK, bg, 0.59), bg) >= 3.0, where

    def test_the_accent_works_as_a_fill_and_as_a_ring(self):
        """#118ab2 -> #1080a6. White on the filled primary button is the case
        that forced it."""
        assert _ratio("#ffffff", self.ACCENT) >= 4.5      # white label on a filled button
        assert _ratio(self.ACCENT, self.CARD) >= 4.5      # accent text, on a card
        assert _ratio(self.ACCENT, self.CANVAS) >= 3.0    # focus ring, non-text

    def test_the_accent_is_not_safe_as_text_on_the_canvas(self):
        """Asserted so a future frame cannot quietly do it.

        §2.2 assigns the accent to "primary actions, focus ring, meters" and
        every capture uses it as a fill or a ring. On the canvas it reaches
        only 3.95:1, so accent-coloured text belongs on a card.
        """
        assert _ratio(self.ACCENT, self.CANVAS) < 4.5

    def test_the_three_pills_clear_4_5(self):
        assert _ratio("#14663f", "#d9f2e4") >= 4.5   # active / green
        assert _ratio("#7a5500", "#fbeec9") >= 4.5   # pending / amber
        assert _ratio("#ffffff", "#b3372b") >= 4.5   # danger-solid

    # -- the two deliberate exceptions ------------------------------------
    def test_muted_text_is_the_one_token_below_the_bar_and_stays_contained(self):
        """Left at 45% by the ruling. §2.2 confines it to "labels, metadata,
        placeholders ONLY" and §6 rule 6 forbids a sentence in it. The test
        pins the exception so nobody widens it by raising the value and
        assuming the restriction went away."""
        assert _ratio(_over(self.INK, self.CANVAS, 0.45), self.CANVAS) < 4.5
        source = TOKENS.read_text()
        assert "labels, metadata, placeholders ONLY" in source or "metadata" in source

    def test_the_two_track_fills_are_out_of_scope_not_overlooked(self):
        """`--switch-off` and `--meter-track` sit under a knob and a bar. What
        carries the state is the knob position and the accent fill, which clear
        their own bars; §14.4 governs text and control borders."""
        assert _ratio(_over(self.INK, self.CARD, 0.22), self.CARD) < 3.0
        assert _ratio(_over(self.INK, self.CARD, 0.10), self.CARD) < 3.0
        assert _ratio("#ffffff", self.ACCENT) >= 4.5     # the knob, when on
        assert _ratio(self.ACCENT, _over(self.INK, self.CARD, 0.10)) >= 3.0  # meter fill vs its track


class TestAcceptanceCriterion14_4Dark:
    """§14.4 says "in all three themes". There are two, and this is the second.

    `OPEN-1` was closed on 29 October 2026 by two founder rulings: add dark,
    and drop warm sunset. The palette below was derived rather than picked —
    the light theme's opacity ladder, inverted over dark surfaces — and then
    every pair was measured before it was written down.

    The accent is the part that could not be inverted. A single teal cannot be
    light enough to read as text on a dark card and dark enough to carry a
    white label; so dark lifts the accent and flips the label to ink, which is
    what `--on-accent` exists for.
    """

    BASE, CANVAS, CARD = "#0b0b0e", "#0e0e11", "#16161a"
    PAPER, INK = "#ececf1", "#343a40"
    ACCENT, DANGER = "#1bb5e9", "#e3938b"

    @property
    def surfaces(self):
        return {"sidebar": self.BASE, "canvas": self.CANVAS, "card": self.CARD}

    def test_primary_text_clears_4_5_everywhere(self):
        for where, bg in self.surfaces.items():
            assert _ratio(self.PAPER, bg) >= 4.5, where

    def test_secondary_text_clears_4_5_everywhere(self):
        for where, bg in self.surfaces.items():
            assert _ratio(_over(self.PAPER, bg, 0.77), bg) >= 4.5, where

    def test_icons_clear_3_1_everywhere(self):
        for where, bg in self.surfaces.items():
            assert _ratio(_over(self.PAPER, bg, 0.59), bg) >= 3.0, where

    def test_control_borders_clear_3_1(self):
        for where in ("canvas", "card"):
            bg = self.surfaces[where]
            assert _ratio(_over(self.PAPER, bg, 0.56), bg) >= 3.0, where

    def test_the_accent_reads_as_text_and_carries_an_ink_label(self):
        """Both at once, which the light accent cannot do on dark."""
        for where, bg in self.surfaces.items():
            assert _ratio(self.ACCENT, bg) >= 4.5, where
        assert _ratio(self.INK, self.ACCENT) >= 4.5

    def test_the_light_accent_would_have_failed_here(self):
        """Pinned so nobody 'simplifies' the two accents back into one."""
        assert _ratio("#1080a6", self.CARD) < 4.5

    def test_danger_reads_as_text_and_carries_an_ink_label(self):
        for where, bg in self.surfaces.items():
            assert _ratio(self.DANGER, bg) >= 4.5, where
        assert _ratio(self.INK, self.DANGER) >= 4.5

    def test_the_three_pills_clear_4_5(self):
        assert _ratio("#3dd68c", "#12271e") >= 4.5   # active
        assert _ratio("#f0c168", "#2e2413") >= 4.5   # pending
        assert _ratio("#b8b8c2", "#1c1c21") >= 4.5   # archived

    def test_muted_text_keeps_the_same_role_as_in_light(self):
        """Below the bar in both themes, by the OPEN-6 ruling, and confined to
        metadata in both. It measures better here (3.99 against light's 2.36)
        purely because the same 45% reads further on a dark ground."""
        worst = min(_ratio(_over(self.PAPER, bg, 0.45), bg) for bg in self.surfaces.values())
        assert worst < 4.5
        assert worst > _ratio(_over(self.INK, "#f0f0f0", 0.45), "#f0f0f0")
