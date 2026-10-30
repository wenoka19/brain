"""`R-17`: the received file is a receipt, not an asset. Sweep it after 30 days.

The founder's ruling, in their words: *"What we receive is a link, a photo,
and a document. After processing them there is no problem with the link, the
image, or the document. I think we should keep them for 30 days and when
those 30 days pass they should be deleted."*

Until this file existed, nothing deleted an upload. `api_upload` writes the
bytes to `data_dir/uploads/{user_id}/` and `op_ingest_file` queues a job
against that path; the one `unlink` in the upload route is the over-size
failure branch. A successful upload stayed on disk forever. That is the leak
this closes, and it is also the reason §7.10 reports no storage figure: once
the bytes are bounded by a window the product enforces, they stop being a
number about the person.

**Why the file is kept at all, instead of deleted the moment the job
finishes.** A job can fail, and the first thing to look at is what was
actually received. A person can also re-upload the same file the same week
and expect the duplicate to be recognised — that works off `content_hash` in
the tenant database, not the file, but a failed extraction is reproducible
only while the bytes are there. Thirty days is long enough for the person to
notice something came out wrong, and short enough that no account quietly
becomes a file server.

**Why age alone decides, with no look at `ingestion_jobs`.** A file is
needed until its job reaches a terminal status, and a job cannot outlive
this window: the lease is 180 seconds and `attempts` is bounded at 3
(`app/worker.py`), so a live job resolves in minutes, and a job past its
attempts is released rather than left pending. `blocked_by_platform` can sit
indefinitely waiting for a human, but §7.3's rescue takes *pasted text* —
`op_rescue_job` never re-reads the file. So the sweep needs no coupling to
`control.db`, which keeps it runnable from anywhere: the worker loop, the
CLI, a test.

A deletion here is permanent and has no trash buffer, which is the one place
in this product where that is right: the 30-day window *is* the buffer, and
§2's "a human deletes" rule is about the person's knowledge — claims, skills,
sources — not about a staging file they never see.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, settings

LOGGER = logging.getLogger("brain.retention")

#: Keep the sweep off the hot path: once an hour is plenty for a window
#: measured in days, and it means the worker's idle loop does not stat the
#: whole uploads tree every two seconds.
SWEEP_INTERVAL_SEC = 3600.0


@dataclass
class Sweep:
    """What one pass deleted. Returned so a caller can log or assert on it."""

    files: int = 0
    bytes: int = 0
    dirs: int = 0
    cutoff: float = 0.0
    failed: list[str] = field(default_factory=list)

    @property
    def deleted_anything(self) -> bool:
        return bool(self.files or self.dirs)


def sweep_uploads(*, cfg: Settings | None = None, now: float | None = None) -> Sweep:
    """Delete every upload older than `cfg.upload_retention_days`.

    `now` is injectable because the alternative is a test that sleeps for a
    month. Everything else reads the filesystem as it is: a file's age is its
    mtime, which is when the upload route finished writing it.
    """
    cfg = cfg or settings()
    now = time.time() if now is None else now
    cutoff = now - max(0, cfg.upload_retention_days) * 86400.0
    swept = Sweep(cutoff=cutoff)

    root = cfg.uploads_dir
    if not root.is_dir():
        return swept

    for user_dir in sorted(root.iterdir()):
        if not user_dir.is_dir():
            # Nothing writes a loose file at the root of `uploads/`. If one
            # appears it is not ours to guess at, so it is left alone.
            continue
        _sweep_one(user_dir, cutoff, swept)

    if swept.deleted_anything:
        LOGGER.info(
            "retention: deleted %s upload(s), %s byte(s), %s empty folder(s)",
            swept.files,
            swept.bytes,
            swept.dirs,
        )
    return swept


def _sweep_one(user_dir: Path, cutoff: float, swept: Sweep) -> None:
    remaining = 0
    for entry in sorted(user_dir.iterdir()):
        try:
            if not entry.is_file():
                remaining += 1
                continue
            stat = entry.stat()
            if stat.st_mtime >= cutoff:
                remaining += 1
                continue
            size = stat.st_size
            entry.unlink()
        except OSError as exc:
            # A sweep is housekeeping. One unreadable entry must not stop the
            # rest, and must not take the worker down with it.
            LOGGER.warning("retention: could not sweep %s: %s", entry.name, exc)
            swept.failed.append(entry.name)
            remaining += 1
            continue
        swept.files += 1
        swept.bytes += size

    if remaining:
        return
    # An account that uploaded once two years ago should not leave a folder
    # behind forever. `rmdir` only ever removes an empty one, so a file that
    # appeared mid-sweep keeps its folder.
    try:
        user_dir.rmdir()
    except OSError:
        return
    swept.dirs += 1


class Sweeper:
    """Rate-limits the sweep for a long-running loop.

    The worker calls `due()` every couple of seconds; it says yes on the
    first call (so a process that has just booted sweeps once, which is what
    covers a deployment that ran without a worker for a while) and then at
    most hourly.
    """

    def __init__(self, *, cfg: Settings | None = None, interval: float = SWEEP_INTERVAL_SEC) -> None:
        self.cfg = cfg or settings()
        self.interval = interval
        self._last: float | None = None

    def due(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self._last is None or now - self._last >= self.interval

    def run_if_due(self, *, now: float | None = None) -> Sweep | None:
        now = time.time() if now is None else now
        if not self.due(now=now):
            return None
        self._last = now
        return sweep_uploads(cfg=self.cfg, now=now)
