"""Heartbeat-while-waiting: one idiom, extracted once it had three independent copies (Conn-F2).

`connectors.calc`'s two CREST-backed jobs (ensemble, complex) beat a Temporal activity's
heartbeat at a cadence derived from a *configured* heartbeat timeout, because each wraps a wait
with nothing finer to report than "still running" — a single opaque CREST subprocess with no unit
boundary to hook a progress callback into. `connectors.bo`'s BoFire fit/acquisition step is the
third instance of exactly the same shape (the fit is one opaque call into `botorch`, with the same
"no unit to report progress at" property), which is the Rule of Three CLAUDE.md asks for: a third
copy is a helper, not a pattern.

Deliberately narrow: this covers the "opaque single call" case only. `connectors.calc`'s
species/scan-point loops have a natural per-iteration boundary already and heartbeat directly at it
(`activity.heartbeat` passed as `progress`) — that shape needs no wrapper, and forcing it through
this one would only hide the loop it is already heartbeating from.
"""

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import TypeVar

from temporalio import activity

_Result = TypeVar("_Result")

# Beats per heartbeat timeout. Several, not one: a single beat placed exactly at the deadline
# leaves no margin for scheduling jitter, and Temporal only needs to hear *something* before the
# configured timeout lapses.
_HEARTBEATS_PER_TIMEOUT = 4.0


async def beating(
    awaitable: Awaitable[_Result], what: str, heartbeat_timeout_seconds: float
) -> _Result:
    """Await `awaitable` while heartbeating, so a long opaque run is not declared dead.

    `heartbeat_timeout_seconds` is the caller's own configured `heartbeat_timeout` for this
    activity — the beat interval is derived from it rather than fixed, so a deployment that
    shortens the timeout shortens the beat with it and the two can never drift apart. A timer
    rather than a progress callback because there is genuinely nothing to report inside the
    wait: the honest signal is "still running", and pretending to know how far along it is would
    be a worse lie than saying nothing.

    **No exit from this wrapper leaves the wrapped work running.** The awaitable runs as a task so
    the timer can run beside it, and `asyncio.wait` does *not* cancel what it was waiting on when
    the waiter is cancelled — so without the `finally` below, an activity that stopped waiting
    would return while its real work carried on detached, still writing.

    Two things make that a `finally` and not an `except asyncio.CancelledError`, and both were
    measured rather than reasoned:

    - **Cancellation is not the only way out.** `activity.heartbeat` raises outside an activity
      context, and can raise inside one if the details payload fails to serialise. A handler keyed
      on `CancelledError` let that exception past while the task ran on — the same detached-write
      defect through a different door.
    - **`task.cancel()` only files the request.** Re-raising immediately after it unwound the
      caller while the work was still inside its own `except`/`finally`, which is where the DB
      commit lives: the window shrank from unbounded to "the length of the work's cleanup" rather
      than closing. Awaiting the cancelled task is what makes `beating(x)` behave-alike to
      `await x` — the caller does not resume until the work has finished unwinding — and that is
      the only thing a caller wrapping an existing `await` in it can reasonably assume. It
      inherits the same limit `await x` has: work that refuses cancellation blocks here exactly as
      it would there.
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
