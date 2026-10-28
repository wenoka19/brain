"""Contradiction / refinement linker.

Similarity alone is not a tension: two experts who agree look identical to a
vector index. So the linker runs in two stages — a cheap batched Top-K cosine
probe to build a candidate set, then a classifier that only emits a link when
the *shape* of the pair says something (asymmetric negation -> contradicts,
added boundary condition -> refines).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from app.config import Settings, settings
from app.db import tenant as tenant_db
from app.db.search import neighbours
from app.ingestion.providers import LinkClassifier


@dataclass
class LinkResult:
    created: list[dict[str, Any]]
    candidates_considered: int
    pairs_classified: int


def link_claims(
    conn: sqlite3.Connection,
    new_claims: Sequence[dict[str, Any]],
    *,
    embeddings: dict[str, Sequence[float]],
    classifier: LinkClassifier,
    cfg: Settings | None = None,
) -> LinkResult:
    """Find and persist tensions between ``new_claims`` and the whole library."""
    cfg = cfg or settings()
    created: list[dict[str, Any]] = []
    considered = 0
    classified = 0
    seen_pairs: set[tuple[str, str]] = set()
    new_ids = {claim["id"] for claim in new_claims}

    for claim in new_claims:
        vector = embeddings.get(claim["id"])
        if vector is None:
            continue
        found = neighbours(
            conn,
            vector,
            top_k=cfg.link_top_k,
            exclude_claim_ids=[claim["id"]],
            cfg=cfg,
        )
        considered += len(found)

        for other, similarity in found:
            if similarity < cfg.link_similarity_floor:
                continue
            if other["source_id"] == claim["source_id"]:
                continue  # a source disagreeing with itself is not a tension
            pair = tuple(sorted((claim["id"], other["id"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            # Both sides new? Classify once, in a stable order.
            if other["id"] in new_ids and other["id"] < claim["id"]:
                continue

            classified += 1
            verdict = classifier.classify(_payload(conn, claim), _payload(conn, other))
            if not verdict:
                continue
            link = tenant_db.add_tension_link(
                conn,
                claim_a_id=claim["id"],
                claim_b_id=other["id"],
                link_type=verdict["link_type"],
                divergence_summary=verdict["divergence_summary"],
            )
            if link:
                link["similarity"] = round(float(similarity), 4)
                created.append(link)

    return LinkResult(created=created, candidates_considered=considered, pairs_classified=classified)


def _payload(conn: sqlite3.Connection, claim: dict[str, Any]) -> dict[str, Any]:
    """Give the classifier the speaker/source context it needs for a summary."""
    record = dict(claim)
    if not record.get("source_title"):
        source = tenant_db.get_source(conn, record["source_id"])
        if source:
            record["source_title"] = source["title"]
            if not record.get("speaker"):
                record["speaker"] = source["author"]
    return record
