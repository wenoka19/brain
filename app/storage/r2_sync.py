"""Event-driven tenant snapshots to S3-compatible object storage (R2).

Why this exists, and why it is not Litestream
---------------------------------------------
Litestream watches files with `inotify`. One watcher per tenant database is
fine at ten tenants and impossible at ten thousand — it exhausts file
descriptors and inotify watches long before that. So continuous replication
stays on `control.db` alone, and tenant databases are replicated a different
way: a whole-file snapshot taken *after* a write, debounced, and uploaded.

That trade is deliberate. A tenant loses at most the last few seconds of
writes if the node dies mid-debounce, where `control.db` — which holds identity,
tokens and the job queue — loses nothing. Tenant content is reconstructible
from its source; an auth session is not.

The payoff is portability: a tenant database is one self-contained file in
object storage, so any node anywhere can serve any tenant by downloading it.
That is what makes Hetzner, or Fly shards, a config change rather than a
rewrite.

SigV4 is implemented here directly rather than via `boto3`, which would add
~50MB and a transitive dependency tree to serve four HTTP verbs.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from app.config import Settings, settings

LOGGER = logging.getLogger("brain.r2")
ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
UNSIGNED_PAYLOAD_HEADERS = ("host", "x-amz-content-sha256", "x-amz-date")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class R2Config:
    endpoint_url: str = ""
    bucket: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    region: str = "auto"
    prefix: str = "tenants/"

    @property
    def enabled(self) -> bool:
        return bool(
            self.endpoint_url and self.bucket and self.access_key_id and self.secret_access_key
        )

    def key_for(self, user_id: str) -> str:
        return f"{self.prefix}user_{user_id}.db"

    def url_for(self, key: str) -> str:
        # Path-style addressing: R2 does not do virtual-hosted buckets.
        encoded = "/".join(quote(part, safe="~") for part in key.split("/"))
        return f"{self.endpoint_url.rstrip('/')}/{quote(self.bucket, safe='~')}/{encoded}"


def load_r2_config() -> R2Config:
    """Read the environment. `R2_ENDPOINT` is accepted too, because
    `litestream.yml` already uses that name for the same endpoint."""
    return R2Config(
        endpoint_url=(
            os.environ.get("R2_ENDPOINT_URL") or os.environ.get("R2_ENDPOINT") or ""
        ).strip().rstrip("/"),
        bucket=(os.environ.get("R2_BUCKET") or "").strip(),
        access_key_id=(os.environ.get("R2_ACCESS_KEY_ID") or "").strip(),
        secret_access_key=(os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip(),
        region=(os.environ.get("R2_REGION") or "auto").strip(),
        prefix=(os.environ.get("R2_TENANT_PREFIX") or "tenants/").strip(),
    )


_config: R2Config | None = None


def config() -> R2Config:
    global _config
    if _config is None:
        _config = load_r2_config()
    return _config


def set_config(value: R2Config | None) -> None:
    """Tests install a fake endpoint; `None` re-reads the environment."""
    global _config
    _config = value


def enabled() -> bool:
    return config().enabled


# ---------------------------------------------------------------------------
# AWS SigV4
# ---------------------------------------------------------------------------
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    key = _sign(f"AWS4{secret}".encode("utf-8"), date_stamp)
    key = _sign(key, region)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def canonical_request(
    method: str, path: str, payload_hash: str, headers: dict[str, str]
) -> str:
    lowered = {name.lower(): value.strip() for name, value in headers.items()}
    names = sorted(lowered)
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    signed_headers = ";".join(names)
    return "\n".join(
        [method.upper(), path, "", canonical_headers, signed_headers, payload_hash]
    )


def sigv4_headers(
    *,
    method: str,
    url: str,
    payload_hash: str,
    cfg: R2Config,
    now: datetime | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the signed headers for one S3 request."""
    from urllib.parse import urlsplit

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = moment.strftime("%Y%m%d")

    parts = urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    headers.update({k.lower(): v for k, v in (extra or {}).items()})

    scope = f"{date_stamp}/{cfg.region}/{SERVICE}/aws4_request"
    request = canonical_request(method, path, payload_hash, headers)
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, _sha256_hex(request.encode("utf-8"))])
    signature = hmac.new(
        signing_key(cfg.secret_access_key, date_stamp, cfg.region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signed_headers = ";".join(sorted(headers))

    return {
        **{name: value for name, value in headers.items()},
        "Authorization": (
            f"{ALGORITHM} Credential={cfg.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
HttpClientFactory = Callable[[], Any]
_http_client_factory: HttpClientFactory | None = None


def set_http_client_factory(factory: HttpClientFactory | None) -> None:
    """Tests install a `MockTransport` client here, so nothing leaves the box."""
    global _http_client_factory
    _http_client_factory = factory


def _client(timeout: float):
    import httpx

    if _http_client_factory is not None:
        return _http_client_factory()
    return httpx.Client(timeout=timeout, follow_redirects=False)


def _timeout(cfg: Settings | None = None) -> float:
    return (cfg or settings()).r2_timeout_sec


def head_object(key: str, *, cfg: R2Config | None = None) -> int | None:
    """Byte size of the object, or `None` when it does not exist."""
    cfg = cfg or config()
    if not cfg.enabled:
        return None
    url = cfg.url_for(key)
    headers = sigv4_headers(method="HEAD", url=url, payload_hash=_sha256_hex(b""), cfg=cfg)
    try:
        with _client(_timeout()) as client:
            response = client.request("HEAD", url, headers=headers)
    except Exception as exc:
        LOGGER.warning("r2 head %s failed: %s", key, exc)
        return None
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        LOGGER.warning("r2 head %s -> %s", key, response.status_code)
        return None
    try:
        return int(response.headers.get("content-length", 0))
    except ValueError:
        return 0


def get_object(key: str, destination: Path, *, cfg: R2Config | None = None) -> bool:
    """Download to `destination` atomically. Returns False when absent."""
    cfg = cfg or config()
    if not cfg.enabled:
        return False
    url = cfg.url_for(key)
    headers = sigv4_headers(method="GET", url=url, payload_hash=_sha256_hex(b""), cfg=cfg)
    try:
        with _client(_timeout()) as client:
            response = client.request("GET", url, headers=headers)
    except Exception as exc:
        LOGGER.warning("r2 get %s failed: %s", key, exc)
        return False
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        LOGGER.warning("r2 get %s -> %s", key, response.status_code)
        return False

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".hydrating")
    staging.write_bytes(response.content)
    # A half-written database must never be openable, so it is renamed into
    # place only once it is whole. `os.replace` is atomic on POSIX.
    os.replace(staging, destination)
    return True


def put_object(key: str, source: Path, *, cfg: R2Config | None = None) -> bool:
    cfg = cfg or config()
    if not cfg.enabled:
        return False
    source = Path(source)
    url = cfg.url_for(key)
    payload_hash = sha256_file(source)
    headers = sigv4_headers(
        method="PUT",
        url=url,
        payload_hash=payload_hash,
        cfg=cfg,
        extra={"content-type": "application/x-sqlite3"},
    )
    try:
        with _client(_timeout()) as client:
            response = client.request("PUT", url, headers=headers, content=source.read_bytes())
    except Exception as exc:
        LOGGER.warning("r2 put %s failed: %s", key, exc)
        return False
    if response.status_code >= 400:
        LOGGER.warning("r2 put %s -> %s", key, response.status_code)
        return False
    return True


# ---------------------------------------------------------------------------
# snapshot scheduling
# ---------------------------------------------------------------------------
_lock = threading.Lock()
#: One lock per tenant, so the background loop and a shutdown flush racing on
#: the same tenant serialise instead of both running `VACUUM INTO` on it.
_snapshot_locks: dict[str, threading.Lock] = {}
_dirty: dict[str, float] = {}
_synced: dict[str, float] = {}
_accessed: dict[str, float] = {}
_thread: threading.Thread | None = None
_stop = threading.Event()


def mark_dirty(user_id: str, *, now: float | None = None) -> None:
    """Record that a tenant has unsynced writes. Cheap, and never blocks."""
    if not user_id:
        return
    with _lock:
        _dirty.setdefault(user_id, now if now is not None else time.monotonic())


def mark_accessed(user_id: str, *, now: float | None = None) -> None:
    with _lock:
        _accessed[user_id] = now if now is not None else time.monotonic()


def is_dirty(user_id: str) -> bool:
    with _lock:
        return user_id in _dirty


def dirty_user_ids() -> list[str]:
    with _lock:
        return sorted(_dirty)


def reset_state() -> None:
    with _lock:
        _dirty.clear()
        _synced.clear()
        _accessed.clear()
        _snapshot_locks.clear()


def _snapshot_lock(user_id: str) -> threading.Lock:
    with _lock:
        return _snapshot_locks.setdefault(user_id, threading.Lock())


def snapshot_and_upload(user_id: str, *, cfg: Settings | None = None) -> bool:
    """`VACUUM INTO` a clean copy, upload it, and clear the dirty flag.

    `VACUUM INTO` is the right primitive: it produces a single consistent file
    with no WAL sidecar, from a live database, without blocking writers for the
    duration of a file copy.
    """
    cfg = cfg or settings()
    r2 = config()
    if not r2.enabled:
        return False
    source = cfg.tenant_db_path(user_id)
    if not source.exists():
        with _lock:
            _dirty.pop(user_id, None)
        return False

    with _snapshot_lock(user_id):
        return _snapshot_and_upload_locked(user_id, cfg=cfg, r2=r2, source=source)


def _snapshot_and_upload_locked(user_id: str, *, cfg: Settings, r2: R2Config, source: Path) -> bool:
    snapshot = Path(cfg.snapshot_dir) / f"user_{user_id}.snap.db"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.unlink(missing_ok=True)

    with _lock:
        started = _dirty.get(user_id, time.monotonic())
    try:
        connection = sqlite3.connect(str(source), timeout=10.0)
        try:
            connection.execute("VACUUM INTO ?", (str(snapshot),))
        finally:
            connection.close()
        uploaded = put_object(r2.key_for(user_id), snapshot)
    except Exception as exc:
        LOGGER.warning("snapshot for %s failed: %s", user_id, exc)
        return False
    finally:
        snapshot.unlink(missing_ok=True)

    if not uploaded:
        return False
    with _lock:
        # Only clear the flag if nothing was written while we were uploading.
        if _dirty.get(user_id) == started:
            _dirty.pop(user_id, None)
        _synced[user_id] = time.monotonic()
    return True


def flush_due(*, cfg: Settings | None = None, now: float | None = None) -> list[str]:
    """Snapshot every tenant whose last write is older than the debounce."""
    cfg = cfg or settings()
    moment = now if now is not None else time.monotonic()
    with _lock:
        due = [
            user_id
            for user_id, marked in _dirty.items()
            if moment - marked >= cfg.snapshot_debounce_sec
        ]
    return [user_id for user_id in sorted(due) if snapshot_and_upload(user_id, cfg=cfg)]


def flush_all_dirty_tenants(*, cfg: Settings | None = None) -> list[str]:
    """Snapshot everything dirty right now, ignoring the debounce.

    This is the shutdown path. A redeploy sends `SIGTERM`, and anything marked
    dirty inside the debounce window has not been uploaded yet — so it is
    uploaded here, synchronously, before the process is allowed to exit. It is
    safe to call when R2 is unconfigured (nothing is dirty that matters) and
    safe to call twice (a successful upload clears the flag).
    """
    return [user_id for user_id in dirty_user_ids() if snapshot_and_upload(user_id, cfg=cfg)]


#: The older, shorter name. Kept because the snapshot tests read better with it.
flush_all = flush_all_dirty_tenants


def start_background(*, cfg: Settings | None = None) -> bool:
    global _thread
    cfg = cfg or settings()
    if not enabled() or _thread is not None:
        return False
    _stop.clear()

    def loop() -> None:
        LOGGER.info("tenant snapshot worker started")
        while not _stop.wait(cfg.snapshot_poll_sec):
            try:
                flushed = flush_due(cfg=cfg)
                if flushed:
                    LOGGER.info("snapshotted %s tenant(s)", len(flushed))
                maybe_evict(cfg=cfg)
            except Exception:
                LOGGER.exception("snapshot loop error")

    _thread = threading.Thread(target=loop, name="brain-snapshots", daemon=True)
    _thread.start()
    return True


def stop_background(*, flush: bool = True, cfg: Settings | None = None) -> list[str]:
    """Stop the loop and flush, in that order.

    The loop is stopped first so it cannot mark progress we then miss, and
    joined before flushing so the two are not snapshotting the same tenant at
    once — though `_snapshot_lock` makes that safe even if the join times out.
    Returns the tenants actually uploaded, so a caller can log it.
    """
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=10)
        _thread = None
    if not flush:
        return []
    flushed = flush_all_dirty_tenants(cfg=cfg)
    if flushed:
        LOGGER.info("flushed %s dirty tenant(s) on shutdown", len(flushed))
    stranded = dirty_user_ids()
    if stranded:
        # Said out loud rather than swallowed: these are writes that exist on
        # this disk and not in R2, and if the volume goes with the container
        # they are gone.
        LOGGER.error(
            "shutdown left %s tenant(s) unsynced: %s", len(stranded), ", ".join(stranded)
        )
    return flushed


# ---------------------------------------------------------------------------
# lazy hydration + LRU eviction
# ---------------------------------------------------------------------------
def hydrate(user_id: str, destination: Path, *, cfg: Settings | None = None) -> bool:
    """Pull a tenant database down from R2 if it is not on this disk yet.

    This is what lets any node serve any tenant: a cold node with an empty
    volume fills in on demand instead of being wrong.
    """
    r2 = config()
    if not r2.enabled or Path(destination).exists():
        return False
    pulled = get_object(r2.key_for(user_id), Path(destination))
    if pulled:
        LOGGER.info("hydrated tenant %s from r2", user_id)
    return pulled


def local_tenant_files(cfg: Settings | None = None) -> list[Path]:
    cfg = cfg or settings()
    return sorted(cfg.users_dir.glob("user_*.db")) if cfg.users_dir.exists() else []


def _user_id_from_path(path: Path) -> str:
    return path.stem[len("user_") :] if path.stem.startswith("user_") else ""


def maybe_evict(*, cfg: Settings | None = None, keep: str | None = None) -> list[str]:
    """Drop the least-recently-used local tenant files over the cap.

    A file is only ever removed once its latest write is known to be in R2 —
    `is_dirty` is cleared only by a successful upload. So the worst case of a
    wrong guess here is a slower next request, never a lost write.
    """
    cfg = cfg or settings()
    if cfg.max_local_tenants <= 0 or not enabled():
        return []

    files = local_tenant_files(cfg)
    if len(files) <= cfg.max_local_tenants:
        return []

    from app.db import tenant as tenant_db

    with _lock:
        accessed = dict(_accessed)
    candidates = []
    for path in files:
        user_id = _user_id_from_path(path)
        if not user_id or user_id == keep or is_dirty(user_id):
            continue
        last = accessed.get(user_id)
        if last is None:
            try:
                last = path.stat().st_mtime - 10**9  # never-accessed sorts first
            except OSError:
                continue
        candidates.append((last, user_id, path))

    evicted: list[str] = []
    for _last, user_id, path in sorted(candidates):
        if len(files) - len(evicted) <= cfg.max_local_tenants:
            break
        tenant_db.close_tenant(user_id, cfg=cfg)
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        with _lock:
            _accessed.pop(user_id, None)
        evicted.append(user_id)
        LOGGER.info("evicted local tenant %s (synced)", user_id)
    return evicted
