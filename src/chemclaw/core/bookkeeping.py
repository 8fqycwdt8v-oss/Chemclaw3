"""Writes a turn owes the record once it has acted: tracked, awaited at the end, never fatal.

A cost row, a budget booking and a spent approval are bookkeeping: losing one must not fail an
answered turn, and a failing one must not delay the answer for long. So each write runs as its own
task that swallows and counts its failure (the call site does, so it can name its subsystem), and
the turn then waits for the lot once, bounded, before it tells the client it is done. A turn
killed after that point has nothing left to lose.

`asyncio.wait` is the wait, not `gather` or `wait_for`: cancelling the waiter (a disconnect, a
Stop) must leave the writes running, as the teardown paths that cannot await rely on. Every task is
held in one registry so the event loop cannot collect it mid-write and a shutdown can drain it.
"""

import asyncio
import logging
from collections.abc import Coroutine, Iterable
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# Strong references: the event loop keeps only weak ones, so an unreferenced write could be
# garbage-collected mid-statement.
_PENDING: set["asyncio.Task[None]"] = set()


def schedule(work: Coroutine[Any, Any, None]) -> "asyncio.Task[None] | None":
    """Run `work` as a tracked task and return it, or `None` when no event loop is running.

    `work` must handle its own failure; a task that raises is nobody's to retrieve. With no loop
    there is nowhere to run it, so the coroutine is closed unstarted rather than leaked.
    """
    try:
        task = asyncio.get_running_loop().create_task(work)
    except RuntimeError:
        work.close()
        return None
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


def pending() -> frozenset["asyncio.Task[None]"]:
    """The writes in flight on the running loop — what a test waits on and a shutdown drains.

    A task belongs to the loop that created it and can only be awaited from there, so writes whose
    loop has ended are dropped from the registry instead of waited on.
    """
    loop = asyncio.get_running_loop()
    for task in [t for t in _PENDING if t.get_loop().is_closed()]:
        _PENDING.discard(task)
    return frozenset(task for task in _PENDING if task.get_loop() is loop)


async def settle(tasks: Iterable["asyncio.Task[None] | None"]) -> None:
    """Wait for these writes for at most `service_turn_bookkeeping_timeout_seconds`.

    Writes still running at the bound are left to finish on their own and counted, so a slow
    database delays an answer by the bound at most and never fails it. Raises `CancelledError` if
    the waiter is cancelled; the writes keep running.
    """
    live = {task for task in tasks if task is not None and not task.done()}
    if not live:
        return
    bound = settings.service_turn_bookkeeping_timeout_seconds
    _, unsettled = await asyncio.wait(live, timeout=bound)
    if unsettled:
        record_metric(
            lambda m: m.increment("chemclaw_bookkeeping_unsettled_total", float(len(unsettled)))
        )
        logger.warning(
            "%d bookkeeping write(s) had not landed after %.1fs; they continue in the background",
            len(unsettled),
            bound,
        )


async def drain() -> None:
    """Wait up to the bookkeeping bound for every write in flight, for an orderly shutdown.

    Called from the front door's lifespan after the turns drain; bounded because the pod is inside
    its termination grace.
    """
    writes = pending()
    if not writes:
        return
    bound = settings.service_turn_bookkeeping_timeout_seconds
    _, unfinished = await asyncio.wait(writes, timeout=bound)
    if unfinished:
        logger.warning(
            "%d bookkeeping write(s) did not land within %.1fs of shutdown", len(unfinished), bound
        )
