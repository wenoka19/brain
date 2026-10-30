"""Per-user tenant database (`user_{id}.db`).

Design notes
------------
* Content tables carry both ``rowid INTEGER PRIMARY KEY AUTOINCREMENT`` and
  ``id TEXT UNIQUE`` so FTS5 external-content indexes and sqlite-vec rowids stay
  valid across ``VACUUM``.
* FTS5 indexes are kept in sync by ``AFTER INSERT/UPDATE/DELETE`` triggers.
* ``claims_vec``/``skills_vec`` cannot be filled directly by a trigger on the
  content table (the embedding does not live there), so embeddings land in the
  staging tables ``claim_vectors``/``skill_vectors`` and triggers fan them out to
  the vec0 index. Triggers on the content tables propagate status/source changes
  and deletions, so the vec index is never stale.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import struct
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import sqlite_vec

from app.config import Settings, settings
from app.db.control import PRAGMAS, new_id
from app.storage import r2_sync

EVIDENCE_BASES = ("data", "case_study", "anecdote", "theory", "opinion", "user_axiom")
LOCATOR_TYPES = ("timestamp", "page", "section", "none")
LINK_TYPES = ("contradicts", "refines", "supersedes")
ORIGIN_TYPES = ("distilled", "github_import", "synthesized")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT UNIQUE NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    author        TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT 'en',
    source_type   TEXT NOT NULL,
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    -- `processing` exists from the moment a job is queued (§7.7's three
    -- states), not from the moment a fetch succeeds.
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','processing','blocked','archived','trash')),
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(content_hash);
CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status, created_at DESC);

CREATE TABLE IF NOT EXISTS source_content (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    full_text TEXT NOT NULL,
    cues_json TEXT NOT NULL DEFAULT '[]',
    word_count INTEGER NOT NULL DEFAULT 0
);

-- `MASTER_PLAN.md` §7.6 and §8. One summary per source, rewritten rather than
-- duplicated when a source is re-processed.
--
-- `points` carries `[{text, t_ms}]` because §7.6 is explicit that every point
-- clicks through to its moment in the transcript: "this is what makes a
-- generated summary trustworthy". A point without its timestamp is the thing
-- the section exists to prevent.
--
-- `gist` and `points` are the Brief reading; `chapters` is what Full adds. The
-- segmented control "must change something real" (§7.6), so the two readings
-- are stored separately rather than one being a truncation of the other.
CREATE TABLE IF NOT EXISTS summaries (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT UNIQUE NOT NULL,
    source_id     TEXT NOT NULL UNIQUE REFERENCES sources(id) ON DELETE CASCADE,
    gist          TEXT NOT NULL,
    points_json   TEXT NOT NULL DEFAULT '[]',
    chapters_json TEXT NOT NULL DEFAULT '[]',
    instruction   TEXT NOT NULL DEFAULT '',
    word_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','archived')),
    archived_at   DATETIME,
    archived_by   TEXT NOT NULL DEFAULT '',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_summaries_status ON summaries(status, created_at DESC);

CREATE TABLE IF NOT EXISTS epistemic_claims (
    rowid              INTEGER PRIMARY KEY AUTOINCREMENT,
    id                 TEXT UNIQUE NOT NULL,
    source_id          TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    claim              TEXT NOT NULL,
    speaker            TEXT NOT NULL DEFAULT '',
    evidence_basis     TEXT NOT NULL
                       CHECK (evidence_basis IN ('data','case_study','anecdote',
                                                 'theory','opinion','user_axiom')),
    domain_tags        TEXT NOT NULL DEFAULT '[]',
    when_it_applies    TEXT NOT NULL,
    when_it_fails      TEXT NOT NULL DEFAULT '',
    exact_steps_json   TEXT NOT NULL DEFAULT '[]',
    metrics_and_quotes TEXT NOT NULL DEFAULT '',
    anchor_quote       TEXT NOT NULL,
    locator_type       TEXT NOT NULL DEFAULT 'timestamp'
                       CHECK (locator_type IN ('timestamp','page','section','none')),
    locator_value      TEXT NOT NULL DEFAULT '',
    salience           INTEGER NOT NULL DEFAULT 3
                       CHECK (salience BETWEEN 1 AND 5),
    extractor_version  TEXT NOT NULL DEFAULT 'v1',
    embedding_model    TEXT NOT NULL DEFAULT 'v1',
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','archived')),
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_claims_source ON epistemic_claims(source_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_status ON epistemic_claims(status, salience DESC);

CREATE TABLE IF NOT EXISTS tension_links (
    id                TEXT PRIMARY KEY,
    claim_a_id        TEXT NOT NULL REFERENCES epistemic_claims(id) ON DELETE CASCADE,
    claim_b_id        TEXT NOT NULL REFERENCES epistemic_claims(id) ON DELETE CASCADE,
    link_type         TEXT NOT NULL
                      CHECK (link_type IN ('contradicts','refines','supersedes')),
    divergence_summary TEXT NOT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (claim_a_id, claim_b_id, link_type)
);
CREATE INDEX IF NOT EXISTS idx_tension_a ON tension_links(claim_a_id);
CREATE INDEX IF NOT EXISTS idx_tension_b ON tension_links(claim_b_id);

CREATE TABLE IF NOT EXISTS skills (
    rowid                INTEGER PRIMARY KEY AUTOINCREMENT,
    id                   TEXT UNIQUE NOT NULL,
    slug                 TEXT UNIQUE NOT NULL,
    name                 TEXT NOT NULL,
    description          TEXT NOT NULL,
    trigger_when         TEXT NOT NULL,
    execution_steps_json TEXT NOT NULL,
    guardrails           TEXT NOT NULL DEFAULT '',
    output_template      TEXT NOT NULL DEFAULT '',
    linked_claim_ids     TEXT NOT NULL DEFAULT '[]',
    origin_type          TEXT NOT NULL DEFAULT 'distilled'
                         CHECK (origin_type IN ('distilled','github_import','synthesized')),
    version              INTEGER NOT NULL DEFAULT 1,
    source_id            TEXT REFERENCES sources(id) ON DELETE SET NULL,
    source_locator       TEXT NOT NULL DEFAULT '',
    -- §7.9's four sections. `proposed` is the review queue: a skill Brain
    -- drafted from a source, which §10.10 says must not be reachable until a
    -- person approves it -- "this is why skills require approval; it is a
    -- security control, not a UX nicety". `inactive` is kept but unreachable.
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('proposed','active','inactive','archived')),
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS skill_versions (
    id            TEXT PRIMARY KEY,
    skill_id      TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (skill_id, version)
);

-- Embedding staging tables; triggers fan these out to the vec0 indexes.
CREATE TABLE IF NOT EXISTS claim_vectors (
    claim_rowid INTEGER PRIMARY KEY
                REFERENCES epistemic_claims(rowid) ON DELETE CASCADE,
    embedding   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_vectors (
    skill_rowid INTEGER PRIMARY KEY REFERENCES skills(rowid) ON DELETE CASCADE,
    embedding   BLOB NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    claim, speaker, domain_tags, when_it_applies, when_it_fails,
    metrics_and_quotes, anchor_quote,
    content='epistemic_claims',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, slug, description, trigger_when, execution_steps_json, guardrails,
    content='skills',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
"""

