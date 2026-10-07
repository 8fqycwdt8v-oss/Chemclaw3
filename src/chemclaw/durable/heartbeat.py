"""Heartbeat-while-waiting for an activity wrapping one opaque call.

Used where there is nothing finer to report than "still running" — a single CREST subprocess, a
BoFire fit. Loops with a natural per-iteration boundary heartbeat directly at it and need no
wrapper.
"""

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import TypeVar

from temporalio import activity

_Result = TypeVar("_Result")

# Beats per heartbeat timeout: several, so scheduling jitter cannot push the only beat past the
# deadline.
_HEARTBEATS_PER_TIMEOUT = 4.0


async def beating(
    awaitable: Awaitable[_Result], what: str, heartbeat_timeout_seconds: float
) -> _Result:
    """Await `awaitable` while heartbeating, so a long opaque run is not declared dead.

    The beat interval is derived from the caller's `heartbeat_timeout_seconds`, so the two cannot
    drift apart.

    No exit from this wrapper leaves the wrapped work running: the `finally` cancels the task and
    then awaits it, so the caller resumes only after the work has finished unwinding (including its
    own cleanup, where a DB commit may live) — the same behaviour as `await x`. A `finally` rather
    than `except CancelledError`, because `activity.heartbeat` itself can raise.
    """
    task = asyncio.ensure_future(awaitable)
    interval = max(1.0, heartbeat_timeout_seconds / _HEARTBEATS_PER_TIMEOUT)
    elapsed = 0.0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return await task
            elapsed += interval
            activity.heartbeat(f"{what}: still running after {elapsed:.0f}s")
    finally:
        if not task.done():
            task.cancel()
            # Only `CancelledError` is suppressed: an error raised by the work's *own* cleanup is
            # what `await x` would surface too, so it is allowed to propagate.
            with contextlib.suppress(asyncio.CancelledError):
                await task
