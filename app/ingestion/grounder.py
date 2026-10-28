"""Quote grounding: no claim enters the library unless its quote is real.

An extractor — heuristic or LLM — can paraphrase, merge or invent a quote. The
grounder treats every ``anchor_quote`` as a *hypothesis* and verifies it against
the fetched text with `rapidfuzz`:

1. Find the best-aligned span in the source (``partial_ratio_alignment``).
2. Reject the claim below a similarity of 88.
3. Replace the hypothesis with the **verbatim span actually found**, so what we
   store and later cite is the source's own words.
4. Derive the exact locator from the cue that contains that span: video seconds
   (``?t=1420``), PDF page (``#page=14``), or section heading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from rapidfuzz import fuzz

from app.config import Settings, settings

__all__ = ["GroundedQuote", "ground_quote", "normalise_for_match"]


def normalise_for_match(text: str) -> str:
    """Lowercase + collapse whitespace, so formatting never breaks a match."""
    return " ".join((text or "").lower().split())


@dataclass(frozen=True)
class GroundedQuote:
    grounded: bool
    score: float
    anchor_quote: str = ""
    locator_type: str = "none"
    locator_value: str = ""
    cue_index: int | None = None
    reason: str = ""


def _word_span(text: str, start: int, end: int) -> str:
    """Expand a character span out to whole-word boundaries."""
    length = len(text)
    start = max(0, min(start, length))
    end = max(start, min(end, length))
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < length and not text[end].isspace():
        end += 1
    return text[start:end].strip()


def _locator_for_cue(cue: dict[str, Any], fallback: str) -> tuple[str, str]:
    if "start" in cue:
        return "timestamp", str(int(cue["start"]))
    if "page" in cue:
        return "page", str(int(cue["page"]))
    if cue.get("section"):
        return "section", str(cue["section"])[:120]
    return (fallback, "") if fallback != "none" else ("none", "")


def _best_cue(
    needle: str, cues: Sequence[dict[str, Any]], threshold: float
) -> tuple[int | None, float, str]:
    """Return the cue whose text best contains the needle."""
    best_index: int | None = None
    best_score = 0.0
    best_span = ""
    for index, cue in enumerate(cues):
        haystack = normalise_for_match(cue.get("text", ""))
        if not haystack:
            continue
        score = fuzz.partial_ratio(needle, haystack)
        if score > best_score:
            alignment = fuzz.partial_ratio_alignment(needle, haystack)
            span = (
                _word_span(cue["text"], alignment.dest_start, alignment.dest_end)
                if alignment
                else cue["text"]
            )
            best_index, best_score, best_span = index, score, span
            if score >= 99.5:
                break
    return best_index, best_score, best_span


def ground_quote(
    anchor_quote: str,
    *,
    full_text: str,
    cues: Sequence[dict[str, Any]] = (),
    default_locator_type: str = "none",
    cfg: Settings | None = None,
) -> GroundedQuote:
    """Verify one quote and resolve its locator."""
    cfg = cfg or settings()
    threshold = cfg.grounding_threshold

    needle = normalise_for_match(anchor_quote)
    if not needle:
        return GroundedQuote(grounded=False, score=0.0, reason="empty_quote")

    word_count = len(needle.split())
    if word_count < cfg.anchor_min_words:
        return GroundedQuote(
            grounded=False,
            score=0.0,
            reason=f"too_short ({word_count} words < {cfg.anchor_min_words})",
        )

    # Cue-level matching first: it gives both the verbatim span and the locator.
    cue_index, cue_score, cue_span = _best_cue(needle, cues, threshold)
    if cue_index is not None and cue_score >= threshold:
        cue = cues[cue_index]
        locator_type, locator_value = _locator_for_cue(cue, default_locator_type)
        verbatim = " ".join(cue_span.split()[: cfg.anchor_max_words]) or cue["text"]
        return GroundedQuote(
            grounded=True,
            score=round(float(cue_score), 2),
            anchor_quote=verbatim,
            locator_type=locator_type,
            locator_value=locator_value,
            cue_index=cue_index,
        )

    # Fall back to the whole document: the quote may straddle two cues.
    haystack = normalise_for_match(full_text)
    if not haystack:
        return GroundedQuote(
            grounded=False, score=round(float(cue_score), 2), reason="no_source_text"
        )

    doc_score = fuzz.partial_ratio(needle, haystack)
    if doc_score < threshold:
        return GroundedQuote(
            grounded=False,
            score=round(float(max(doc_score, cue_score)), 2),
            reason=f"below_threshold ({max(doc_score, cue_score):.0f} < {threshold})",
        )

    alignment = fuzz.partial_ratio_alignment(needle, haystack)
    span = _word_span(full_text, alignment.dest_start, alignment.dest_end) if alignment else ""
    verbatim = " ".join((span or anchor_quote).split()[: cfg.anchor_max_words])
    locator_type, locator_value = (
        ("section", str(cues[0].get("section", ""))[:120])
        if cues and cues[0].get("section")
        else (default_locator_type if default_locator_type != "timestamp" else "none", "")
    )
    return GroundedQuote(
        grounded=True,
        score=round(float(doc_score), 2),
        anchor_quote=verbatim,
        locator_type=locator_type,
        locator_value=locator_value,
        cue_index=None,
    )
