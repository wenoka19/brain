"""`control.db` — the single source of truth for identity, jobs and audit.

Tenant isolation rule: a tenant database path is *only ever* derived from a
``user_id`` that was read out of this database. Nothing a client sends is ever
interpolated into a filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import Settings, settings

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)

JOB_STATUSES = (
    "queued",
    "fetching",
    "extracting",
    "linking",
    "ready",
    "failed",
    "blocked_by_platform",
)
TERMINAL_STATUSES = ("ready", "failed", "blocked_by_platform")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id            TEXT PRIMARY KEY,
    email              TEXT UNIQUE NOT NULL,
    about_you_context  TEXT NOT NULL DEFAULT '',
    theme_mode         TEXT NOT NULL DEFAULT 'warm_sunset'
                       CHECK (theme_mode IN ('light','dark','warm_sunset')),
    -- `R-18` fossils. Credits replaced them; neither MIGRATIONS tuple has a
    -- DROP precedent and a column nothing reads costs nothing, so they stay
    -- rather than being removed from under an existing control.db.
    media_minutes_used INTEGER NOT NULL DEFAULT 0,
    files_used         INTEGER NOT NULL DEFAULT 0,
    -- `R-18`: the one allowance. Characters of source text, spent at the
    -- gate in `_ingest_content`, reset on the first of the month.
    credits_used       INTEGER NOT NULL DEFAULT 0,
    credits_period     TEXT NOT NULL DEFAULT '',
    keep_uploads       INTEGER NOT NULL DEFAULT 1,
    schema_version     INTEGER NOT NULL DEFAULT 1,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_sessions (
    session_id         TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    client_name        TEXT NOT NULL DEFAULT 'mcp_client',
    access_token_hash  TEXT UNIQUE NOT NULL,
    access_expires_at  DATETIME NOT NULL,
    refresh_token_hash TEXT UNIQUE NOT NULL,
    refresh_expires_at DATETIME NOT NULL,
    audience           TEXT NOT NULL DEFAULT '',
    revoked            INTEGER NOT NULL DEFAULT 0,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_oauth_user ON oauth_sessions(user_id, revoked);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id           TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    source_id        TEXT NOT NULL,
    input_target     TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    user_note        TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','fetching','extracting','linking',
                                       'ready','failed','blocked_by_platform')),
    error_message    TEXT NOT NULL DEFAULT '',
    reserved_minutes INTEGER NOT NULL DEFAULT 0,
    reserved_files   INTEGER NOT NULL DEFAULT 0,
    -- `R-18`: what this job actually spent. It is what makes a retry charge
    -- once and a refund exact — both read it from the ROW, never from the
    -- job dict, which a worker holds as a stale in-memory copy.
    charged_credits  INTEGER NOT NULL DEFAULT 0,
    claims_extracted INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    locked_until     DATETIME,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON ingestion_jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_pending ON ingestion_jobs(status, locked_until);

-- One row per pipeline step, per job. `MASTER_PLAN.md` §9.1: "Each transition
-- writes a `job` row. The UI polls or streams from those rows -- the work block
-- is a view of real state, never a fake animation."
--
-- The job's own `status` column cannot carry this: it holds one value, and the
-- work block of §4.4 shows six, each with its own result line, its own timing
-- and its own failure. `detail` is what a finished step reveals beside its
-- name -- "8,412 words captured", "3 found" -- and it stays empty while the
-- step runs, because §4.4 is explicit that a running step reveals nothing.
CREATE TABLE IF NOT EXISTS job_steps (
    job_id      TEXT NOT NULL REFERENCES ingestion_jobs(job_id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    step        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','running','done','skipped','failed')),
    detail      TEXT NOT NULL DEFAULT '',
    started_at  DATETIME,
    finished_at DATETIME,
    PRIMARY KEY (job_id, step)
);
CREATE INDEX IF NOT EXISTS idx_job_steps ON job_steps(job_id, ordinal);

-- Authorization codes are short-lived (60s), single-use and PKCE-bound. They
-- live in the database rather than in process memory so the token exchange
-- still works behind more than one uvicorn worker.
CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code           TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    client_id      TEXT NOT NULL,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    challenge_method TEXT NOT NULL DEFAULT 'S256',
    scope          TEXT NOT NULL DEFAULT '',
    resource       TEXT NOT NULL DEFAULT '',
    expires_at     DATETIME NOT NULL,
    consumed       INTEGER NOT NULL DEFAULT 0,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- §7.11. "One server. Two ways to authenticate. This is verified behaviour,
-- not a choice." A chat app's connector dialog has no header field, so the
-- key rides in the URL; an editor or an agent framework cannot run a browser
-- sign-in, so it sends a bearer. Both arrive at one validation function.
--
-- Only the SHA-256 digest is stored (§2 rule 4), so a stolen database yields
-- no working key. `key_prefix` is the first few characters, kept so the list
-- can show which key a row is without being able to reconstruct it.
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name          TEXT NOT NULL DEFAULT 'My AI',
    key_hash      TEXT UNIQUE NOT NULL,
    key_prefix    TEXT NOT NULL DEFAULT '',
    last_call_at  DATETIME,
    revoked       INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_connections_user ON connections(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id       TEXT PRIMARY KEY,
    client_name     TEXT NOT NULL DEFAULT 'mcp_client',
    redirect_uris   TEXT NOT NULL DEFAULT '[]',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    client_name TEXT,
    surface     TEXT CHECK (surface IN ('mcp','web_omnibar','web_ui','system')),
    action      TEXT,
    target_id   TEXT,
    status      TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    -- A short, human-readable "why this happened", written for the person
    -- reading their own audit log rather than for a machine.
    reasoning_trace TEXT NOT NULL DEFAULT '',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id, created_at DESC);
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ").split(".")[0].replace("Z", "")
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hash_token(token: str) -> str:
    """Tokens are never stored in plaintext — only their SHA-256 digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def new_user_id() -> str:
    return uuid.uuid4().hex[:20]