TRIGGERS = """
-- ---- claims -> claims_fts -------------------------------------------------
CREATE TRIGGER IF NOT EXISTS claims_ai AFTER INSERT ON epistemic_claims BEGIN
    INSERT INTO claims_fts(rowid, claim, speaker, domain_tags, when_it_applies,
                           when_it_fails, metrics_and_quotes, anchor_quote)
    VALUES (new.rowid, new.claim, new.speaker, new.domain_tags, new.when_it_applies,
            new.when_it_fails, new.metrics_and_quotes, new.anchor_quote);
END;

CREATE TRIGGER IF NOT EXISTS claims_ad AFTER DELETE ON epistemic_claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, claim, speaker, domain_tags,
                           when_it_applies, when_it_fails, metrics_and_quotes, anchor_quote)
    VALUES ('delete', old.rowid, old.claim, old.speaker, old.domain_tags,
            old.when_it_applies, old.when_it_fails, old.metrics_and_quotes, old.anchor_quote);
END;

CREATE TRIGGER IF NOT EXISTS claims_au AFTER UPDATE ON epistemic_claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, claim, speaker, domain_tags,
                           when_it_applies, when_it_fails, metrics_and_quotes, anchor_quote)
    VALUES ('delete', old.rowid, old.claim, old.speaker, old.domain_tags,
            old.when_it_applies, old.when_it_fails, old.metrics_and_quotes, old.anchor_quote);
    INSERT INTO claims_fts(rowid, claim, speaker, domain_tags, when_it_applies,
                           when_it_fails, metrics_and_quotes, anchor_quote)
    VALUES (new.rowid, new.claim, new.speaker, new.domain_tags, new.when_it_applies,
            new.when_it_fails, new.metrics_and_quotes, new.anchor_quote);
END;

-- ---- claims -> claims_vec (status / source / delete propagation) ---------
CREATE TRIGGER IF NOT EXISTS claims_au_vec AFTER UPDATE ON epistemic_claims BEGIN
    DELETE FROM claims_vec WHERE claim_rowid = new.rowid;
    INSERT INTO claims_vec(claim_rowid, status, source_id, embedding)
    SELECT new.rowid, new.status, new.source_id, v.embedding
    FROM claim_vectors v WHERE v.claim_rowid = new.rowid;
END;

CREATE TRIGGER IF NOT EXISTS claims_ad_vec AFTER DELETE ON epistemic_claims BEGIN
    DELETE FROM claims_vec WHERE claim_rowid = old.rowid;
END;

CREATE TRIGGER IF NOT EXISTS claim_vectors_ai AFTER INSERT ON claim_vectors BEGIN
    DELETE FROM claims_vec WHERE claim_rowid = new.claim_rowid;
    INSERT INTO claims_vec(claim_rowid, status, source_id, embedding)
    SELECT new.claim_rowid, c.status, c.source_id, new.embedding
    FROM epistemic_claims c WHERE c.rowid = new.claim_rowid;
END;

CREATE TRIGGER IF NOT EXISTS claim_vectors_au AFTER UPDATE ON claim_vectors BEGIN
    DELETE FROM claims_vec WHERE claim_rowid = new.claim_rowid;
    INSERT INTO claims_vec(claim_rowid, status, source_id, embedding)
    SELECT new.claim_rowid, c.status, c.source_id, new.embedding
    FROM epistemic_claims c WHERE c.rowid = new.claim_rowid;
END;

CREATE TRIGGER IF NOT EXISTS claim_vectors_ad AFTER DELETE ON claim_vectors BEGIN
    DELETE FROM claims_vec WHERE claim_rowid = old.claim_rowid;
END;

-- ---- skills -> skills_fts ------------------------------------------------
CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid, name, slug, description, trigger_when,
                           execution_steps_json, guardrails)
    VALUES (new.rowid, new.name, new.slug, new.description, new.trigger_when,
            new.execution_steps_json, new.guardrails);
END;

CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, slug, description, trigger_when,
                           execution_steps_json, guardrails)
    VALUES ('delete', old.rowid, old.name, old.slug, old.description, old.trigger_when,
            old.execution_steps_json, old.guardrails);
END;

CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, slug, description, trigger_when,
                           execution_steps_json, guardrails)
    VALUES ('delete', old.rowid, old.name, old.slug, old.description, old.trigger_when,
            old.execution_steps_json, old.guardrails);
    INSERT INTO skills_fts(rowid, name, slug, description, trigger_when,
                           execution_steps_json, guardrails)
    VALUES (new.rowid, new.name, new.slug, new.description, new.trigger_when,
            new.execution_steps_json, new.guardrails);
END;

-- ---- skills -> skills_vec ------------------------------------------------
CREATE TRIGGER IF NOT EXISTS skills_au_vec AFTER UPDATE ON skills BEGIN
    DELETE FROM skills_vec WHERE skill_rowid = new.rowid;
    INSERT INTO skills_vec(skill_rowid, status, slug, embedding)
    SELECT new.rowid, new.status, new.slug, v.embedding
    FROM skill_vectors v WHERE v.skill_rowid = new.rowid;
END;

CREATE TRIGGER IF NOT EXISTS skills_ad_vec AFTER DELETE ON skills BEGIN
    DELETE FROM skills_vec WHERE skill_rowid = old.rowid;
END;

CREATE TRIGGER IF NOT EXISTS skill_vectors_ai AFTER INSERT ON skill_vectors BEGIN
    DELETE FROM skills_vec WHERE skill_rowid = new.skill_rowid;
    INSERT INTO skills_vec(skill_rowid, status, slug, embedding)
    SELECT new.skill_rowid, s.status, s.slug, new.embedding
    FROM skills s WHERE s.rowid = new.skill_rowid;
END;

CREATE TRIGGER IF NOT EXISTS skill_vectors_au AFTER UPDATE ON skill_vectors BEGIN
    DELETE FROM skills_vec WHERE skill_rowid = new.skill_rowid;
    INSERT INTO skills_vec(skill_rowid, status, slug, embedding)
    SELECT new.skill_rowid, s.status, s.slug, new.embedding
    FROM skills s WHERE s.rowid = new.skill_rowid;
END;

CREATE TRIGGER IF NOT EXISTS skill_vectors_ad AFTER DELETE ON skill_vectors BEGIN
    DELETE FROM skills_vec WHERE skill_rowid = old.skill_rowid;
END;
"""


# ---------------------------------------------------------------------------
# embedding serialization
# ---------------------------------------------------------------------------
def pack_embedding(vector: Sequence[float], *, dim: int | None = None) -> bytes:
    dim = dim or settings().embedding_dim
    if len(vector) != dim:
        raise ValueError(f"embedding must have {dim} dims, got {len(vector)}")
    return struct.pack(f"{dim}f", *(float(x) for x in vector))


def unpack_embedding(blob: bytes, *, dim: int | None = None) -> list[float]:
    dim = dim or settings().embedding_dim
    return list(struct.unpack(f"{dim}f", blob))


