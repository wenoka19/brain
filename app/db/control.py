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
    media_minutes_used INTEGER NOT NULL DEFAULT 0,
    files_used         INTEGER NOT NULL DEFAULT 0,
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
    claims_extracted INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    locked_until     DATETIME,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON ingestion_jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_pending ON ingestion_jobs(status, locked_until);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    client_name TEXT,
    surface     TEXT CHECK (surface IN ('mcp','web_omnibar','web_ui','system')),
    action      TEXT,
    target_id   TEXT,
    status      TEXT,
    detail      TEXT NOT NULL DEFAULT '',
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
_connections: dict[str, sqlite3.Connection] = {}


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
    return conn


def control(path: Path | None = None) -> sqlite3.Connection:
    """Cached control-db handle for the current process."""
    cfg = settings()
    key = str(Path(path) if path else cfg.control_db_path)
    conn = _connections.get(key)
    if conn is None:
        conn = open_control_db(Path(key), cfg=cfg)
        _connections[key] = conn
    return conn


def close_all() -> None:
    for conn in list(_connections.values()):
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _connections.clear()


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


def update_profile(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    about_you_context: str | None = None,
    theme_mode: str | None = None,
) -> dict[str, Any] | None:
    sets, params = [], []
    if about_you_context is not None:
        sets.append("about_you_context = ?")
        params.append(about_you_context[:20_000])
    if theme_mode is not None:
        if theme_mode not in ("light", "dark", "warm_sunset"):
            raise ValueError(f"unknown theme_mode: {theme_mode}")
        sets.append("theme_mode = ?")
        params.append(theme_mode)
    if sets:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)
    return get_user(conn, user_id)


# ---------------------------------------------------------------------------
# quotas  (reserved up-front, released when a job fails)
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
# ingestion jobs
# ---------------------------------------------------------------------------
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
    return get_job(conn, job_id)  # type: ignore[return-value]


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


def set_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    error_message: str | None = None,
    claims_extracted: int | None = None,
) -> None:
    if status not in JOB_STATUSES:
        raise ValueError(f"unknown job status: {status}")
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: list[Any] = [status]
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message[:2000])
    if claims_extracted is not None:
        sets.append("claims_extracted = ?")
        params.append(claims_extracted)
    if status in TERMINAL_STATUSES:
        sets.append("locked_until = NULL")
    params.append(job_id)
    conn.execute(f"UPDATE ingestion_jobs SET {', '.join(sets)} WHERE job_id = ?", params)


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
) -> None:
    if surface not in ("mcp", "web_omnibar", "web_ui", "system"):
        surface = "system"
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str, ensure_ascii=False)[:2000]
    conn.execute(
        "INSERT INTO audit_logs (user_id, client_name, surface, action, target_id, status, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, client_name, surface, action, target_id, status, detail[:2000]),
    )


def recent_audit(conn: sqlite3.Connection, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_logs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, max(1, min(limit, 200))),
    ).fetchall()
    return [dict(r) for r in rows]