def new_token() -> str:
    return secrets.token_urlsafe(40)


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------
#: Connections are cached **per thread**. A single sqlite3 connection must not
#: be shared across threads: ASGI servers run sync work in a threadpool, and two
#: threads interleaving `BEGIN IMMEDIATE` on one connection would corrupt each
#: other's transactions. WAL mode makes one connection per thread cheap.
_local = threading.local()
_registries: list[dict[str, sqlite3.Connection]] = []
_registries_lock = threading.Lock()


def _registry() -> dict[str, sqlite3.Connection]:
    registry = getattr(_local, "connections", None)
    if registry is None:
        registry = {}
        _local.connections = registry
        with _registries_lock:
            _registries.append(registry)
    return registry


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    for pragma in PRAGMAS:
        conn.execute(pragma)


def open_control_db(path: Path | None = None, *, cfg: Settings | None = None) -> sqlite3.Connection:
    cfg = cfg or settings()
    db_path = Path(path) if path else cfg.control_db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
#: nothing to a table that already exists, so an existing `control.db` needs
#: these added explicitly or every write naming them fails.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("audit_logs", "reasoning_trace", "ALTER TABLE audit_logs ADD COLUMN reasoning_trace TEXT NOT NULL DEFAULT ''"),
    ("ingestion_jobs", "reserved_files", "ALTER TABLE ingestion_jobs ADD COLUMN reserved_files INTEGER NOT NULL DEFAULT 0"),
    ("ingestion_jobs", "claims_extracted", "ALTER TABLE ingestion_jobs ADD COLUMN claims_extracted INTEGER NOT NULL DEFAULT 0"),
    # §7.13's Account block: the name Brain calls the person on the home
    # screen, and the language it speaks to them in.
    ("users", "display_name", "ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"),
    ("users", "locale", "ALTER TABLE users ADD COLUMN locale TEXT NOT NULL DEFAULT ''"),
    # §7.13's "What Brain does with new sources". §9.1 says steps 3 to 5
    # honour these, so they are stored as what the pipeline reads — not as
    # three switches that look wired and are not.
    ("users", "want_summary", "ALTER TABLE users ADD COLUMN want_summary INTEGER NOT NULL DEFAULT 1"),
    ("users", "want_rules", "ALTER TABLE users ADD COLUMN want_rules INTEGER NOT NULL DEFAULT 1"),
    ("users", "want_skills", "ALTER TABLE users ADD COLUMN want_skills INTEGER NOT NULL DEFAULT 1"),
    ("users", "write_in", "ALTER TABLE users ADD COLUMN write_in TEXT NOT NULL DEFAULT 'source'"),
    # §7.7's `Fix this` tooltip "states the remedy" — "Make it public, then
    # retry", "No text layer — retype or re-scan". A sentence written for the
    # person cannot be matched on, so the provider's own reason code is kept
    # beside it and the frame maps the code to the remedy.
    ("ingestion_jobs", "error_reason", "ALTER TABLE ingestion_jobs ADD COLUMN error_reason TEXT NOT NULL DEFAULT ''"),
    # §7.10's four columns — `STACK_AUDIT.md` §9.2 row 2, approved. The capture
    # is what made the list exact: a row reads
    #   `14:02 ● Claude Desktop search_rules "planning cycle" 180 ms [OK]`
    # and expands to `Asked for` / `Returned` / `Payload` / `Connection`.
    # Nothing in the shipped `audit_logs` could supply the latency, the size,
    # the one-line result, or which connection made the call.
    ("audit_logs", "latency_ms", "ALTER TABLE audit_logs ADD COLUMN latency_ms INTEGER"),
    ("audit_logs", "payload_bytes", "ALTER TABLE audit_logs ADD COLUMN payload_bytes INTEGER"),
    ("audit_logs", "result_summary", "ALTER TABLE audit_logs ADD COLUMN result_summary TEXT NOT NULL DEFAULT ''"),
    ("audit_logs", "connection_id", "ALTER TABLE audit_logs ADD COLUMN connection_id TEXT NOT NULL DEFAULT ''"),
    # `R-18` — credits replace media minutes as the account's one allowance.
    # `credits_period` is the `YYYY-MM` the counter belongs to; a read in a
    # later month zeroes it. Storing the period rather than a reset timestamp
    # means no cron, no worker and no clock skew: the month a row names is
    # either this one or it is not.
    ("users", "credits_used", "ALTER TABLE users ADD COLUMN credits_used INTEGER NOT NULL DEFAULT 0"),
    ("users", "credits_period", "ALTER TABLE users ADD COLUMN credits_period TEXT NOT NULL DEFAULT ''"),
    ("ingestion_jobs", "charged_credits", "ALTER TABLE ingestion_jobs ADD COLUMN charged_credits INTEGER NOT NULL DEFAULT 0"),
    # `R-18`'s opt-out. On by default, because the 30-day window exists so a
    # failed read can be looked at — turning it off is the person choosing to
    # give that up, not a default imposed on them.
    ("users", "keep_uploads", "ALTER TABLE users ADD COLUMN keep_uploads INTEGER NOT NULL DEFAULT 1"),
)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply additive column migrations. Idempotent, and safe to run on boot."""
    applied: list[str] = []
    for table, column, statement in MIGRATIONS:
        columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns or column in columns:
            continue
        conn.execute(statement)
        applied.append(f"{table}.{column}")
    return applied


def control(path: Path | None = None) -> sqlite3.Connection:
    """Cached control-db handle for the calling thread."""
    cfg = settings()
    key = str(Path(path) if path else cfg.control_db_path)
    registry = _registry()
    conn = registry.get(key)
    if conn is None:
        conn = open_control_db(Path(key), cfg=cfg)
        registry[key] = conn
    return conn


def close_all() -> None:
    """Close every cached handle this process holds, in any thread."""
    with _registries_lock:
        registries = list(_registries)
    for registry in registries:
        for conn in list(registry.values()):
            try:
                conn.close()
            except sqlite3.Error:
                pass  # a handle owned by another thread; it dies with the thread
        registry.clear()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------
def ensure_user(conn: sqlite3.Connection, email: str) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("a valid email is required")
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return dict(row)
    user_id = new_user_id()
    conn.execute(
        "INSERT INTO users (user_id, email) VALUES (?, ?)",
        (user_id, email),
    )
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def get_user(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def delete_user(conn: sqlite3.Connection, user_id: str) -> bool:
    """§7.13's `Delete account`. One row, and the schema does the rest.

    `oauth_sessions`, `ingestion_jobs` and `job_steps` all declare
    `ON DELETE CASCADE` against this row, so they go with it — which is why
    `PRAGMA foreign_keys = ON` is not optional here, and is asserted in
    `tests/test_phase1_db_and_security.py`.

    `audit_logs.user_id` carries no foreign key and is left behind on purpose:
    the rows hold no content, and a deletion that erased its own record would
    not be one the person could later show had happened.
    """
    cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


#: §7.13's "Write them in": the language summaries and rules come out in,
#: whatever the source was.
WRITE_IN_CHOICES = ("source", "en", "fr")


def update_profile(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    about_you_context: str | None = None,
    theme_mode: str | None = None,
    display_name: str | None = None,
    locale: str | None = None,
    want_summary: bool | None = None,
    want_rules: bool | None = None,
    want_skills: bool | None = None,
    keep_uploads: bool | None = None,
    write_in: str | None = None,
) -> dict[str, Any] | None:
    """Write the fields §7.13 lets the person change.

    A value outside its allowed set raises rather than being dropped: the set
    is this module's contract, and silently storing nothing would leave the
    person looking at a switch that did not move.
    """
    sets, params = [], []
    if about_you_context is not None:
        sets.append("about_you_context = ?")
        params.append(about_you_context[:20_000])
    if theme_mode is not None:
        if theme_mode not in ("light", "dark", "warm_sunset"):
            raise ValueError(f"unknown theme_mode: {theme_mode}")
        sets.append("theme_mode = ?")
        params.append(theme_mode)
    if display_name is not None:
        sets.append("display_name = ?")
        params.append(str(display_name).strip()[:120])
    if locale is not None:
        if locale not in ("", "en", "fr"):
            raise ValueError(f"unknown locale: {locale}")
        sets.append("locale = ?")
        params.append(locale)
    if write_in is not None:
        if write_in not in WRITE_IN_CHOICES:
            raise ValueError(f"unknown write_in: {write_in}")
        sets.append("write_in = ?")
        params.append(write_in)
    for name, value in (
        ("want_summary", want_summary),
        ("want_rules", want_rules),
        ("want_skills", want_skills),
        ("keep_uploads", keep_uploads),
    ):
        if value is not None:
            sets.append(f"{name} = ?")
            params.append(1 if value else 0)
    if sets:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)
    return get_user(conn, user_id)


# ---------------------------------------------------------------------------
# credits  (`R-18`) — the account's one allowance
# ---------------------------------------------------------------------------
# Characters of source text, spent at the single gate in `_ingest_content`
# and reset on the first of the month.
#
# **Why a debit and not a reservation.** The quota machinery this replaces
# reserved up-front and released on failure, which it had to: minutes were
# guessable before the fetch (a flat provisional 15 for YouTube, nothing at
# all for an article). Characters are not guessable — `normalize_url` knows
# no duration, no byte count and no page count — but they ARE exactly known
# the moment the fetch returns, which is still before every LLM call. So
# there is nothing to reserve: one atomic debit at the one line where the
# number is real, with the ceiling inside the `WHERE` clause so two
# concurrent jobs cannot both cross it.
#
# **Why the period is a string and not a timestamp.** `credits_period` holds
# the `YYYY-MM` the counter belongs to. A read in a later month zeroes it in
# passing. No cron, no worker, no scheduled reset to miss during a redeploy,
# and no clock-skew window: the month a row names is either this one or it
# is not.


def current_period(now: datetime | None = None) -> str:
    """The `YYYY-MM` a charge belongs to. UTC, so it turns over once."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def _roll_period(conn: sqlite3.Connection, user_id: str, period: str) -> None:
    """Zero the counter if it belongs to a month that is over. Idempotent."""
    conn.execute(
        "UPDATE users SET credits_used = 0, credits_period = ? "
        "WHERE user_id = ? AND credits_period IS NOT ?",
        (period, user_id, period),
    )


