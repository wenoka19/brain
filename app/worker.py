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
from app.storage import r2_sync, retention

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
        # `R-17`. The worker is the one role that exists in both deployments
        # (`BRAIN_ROLE=worker`, and in-process under `=web`), so hanging the
        # sweep here means every production container runs it and no surface
        # has to remember to.
        self.sweeper = retention.Sweeper(cfg=self.cfg)

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

    def sweep_if_due(self) -> retention.Sweep | None:
        """§7.10's missing storage meter, enforced. Never raises.

        Housekeeping must not be able to stop the queue: an upload that
        cannot be deleted is a warning in the log, not a dead worker.
        """
        try:
            return self.sweeper.run_if_due()
        except Exception:
            LOGGER.exception("upload sweep failed; the queue carries on")
            return None

    async def run(self, *, max_jobs: int | None = None) -> list[dict[str, Any]]:
        LOGGER.info("worker started in %s mode", self.cfg.env)
        while not self._stop.is_set():
            # Before claiming, so a container that boots sweeps once even if
            # the queue never goes idle.
            await asyncio.to_thread(self.sweep_if_due)
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
    # The worker writes tenant databases too, so it carries its own snapshot
    # thread — otherwise an ingestion's claims would sit dirty until the web
    # process happened to touch that tenant.
    r2_sync.start_background(cfg=cfg)
    worker = Worker(cfg=cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run(max_jobs=max_jobs)
    finally:
        r2_sync.stop_background(cfg=cfg)
        tenant_db.close_all()
        control_db.close_all()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
