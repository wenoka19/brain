"""Standalone ingestion worker with lease recovery.

In production this process drains `ingestion_jobs` continuously. It is safe to
run several replicas: a job is claimed under `BEGIN IMMEDIATE` with a lease, so
two workers never process the same job, and a worker killed mid-job leaves a
lease that expires and is picked up again — with `attempts` bounded so a
poisonous job cannot loop forever.

In a Cloud Session VM there is no daemon between prompts, which is why every
other surface can call `engine.run_pending_jobs_sync()` itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from typing import Any

from app.config import Settings, settings
from app.core.engine import process_job
from app.db import control as control_db
from app.db import tenant as tenant_db

LOGGER = logging.getLogger("brain.worker")
IDLE_SLEEP_SEC = 2.0
ERROR_BACKOFF_SEC = 5.0
LEASE_SECONDS = 180
MAX_ATTEMPTS = 3


class Worker:
    def __init__(self, *, cfg: Settings | None = None, poll_interval: float = IDLE_SLEEP_SEC) -> None:
        self.cfg = cfg or settings()
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self.processed: list[dict[str, Any]] = []

    def request_stop(self) -> None:
        LOGGER.info("stop requested; finishing the current job")
        self._stop.set()

    def drain_once(self) -> dict[str, Any] | None:
        """Claim and run exactly one job. Returns its outcome, or None if idle."""
        control = control_db.control()
        job = control_db.claim_next_job(
            control, lease_seconds=LEASE_SECONDS, max_attempts=MAX_ATTEMPTS
        )
        if job is None:
            return None
        LOGGER.info("job %s (%s) claimed, attempt %s", job["job_id"], job["source_type"], job["attempts"])
        try:
            outcome = process_job(job, cfg=self.cfg)
        except Exception as exc:  # a crash must not take the worker down
            LOGGER.exception("job %s crashed", job["job_id"])
            control_db.set_job_status(
                control, job["job_id"], "failed", error_message=f"Worker error: {exc}"
            )
            control_db.release_quota(
                control,
                job["user_id"],
                minutes=job["reserved_minutes"],
                files=job["reserved_files"],
            )
            outcome = {"job_id": job["job_id"], "status": "failed", "claims": 0, "message": str(exc)}
        LOGGER.info("job %s -> %s (%s claims)", outcome["job_id"], outcome["status"], outcome["claims"])
        self.processed.append(outcome)
        return outcome

    async def run(self, *, max_jobs: int | None = None) -> list[dict[str, Any]]:
        LOGGER.info("worker started in %s mode", self.cfg.env)
        while not self._stop.is_set():
            try:
                # The pipeline is synchronous and SQLite-bound, so it runs in a
                # thread to keep the event loop free for signals.
                outcome = await asyncio.to_thread(self.drain_once)
            except Exception:
                LOGGER.exception("worker loop error; backing off")
                await self._sleep(ERROR_BACKOFF_SEC)
                continue
            if outcome is None:
                await self._sleep(self.poll_interval)
                continue
            if max_jobs is not None and len(self.processed) >= max_jobs:
                break
        LOGGER.info("worker stopped after %s job(s)", len(self.processed))
        return self.processed

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)


async def main_async(*, max_jobs: int | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    cfg = settings()
    cfg.ensure_dirs()
    control_db.control()  # create/migrate before the first claim
    worker = Worker(cfg=cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run(max_jobs=max_jobs)
    finally:
        tenant_db.close_all()
        control_db.close_all()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