def credits_state(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    cfg: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Used, cap and left — after rolling the month if it has turned over."""
    cfg = cfg or settings()
    period = current_period(now)
    with transaction(conn):
        _roll_period(conn, user_id, period)
        row = conn.execute(
            "SELECT COALESCE(credits_used, 0) AS used FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    used = int(row["used"]) if row else 0
    return {
        "used": used,
        "cap": cfg.quota_credits,
        "left": max(0, cfg.quota_credits - used),
        "period": period,
    }


def credits_left(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    cfg: Settings | None = None,
    now: datetime | None = None,
) -> int:
    return credits_state(conn, user_id, cfg=cfg, now=now)["left"]


def debit_credits(
    conn: sqlite3.Connection,
    user_id: str,
    credits: int,
    *,
    cfg: Settings | None = None,
    now: datetime | None = None,
) -> bool:
    """Spend `credits`, or return False and spend nothing.

    The ceiling lives in the `WHERE` clause, so the check and the write are
    one statement and two workers racing the same account cannot both pass.
    A negative is clamped rather than trusted: the quota function this
    replaces guarded with `if minutes and ...`, which a negative slipped
    straight through as a silent refund.
    """
    cfg = cfg or settings()
    credits = max(0, int(credits))
    period = current_period(now)
    with transaction(conn):
        _roll_period(conn, user_id, period)
        cursor = conn.execute(
            "UPDATE users SET credits_used = credits_used + ? "
            "WHERE user_id = ? AND credits_used + ? <= ?",
            (credits, user_id, credits, cfg.quota_credits),
        )
        return cursor.rowcount == 1


def refund_credits(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    credits: int,
    now: datetime | None = None,
) -> None:
    """Give back exactly what a job charged, because a failure costs nothing.

    The month is rolled first, which handles the one awkward case for free: a
    job charged on the 31st and failed on the 1st refunds into a counter that
    is already zero, and `MAX(0, ...)` leaves it there. The person starts the
    new month whole either way.
    """
    credits = max(0, int(credits))
    if not credits:
        return
    with transaction(conn):
        _roll_period(conn, user_id, current_period(now))
        conn.execute(
            "UPDATE users SET credits_used = MAX(0, credits_used - ?) WHERE user_id = ?",
            (credits, user_id),
        )


def set_job_charge(conn: sqlite3.Connection, job_id: str, credits: int) -> None:
    conn.execute(
        "UPDATE ingestion_jobs SET charged_credits = ? WHERE job_id = ?",
        (max(0, int(credits)), job_id),
    )


def job_charge(conn: sqlite3.Connection, job_id: str) -> int:
    """What this job spent, read from the ROW.

    Never from the job dict a worker is holding: that is an in-memory copy
    taken at claim time, and the charge is written after it.
    """
    row = conn.execute(
        "SELECT COALESCE(charged_credits, 0) AS c FROM ingestion_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def reap_exhausted_jobs(
    conn: sqlite3.Connection, *, user_id: str | None = None, max_attempts: int = 3
) -> list[dict[str, Any]]:
    """Fail the jobs nothing will ever claim again, and refund what they spent.

    `claim_next_job` filters on `attempts < max_attempts`, so a job that
    crashed its last attempt simply stops being selected: it keeps a
    non-terminal status forever and nothing ever runs the code that would
    give its charge back. Before credits that leaked a reservation quietly;
    with credits it would leak the person's allowance, which they can see.

    So the reap runs at the top of every drain, which is the only loop
    guaranteed to exist wherever jobs are processed at all.
    """
    where = "status NOT IN ('ready','failed','blocked_by_platform') AND attempts >= ?"
    params: list[Any] = [max_attempts]
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    rows = conn.execute(
        f"SELECT job_id, user_id, COALESCE(charged_credits, 0) AS charged "
        f"FROM ingestion_jobs WHERE {where}",
        params,
    ).fetchall()
    reaped = []
    for row in rows:
        set_job_status(
            conn,
            row["job_id"],
            "failed",
            error_message="Brain tried this three times and could not finish it.",
            error_reason="attempts_exhausted",
        )
        refund_credits(conn, row["user_id"], credits=row["charged"])
        reaped.append({"job_id": row["job_id"], "refunded": int(row["charged"])})
    return reaped


# ---------------------------------------------------------------------------
# quotas  (`R-18` fossils — superseded by credits above)
# ---------------------------------------------------------------------------
class QuotaExceeded(Exception):
    def __init__(self, kind: str, used: int, cap: int, requested: int) -> None:
        super().__init__(
            f"{kind} quota exceeded: {used}/{cap} used, {requested} more requested"
        )
        self.kind = kind
        self.used = used
        self.cap = cap
        self.requested = requested


def reserve_quota(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    minutes: int = 0,
    files: int = 0,
    cfg: Settings | None = None,
) -> None:
    """Atomically reserve media minutes / file slots, or raise QuotaExceeded."""
    cfg = cfg or settings()
    with transaction(conn):
        row = conn.execute(
            "SELECT media_minutes_used, files_used FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown user_id {user_id!r}")
        if minutes and row["media_minutes_used"] + minutes > cfg.quota_media_minutes:
            raise QuotaExceeded(
                "media_minutes", row["media_minutes_used"], cfg.quota_media_minutes, minutes
            )
        if files and row["files_used"] + files > cfg.quota_files:
            raise QuotaExceeded("files", row["files_used"], cfg.quota_files, files)
        conn.execute(
            "UPDATE users SET media_minutes_used = media_minutes_used + ?, "
            "files_used = files_used + ? WHERE user_id = ?",
            (minutes, files, user_id),
        )


def release_quota(conn: sqlite3.Connection, user_id: str, *, minutes: int = 0, files: int = 0) -> None:
    conn.execute(
        "UPDATE users SET "
        "media_minutes_used = MAX(0, media_minutes_used - ?), "
        "files_used = MAX(0, files_used - ?) WHERE user_id = ?",
        (minutes, files, user_id),
    )


# ---------------------------------------------------------------------------
# oauth sessions
# ---------------------------------------------------------------------------
def create_oauth_session(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    client_name: str = "mcp_client",
    audience: str | None = None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    """Issue an access/refresh pair. Plaintext is returned once, never stored."""
    cfg = cfg or settings()
    access_token = new_token()
    refresh_token = new_token()
    started = now_utc()
    session_id = new_id("sess")
    conn.execute(
        """
        INSERT INTO oauth_sessions (
            session_id, user_id, client_name, access_token_hash, access_expires_at,
            refresh_token_hash, refresh_expires_at, audience
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            user_id,
            client_name,
            hash_token(access_token),
            iso(started + timedelta(seconds=cfg.access_token_ttl_sec)),
            hash_token(refresh_token),
            iso(started + timedelta(seconds=cfg.refresh_token_ttl_sec)),
            audience or cfg.audience,
        ),
    )
    return {
        "session_id": session_id,
        "user_id": user_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": cfg.access_token_ttl_sec,
        "refresh_expires_in": cfg.refresh_token_ttl_sec,
        "audience": audience or cfg.audience,
    }


