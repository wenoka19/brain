"""`R-23`: the reader. Fetches videos from a residential connection.

YouTube refuses anonymous requests from datacentre IPs. That is measured, it
is not fixable from yt-dlp's side, and there is no official endpoint that
serves third-party captions at any price. So the fetch — and only the fetch —
moves to a machine on an ordinary home connection, which leases work from the
server over outbound HTTPS and posts text back.

What this process is, precisely:

* **Outbound only.** It calls the server; the server never calls it. No port
  forwarding, no tunnel, no public address, nothing to expose on a home
  router.
* **Blind.** It learns a job id, a URL the gatekeeper already validated, and
  a medium. Not who asked, not the note that steers extraction, not a
  database path.
* **Replaceable.** The same loop runs on a laptop, on a small always-on
  machine, or on a server with `HTTPS_PROXY` pointing at a residential proxy.
  Where the IP comes from is a deployment choice, not an architecture.

The design rule it follows, in the founder's words: do not dress up a failure
state you can avoid.

* A reader whose own tooling is out of date **does not lease**. It cannot fail
  a video for a fault of its own, because it never takes one. The jobs stay
  queued, the person's screen says what it already said, and the operator
  reads the remedy in this log.
* A platform refusal is **handed back**, not reported as a verdict. The server
  re-offers the job later and the person sees nothing.
* Nothing here raises. A loop that dies is a reader that is not reading.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any

import httpx

from app.config import Settings, settings
from app.ingestion import gatekeeper
from app.ingestion.providers import ProviderError

LOGGER = logging.getLogger("brain.fetch_worker")

#: How long to wait when the queue is empty. Long enough that an idle reader
#: is invisible in the server's logs, short enough that a person who just
#: pasted a link does not sit watching.
IDLE_SLEEP_SEC = 5.0
#: Backoff when the server cannot be reached, so a reader left running
#: through a night of downtime does not hammer it.
UNREACHABLE_SLEEP_SEC = 30.0
#: How often to re-check this machine's own tooling while it is unfit.
UNFIT_SLEEP_SEC = 60.0


class FetchWorker:
    """Lease, fetch, post. Nothing else."""

    def __init__(
        self,
        server: str,
        token: str,
        *,
        cfg: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self.cfg = cfg or settings()
        self.server = server.rstrip("/")
        self.token = token
        self._client = client
        self._fit: bool | None = None

    # ---- plumbing --------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(30.0, read=120.0),
                headers={"Authorization": f"Bearer {self.token}"},
                follow_redirects=False,
            )
        return self._client

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.post(
            f"{self.server}{path}",
            json=payload or {},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code == 401:
            raise PermissionError("the server refused this reader's token")
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        if response.status_code >= 400:
            raise RuntimeError(
                f"{path} answered {response.status_code}: "
                f"{body.get('message') or response.text[:200]}"
            )
        return body

    # ---- the one check that prevents a whole class of failure ------------
    def fit_to_read(self) -> bool:
        """Can this machine actually read a video right now?

        Asks yt-dlp, offline, the same way `doctor` does — which runtime it
        will use, whether it considers that runtime supported, and whether the
        challenge-solver scripts are installed.

        A reader that answers no here **takes no work**. That is the whole
        point: on 31 October 2026 a server whose deno was one minor version
        below yt-dlp's minimum failed every video in the queue and reported
        `0 failure(s). Ready.` while doing it. A reader that cannot read must
        be invisible, not destructive.
        """
        from app.ingestion.fetchers import youtube_readiness

        ready = youtube_readiness(cfg=self.cfg)
        if ready.ok:
            if self._fit is not True:
                LOGGER.info("reader is fit — %s, solver %s", ready.runtimes, ready.solver or "builtin")
            self._fit = True
            return True
        if self._fit is not False:
            LOGGER.error(
                "reader is NOT fit, so it will take no work: %s. Remedy: %s",
                ready.problem, ready.remedy,
            )
        self._fit = False
        return False

    # ---- one job ---------------------------------------------------------
    def fetch_one(self) -> dict[str, Any] | None:
        """Lease a job, read it, post the result. Returns what happened."""
        leased = self._post("/api/fetch-queue/lease").get("job")
        if not leased:
            return None
        job_id = leased["job_id"]
        url = leased["url"]
        LOGGER.info("leased %s — %s", job_id, url)

        try:
            content = self._read(url)
        except ProviderError as exc:
            LOGGER.warning("%s could not be read (%s): %s", job_id, exc.reason, exc.message)
            self._post(
                f"/api/fetch-queue/{job_id}/problem",
                {"reason": exc.reason, "message": exc.message},
            )
            return {"job_id": job_id, "outcome": "problem", "reason": exc.reason}
        except gatekeeper.GatekeeperError as exc:
            self._post(
                f"/api/fetch-queue/{job_id}/problem",
                {"reason": exc.reason, "message": exc.message},
            )
            return {"job_id": job_id, "outcome": "problem", "reason": exc.reason}
        except Exception as exc:  # the reader's own fault, never the video's
            LOGGER.exception("%s crashed in the reader", job_id)
            self._post(
                f"/api/fetch-queue/{job_id}/problem",
                {"reason": "reader_error", "message": str(exc)[:300]},
            )
            return {"job_id": job_id, "outcome": "problem", "reason": "reader_error"}

        posted = self._post(f"/api/fetch-queue/{job_id}/content", content)
        LOGGER.info(
            "%s read and posted — %s claims, status %s",
            job_id, posted.get("claims"), posted.get("status"),
        )
        return {"job_id": job_id, "outcome": "read", "claims": posted.get("claims")}

    def _read(self, url: str) -> dict[str, Any]:
        """The same fetch the server used to run, on this machine's IP."""
        from app.ingestion import fetchers

        target = gatekeeper.normalize_url(url, cfg=self.cfg)
        content = fetchers.fetch_youtube(target, cfg=self.cfg)
        payload = asdict(content)
        # The server takes these from the job row, not from here.
        payload.pop("source_type", None)
        payload.pop("metadata", None)
        payload.pop("content_hash", None)
        return payload

    # ---- the loop --------------------------------------------------------
    def run(self, *, once: bool = False, sleep=time.sleep) -> int:
        """Poll until stopped. Returns how many jobs were read."""
        read = 0
        while True:
            if not self.fit_to_read():
                if once:
                    return read
                sleep(UNFIT_SLEEP_SEC)
                continue
            try:
                outcome = self.fetch_one()
            except PermissionError as exc:
                # Wrong or rotated token. Nothing this loop does will fix it,
                # and retrying every five seconds would only fill a log.
                LOGGER.error("%s — stopping", exc)
                return read
            except Exception as exc:
                LOGGER.warning("server unreachable (%s) — retrying", exc)
                if once:
                    return read
                sleep(UNREACHABLE_SLEEP_SEC)
                continue
            if outcome and outcome.get("outcome") == "read":
                read += 1
            if once:
                return read
            if outcome is None:
                sleep(IDLE_SLEEP_SEC)
