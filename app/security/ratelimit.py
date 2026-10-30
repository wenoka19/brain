"""Per-user sliding-window rate limiting + runaway-agent circuit breaker.

Two independent protections (OWASP MCP02 / MCP08 / MCP10):

* **Sliding windows** — 30 tool calls/min and 10 ingestions/min per user.
* **Loop breaker** — the *same* tool called with the *same* arguments inside a
  15 second window is refused with a polite message that tells the agent to
  stop retrying rather than to try harder.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from app.config import settings

TOOL_BUCKET = "tool_calls"
INGEST_BUCKET = "ingestions"
#: §2's ceiling is "max 30 tool calls/min" and its subject is an agent in a
#: runaway loop. A browser is not that, and it does not spend its budget the
#: same way: every frame reads `/api/counts` plus its own list, so clicking
#: through the app for a minute could lock the person out of their own
#: library with a message written for an LLM. The web gets its own window,
#: which leaves §2's guarantee for the MCP surface exactly where it was.
WEB_BUCKET = "web_requests"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    retry_after: float = 0.0
    message: str = ""

    def __bool__(self) -> bool:  # lets callers write `if decision:`
        return self.allowed


ALLOWED = Decision(allowed=True)


def fingerprint(tool_name: str, arguments: dict[str, Any] | None) -> str:
    """Stable hash of a tool invocation, used by the loop breaker."""
    payload = json.dumps(arguments or {}, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(f"{tool_name}\x00{payload}".encode("utf-8")).hexdigest()
    return digest[:32]


class RateLimiter:
    """Thread-safe, in-process limiter. One instance per server process."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        tool_calls_per_min: int | None = None,
        ingestions_per_min: int | None = None,
        web_requests_per_min: int | None = None,
        duplicate_window_sec: float | None = None,
    ) -> None:
        cfg = settings()
        self._clock = clock
        self._limits = {
            TOOL_BUCKET: tool_calls_per_min or cfg.tool_calls_per_min,
            INGEST_BUCKET: ingestions_per_min or cfg.ingestions_per_min,
            WEB_BUCKET: web_requests_per_min or cfg.web_requests_per_min,
        }
        self._dup_window = (
            cfg.duplicate_call_window_sec
            if duplicate_window_sec is None
            else duplicate_window_sec
        )
        self._windows: dict[tuple[str, str], deque[float]] = {}
        self._recent: dict[tuple[str, str], tuple[float, int]] = {}
        self._lock = threading.Lock()

    # ---- sliding windows --------------------------------------------------
    def _consume(self, user_id: str, bucket: str) -> Decision:
        limit = self._limits[bucket]
        now = self._clock()
        window = self._windows.setdefault((user_id, bucket), deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= limit:
            retry_after = max(0.0, 60.0 - (now - window[0]))
            return Decision(
                allowed=False,
                reason=f"{bucket}_rate_limited",
                retry_after=round(retry_after, 2),
                message=(
                    f"Rate limit reached ({limit} {bucket.replace('_', ' ')}/min). "
                    f"Pause for {retry_after:.0f}s before calling brain again, and "
                    "tell the user what you already have rather than retrying."
                ),
            )
        window.append(now)
        return ALLOWED

    # ---- loop breaker -----------------------------------------------------
    def _check_loop(self, user_id: str, tool_name: str, arguments: dict[str, Any] | None) -> Decision:
        if self._dup_window <= 0:
            return ALLOWED
        key = (user_id, fingerprint(tool_name, arguments))
        now = self._clock()
        last, count = self._recent.get(key, (None, 0))
        if last is not None and now - last < self._dup_window:
            self._recent[key] = (last, count + 1)
            return Decision(
                allowed=False,
                reason="duplicate_call_circuit_breaker",
                retry_after=round(self._dup_window - (now - last), 2),
                message=(
                    f"Circuit breaker: `{tool_name}` was already called with these exact "
                    "arguments moments ago and the answer has not changed. Use the "
                    "previous result instead of repeating the call."
                ),
            )
        self._recent[key] = (now, 1)
        self._prune(now)
        return ALLOWED

    def _prune(self, now: float) -> None:
        if len(self._recent) < 512:
            return
        horizon = self._dup_window * 4
        for key, (seen, _) in list(self._recent.items()):
            if now - seen > horizon:
                self._recent.pop(key, None)

    # ---- public API -------------------------------------------------------
    def check(
        self,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        is_ingestion: bool = False,
        loop_guard: bool = True,
        bucket: str = TOOL_BUCKET,
    ) -> Decision:
        """Screen one tool call. Returns the first failing protection.

        `loop_guard=False` turns off the duplicate check for this call only.
        It exists for an idempotent read from the person's own browser: Back,
        a reload, two tabs and a second visit to the same list are navigation,
        not a runaway agent, and refusing them turns the product's own pages
        into errors. The sliding window still applies — that is the ceiling
        that actually bounds abuse — and every write keeps the guard, because
        a double-submitted POST is precisely what it is for.

        `bucket` picks which sliding window this call spends from. The web
        surface passes `WEB_BUCKET` so that a person navigating their own
        pages cannot exhaust the ceiling §2 sets for agents — and so that an
        agent cannot exhaust theirs.
        """
        with self._lock:
            if loop_guard:
                loop = self._check_loop(user_id, tool_name, arguments)
                if not loop.allowed:
                    return loop
            decision = self._consume(user_id, bucket)
            if not decision.allowed:
                return decision
            if is_ingestion:
                ingest = self._consume(user_id, INGEST_BUCKET)
                if not ingest.allowed:
                    return ingest
            return ALLOWED

    def reset(self, user_id: str | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._windows.clear()
                self._recent.clear()
                return
            for key in [k for k in self._windows if k[0] == user_id]:
                self._windows.pop(key, None)
            for key in [k for k in self._recent if k[0] == user_id]:
                self._recent.pop(key, None)


_limiter: RateLimiter | None = None


def limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None