# ---------------------------------------------------------------------------
# connection cache
# ---------------------------------------------------------------------------
class _LRUConnections:
    """Bounded cache of tenant connections, keyed by resolved path."""

    def __init__(self, max_size: int = 32) -> None:
        self.max_size = max_size
        self._cache: OrderedDict[str, sqlite3.Connection] = OrderedDict()

    def get(self, path: Path, cfg: Settings) -> sqlite3.Connection:
        key = str(path)
        conn = self._cache.get(key)
        if conn is not None:
            self._cache.move_to_end(key)
            return conn
        conn = _open(path, cfg)
        self._cache[key] = conn
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_size:
            _, evicted = self._cache.popitem(last=False)
            try:
                evicted.close()
            except sqlite3.Error:
                pass
        return conn

    def close_all(self) -> None:
        for conn in self._cache.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._cache.clear()

    def discard(self, path: Path) -> None:
        conn = self._cache.pop(str(path), None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            # The connection may belong to another thread, which sqlite3
            # refuses to let us close. Dropping the reference is enough: the
            # file is about to be unlinked, and POSIX keeps the inode alive
            # for whoever still holds it.
            pass


#: One pool per thread, for the same reason as `control.py`: a sqlite3
#: connection belongs to the thread that opened it.
_pool_local = threading.local()
_pools: list[_LRUConnections] = []
_pools_lock = threading.Lock()


def _pool_for_thread() -> _LRUConnections:
    pool = getattr(_pool_local, "pool", None)
    if pool is None:
        pool = _LRUConnections()
        _pool_local.pool = pool
        with _pools_lock:
            _pools.append(pool)
    return pool


def _open(path: Path, cfg: Settings) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    init_schema(conn, cfg=cfg)
    return conn


def _now() -> str:
    """UTC, in the same shape SQLite's `CURRENT_TIMESTAMP` writes.

    Matching the format matters: `archived_at` is compared and ordered against
    `created_at`, and a column that mixes two formats sorts wrong without ever
    raising.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
#: nothing to a table that already exists, so an existing tenant database needs
#: these added explicitly or every write naming them fails. The same shape as
#: `control.MIGRATIONS` — which tenant.py did not have until the Archive needed
#: to say when something was set aside, and by whom.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("sources", "archived_at", "ALTER TABLE sources ADD COLUMN archived_at DATETIME"),
    ("sources", "archived_by", "ALTER TABLE sources ADD COLUMN archived_by TEXT NOT NULL DEFAULT ''"),
    ("epistemic_claims", "archived_at", "ALTER TABLE epistemic_claims ADD COLUMN archived_at DATETIME"),
    ("epistemic_claims", "archived_by", "ALTER TABLE epistemic_claims ADD COLUMN archived_by TEXT NOT NULL DEFAULT ''"),
    ("skills", "archived_at", "ALTER TABLE skills ADD COLUMN archived_at DATETIME"),
    ("skills", "archived_by", "ALTER TABLE skills ADD COLUMN archived_by TEXT NOT NULL DEFAULT ''"),
    # §7.9 shows a drafted skill's provenance — "How Linear builds software ·
    # 02:24" — so a draft carries where it came from.
    ("skills", "source_id", "ALTER TABLE skills ADD COLUMN source_id TEXT"),
    ("skills", "source_locator", "ALTER TABLE skills ADD COLUMN source_locator TEXT NOT NULL DEFAULT ''"),
    # §7.4 and §7.5 both print a word count. Computing it per render means
    # reading every transcript in full to draw a list of twenty-four rows.
    ("source_content", "word_count", "ALTER TABLE source_content ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0"),
)


#: The rebuild target. Written out rather than derived from `SCHEMA` by string
#: surgery: a rebuild that silently drifts from the real table is worse than a
#: duplicated literal, and `test_redesign_skills.py` asserts the two agree
#: column for column.
SCHEMA_SKILLS_REBUILD = """
CREATE TABLE skills_rebuild (
    rowid                INTEGER PRIMARY KEY AUTOINCREMENT,
    id                   TEXT UNIQUE NOT NULL,
    slug                 TEXT UNIQUE NOT NULL,
    name                 TEXT NOT NULL,
    description          TEXT NOT NULL,
    trigger_when         TEXT NOT NULL,
    execution_steps_json TEXT NOT NULL,
    guardrails           TEXT NOT NULL DEFAULT '',
    output_template      TEXT NOT NULL DEFAULT '',
    linked_claim_ids     TEXT NOT NULL DEFAULT '[]',
    origin_type          TEXT NOT NULL DEFAULT 'distilled'
                         CHECK (origin_type IN ('distilled','github_import','synthesized')),
    version              INTEGER NOT NULL DEFAULT 1,
    source_id            TEXT REFERENCES sources(id) ON DELETE SET NULL,
    source_locator       TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('proposed','active','inactive','archived')),
    archived_at          DATETIME,
    archived_by          TEXT NOT NULL DEFAULT '',
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_SOURCES_REBUILD = """
CREATE TABLE sources_rebuild (
    rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT UNIQUE NOT NULL,
    canonical_url TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    author        TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT 'en',
    source_type   TEXT NOT NULL,
    duration_sec  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','processing','blocked','archived','trash')),
    archived_at   DATETIME,
    archived_by   TEXT NOT NULL DEFAULT '',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SKILL_STATUSES = ("proposed", "active", "inactive", "archived")

#: The one status an MCP client may ever see. §9.2: "Never exposed: anything
#: archived, any inactive skill, any proposed skill."
SKILL_REACHABLE = "active"


def _statements(script: str) -> list[str]:
    """Split a schema script into statements that can run inside a transaction.

    `sqlite3.executescript()` issues a `COMMIT` before it runs — documented,
    and fatal here: it ends the transaction the rebuild depends on, and the
    later `COMMIT` then fails with "no transaction is active". Every statement
    in `TRIGGERS` is a `CREATE TRIGGER ... BEGIN ... END;` block, so the split
    is on `END;` and a plain `;` inside a trigger body is left alone.
    """
    out: list[str] = []
    for chunk in re.split(r"\bEND\s*;", script):
        text = re.sub(r"--[^\n]*", "", chunk).strip()
        if not text:
            continue
        out.append(f"{text}\nEND" if "BEGIN" in text.upper() else text)
    return out


def _check_allows(conn: sqlite3.Connection, table: str, value: str) -> bool:
    """Does `table`'s status CHECK already permit `value`?

    Read off the constraint with comments stripped, not off the raw SQL:
    `sqlite_master` keeps comments, and a comment that mentions the value
    reports a migration as applied on a database that has never seen it.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is None:
        return True
    sql = re.sub(r"--[^\n]*", "", row["sql"] or "")
    match = re.search(r"status[^)]*CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)", sql)
    return value in (match.group(1) if match else "")


def _rebuild_sources_for_processing(conn: sqlite3.Connection) -> bool:
    """Let a source be `processing` before anything has read it.

    `sources` carries no triggers and no FTS or vec index of its own, so this
    is the plain form of the rebuild `skills` needed: copy with rowids, swap,
    recreate the two indexes. The foreign keys that point AT it are the reason
    they go off for the swap.
    """
    if _check_allows(conn, "sources", "blocked"):
        return False

    columns = [r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
    carried = ", ".join(c for c in columns if c != "rowid")

    conn.commit()
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(SCHEMA_SOURCES_REBUILD)
        conn.execute(
            f"INSERT INTO sources_rebuild (rowid, {carried}) "
            f"SELECT rowid, {carried} FROM sources"
        )
        conn.execute("DROP TABLE sources")
        conn.execute("ALTER TABLE sources_rebuild RENAME TO sources")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(content_hash)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status, created_at DESC)"
        )
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise sqlite3.IntegrityError(f"sources rebuild broke {len(broken)} reference(s)")
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = previous_isolation
    return True


