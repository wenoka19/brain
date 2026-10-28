"""Hybrid retrieval: FTS5 BM25 + sqlite-vec cosine KNN, fused with RRF.

Reciprocal Rank Fusion is used instead of score normalisation because BM25 and
cosine distance live on incomparable scales; RRF only needs the *rank*:

    score(d) = Σ_lists 1 / (k + rank_list(d))        (k = 60)

Every claim hit is returned with its ``tension_links`` already joined, so an
expert disagreement can never be silently dropped at the formatting layer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import Settings, settings
from app.db.tenant import (
    hydrate_claim,
    hydrate_skill,
    pack_embedding,
    tensions_for_claims,
)
from app.security.sanitizer import sanitize_fts_query

SCOPES = ("all", "claims", "skills")


@dataclass
class Hit:
    kind: str  # 'claim' | 'skill'
    id: str
    record: dict[str, Any]
    rrf_score: float = 0.0
    fts_rank: int | None = None
    vec_rank: int | None = None
    vec_similarity: float | None = None
    tensions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def matched_by(self) -> str:
        if self.fts_rank is not None and self.vec_rank is not None:
            return "keyword+semantic"
        if self.fts_rank is not None:
            return "keyword"
        return "semantic"


@dataclass
class SearchResults:
    query: str
    scope: str
    claims: list[Hit] = field(default_factory=list)
    skills: list[Hit] = field(default_factory=list)
    fts_expression: str | None = None
    used_vector: bool = False

    @property
    def total(self) -> int:
        return len(self.claims) + len(self.skills)


def _rrf(rank: int, k: int) -> float:
    return 1.0 / (k + rank)


# ---------------------------------------------------------------------------
# claim lanes
# ---------------------------------------------------------------------------
def _claim_fts_lane(
    conn: sqlite3.Connection, expression: str, limit: int
) -> list[tuple[str, dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT c.*, s.title AS source_title, s.canonical_url AS source_url,
               s.source_type AS source_type, s.author AS source_author,
               s.duration_sec AS source_duration_sec, s.published_at AS source_published_at
        FROM claims_fts f
        JOIN epistemic_claims c ON c.rowid = f.rowid
        JOIN sources s ON s.id = c.source_id
        WHERE claims_fts MATCH ?
          AND c.status = 'active'
          AND s.status = 'active'
        ORDER BY f.rank
        LIMIT ?
        """,
        (expression, limit),
    ).fetchall()
    return [(row["id"], hydrate_claim(row)) for row in rows]


def _claim_vec_lane(
    conn: sqlite3.Connection,
    embedding: Sequence[float],
    limit: int,
    cfg: Settings,
    *,
    min_similarity: float = 0.0,
) -> list[tuple[str, dict[str, Any], float]]:
    knn = conn.execute(
        """
        SELECT claim_rowid, distance
        FROM claims_vec
        WHERE embedding MATCH ? AND k = ? AND status = 'active'
        ORDER BY distance
        """,
        (pack_embedding(embedding, dim=cfg.embedding_dim), limit),
    ).fetchall()
    if not knn:
        return []
    by_rowid = {int(r["claim_rowid"]): float(r["distance"]) for r in knn}
    placeholders = ",".join("?" * len(by_rowid))
    rows = conn.execute(
        f"""
        SELECT c.*, s.title AS source_title, s.canonical_url AS source_url,
               s.source_type AS source_type, s.author AS source_author,
               s.duration_sec AS source_duration_sec, s.published_at AS source_published_at
        FROM epistemic_claims c
        JOIN sources s ON s.id = c.source_id
        WHERE c.rowid IN ({placeholders})
          AND c.status = 'active'
          AND s.status = 'active'
        """,
        tuple(by_rowid.keys()),
    ).fetchall()
    hydrated = {row["rowid"]: hydrate_claim(row) for row in rows}
    out: list[tuple[str, dict[str, Any], float]] = []
    for rowid, distance in sorted(by_rowid.items(), key=lambda kv: kv[1]):
        record = hydrated.get(rowid)
        similarity = 1.0 - distance
        if record is not None and similarity >= min_similarity:
            out.append((record["id"], record, similarity))
    return out


# ---------------------------------------------------------------------------
# skill lanes
# ---------------------------------------------------------------------------
def _skill_fts_lane(conn: sqlite3.Connection, expression: str, limit: int):
    rows = conn.execute(
        """
        SELECT sk.*
        FROM skills_fts f
        JOIN skills sk ON sk.rowid = f.rowid
        WHERE skills_fts MATCH ? AND sk.status = 'active'
        ORDER BY f.rank
        LIMIT ?
        """,
        (expression, limit),
    ).fetchall()
    return [(row["id"], hydrate_skill(row)) for row in rows]