def session_by_access_token(conn: sqlite3.Connection, access_token: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM oauth_sessions WHERE access_token_hash = ?",
        (hash_token(access_token),),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    expires = parse_iso(record["access_expires_at"])
    record["expired"] = bool(expires and expires <= now_utc())
    return record


def session_by_refresh_token(conn: sqlite3.Connection, refresh_token: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM oauth_sessions WHERE refresh_token_hash = ?",
        (hash_token(refresh_token),),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    expires = parse_iso(record["refresh_expires_at"])
    record["expired"] = bool(expires and expires <= now_utc())
    return record


def rotate_refresh_token(
    conn: sqlite3.Connection, refresh_token: str, *, cfg: Settings | None = None
) -> dict[str, Any] | None:
    """Rotate one refresh token. The 30-day absolute lifetime never extends."""
    cfg = cfg or settings()
    record = session_by_refresh_token(conn, refresh_token)
    if record is None or record["revoked"] or record["expired"]:
        return None
    access_token = new_token()
    new_refresh = new_token()
    conn.execute(
        "UPDATE oauth_sessions SET access_token_hash = ?, access_expires_at = ?, "
        "refresh_token_hash = ? WHERE session_id = ?",
        (
            hash_token(access_token),
            iso(now_utc() + timedelta(seconds=cfg.access_token_ttl_sec)),
            hash_token(new_refresh),
            record["session_id"],
        ),
    )
    return {
        "session_id": record["session_id"],
        "user_id": record["user_id"],
        "access_token": access_token,
        "refresh_token": new_refresh,
        "expires_in": cfg.access_token_ttl_sec,
        # Absolute lifetime is preserved from the original grant.
        "refresh_expires_at": record["refresh_expires_at"],
        "audience": record["audience"],
    }


def revoke_session(conn: sqlite3.Connection, session_id: str) -> bool:
    cur = conn.execute(
        "UPDATE oauth_sessions SET revoked = 1 WHERE session_id = ? AND revoked = 0",
        (session_id,),
    )
    return cur.rowcount > 0


def revoke_all_sessions(conn: sqlite3.Connection, user_id: str) -> int:
    cur = conn.execute(
        "UPDATE oauth_sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0",
        (user_id,),
    )
    return cur.rowcount


def list_sessions(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT session_id, client_name, audience, revoked, created_at, "
        "access_expires_at, refresh_expires_at FROM oauth_sessions "
        "WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# §7.11 connections — the key half
# ---------------------------------------------------------------------------
#: §7.11's own example: `https://brain.app/mcp?key=sk-brain-7Qd2xV9mTb4f2a`.
KEY_PREFIX = "sk-brain-"


def new_connection_key() -> str:
    """A key with enough entropy that guessing is not a strategy.

    `token_urlsafe(24)` is 192 bits. The prefix is there so that a key found
    in a log or a pasted URL is recognisable as this product's — which is what
    makes a leaked one reportable.
    """
    return f"{KEY_PREFIX}{secrets.token_urlsafe(24)}"


def create_connection(
    conn: sqlite3.Connection, user_id: str, *, name: str = "My AI"
) -> tuple[dict[str, Any], str]:
    """Return the row and **the key in plaintext, once**.

    The caller shows it to the person and forgets it. Nothing stores it: only
    the digest is written, so there is no second chance to read it and no way
    for a stolen database to yield a working key.
    """
    key = new_connection_key()
    connection_id = new_id("con")
    conn.execute(
        "INSERT INTO connections (connection_id, user_id, name, key_hash, key_prefix) "
        "VALUES (?, ?, ?, ?, ?)",
        (connection_id, user_id, (name or "My AI")[:120], hash_token(key), key[:16]),
    )
    return get_connection(conn, connection_id), key  # type: ignore[return-value]


def get_connection(conn: sqlite3.Connection, connection_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM connections WHERE connection_id = ?", (connection_id,)
    ).fetchone()
    return dict(row) if row else None


def connection_by_key(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """Resolve a presented key to its connection, by digest.

    The lookup is on the hash, so the plaintext is never compared against
    anything stored and never has to be.
    """
    if not key:
        return None
    row = conn.execute(
        "SELECT * FROM connections WHERE key_hash = ?", (hash_token(key),)
    ).fetchone()
    return dict(row) if row else None


def list_connections(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM connections WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def touch_connection(conn: sqlite3.Connection, connection_id: str) -> None:
    """§7.11's `last call …` column, and §7.10's reason for existing: "revoking
    a key is only meaningful once you can see what it did"."""
    conn.execute(
        "UPDATE connections SET last_call_at = CURRENT_TIMESTAMP WHERE connection_id = ?",
        (connection_id,),
    )


def rename_connection(conn: sqlite3.Connection, connection_id: str, name: str) -> bool:
    cur = conn.execute(
        "UPDATE connections SET name = ? WHERE connection_id = ?",
        ((name or "")[:120], connection_id),
    )
    return cur.rowcount > 0


def roll_connection_key(conn: sqlite3.Connection, connection_id: str) -> str | None:
    """§7.11's `Roll the key`: the same connection, a new credential.

    The old digest is overwritten, so the previous key stops working the
    instant this returns — which is the point. The connection keeps its name
    and its history, so §7.10's log still reads continuously across the roll.
    """
    if get_connection(conn, connection_id) is None:
        return None
    key = new_connection_key()
    conn.execute(
        "UPDATE connections SET key_hash = ?, key_prefix = ?, revoked = 0 "
        "WHERE connection_id = ?",
        (hash_token(key), key[:16], connection_id),
    )
    return key


def revoke_connection(conn: sqlite3.Connection, connection_id: str) -> bool:
    """§7.11: "One credential per connection. Revoking one leaves the others
    working." """
    cur = conn.execute(
        "UPDATE connections SET revoked = 1 WHERE connection_id = ? AND revoked = 0",
        (connection_id,),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# ingestion jobs
# ---------------------------------------------------------------------------
#: The pipeline, as the person sees it (§9.1). Fixed, ordered, and the same six
#: for every source — a step that does not apply to this source type is marked
#: `skipped` with a reason rather than quietly left out, because a list whose
#: length changes cannot be read at a glance.
JOB_STEPS: tuple[tuple[str, str], ...] = (
    ("read", "Reading the source"),
    ("transcribe", "Transcribing audio"),
    ("summarise", "Writing the summary"),
    ("extract", "Extracting rules"),
    ("draft_skills", "Drafting skills"),
    ("remember", "Updating memory"),
)

STEP_KEYS = tuple(key for key, _ in JOB_STEPS)
STEP_LABELS = dict(JOB_STEPS)


def seed_job_steps(conn: sqlite3.Connection, job_id: str) -> None:
    """Lay all six down as `pending` the moment the job is queued.

    The frame shows six rows from the first paint (§7.3), so they must exist
    before any of them runs. `INSERT OR IGNORE` keeps a retried job's history.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO job_steps (job_id, ordinal, step) VALUES (?, ?, ?)",
        [(job_id, i, key) for i, key in enumerate(STEP_KEYS)],
    )


def create_job(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    source_id: str,
    input_target: str,
    source_type: str,
    user_note: str = "",
    reserved_minutes: int = 0,
    reserved_files: int = 0,
) -> dict[str, Any]:
    job_id = new_id("job")
    conn.execute(
        """
        INSERT INTO ingestion_jobs (
            job_id, user_id, source_id, input_target, source_type, user_note,
            reserved_minutes, reserved_files
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            user_id,
            source_id,
            input_target,
            source_type,
            (user_note or "")[:2000],
            reserved_minutes,
            reserved_files,
        ),
    )
    # Seeded here, at queue time, not when a worker claims the job. §7.3 draws
    # six rows from the first paint, and a job sits `queued` for as long as the
    # queue is busy — during which the work block would otherwise have nothing
    # to show at all.
    seed_job_steps(conn, job_id)
    return get_job(conn, job_id)  # type: ignore[return-value]


def append_user_note(conn: sqlite3.Connection, job_id: str, text: str) -> str:
    """§7.2's interjection, added to the instruction the pipeline reads.

    "An interjection is acknowledged without restarting or stopping the
    pipeline." Nothing is cancelled and no step is rewound: the note is
    appended, and every step that has not yet read the instruction will read
    the new one. Which steps those are is what makes the acknowledgement
    honest rather than reassuring — see `op_add_instruction`.

    Returns the instruction as it now stands.
    """
    job = get_job(conn, job_id)
    if job is None:
        return ""
    joined = " ".join(part for part in (job["user_note"], text.strip()) if part)[:2000]
    conn.execute(
        "UPDATE ingestion_jobs SET user_note = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE job_id = ?",
        (joined, job_id),
    )
    return joined


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM ingestion_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(
    conn: sqlite3.Connection, user_id: str, *, limit: int = 10, active_only: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM ingestion_jobs WHERE user_id = ?"
    params: list[Any] = [user_id]
    if active_only:
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        sql += f" AND status NOT IN ({placeholders})"
        params.extend(TERMINAL_STATUSES)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, min(limit, 50)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def charged_jobs(
    conn: sqlite3.Connection, user_id: str, *, limit: int = 10, period: str | None = None
) -> list[dict[str, Any]]:
    """`R-18`: what this account's credits were spent on, dearest first.

    Scoped to the current period by default, because that is the one the
    meter above it is counting — a list that spans months under a monthly
    meter would not add up to what the meter says.
    """
    period = period or current_period()
    return [
        dict(r)
        for r in conn.execute(
            "SELECT job_id, source_id, source_type, charged_credits, created_at "
            "FROM ingestion_jobs "
            "WHERE user_id = ? AND charged_credits > 0 "
            "  AND strftime('%Y-%m', created_at) = ? "
            "ORDER BY charged_credits DESC, rowid DESC LIMIT ?",
            (user_id, period, max(1, min(limit, 50))),
        ).fetchall()
    ]


def count_active_jobs(conn: sqlite3.Connection, user_id: str) -> int:
    """Jobs that are queued or mid-pipeline — the Omni-Bar / receipt counter."""
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    row = conn.execute(
        f"SELECT COUNT(*) FROM ingestion_jobs WHERE user_id = ? "
        f"AND status NOT IN ({placeholders})",
        (user_id, *TERMINAL_STATUSES),
    ).fetchone()
    return int(row[0])


def ingestion_defaults(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """What the pipeline reads before steps 3, 4 and 5.

    Defaults to doing everything, for a row written before these columns
    existed: `COALESCE` rather than a Python fallback, so a NULL from an old
    row and a missing row answer the same way.
    """
    row = conn.execute(
        "SELECT COALESCE(want_summary, 1) AS want_summary, "
        "       COALESCE(want_rules, 1) AS want_rules, "
        "       COALESCE(want_skills, 1) AS want_skills, "
        "       COALESCE(keep_uploads, 1) AS keep_uploads, "
        "       COALESCE(write_in, 'source') AS write_in "
        "FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return {
            "want_summary": 1,
            "want_rules": 1,
            "want_skills": 1,
            "keep_uploads": 1,
            "write_in": "source",
        }
    return dict(row)


def set_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    error_message: str | None = None,
    error_reason: str | None = None,
    claims_extracted: int | None = None,
) -> None:
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown job status: {status}")
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list[Any] = [status]
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message[:2000])
    if error_reason is not None:
        sets.append("error_reason = ?")
        params.append(error_reason[:64])
    if claims_extracted is not None:
        sets.append("claims_extracted = ?")
        params.append(claims_extracted)
    if status in TERMINAL_STATUSES:
        sets.append("locked_until = NULL")
    params.append(job_id)
    conn.execute(f"UPDATE ingestion_jobs SET {', '.join(sets)} WHERE job_id = ?", params)


def start_step(conn: sqlite3.Connection, job_id: str, step: str) -> None:
    if step not in STEP_KEYS:
        raise ValueError(f"unknown pipeline step: {step}")
    conn.execute(
        "UPDATE job_steps SET state = 'running', detail = '', "
        "started_at = CURRENT_TIMESTAMP, finished_at = NULL "
        "WHERE job_id = ? AND step = ?",
        (job_id, step),
    )


def finish_step(
    conn: sqlite3.Connection,
    job_id: str,
    step: str,
    *,
    detail: str = "",
    state: str = "done",
) -> None:
    """Close a step and record what it found.

    `detail` is the only thing the frame shows beside a step's name, and §4.4
    says it appears only once the step is done — so it is written here and
    never while the step is running.
    """
    if step not in STEP_KEYS:
        raise ValueError(f"unknown pipeline step: {step}")
    if state not in ("done", "skipped", "failed"):
        raise ValueError(f"not a terminal step state: {state}")
    conn.execute(
        "UPDATE job_steps SET state = ?, detail = ?, finished_at = CURRENT_TIMESTAMP, "
        "started_at = COALESCE(started_at, CURRENT_TIMESTAMP) "
        "WHERE job_id = ? AND step = ?",
        (state, detail[:300], job_id, step),
    )


def abandon_steps(conn: sqlite3.Connection, job_id: str, *, reason: str = "") -> None:
    """A job died. Everything still open stops, so no step spins forever.

    The failing step has already recorded its own reason; the ones after it
    never started, and saying "failed" of a step that never ran would be a
    second lie on top of the first.
    """
    conn.execute(
        "UPDATE job_steps SET state = 'failed', detail = ?, finished_at = CURRENT_TIMESTAMP "
        "WHERE job_id = ? AND state = 'running'",
        (reason[:300], job_id),
    )
    conn.execute(
        "UPDATE job_steps SET state = 'skipped', finished_at = CURRENT_TIMESTAMP "
        "WHERE job_id = ? AND state = 'pending'",
        (job_id,),
    )


def latest_job_for_source(
    conn: sqlite3.Connection, user_id: str, source_id: str
) -> dict[str, Any] | None:
    """The job behind a source page.

    Scoped to `user_id` and not to `source_id` alone: a source id arrives from
    the client, and §10.7 is explicit that every query filters on the user —
    "the single most likely data-leak bug in this app".

    Newest first, because a re-processed source has more than one.
    """
    row = conn.execute(
        "SELECT * FROM ingestion_jobs WHERE user_id = ? AND source_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (user_id, source_id),
    ).fetchone()
    return dict(row) if row else None


def job_steps(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ordinal, step, state, detail, started_at, finished_at "
        "FROM job_steps WHERE job_id = ? ORDER BY ordinal",
        (job_id,),
    ).fetchall()
    return [{**dict(row), "label": STEP_LABELS[row["step"]]} for row in rows]


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    user_id: str | None = None,
    lease_seconds: int = 120,
    max_attempts: int = 3,
) -> dict[str, Any] | None:
    """Lease the next runnable job (recovering leases that expired mid-flight)."""
    lease_until = iso(now_utc() + timedelta(seconds=lease_seconds))
    now = iso(now_utc())
    with transaction(conn):
        sql = (
            "SELECT * FROM ingestion_jobs WHERE status NOT IN "
            "('ready','failed','blocked_by_platform') "
            "AND (locked_until IS NULL OR locked_until < ?) AND attempts < ? "
        )
        params: list[Any] = [now, max_attempts]
        if user_id:
            sql += "AND user_id = ? "
            params.append(user_id)
        sql += "ORDER BY created_at ASC, rowid ASC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE ingestion_jobs SET locked_until = ?, attempts = attempts + 1, "
            "status = CASE WHEN status = 'queued' THEN 'fetching' ELSE status END, "
            "updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
            (lease_until, row["job_id"]),
        )
        return dict(conn.execute(
            "SELECT * FROM ingestion_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone())


# ---------------------------------------------------------------------------
# audit log (append-only)
# ---------------------------------------------------------------------------
def audit(
    conn: sqlite3.Connection,
    *,
    user_id: str | None,
    action: str,
    status: str,
    surface: str = "mcp",
    client_name: str = "mcp_client",
    target_id: str | None = None,
    detail: Any = "",
    reasoning_trace: str = "",
    latency_ms: int | None = None,
    payload_bytes: int | None = None,
    result_summary: str = "",
    connection_id: str = "",
) -> None:
    """Append one immutable audit row.

    `reasoning_trace` is the plain-English "why" the owner reads in their
    Profile — "blocked: the skill text tried to override the host model's
    instructions" — rather than a machine-readable detail blob.

    The last four are §7.10's row, collapsed and expanded: the `180 ms`, the
    `1.2 kB`, the `3 rules returned`, and which connection made the call —
    which is also the link from Activity to §7.11, because "revoking a key is
    only meaningful once you can see what it did".

    `latency_ms` and `payload_bytes` are nullable on purpose. A call that was
    refused before it ran has no duration and no payload, and `0` would read
    as "instant and empty" on a page whose whole job is being readable.
    """
    if surface not in ("mcp", "web_omnibar", "web_ui", "system"):
        surface = "system"
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str, ensure_ascii=False)[:2000]
    conn.execute(
        "INSERT INTO audit_logs (user_id, client_name, surface, action, target_id, status, "
        "detail, reasoning_trace, latency_ms, payload_bytes, result_summary, connection_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            client_name,
            surface,
            action,
            target_id,
            status,
            detail[:2000],
            (reasoning_trace or "")[:1000],
            latency_ms,
            payload_bytes,
            (result_summary or "")[:300],
            (connection_id or "")[:64],
        ),
    )


def recent_calls(
    conn: sqlite3.Connection, user_id: str, *, limit: int = 60
) -> list[dict[str, Any]]:
    """§7.10's `Calls` — the MCP log, and only it.

    A row with a `connection_id` is a call a connection made. The operations
    write their own rows too (§2's internal trail: every tool call is logged
    with its action, target and status), and those are steps inside a call
    rather than calls — listing them would make one `search` read as five
    lines and the log unreadable, which is the opposite of §7.10's purpose.
    """
    rows = conn.execute(
        "SELECT * FROM audit_logs WHERE user_id = ? AND connection_id != '' "
        "ORDER BY id DESC LIMIT ?",
        (user_id, max(1, min(limit, 200))),
    ).fetchall()
    return [dict(row) for row in rows]


def calls_in_last_minute(conn: sqlite3.Connection, user_id: str) -> int:
    """§7.10's `Calls a minute` meter, against the ceiling the limiter keeps.

    Counted from the log rather than from the limiter's own window: the
    limiter is per-process in-memory state, and the page has to be right
    behind more than one uvicorn worker.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE user_id = ? AND connection_id != '' "
        "AND created_at >= datetime('now', '-60 seconds')",
        (user_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def recent_audit(conn: sqlite3.Connection, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_logs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, max(1, min(limit, 200))),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# oauth clients + authorization codes (PKCE)
# ---------------------------------------------------------------------------
def register_client(
    conn: sqlite3.Connection, *, client_name: str, redirect_uris: list[str]
) -> dict[str, Any]:
    """RFC 7591 dynamic registration — MCP clients self-register."""
    client_id = new_id("client")
    conn.execute(
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris) VALUES (?, ?, ?)",
        (client_id, (client_name or "mcp_client")[:200], json.dumps(redirect_uris)),
    )
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
    }


def get_client(conn: sqlite3.Connection, client_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["redirect_uris"] = json.loads(record["redirect_uris"] or "[]")
    return record


def create_auth_code(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scope: str = "",
    resource: str = "",
    ttl_seconds: int = 60,
) -> str:
    code = new_token()
    conn.execute(
        """
        INSERT INTO oauth_auth_codes (
            code, user_id, client_id, redirect_uri, code_challenge, scope,
            resource, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hash_token(code),
            user_id,
            client_id,
            redirect_uri,
            code_challenge,
            scope,
            resource,
            iso(now_utc() + timedelta(seconds=ttl_seconds)),
        ),
    )
    return code


def consume_auth_code(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    """Atomically claim an authorization code. A replay returns ``None``."""
    digest = hash_token(code)
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM oauth_auth_codes WHERE code = ?", (digest,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        conn.execute("UPDATE oauth_auth_codes SET consumed = 1 WHERE code = ?", (digest,))
    if record["consumed"]:
        return None
    expires = parse_iso(record["expires_at"])
    if expires and expires <= now_utc():
        return None
    return record


def purge_expired_auth_codes(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM oauth_auth_codes WHERE expires_at < ? OR consumed = 1",
        (iso(now_utc() - timedelta(minutes=10)),),
    )
    return cur.rowcount
