"""The redesign, layer 1 — the token contract of `MASTER_PLAN.md` §2.

`§0` rule 1 is the rule this file enforces: *never invent a design value*. The
plan lists every primitive and every semantic; `app/static/tokens.css` must
contain exactly those, with exactly those values, and nothing else. A literal
that appears outside the primitives block is the failure mode this guards —
it is how a design system quietly stops being one.

The contrast test at the bottom does not assert a pass. It records what the
plan's own values measure, because three of them cannot satisfy the plan's own
acceptance criterion (§14.4). See `OPEN-6`. When that is ruled on, this test
becomes an assertion rather than a record.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "app/static/tokens.css"

DECL = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*(.+?);", re.MULTILINE)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def declarations() -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in DECL.finditer(TOKENS.read_text())}


# ---------------------------------------------------------------------------
# §2.1 — the primitives, transcribed from the plan
# ---------------------------------------------------------------------------
PRIMITIVES = {
    # colour
    "--grey-100": "#f5f5f5", "--grey-150": "#f0f0f0", "--grey-200": "#d9d9d9",
    "--white": "#ffffff", "--ink-900": "#343a40",
    "--blue-600": "#118ab2", "--blue-200": "#a2d2ff",
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

# §2.2 — each semantic and the primitive it must reference, with its opacity.
SEMANTICS = {
    "--bg-base": ("--grey-200", None), "--bg-canvas": ("--grey-150", None),
    "--bg-card": ("--white", None), "--bg-code": ("--grey-150", None),
    "--bg-hover": ("--grey-100", None), "--bg-selected": ("--white", None),
    "--bg-field": ("--white", None), "--bg-overlay": ("--ink-900", 32),
    "--text-primary": ("--ink-900", None),
    "--text-secondary": ("--ink-900", 62), "--text-muted": ("--ink-900", 45),
    "--border-subtle": ("--ink-900", 9), "--border-control": ("--ink-900", 16),
    "--icon-default": ("--ink-900", None), "--icon-muted": ("--ink-900", 55),
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
        extra = declared - set(PRIMITIVES) - set(SEMANTICS) - {"--font-sans", "--font-mono", "--ease-standard"}
        assert not extra, f"tokens.css invents {sorted(extra)}; §0 rule 1 forbids it"

    def test_the_two_fonts_and_the_easing_are_the_plan_s(self):
        d = declarations()
        assert d["--font-sans"].startswith('"Inter"')
        assert "system-ui" in d["--font-sans"] and "sans-serif" in d["--font-sans"]
        assert d["--font-mono"].startswith("ui-monospace")
        assert d["--ease-standard"] == "cubic-bezier(.2, 0, .2, 1)"

    def test_dark_and_sunset_are_not_invented(self):
        """§2.3: "dark and sunset palettes are not yet specified. Do not invent
        them." `OPEN-1` is the gate, so the file must not guess at them."""
        text = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(), flags=re.DOTALL)
        assert '[data-theme="dark"]' not in text
        assert '[data-theme="sunset"]' not in text
        assert '[data-theme="warm_sunset"]' not in text

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


class TestMeasuredContrast:
    """What the plan's own values actually measure.

    These are recorded, not judged, so that any later edit to the palette is a
    deliberate act rather than a drift. Three of them are below what §14.4
    demands, which is `OPEN-6`.
    """

    INK, CANVAS, CARD, BASE = "#343a40", "#f0f0f0", "#ffffff", "#d9d9d9"

    def test_primary_text_clears_the_bar_everywhere(self):
        for surface in (self.CANVAS, self.CARD, self.BASE):
            assert _ratio(self.INK, surface) >= 4.5

    def test_the_three_pills_clear_the_bar(self):
        assert _ratio("#14663f", "#d9f2e4") >= 4.5   # active / green
        assert _ratio("#7a5500", "#fbeec9") >= 4.5   # pending / amber
        assert _ratio("#ffffff", "#b3372b") >= 4.5   # danger-solid

    def test_the_shortfalls_are_on_record(self):
        """OPEN-6: the plan's values against the plan's §14.4 criterion."""
        measured = {
            "text-secondary@62% on canvas": round(_ratio(_over(self.INK, self.CANVAS, .62), self.CANVAS), 2),
            "text-muted@45% on canvas": round(_ratio(_over(self.INK, self.CANVAS, .45), self.CANVAS), 2),
            "icon-muted@55% on sidebar": round(_ratio(_over(self.INK, self.BASE, .55), self.BASE), 2),
            "border-control@16% on card": round(_ratio(_over(self.INK, self.CARD, .16), self.CARD), 2),
            "accent on card": round(_ratio("#118ab2", self.CARD), 2),
            "white on accent": round(_ratio("#ffffff", "#118ab2"), 2),
        }
        assert measured == {
            "text-secondary@62% on canvas": 3.54,
            "text-muted@45% on canvas": 2.36,
            "icon-muted@55% on sidebar": 2.75,
            "border-control@16% on card": 1.33,
            "accent on card": 3.96,
            "white on accent": 3.96,
        }, f"the palette changed: {measured}"

    def test_border_control_contradicts_its_own_annotation(self):
        """§2.2 annotates `--border-control` "(MUST pass 3:1)" and specifies a
        value that cannot. This asserts the contradiction so it cannot be
        forgotten; it is deleted when OPEN-6 is ruled on."""
        assert _ratio(_over(self.INK, self.CARD, .16), self.CARD) < 3.0