def _skill_vec_lane(
    conn: sqlite3.Connection,
    embedding: Sequence[float],
    limit: int,
    cfg: Settings,
    *,
    min_similarity: float = 0.0,
):
    knn = conn.execute(
        """
        SELECT skill_rowid, distance
        FROM skills_vec
        WHERE embedding MATCH ? AND k = ? AND status = 'active'
        ORDER BY distance
        """,
        (pack_embedding(embedding, dim=cfg.embedding_dim), limit),
    ).fetchall()
    if not knn:
        return []
    by_rowid = {int(r["skill_rowid"]): float(r["distance"]) for r in knn}
    placeholders = ",".join("?" * len(by_rowid))
    rows = conn.execute(
        f"SELECT * FROM skills WHERE rowid IN ({placeholders}) AND status = 'active'",
        tuple(by_rowid.keys()),
    ).fetchall()
    hydrated = {row["rowid"]: hydrate_skill(row) for row in rows}
    out = []
    for rowid, distance in sorted(by_rowid.items(), key=lambda kv: kv[1]):
        record = hydrated.get(rowid)
        similarity = 1.0 - distance
        if record is not None and similarity >= min_similarity:
            out.append((record["id"], record, similarity))
    return out


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------
def _fuse(
    kind: str,
    fts_lane: list[tuple[str, dict[str, Any]]],
    vec_lane: list[tuple[str, dict[str, Any], float]],
    *,
    k: int,
    limit: int,
) -> list[Hit]:
    hits: dict[str, Hit] = {}

    for rank, (item_id, record) in enumerate(fts_lane, start=1):
        hit = hits.setdefault(item_id, Hit(kind=kind, id=item_id, record=record))
        hit.fts_rank = rank
        hit.rrf_score += _rrf(rank, k)

    for rank, (item_id, record, similarity) in enumerate(vec_lane, start=1):
        hit = hits.get(item_id)
        if hit is None:
            hit = Hit(kind=kind, id=item_id, record=record)
            hits[item_id] = hit
        hit.vec_rank = rank
        hit.vec_similarity = round(similarity, 4)
        hit.rrf_score += _rrf(rank, k)

    ordered = sorted(
        hits.values(),
        key=lambda h: (-h.rrf_score, -(h.record.get("salience") or 0), h.id),
    )
    for hit in ordered:
        hit.rrf_score = round(hit.rrf_score, 6)
    return ordered[:limit]


def hybrid_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    embedding: Sequence[float] | None = None,
    scope: str = "all",
    limit: int | None = None,
    cfg: Settings | None = None,
    attach_tensions: bool = True,
) -> SearchResults:
    """Run both retrieval lanes, fuse with RRF and attach expert tensions."""
    cfg = cfg or settings()
    if scope not in SCOPES:
        scope = "all"
    limit = limit or cfg.search_limit
    expression = sanitize_fts_query(query)
    results = SearchResults(
        query=query,
        scope=scope,
        fts_expression=expression,
        used_vector=embedding is not None,
    )

    want_claims = scope in ("all", "claims")
    want_skills = scope in ("all", "skills")

    if want_claims:
        fts_lane = _claim_fts_lane(conn, expression, cfg.fts_candidates) if expression else []
        vec_lane = (
            _claim_vec_lane(
                conn,
                embedding,
                cfg.vec_candidates,
                cfg,
                min_similarity=cfg.search_similarity_floor,
            )
            if embedding is not None
            else []
        )
        results.claims = _fuse("claim", fts_lane, vec_lane, k=cfg.rrf_k, limit=limit)

    if want_skills:
        fts_lane_s = _skill_fts_lane(conn, expression, cfg.fts_candidates) if expression else []
        vec_lane_s = (
            _skill_vec_lane(
                conn,
                embedding,
                cfg.vec_candidates,
                cfg,
                min_similarity=cfg.search_similarity_floor,
            )
            if embedding is not None
            else []
        )
        results.skills = _fuse("skill", fts_lane_s, vec_lane_s, k=cfg.rrf_k, limit=limit)

    if attach_tensions and results.claims:
        tension_map = tensions_for_claims(conn, [hit.id for hit in results.claims])
        for hit in results.claims:
            hit.tensions = tension_map.get(hit.id, [])

    return results


def neighbours(
    conn: sqlite3.Connection,
    embedding: Sequence[float],
    *,
    top_k: int,
    exclude_claim_ids: Sequence[str] = (),
    cfg: Settings | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Nearest active claims by cosine similarity — the linker's primitive."""
    cfg = cfg or settings()
    excluded = set(exclude_claim_ids)
    lane = _claim_vec_lane(conn, embedding, top_k + len(excluded) + 1, cfg)
    return [(record, similarity) for _id, record, similarity in lane if _id not in excluded][:top_k]