def _rebuild_skills_for_the_wider_status(conn: sqlite3.Connection) -> bool:
    """Replace the two-state CHECK on `skills.status` with the four-state one.

    SQLite cannot alter a CHECK, so this is the documented table rebuild. Three
    things make it delicate here, and each is why a step below exists:

    * `skills_fts` and `skills_vec` are keyed on `skills.rowid`, so rowids are
      carried across explicitly. Letting AUTOINCREMENT reassign them would
      silently point every index entry at the wrong skill.
    * The five triggers live with the table and go with it; `TRIGGERS` is
      re-run afterwards, and it is `CREATE TRIGGER IF NOT EXISTS` throughout.
    * `skill_versions` references `skills(id)`, so foreign keys are off for the
      swap and `foreign_key_check` runs before it is allowed to commit.
    """
    # Detect on the CONSTRAINT, not on the word: `sqlite_master.sql` keeps
    # the comments, and this table's own comment explains what `proposed`
    # means. Found by running the migration against a database built on the
    # old schema, which is the only test of a rebuild that means anything.
    if _check_allows(conn, "skills", "proposed"):
        return False

    columns = [r["name"] for r in conn.execute("PRAGMA table_info(skills)").fetchall()]
    carried = ", ".join(c for c in columns if c != "rowid")

    # Flush whatever the ALTER migrations above left open: Python's sqlite3
    # opens an implicit transaction for them, and `BEGIN IMMEDIATE` inside one
    # is an error. `PRAGMA foreign_keys` is also a no-op inside a transaction,
    # which would have quietly left foreign keys ON through the swap.
    conn.commit()

    # Python's sqlite3 runs DDL outside the transaction it manages, so an
    # explicit `BEGIN` here is committed out from under us by the first
    # `DROP TRIGGER` — and the `ROLLBACK` in the handler then fails and masks
    # whatever actually went wrong. Autocommit for the duration, and the
    # transaction is ours alone.
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(SCHEMA_SKILLS_REBUILD)
        conn.execute(
            f"INSERT INTO skills_rebuild (rowid, {carried}) "
            f"SELECT rowid, {carried} FROM skills"
        )
        # Triggers on OTHER tables that reference `skills` must go first.
        # `skill_vectors_ai` and its siblings live on `skill_vectors` and read
        # `skills` in their body: they survive the DROP, and then `ALTER ...
        # RENAME` — which validates every trigger in the schema — fails on
        # them. The ones on `skills` itself are dropped with the table.
        # `TRIGGERS` recreates all of them below, being
        # `CREATE TRIGGER IF NOT EXISTS` throughout.
        dangling = [
            row["name"]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name != 'skills'"
            ).fetchall()
            if "skills" in (row["sql"] or "")
        ]
        for name in dangling:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")

        conn.execute("DROP TABLE skills")
        conn.execute("ALTER TABLE skills_rebuild RENAME TO skills")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(status, updated_at DESC)"
        )
        for statement in _statements(TRIGGERS):
            conn.execute(statement)
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise sqlite3.IntegrityError(f"skills rebuild broke {len(broken)} reference(s)")
        conn.execute("COMMIT")
    except Exception:
        # Never let the rollback's own failure replace the real traceback.
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = previous_isolation
    return True


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply additive column migrations. Idempotent, and safe to run on boot."""
    applied: list[str] = []
    for table, column, statement in MIGRATIONS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not columns or column in columns:
            continue
        conn.execute(statement)
        applied.append(f"{table}.{column}")
    if _rebuild_skills_for_the_wider_status(conn):
        applied.append("skills.status(proposed|inactive)")
    if _rebuild_sources_for_processing(conn):
        applied.append("sources.status(processing)")
    # `R-19`, the cutover: Trash folds into Archive.
    #
    # Trash was a source-level status the shipped control plane at `/` could
    # list and restore from. The redesigned app has no Trash screen and no way
    # to put anything in one — its model is active -> archived -> deleted for
    # good, and §2's 30-day recovery buffer is Archive (`R-14`). So when the
    # old app goes, a row left at `status='trash'` would be reachable by
    # nothing: invisible in every list, restorable from nowhere.
    #
    # Moving them to `archived` is the safe direction. Nothing is destroyed,
    # they appear in §7.12 where the person can restore them or delete them
    # for good, and the attribution says it was them rather than Brain —
    # because it was: they put it in the Trash.
    folded = conn.execute(
        "UPDATE sources SET status = 'archived', "
        "  archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP), "
        "  archived_by = CASE WHEN archived_by = '' THEN 'you' ELSE archived_by END "
        "WHERE status = 'trash'"
    ).rowcount
    if folded:
        applied.append(f"sources.status(trash->archived, {folded} rows)")
    # Backfill the counts the column was added for; a row written before it
    # existed would otherwise read "0 words" forever.
    backfilled = conn.execute(
        "UPDATE source_content SET word_count = "
        "  LENGTH(TRIM(full_text)) - LENGTH(REPLACE(TRIM(full_text), ' ', '')) + 1 "
        "WHERE word_count = 0 AND TRIM(full_text) != ''"
    ).rowcount
    if backfilled:
        applied.append(f"source_content.word_count({backfilled} rows)")
    return applied


def init_schema(conn: sqlite3.Connection, *, cfg: Settings | None = None) -> None:
    cfg = cfg or settings()
    dim = cfg.embedding_dim
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS claims_vec USING vec0("
        "claim_rowid INTEGER PRIMARY KEY, status TEXT, source_id TEXT, "
        f"embedding float[{dim}] distance_metric=cosine)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS skills_vec USING vec0("
        "skill_rowid INTEGER PRIMARY KEY, status TEXT, slug TEXT, "
        f"embedding float[{dim}] distance_metric=cosine)"
    )
    conn.executescript(TRIGGERS)
    migrate(conn)


def tenant(user_id: str, *, cfg: Settings | None = None) -> sqlite3.Connection:
    """Open (or reuse) the tenant DB for a *verified* ``user_id``.

    When object storage is configured and this node has never seen the tenant,
    its database is pulled down from R2 first. That is what lets a cold node —
    a fresh Fly machine, a migrated Hetzner box — serve an existing user
    instead of handing them an empty library.
    """
    cfg = cfg or settings()
    path = cfg.tenant_db_path(user_id)
    if not path.exists():
        r2_sync.hydrate(user_id, path, cfg=cfg)
    r2_sync.mark_accessed(user_id)
    return _pool_for_thread().get(path, cfg)


def close_tenant(user_id: str, *, cfg: Settings | None = None) -> None:
    """Forget every cached connection to one tenant, on every thread.

    Called before the LRU guard unlinks a synced tenant file.
    """
    cfg = cfg or settings()
    path = cfg.tenant_db_path(user_id)
    with _pools_lock:
        pools = list(_pools)
    for pool in pools:
        pool.discard(path)


def close_all() -> None:
    with _pools_lock:
        pools = list(_pools)
    for pool in pools:
        pool.close_all()


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def upsert_source(
    conn: sqlite3.Connection,
    *,
    source_id: str | None = None,
    title: str,
    source_type: str,
    canonical_url: str = "",
    content_hash: str = "",
    author: str = "",
    published_at: str = "",
    language: str = "en",
    duration_sec: int = 0,
    status: str | None = None,
) -> dict[str, Any]:
    """Create or update a source.

    `status` is written only when given, and never on an update: a row that
    reached `active` must not be dragged back to `processing` by the pipeline
    re-reading it, and one the person archived must not be revived by a
    re-process.
    """
    if status is not None and status not in ("active", "processing", "blocked", "archived", "trash"):
        raise ValueError(f"unknown source status: {status}")
    source_id = source_id or new_id("src")
    conn.execute(
        """
        INSERT INTO sources (id, canonical_url, content_hash, title, author,
                             published_at, language, source_type, duration_sec, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'active'))
        ON CONFLICT(id) DO UPDATE SET
            canonical_url = excluded.canonical_url,
            content_hash  = excluded.content_hash,
            title         = excluded.title,
            author        = excluded.author,
            published_at  = excluded.published_at,
            language      = excluded.language,
            duration_sec  = excluded.duration_sec
        """,
        (
            source_id,
            canonical_url,
            content_hash,
            title[:500],
            author[:300],
            published_at[:40],
            language[:16],
            source_type,
            int(duration_sec or 0),
            status,
        ),
    )
    return get_source(conn, source_id)  # type: ignore[return-value]


def get_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def find_source_by_hash(conn: sqlite3.Connection, content_hash: str) -> dict[str, Any] | None:
    if not content_hash:
        return None
    row = conn.execute(
        "SELECT * FROM sources WHERE content_hash = ? AND status != 'trash' LIMIT 1",
        (content_hash,),
    ).fetchone()
    return dict(row) if row else None


def find_source_by_url(conn: sqlite3.Connection, canonical_url: str) -> dict[str, Any] | None:
    if not canonical_url:
        return None
    row = conn.execute(
        "SELECT * FROM sources WHERE canonical_url = ? AND status != 'trash' LIMIT 1",
        (canonical_url,),
    ).fetchone()
    return dict(row) if row else None


def set_source_content(
    conn: sqlite3.Connection, source_id: str, full_text: str, cues: list[dict[str, Any]]
) -> None:
    # Counted once, on write. §7.4 lists two dozen transcripts and prints a
    # word count on each; recomputing would mean reading every transcript in
    # full to draw the list.
    conn.execute(
        "INSERT INTO source_content (source_id, full_text, cues_json, word_count) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET full_text = excluded.full_text, "
        "cues_json = excluded.cues_json, word_count = excluded.word_count",
        (source_id, full_text, json.dumps(cues, ensure_ascii=False), len(full_text.split())),
    )


def list_sources(
    conn: sqlite3.Connection, *, limit: int = 100, active_only: bool = True
) -> list[dict[str, Any]]:
    """§7.7's list, with the yield that closes each meta line.

    "A source with nothing green produced nothing. This is the most useful
    column on the page" — so the counts are part of the read, not a second
    round trip per row.

    Proposed skills count toward the yield: the source did produce them, and
    §7.7 reports what came out rather than what has been approved.
    """
    sql = """
        SELECT s.*,
               (SELECT COUNT(*) FROM epistemic_claims c
                 WHERE c.source_id = s.id AND c.status = 'active') AS rule_count,
               (SELECT COUNT(*) FROM skills k
                 WHERE k.source_id = s.id AND k.status IN ('active','proposed')) AS skill_count,
               (SELECT COUNT(*) FROM summaries su
                 WHERE su.source_id = s.id AND su.status = 'active') AS summary_count,
               (SELECT word_count FROM source_content sc WHERE sc.source_id = s.id) AS word_count
        FROM sources s
    """
    # `user_note` is not a source the person gave Brain — it is the container
    # a hand-written rule hangs from (§7.8's "Written here"). §7.7 lists
    # Link, Document and Image, and a pseudo-source would be a row nobody can
    # open, with no type, no yield and nothing to fix.
    where = ["s.source_type != 'user_note'"]
    if active_only:
        # §7.7 has three states — in memory, `Processing`, `Needs you` — and
        # all three are rows on this page. Filtering to `active` would make a
        # source vanish the moment it was queued and come back when it
        # finished, which is the opposite of what the page is for.
        where.append("s.status IN ('active','processing','blocked')")
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.created_at DESC LIMIT ?"
    return [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]


def list_transcripts(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[dict[str, Any]]:
    """§7.4. A transcript is a source that has readable content.

    It is not an entity of its own — that was the plan's model, not this one
    (`STACK_AUDIT.md` §4) — so the list is the sources whose text was
    captured, which is exactly what the frame shows.
    """
    rows = conn.execute(
        """
        SELECT s.*, sc.word_count,
               (SELECT COUNT(*) FROM summaries su
                 WHERE su.source_id = s.id AND su.status = 'active') AS summary_count
        FROM sources s JOIN source_content sc ON sc.source_id = s.id
        WHERE s.status = 'active' AND s.source_type != 'user_note'
              AND TRIM(sc.full_text) != ''
        ORDER BY s.created_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_transcript(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    """§7.5's reading surface: the source, its text, and its cues."""
    source = get_source(conn, source_id)
    if source is None:
        return None
    content = get_source_content(conn, source_id)
    if content is None:
        return None
    return {
        "source": source,
        "full_text": content["full_text"],
        "cues": content["cues"],
        "word_count": content.get("word_count") or len(content["full_text"].split()),
    }


def list_archive(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    """§7.12. Every archived thing, newest-archived first.

    "Order: by archive date, most recent first. Not creation date — people
    come here to undo something they just did." Which is why `archived_at`
    had to exist before this list could be honest: sorting by `created_at`
    would put the thing they archived a minute ago wherever it happened to
    have been made.

    One heterogeneous list, which is why §7.12's type filter exists here and
    nowhere else.
    """
    rows = conn.execute(
        """
        SELECT 'source' AS kind, id, title AS label, '' AS detail,
               archived_at, archived_by, created_at, canonical_url AS extra
          FROM sources WHERE status = 'archived'
        UNION ALL
        SELECT 'rule', id, claim, when_it_applies, archived_at, archived_by, created_at, ''
          FROM epistemic_claims WHERE status = 'archived'
        UNION ALL
        SELECT 'skill', id, name, description, archived_at, archived_by, created_at, ''
          FROM skills WHERE status = 'archived'
        UNION ALL
        SELECT 'summary', su.id, s.title, su.gist, su.archived_at, su.archived_by,
               su.created_at, ''
          FROM summaries su JOIN sources s ON s.id = su.source_id
         WHERE su.status = 'archived'
        ORDER BY archived_at DESC, created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_rules(
    conn: sqlite3.Connection, *, limit: int = 200, active_only: bool = True
) -> list[dict[str, Any]]:
    """§7.8's two groups, in one read.

    A rule written by the person lives under the synthetic `user_note` source;
    everything else came out of something they saved. That is the whole
    distinction between `My rules` and `From your sources`, and it is already
    in the data rather than needing a new column.
    """
    sql = """
        SELECT c.*, s.title AS source_title, s.source_type, s.canonical_url
        FROM epistemic_claims c JOIN sources s ON s.id = c.source_id
    """
    if active_only:
        sql += " WHERE c.status = 'active'"
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()
    out = []
    for row in rows:
        record = hydrate_claim(row)
        record["written_by_hand"] = row["source_type"] == "user_note"
        out.append(record)
    return out


def get_source_content(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT full_text, cues_json FROM source_content WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None
    return {"full_text": row["full_text"], "cues": json.loads(row["cues_json"] or "[]")}


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------
def insert_claim(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    claim: str,
    when_it_applies: str,
    anchor_quote: str,
    evidence_basis: str = "opinion",
    speaker: str = "",
    domain_tags: Iterable[str] = (),
    when_it_fails: str = "",
    exact_steps: Iterable[str] = (),
    metrics_and_quotes: str = "",
    locator_type: str = "none",
    locator_value: str = "",
    salience: int = 3,
    claim_id: str | None = None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    cfg = cfg or settings()
    if evidence_basis not in EVIDENCE_BASES:
        raise ValueError(f"unknown evidence_basis: {evidence_basis}")
    if locator_type not in LOCATOR_TYPES:
        raise ValueError(f"unknown locator_type: {locator_type}")
    claim_id = claim_id or new_id("clm")
    conn.execute(
        """
        INSERT INTO epistemic_claims (
            id, source_id, claim, speaker, evidence_basis, domain_tags,
            when_it_applies, when_it_fails, exact_steps_json, metrics_and_quotes,
            anchor_quote, locator_type, locator_value, salience,
            extractor_version, embedding_model
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_id,
            source_id,
            claim,
            speaker[:200],
            evidence_basis,
            json.dumps(sorted({t.strip().lower() for t in domain_tags if t.strip()}), ensure_ascii=False),
            when_it_applies,
            when_it_fails,
            json.dumps(list(exact_steps), ensure_ascii=False),
            metrics_and_quotes,
            anchor_quote,
            locator_type,
            str(locator_value),
            max(1, min(5, int(salience))),
            cfg.extractor_version,
            cfg.embedding_model,
        ),
    )
    return get_claim(conn, claim_id)  # type: ignore[return-value]


def get_claim(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM epistemic_claims WHERE id = ?", (claim_id,)).fetchone()
    return hydrate_claim(row) if row else None


def hydrate_claim(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["domain_tags"] = json.loads(record.get("domain_tags") or "[]")
    record["exact_steps"] = json.loads(record.pop("exact_steps_json", "[]") or "[]")
    return record


def set_claim_embedding(
    conn: sqlite3.Connection, claim_id: str, vector: Sequence[float], *, cfg: Settings | None = None
) -> None:
    cfg = cfg or settings()
    row = conn.execute("SELECT rowid FROM epistemic_claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown claim id {claim_id!r}")
    conn.execute(
        "INSERT INTO claim_vectors (claim_rowid, embedding) VALUES (?, ?) "
        "ON CONFLICT(claim_rowid) DO UPDATE SET embedding = excluded.embedding",
        (row["rowid"], pack_embedding(vector, dim=cfg.embedding_dim)),
    )


#: The only two statuses that mean "the person set this aside".
_SET_ASIDE = ("archived", "trash")


def _archival(status: str, actor: str) -> tuple[str | None, str]:
    """When and by whom, or cleared when it is not set aside at all.

    §7.12 orders the Archive by archive date and prints `archived by you` or
    `archived by Brain` beside every row — it calls that attribution the point
    of the page. Neither can be reconstructed after the fact, so both are
    recorded at the moment the status changes.

    **Only for `archived` and `trash`.** The condition used to be "anything
    but active", which quietly stamped `processing` and `blocked` too: a
    source that had just failed to fetch carried an archive date and the words
    `archived by you`, having been archived by nobody. Nothing read it from
    the Archive — that page selects on the status — but a column that says
    `archived_at` has to mean it, and §7.3's blocked row is the one place the
    frame reads a source's own dates.
    """
    if status not in _SET_ASIDE:
        return None, ""
    return _now(), ("brain" if actor == "brain" else "you")


def upsert_summary(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    gist: str,
    points: Sequence[dict[str, Any]],
    chapters: Sequence[dict[str, Any]] = (),
    instruction: str = "",
) -> dict[str, Any]:
    """Write the one summary a source has, replacing any earlier one.

    Re-processing a source (§7.3's overflow menu) must not leave two summaries
    behind — hence the unique `source_id` and the upsert.
    """
    existing = conn.execute(
        "SELECT id FROM summaries WHERE source_id = ?", (source_id,)
    ).fetchone()
    summary_id = existing["id"] if existing else new_id("sum")
    words = len(gist.split()) + sum(len(str(p.get("text", "")).split()) for p in points)

    conn.execute(
        """
        INSERT INTO summaries (id, source_id, gist, points_json, chapters_json,
                               instruction, word_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            gist = excluded.gist,
            points_json = excluded.points_json,
            chapters_json = excluded.chapters_json,
            instruction = excluded.instruction,
            word_count = excluded.word_count,
            status = 'active',
            archived_at = NULL,
            archived_by = '',
            created_at = CURRENT_TIMESTAMP
        """,
        (
            summary_id,
            source_id,
            gist,
            json.dumps(list(points), ensure_ascii=False),
            json.dumps(list(chapters), ensure_ascii=False),
            instruction[:2000],
            words,
        ),
    )
    return get_summary(conn, source_id=source_id)  # type: ignore[return-value]


def hydrate_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["points"] = json.loads(record.pop("points_json", "[]") or "[]")
    record["chapters"] = json.loads(record.pop("chapters_json", "[]") or "[]")
    # §7.6 puts a read time in the meta, and the Brief/Full control changes it.
    # 220 words a minute, floored at one, so a short summary never reads "0 min".
    record["read_minutes"] = max(1, round(record.get("word_count", 0) / 220))
    return record


def get_summary(
    conn: sqlite3.Connection, *, summary_id: str | None = None, source_id: str | None = None
) -> dict[str, Any] | None:
    if summary_id:
        row = conn.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone()
    elif source_id:
        row = conn.execute(
            "SELECT * FROM summaries WHERE source_id = ?", (source_id,)
        ).fetchone()
    else:
        raise ValueError("one of summary_id or source_id is required")
    return hydrate_summary(row) if row else None


def list_summaries(
    conn: sqlite3.Connection, *, limit: int = 50, active_only: bool = True
) -> list[dict[str, Any]]:
    sql = (
        "SELECT su.*, s.title AS source_title, s.source_type, s.canonical_url, "
        "       s.duration_sec "
        "FROM summaries su JOIN sources s ON s.id = su.source_id"
    )
    if active_only:
        sql += " WHERE su.status = 'active' AND s.status = 'active'"
    sql += " ORDER BY su.created_at DESC LIMIT ?"
    return [hydrate_summary(row) for row in conn.execute(sql, (limit,)).fetchall()]


def set_summary_status(
    conn: sqlite3.Connection, summary_id: str, status: str, *, actor: str = "you"
) -> bool:
    if status not in ("active", "archived"):
        raise ValueError(status)
    at, by = _archival(status, actor)
    cur = conn.execute(
        "UPDATE summaries SET status = ?, archived_at = ?, archived_by = ? "
        "WHERE id = ? AND status != ?",
        (status, at, by, summary_id, status),
    )
    return cur.rowcount > 0


def set_claim_status(
    conn: sqlite3.Connection, claim_id: str, status: str, *, actor: str = "you"
) -> bool:
    if status not in ("active", "archived"):
        raise ValueError(status)
    at, by = _archival(status, actor)
    cur = conn.execute(
        "UPDATE epistemic_claims SET status = ?, archived_at = ?, archived_by = ? "
        "WHERE id = ? AND status != ?",
        (status, at, by, claim_id, status),
    )
    return cur.rowcount > 0


def claims_for_source(
    conn: sqlite3.Connection, source_id: str, *, limit: int = 50, active_only: bool = True
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM epistemic_claims WHERE source_id = ?"
    params: list[Any] = [source_id]
    if active_only:
        sql += " AND status = 'active'"
    sql += " ORDER BY salience DESC, rowid ASC LIMIT ?"
    params.append(limit)
    return [hydrate_claim(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# tension links
# ---------------------------------------------------------------------------
def add_tension_link(
    conn: sqlite3.Connection,
    *,
    claim_a_id: str,
    claim_b_id: str,
    link_type: str,
    divergence_summary: str,
) -> dict[str, Any] | None:
    if link_type not in LINK_TYPES:
        raise ValueError(f"unknown link_type: {link_type}")
    if claim_a_id == claim_b_id:
        return None
    link_id = new_id("ten")
    cur = conn.execute(
        "INSERT OR IGNORE INTO tension_links (id, claim_a_id, claim_b_id, link_type, "
        "divergence_summary) VALUES (?, ?, ?, ?, ?)",
        (link_id, claim_a_id, claim_b_id, link_type, divergence_summary[:1000]),
    )
    if cur.rowcount == 0:
        return None
    row = conn.execute("SELECT * FROM tension_links WHERE id = ?", (link_id,)).fetchone()
    return dict(row) if row else None


def tensions_for_claims(conn: sqlite3.Connection, claim_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Return ``{claim_id: [{counterpart claim + link metadata}, ...]}``.

    Links are symmetric for display: a claim hears about a tension whether it
    sits on the ``a`` or the ``b`` side — and when *both* sides are in the same
    result set, both of them get the link (fanned out below).
    """
    if not claim_ids:
        return {}
    wanted = list(dict.fromkeys(claim_ids))
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"""
        SELECT t.id AS link_id, t.link_type, t.divergence_summary,
               t.claim_a_id, t.claim_b_id,
               ca.claim AS a_claim, ca.speaker AS a_speaker,
               ca.evidence_basis AS a_evidence_basis, ca.anchor_quote AS a_anchor_quote,
               ca.locator_type AS a_locator_type, ca.locator_value AS a_locator_value,
               ca.when_it_applies AS a_when_it_applies,
               ca.when_it_fails AS a_when_it_fails,
               ca.metrics_and_quotes AS a_metrics_and_quotes,
               ca.salience AS a_salience, ca.domain_tags AS a_domain_tags,
               ca.status AS a_status, sa.title AS a_source_title,
               sa.canonical_url AS a_source_url, sa.source_type AS a_source_type,
               cb.claim AS b_claim, cb.speaker AS b_speaker,
               cb.evidence_basis AS b_evidence_basis, cb.anchor_quote AS b_anchor_quote,
               cb.locator_type AS b_locator_type, cb.locator_value AS b_locator_value,
               cb.when_it_applies AS b_when_it_applies,
               cb.when_it_fails AS b_when_it_fails,
               cb.metrics_and_quotes AS b_metrics_and_quotes,
               cb.salience AS b_salience, cb.domain_tags AS b_domain_tags,
               cb.status AS b_status, sb.title AS b_source_title,
               sb.canonical_url AS b_source_url, sb.source_type AS b_source_type
        FROM tension_links t
        JOIN epistemic_claims ca ON ca.id = t.claim_a_id
        JOIN epistemic_claims cb ON cb.id = t.claim_b_id
        JOIN sources sa ON sa.id = ca.source_id
        JOIN sources sb ON sb.id = cb.source_id
        WHERE t.claim_a_id IN ({placeholders}) OR t.claim_b_id IN ({placeholders})
        """,
        (*wanted, *wanted),
    ).fetchall()

    wanted_set = set(wanted)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        record = dict(row)
        for near, far in (("a", "b"), ("b", "a")):
            anchor_id = record[f"claim_{near}_id"]
            if anchor_id not in wanted_set:
                continue
            if record[f"{far}_status"] != "active":
                continue  # never surface an archived counterpart
            out.setdefault(anchor_id, []).append(
                {
                    "link_id": record["link_id"],
                    "link_type": record["link_type"],
                    "divergence_summary": record["divergence_summary"],
                    "other_id": record[f"claim_{far}_id"],
                    "other_claim": record[f"{far}_claim"],
                    "other_speaker": record[f"{far}_speaker"],
                    "other_evidence_basis": record[f"{far}_evidence_basis"],
                    "other_anchor_quote": record[f"{far}_anchor_quote"],
                    "other_locator_type": record[f"{far}_locator_type"],
                    "other_locator_value": record[f"{far}_locator_value"],
                    "other_when_it_applies": record[f"{far}_when_it_applies"],
                    "other_when_it_fails": record[f"{far}_when_it_fails"],
                    "other_metrics_and_quotes": record[f"{far}_metrics_and_quotes"],
                    "other_salience": record[f"{far}_salience"],
                    "other_domain_tags": json.loads(record[f"{far}_domain_tags"] or "[]"),
                    "other_source_title": record[f"{far}_source_title"],
                    "other_source_url": record[f"{far}_source_url"],
                    "other_source_type": record[f"{far}_source_type"],
                }
            )
    return out


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------
def slugify(raw: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (raw or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    slug = cleaned.strip("-")[:64]
    return slug or f"skill-{uuid.uuid4().hex[:8]}"


def upsert_skill(
    conn: sqlite3.Connection,
    *,
    slug: str,
    name: str,
    description: str,
    trigger_when: str,
    execution_steps: Sequence[str],
    guardrails: str = "",
    output_template: str = "",
    linked_claim_ids: Sequence[str] = (),
    origin_type: str = "distilled",
    status: str = "active",
    source_id: str | None = None,
    source_locator: str = "",
) -> dict[str, Any]:
    """Create or version-bump a skill, snapshotting the previous revision.

    `status` applies to a NEW skill only. An existing one keeps whatever it
    has: §10.10 calls approval a security control, and an edit that promoted a
    draft would be that control with a hole in it.
    """
    if origin_type not in ORIGIN_TYPES:
        raise ValueError(f"unknown origin_type: {origin_type}")
    if status not in SKILL_STATUSES:
        raise ValueError(f"unknown skill status: {status}")
    slug = slugify(slug)
    steps_json = json.dumps([s for s in execution_steps if str(s).strip()], ensure_ascii=False)
    links_json = json.dumps(list(linked_claim_ids), ensure_ascii=False)

    existing = conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
    if existing is None:
        skill_id = new_id("skl")
        conn.execute(
            """
            INSERT INTO skills (id, slug, name, description, trigger_when,
                                execution_steps_json, guardrails, output_template,
                                linked_claim_ids, origin_type, status,
                                source_id, source_locator)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                slug,
                name[:200],
                description,
                trigger_when,
                steps_json,
                guardrails,
                output_template,
                links_json,
                origin_type,
                status,
                source_id,
                source_locator,
            ),
        )
    else:
        skill_id = existing["id"]
        conn.execute(
            "INSERT OR IGNORE INTO skill_versions (id, skill_id, version, snapshot_json) "
            "VALUES (?, ?, ?, ?)",
            (
                new_id("skv"),
                skill_id,
                existing["version"],
                json.dumps(dict(existing), ensure_ascii=False, default=str),
            ),
        )
        conn.execute(
            """
            UPDATE skills SET name = ?, description = ?, trigger_when = ?,
                   execution_steps_json = ?, guardrails = ?, output_template = ?,
                   linked_claim_ids = ?, origin_type = ?, version = version + 1,
                   updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                name[:200],
                description,
                trigger_when,
                steps_json,
                guardrails,
                output_template,
                links_json,
                origin_type,
                skill_id,
            ),
        )
    return get_skill(conn, skill_id)  # type: ignore[return-value]


def hydrate_skill(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["execution_steps"] = json.loads(record.pop("execution_steps_json", "[]") or "[]")
    record["linked_claim_ids"] = json.loads(record.get("linked_claim_ids") or "[]")
    return record


def get_skill(conn: sqlite3.Connection, skill_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    return hydrate_skill(row) if row else None


def get_skill_by_slug(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM skills WHERE slug = ?", (slugify(slug),)).fetchone()
    return hydrate_skill(row) if row else None


def set_skill_embedding(
    conn: sqlite3.Connection, skill_id: str, vector: Sequence[float], *, cfg: Settings | None = None
) -> None:
    cfg = cfg or settings()
    row = conn.execute("SELECT rowid FROM skills WHERE id = ?", (skill_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown skill id {skill_id!r}")
    conn.execute(
        "INSERT INTO skill_vectors (skill_rowid, embedding) VALUES (?, ?) "
        "ON CONFLICT(skill_rowid) DO UPDATE SET embedding = excluded.embedding",
        (row["rowid"], pack_embedding(vector, dim=cfg.embedding_dim)),
    )


def list_skills(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str] = ("proposed", "active", "inactive"),
    limit: int = 200,
) -> list[dict[str, Any]]:
    """§7.9's three sections in one read.

    The default deliberately excludes `archived`: that is the Archive's view,
    not this page's. A caller that wants archived skills asks for them.
    """
    unknown = set(statuses) - set(SKILL_STATUSES)
    if unknown:
        raise ValueError(f"unknown skill status: {sorted(unknown)}")
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        # §7.9's expanded body ends with provenance, and a drafted skill's
        # provenance is the source it was drafted from — so the title comes
        # along rather than costing a read per card to draw it.
        f"SELECT sk.*, s.title AS source_title, s.canonical_url AS source_url "
        f"FROM skills sk LEFT JOIN sources s ON s.id = sk.source_id "
        f"WHERE sk.status IN ({placeholders}) "
        "ORDER BY CASE sk.status WHEN 'proposed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, "
        "sk.updated_at DESC LIMIT ?",
        (*statuses, limit),
    ).fetchall()
    return [hydrate_skill(row) for row in rows]


def count_skills_needing_review(conn: sqlite3.Connection) -> int:
    """What the sidebar's amber pill counts (§5, §18.5): the queue, not the
    library. When it reaches zero the pill becomes a plain total."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM skills WHERE status = 'proposed'"
    ).fetchone()["n"]


def set_skill_status(
    conn: sqlite3.Connection, skill_id: str, status: str, *, actor: str = "you"
) -> bool:
    if status not in SKILL_STATUSES:
        raise ValueError(status)
    at, by = _archival(status, actor)
    cur = conn.execute(
        "UPDATE skills SET status = ?, archived_at = ?, archived_by = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status != ?",
        (status, at, by, skill_id, status),
    )
    return cur.rowcount > 0


def rename_source(conn: sqlite3.Connection, source_id: str, title: str) -> bool:
    """§7.4's overflow `Rename`.

    One UPDATE and nothing else: the title is not denormalised anywhere, and
    search reads it through a join, so there is no index to re-sync. If a
    `sources_fts` is ever added, this is where its trigger has to be checked.
    """
    cur = conn.execute("UPDATE sources SET title = ? WHERE id = ?", (title[:500], source_id))
    return cur.rowcount > 0


def set_source_status(
    conn: sqlite3.Connection, source_id: str, status: str, *, actor: str = "you"
) -> bool:
    if status not in ("active", "processing", "blocked", "archived", "trash"):
        raise ValueError(status)
    at, by = _archival(status, actor)
    cur = conn.execute(
        "UPDATE sources SET status = ?, archived_at = ?, archived_by = ? "
        "WHERE id = ? AND status != ?",
        (status, at, by, source_id, status),
    )
    if cur.rowcount and status in ("archived", "trash"):
        # Archiving a source archives the claims that came out of it — and
        # they carry the same stamp, so the Archive can say who and when for
        # every row rather than only for the one that was clicked.
        conn.execute(
            "UPDATE epistemic_claims SET status = 'archived', archived_at = ?, archived_by = ? "
            "WHERE source_id = ? AND status = 'active'",
            (at, by, source_id),
        )
        # And the summary that came out of it. §7.12's filter offers a
        # `Summary` pill, so an orphan summary of an archived source would sit
        # in the library pointing at nothing.
        conn.execute(
            "UPDATE summaries SET status = 'archived', archived_at = ?, archived_by = ? "
            "WHERE source_id = ? AND status = 'active'",
            (at, by, source_id),
        )
    elif cur.rowcount and status == "active":
        conn.execute(
            "UPDATE epistemic_claims SET status = 'active', archived_at = NULL, archived_by = '' "
            "WHERE source_id = ? AND status = 'archived'",
            (source_id,),
        )
        conn.execute(
            "UPDATE summaries SET status = 'active', archived_at = NULL, archived_by = '' "
            "WHERE source_id = ? AND status = 'archived'",
            (source_id,),
        )
    return cur.rowcount > 0


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        # §5's navigation, where counts live and nowhere else. `sources`
        # excludes the `user_note` container, because §7.7 does not list it.
        "sources": one(
            "SELECT COUNT(*) FROM sources WHERE status = 'active' AND source_type != 'user_note'"
        ),
        "transcripts": one(
            "SELECT COUNT(*) FROM sources s JOIN source_content sc ON sc.source_id = s.id "
            "WHERE s.status = 'active' AND s.source_type != 'user_note' "
            "AND TRIM(sc.full_text) != ''"
        ),
        "summaries": one("SELECT COUNT(*) FROM summaries WHERE status = 'active'"),
        "rules": one("SELECT COUNT(*) FROM epistemic_claims WHERE status = 'active'"),
        # The amber pill shows the queue; the plain count shows the library
        # (§18.5). Both are returned and the frame picks.
        "skills": one("SELECT COUNT(*) FROM skills WHERE status = 'active'"),
        "needs_review": one("SELECT COUNT(*) FROM skills WHERE status = 'proposed'"),
        "archive": one(
            "SELECT (SELECT COUNT(*) FROM epistemic_claims WHERE status = 'archived') "
            "     + (SELECT COUNT(*) FROM skills WHERE status = 'archived') "
            "     + (SELECT COUNT(*) FROM sources WHERE status = 'archived') "
            "     + (SELECT COUNT(*) FROM summaries WHERE status = 'archived')"
        ),
        # Kept: existing callers read these.
        "claims": one("SELECT COUNT(*) FROM epistemic_claims WHERE status = 'active'"),
        "tensions": one("SELECT COUNT(*) FROM tension_links"),
        "archived_claims": one("SELECT COUNT(*) FROM epistemic_claims WHERE status = 'archived'"),
    }
