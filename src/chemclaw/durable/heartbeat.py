"""Heartbeat-while-waiting: one idiom, extracted once it had three independent copies (Conn-F2).

`connectors.calc`'s two CREST-backed jobs (ensemble, complex) and `connectors.qm`'s HPC poll both
beat a Temporal activity's heartbeat at a cadence derived from a *configured* heartbeat timeout,
because both wrap a wait with nothing finer to report than "still running" — a single opaque CREST
subprocess with no unit boundary to hook a progress callback into, or a poll against an external
scheduler. `connectors.bo`'s BoFire fit/acquisition step is the third instance of exactly the same
shape (the fit is one opaque call into `botorch`, with the same "no unit to report progress at"
property), which is the Rule of Three CLAUDE.md asks for: a third copy is a helper, not a pattern.

Deliberately narrow: this covers the "opaque single call" case only. `connectors.calc`'s
species/scan-point loops and `connectors.qm`'s poll loop both have a natural per-iteration
boundary already and heartbeat directly at it (`activity.heartbeat` passed as `progress`, or called
once per poll) — that shape needs no wrapper, and forcing it through this one would only hide the
loop it is already heartbeating from.
"""

import asyncio
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
        # `asyncio.wait` does not cancel what it waits on, so every exit that is not "done" left
        # this task running with nobody awaiting it — a chemist's `cancel_job` returned promptly
        # and the work carried on, and if the orphan later raised, the exception was never
        # retrieved. Cancelling here is the one place that knows the task exists.
        #
        # **What cancellation actually reaches, measured rather than assumed.** A coroutine is
        # cancelled properly. A `to_thread` — which is what both `bo` propose activities are —
        # cancels only the *future*: the pooled thread runs to completion regardless, so the CPU
        # is released when the fit finishes, not when the cancel lands. The CREST and xtb
        # subprocesses under `calc` are bounded by `run_isolated`'s own timeout and its process
        # group kill, so their burn ends at that timeout rather than never.
        #
        # So this is necessary and not sufficient, and the insufficient half is a property of
        # `to_thread`, not of this function — see `docs/planning/DEFERRED.md`.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
