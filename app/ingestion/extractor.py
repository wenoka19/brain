"""Semantic chunker + the grounding gate.

The extractor is deliberately split from the grounder: the extractor *proposes*
Toulmin-shaped claims, and nothing it proposes is trusted. Every candidate must
survive :func:`app.ingestion.grounder.ground_quote` before it becomes a claim,
and the stored ``anchor_quote`` is the span the grounder actually found — not
the one the extractor wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.config import Settings, settings
from app.ingestion.grounder import ground_quote
from app.ingestion.providers import ClaimExtractor, FetchedContent
from app.security.sanitizer import clean_text

EVIDENCE_BASES = ("data", "case_study", "anecdote", "theory", "opinion", "user_axiom")

CHUNK_TARGET_CHARS = 1400
CHUNK_MAX_CHARS = 2200


@dataclass
class Chunk:
    text: str
    cue_indices: list[int] = field(default_factory=list)


@dataclass
class Extraction:
    claims: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    rejections: list[dict[str, Any]] = field(default_factory=list)
    chunks: int = 0


def chunk_content(content: FetchedContent, *, target: int = CHUNK_TARGET_CHARS) -> list[Chunk]:
    """Group cues into chunks that respect cue boundaries.

    Chunking on cue boundaries (caption lines, pages, headings) rather than on a
    raw character window is what keeps a quote inside a single locator, so the
    grounder can resolve it to one timestamp or page.
    """
    if not content.cues:
        return _chunk_plain_text(content.full_text, target=target)

    chunks: list[Chunk] = []
    buffer: list[str] = []
    indices: list[int] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, indices, size
        text = clean_text("\n".join(buffer))
        if text:
            chunks.append(Chunk(text=text, cue_indices=list(indices)))
        buffer, indices, size = [], [], 0

    for index, cue in enumerate(content.cues):
        text = (cue.get("text") or "").strip()
        if not text:
            continue
        # A single oversized cue (a dense PDF page) becomes its own chunk.
        if len(text) > CHUNK_MAX_CHARS:
            flush()
            for piece in _split_long(text, target=target):
                chunks.append(Chunk(text=piece, cue_indices=[index]))
            continue
        if size + len(text) > target and buffer:
            flush()
        buffer.append(text)
        indices.append(index)
        size += len(text) + 1
    flush()
    return chunks


def _chunk_plain_text(text: str, *, target: int) -> list[Chunk]:
    return [Chunk(text=piece) for piece in _split_long(clean_text(text), target=target)]


def _split_long(text: str, *, target: int) -> list[str]:
    """Split on paragraph, then sentence boundaries — never mid-word."""
    pieces: list[str] = []
    buffer: list[str] = []
    size = 0
    units = [p for p in text.split("\n") if p.strip()] or [text]
    for unit in units:
        if len(unit) > target * 2:
            for sentence in unit.replace(". ", ".\n").split("\n"):
                if not sentence.strip():
                    continue
                if size + len(sentence) > target and buffer:
                    pieces.append("\n".join(buffer))
                    buffer, size = [], 0
                buffer.append(sentence.strip())
                size += len(sentence)
            continue
        if size + len(unit) > target and buffer:
            pieces.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(unit.strip())
        size += len(unit) + 1
    if buffer:
        pieces.append("\n".join(buffer))
    return [p for p in pieces if p.strip()]


def _coerce_draft(raw: dict[str, Any], *, cfg: Settings) -> dict[str, Any] | None:
    """Validate and clamp one extractor proposal against the claim schema."""
    claim = clean_text(str(raw.get("claim") or ""))[:600]
    anchor = clean_text(str(raw.get("anchor_quote") or ""))
    applies = clean_text(str(raw.get("when_it_applies") or ""))[:300]
    if not claim or not anchor or not applies:
        return None

    basis = str(raw.get("evidence_basis") or "anecdote").strip().lower()
    if basis not in EVIDENCE_BASES or basis == "user_axiom":
        # `user_axiom` is reserved for `remember_this`; an extractor may not claim it.
        basis = "anecdote" if basis not in EVIDENCE_BASES else "opinion"

    tags_raw = raw.get("domain_tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [tags_raw]
    tags = [clean_text(str(t))[:40].lower() for t in tags_raw if clean_text(str(t))][:5]

    steps_raw = raw.get("exact_steps") or []
    if isinstance(steps_raw, str):
        steps_raw = [steps_raw]
    steps = [clean_text(str(s))[:500] for s in steps_raw if clean_text(str(s))][:12]

    try:
        salience = int(raw.get("salience") or 3)
    except (TypeError, ValueError):
        salience = 3

    return {
        "claim": claim,
        "speaker": clean_text(str(raw.get("speaker") or ""))[:200],
        "evidence_basis": basis,
        "domain_tags": tags,
        "when_it_applies": applies,
        "when_it_fails": clean_text(str(raw.get("when_it_fails") or ""))[:300],
        "exact_steps": steps,
        "metrics_and_quotes": clean_text(str(raw.get("metrics_and_quotes") or ""))[:400],
        "anchor_quote": anchor,
        "salience": max(1, min(5, salience)),
    }


def extract_grounded_claims(
    content: FetchedContent,
    *,
    extractor: ClaimExtractor,
    user_note: str = "",
    cfg: Settings | None = None,
) -> Extraction:
    """Chunk, extract, then **verify every quote** before accepting a claim."""
    cfg = cfg or settings()
    chunks = chunk_content(content)
    extraction = Extraction(chunks=len(chunks))
    seen_claims: set[str] = set()

    context = {
        "title": content.title,
        "author": content.author,
        "source_type": content.source_type,
        "user_note": clean_text(user_note)[:500],
        "domain_tags": [],
    }

    for chunk in chunks:
        chunk_cues = [content.cues[i] for i in chunk.cue_indices] if chunk.cue_indices else content.cues
        try:
            proposals = extractor.extract(chunk.text, dict(context)) or []
        except Exception as exc:  # a flaky LLM must not kill the whole job
            extraction.rejected += 1
            extraction.rejections.append({"reason": f"extractor_error: {exc}"})
            continue

        for raw in proposals:
            draft = _coerce_draft(raw if isinstance(raw, dict) else {}, cfg=cfg)
            if draft is None:
                extraction.rejected += 1
                extraction.rejections.append({"reason": "schema_invalid"})
                continue

            fingerprint = draft["claim"].lower()
            if fingerprint in seen_claims:
                continue

            # The grounding gate. A quote that is not in the source is not a quote.
            grounded = ground_quote(
                draft["anchor_quote"],
                full_text=content.full_text,
                cues=chunk_cues,
                default_locator_type=content.locator_type,
                cfg=cfg,
            )
            if not grounded.grounded:
                extraction.rejected += 1
                extraction.rejections.append(
                    {
                        "claim": draft["claim"][:120],
                        "reason": grounded.reason,
                        "score": grounded.score,
                    }
                )
                continue

            seen_claims.add(fingerprint)
            draft["anchor_quote"] = grounded.anchor_quote
            draft["locator_type"] = grounded.locator_type
            draft["locator_value"] = grounded.locator_value
            draft["grounding_score"] = grounded.score
            extraction.claims.append(draft)

    return extraction
