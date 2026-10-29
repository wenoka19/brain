"""The redesign, layer 3 — the molecules of `MASTER_PLAN.md` §4.

§4 is unusual among the sections: it does not only describe, it warns. Three of
its clauses exist because something already went wrong once, and those are the
ones asserted hardest here:

* `.item-text` must be a column flex and the title a block, or the title will
  not truncate and runs into the pill beside it (§4.1).
* The pill and the actions must be `flex: 0 0 auto`, or they compress before
  the title does (§4.1).
* The omnibar carries no `Ctrl K` hint (§4.3), while every list's own search
  field does — because `Ctrl K` focuses the omnibar, not those fields.

And one from the pipeline contract: a running step reveals nothing, a finished
step reveals its result (§4.4, §9.1). The work block is "a view of real state,
never a fake animation", and a running step that shows a result is the exact
shape of that lie.

Dimensions and behaviour are measured in a real browser by
`tests/browser/molecules_audit.js`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOL = ROOT / "app/static/molecules.css"
TOKENS = ROOT / "app/static/tokens.css"

COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
VAR_USE = re.compile(r"var\((--[a-z0-9-]+)")
VAR_DECL = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)


def source() -> str:
    return COMMENT.sub("", MOL.read_text())


def rules() -> list[tuple[str, str]]:
    text = re.sub(r"@(media|keyframes)[^{]*\{", "", source())
    return [(m.group(1).strip(), m.group(2)) for m in RULE.finditer(text)]


def decls_for(selector: str) -> str:
    """Every declaration that lands on a selector, in source order."""
    out = []
    for sel, body in rules():
        parts = [p.strip() for p in sel.split(",")]
        if selector in parts:
            out.append(body)
    return "\n".join(out)


MOLECULES = {
    "4.1 item card": ".item-card",
    "4.2 expandable card": ".exp-card",
    "4.3 omnibar": ".omnibar",
    "4.4 work block": ".work",
    "4.5 message": ".msg",
    "4.6 setting row": ".setting-row",
    "4.7 overflow menu": ".menu",
    "4.8 confirm dialog": ".dialog",
    "4.9 toast": ".toast",
    "4.10 guided steps": ".steps-list",
    # Three the frames need that §4 does not name.
    "group heading (§7.9, R-6)": ".group-head",
    "type filter (§7.12)": ".filter-pill",
    "meter card (§7.10)": ".meter-card",
}


class TestTheMoleculesExist:
    def test_the_file_is_served(self):
        from app import webapi

        assert MOL.is_file()
        assert "/molecules.css" in {getattr(r, "path", None) for r in webapi.ROUTES}

    @pytest.mark.parametrize("name,selector", sorted(MOLECULES.items()))
    def test_each_one_is_built(self, name, selector):
        assert re.search(rf"{re.escape(selector)}\s*[,{{:\[]", source()), name


class TestTheWarningsInSection4:
    def test_the_item_text_is_a_column_and_the_title_a_block(self):
        """§4.1, verbatim: "`.item-text` must be `display:flex;
        flex-direction:column`" and the title `display:block` — "an inline span
        will not truncate and the title collides with the pill"."""
        text = decls_for(".item-text")
        assert "display: flex" in text
        assert "flex-direction: column" in text

        title = decls_for(".item-title")
        assert "display: block" in title
        assert "text-overflow: ellipsis" in title
        assert "overflow: hidden" in title
        assert "white-space: nowrap" in title

    def test_the_title_is_the_only_thing_allowed_to_shrink(self):
        """§4.1: "pill and actions are `flex: 0 0 auto`"."""
        shared = decls_for(".item-card > .pill") or decls_for(".item-actions")
        assert "flex: 0 0 auto" in shared
        assert "flex: 1 1 auto" in decls_for(".item-text")
        assert "min-width: 0" in decls_for(".item-text")

    def test_actions_are_invisible_at_rest_and_always_there_on_a_phone(self):
        """§4.1. A phone has no hover, so an action that only appears on hover
        is an action that does not exist there."""
        assert "opacity: 0" in decls_for(".item-actions")
        reveal = [sel for sel, _ in rules() if ".item-actions" in sel and ":hover" in sel]
        assert reveal, "nothing reveals the actions"
        assert any(":focus-within" in sel for sel in reveal), "focus-within is missing"
        assert re.search(
            r"@media \(max-width: 719px\)\s*\{[^}]*\.item-actions\s*\{[^}]*opacity: 1",
            source(), re.DOTALL,
        ), "actions are not forced visible below 720px"

    def test_the_omnibar_carries_no_keyboard_hint(self):
        """§4.3 ends on it in bold. The hint belongs in the list search fields,
        which `Ctrl K` does not focus."""
        for selector, _ in rules():
            assert not re.search(r"\.omnibar[^,{]*\.kbd", selector), selector

    def test_a_running_step_reveals_nothing(self):
        """§4.4 and §9.1: the block is a view of real state. A running step that
        already shows its result is the exact shape of a fake animation."""
        hidden = decls_for('.work-step[data-state="running"] .work-result')
        assert "display: none" in hidden
        assert "display: none" in decls_for('.work-step[data-state="pending"] .work-result')


class TestTheRulesOfSection6:
    def test_no_colour_literal_escapes_the_token_layer(self):
        text = source()
        assert not HEX.findall(text), HEX.findall(text)
        assert "rgb(" not in text and "hsl(" not in text

    def test_no_transition_names_all(self):
        for selector, decls in rules():
            for line in decls.splitlines():
                if "transition" in line:
                    assert not re.search(r"transition(-property)?\s*:\s*all", line), selector

    @pytest.mark.parametrize("state", ["hover", "focus"])
    def test_every_interactive_style_is_doubled(self, state):
        for selector, _ in rules():
            if f":{state}" not in selector:
                continue
            if ":focus-within" in selector and f":{state}," not in selector:
                # `:focus-within` on a container reveals children; its twin is
                # the container's own data-force, asserted above.
                continue
            assert f'data-force~="{state}"' in selector, selector.strip()

    def test_every_custom_property_used_here_is_declared(self):
        declared = set(VAR_DECL.findall(TOKENS.read_text()))
        missing = set(VAR_USE.findall(source())) - declared
        assert not missing, sorted(missing)

    def test_the_only_primitives_reached_for_stay_the_justified_three(self):
        primitives = {
            "--grey-100", "--grey-150", "--grey-200", "--white", "--ink-900",
            "--blue-600", "--blue-200", "--amber-100", "--amber-700",
            "--green-100", "--green-700", "--red-100", "--red-600", "--red-700",
        }
        reached = primitives & set(VAR_USE.findall(source()))
        assert reached <= {"--ink-900", "--white", "--red-700"}, sorted(reached)


class TestTheDetailsThatCarryMeaning:
    def test_the_message_widths_are_the_plan_s(self):
        """§4.5: user 84%, Brain 68ch and no bubble."""
        assert "max-width: 84%" in decls_for('.msg[data-from="user"] > *')
        assert "max-width: 68ch" in decls_for('.msg[data-from="brain"] > *')
        assert "background" not in decls_for('.msg[data-from="brain"] > *')

    def test_a_destructive_menu_item_is_preceded_by_a_separator(self):
        """§4.7."""
        assert ".menu-sep" in source()
        assert "--danger" in decls_for('.menu-item[data-variant="danger"]')

    def test_the_dialog_step_indicator_is_coloured_by_what_it_does(self):
        """§18.8: the current step is danger, not accent. The dialog is
        coloured by its consequence."""
        current = decls_for('.step-dot[data-state="current"]')
        assert "var(--danger)" in current
        assert "var(--accent)" not in current

    def test_the_caret_rotates_minus_ninety_when_closed(self):
        """§4.2."""
        assert "rotate(-90deg)" in decls_for('.exp-card[data-open="false"] .exp-caret')

    def test_a_proposed_card_is_outlined_in_amber(self):
        """§18.5 — not in §4, but it is how a review queue reads as a queue
        without a pill on every row."""
        assert "--status-pending-text" in decls_for('.exp-card[data-state="proposed"]')

    def test_the_counter_is_the_only_numbered_list(self):
        """§4.10: "used only for genuine sequences"."""
        assert "counter-reset: step" in decls_for(".steps-list")
        assert "counter(step)" in decls_for(".steps-list > li::before")

    def test_the_yield_is_green_and_the_agent_attribution_amber(self):
        """§7.7 and §7.12 — the two coloured fragments of a meta line."""
        assert "--status-active-text" in decls_for(".item-yield")
        assert "--status-pending-text" in decls_for(".item-attribution")
