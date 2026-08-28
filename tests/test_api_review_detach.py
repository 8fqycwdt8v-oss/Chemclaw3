"""The detachable turn's three quiet defects: a dead control, and a swallowed cancellation.

`api/detach.py` is where a client's connection stops being the turn's lifeline, so everything in it
runs on a path nobody is watching by construction. That is exactly the condition under which a
control can stop working without anybody noticing, and one had: `_note_pump_failure` was documented
as the thing that keeps a raised pump from being reported by asyncio at garbage-collection time
"under no session and no correlation id", and it had never once been called.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import pytest

from chemclaw.api.detach import DetachableTurn


class _Boom(RuntimeError):
    """A failure from *above* `run_turn`, which is the only kind that reaches the pump."""


async def _raising(after: int = 0) -> AsyncIterator[dict[str, str]]:
    """Yield `after` events, then raise the way a pump failure actually arrives."""
    for index in range(after):
        yield {"event": "token", "data": str(index)}
    raise _Boom("the pump ended by raising")


async def _quiet() -> AsyncIterator[dict[str, str]]:
    """One event and a clean end — the control case for the failure test."""
    yield {"event": "token", "data": "0"}


async def _forever() -> AsyncIterator[dict[str, str]]:
    """A turn that never ends on its own — what the Stop button is for."""
    while True:
        await asyncio.sleep(0.01)
        yield {"event": "token", "data": "."}


async def _slow_then_raise() -> AsyncIterator[dict[str, str]]:
    """One event, a pause long enough for the reader to go, then a raise."""
    yield {"event": "token", "data": "0"}
    await asyncio.sleep(0.02)
    raise _Boom("the detached pump ended by raising")


# --------------------------------------------------------------------------------------------
# 6 — the pump failure that was reported by nobody.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("events_before_the_raise", [0, 1, 8])
def test_a_pump_that_raises_is_logged_and_its_exception_retrieved(
    caplog: pytest.LogCaptureFixture, events_before_the_raise: int
) -> None:
    """Measured across eight raise scenarios before this: **0 log records, 0 calls**.

    And in all eight `task._log_traceback` was still `True` — asyncio's own flag for "I will print
    `Task exception was never retrieved` when this is collected", which is the outcome
    `_note_pump_failure`'s docstring says it prevents. The cause was placement: it was called on
    one branch of `_next_event`, and a raising pump cannot reach that branch, because `_pump`'s
    `finally` offers `_DONE` first and the parked reader wakes with the marker one line earlier.

    `_log_traceback` is a private attribute and is asserted deliberately: it is the only thing that
    distinguishes "retrieved" from "logged by us and *also* shouted about at GC", and the shout is
    the half a reader of the log never sees.
    """

    async def _run() -> "asyncio.Task[None]":
        turn = DetachableTurn(_raising(events_before_the_raise), session_id="s-raise")
        async for _event in turn.events():
            pass
        # The done callback is scheduled with `call_soon`, so it lands on the next tick.
        await asyncio.sleep(0)
        return turn._task

    with caplog.at_level(logging.WARNING):
        task = asyncio.run(_run())

    warnings = [r for r in caplog.records if "ended by raising" in r.getMessage()]
    assert len(warnings) == 1, f"the pump failure produced {len(warnings)} records"
    assert "s-raise" in warnings[0].getMessage(), "the record does not name the session"
    assert warnings[0].exc_info is not None and warnings[0].exc_info[0] is _Boom
    assert task._log_traceback is False, (
        "asyncio will still print 'Task exception was never retrieved' at collection time"
    )


def test_a_clean_pump_and_a_stopped_one_say_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The callback fires on *every* ending, so the two ordinary ones must stay silent.

    A cancelled pump is the Stop button working, and a pump that ran out of events is a turn that
    answered. A control that logged either would be a control nobody reads.
    """

    async def _run() -> None:
        clean = DetachableTurn(_quiet(), session_id="s-clean")
        async for _event in clean.events():
            pass
        stopped = DetachableTurn(_forever(), session_id="s-stopped")
        await asyncio.sleep(0.02)
        await stopped.stop()
        await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())

    assert not [r for r in caplog.records if "ended by raising" in r.getMessage()]


def test_a_detached_turn_that_raises_is_still_retrieved() -> None:
    """The case that rules out both reader-side placements the review offered.

    A detached turn has no reader: `events()` has already returned and `_next_event` will never be
    called again. If the retrieval lived on either, this failure would be reported by nobody —
    the same hole one level along.
    """

    async def _run() -> "asyncio.Task[None]":
        turn = DetachableTurn(_slow_then_raise(), session_id="s-gone")

        async def _read_one() -> None:
            async for _event in turn.events():
                raise asyncio.CancelledError  # the client dropped after the first event

        reader = asyncio.create_task(_read_one())
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        async with asyncio.timeout(5):
            while turn.running:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        return turn._task

    task = asyncio.run(_run())
    assert task._log_traceback is False, "a detached turn's failure is retrieved by nobody"


# --------------------------------------------------------------------------------------------
# 8 — `stop()` and whose cancellation it just caught.
# --------------------------------------------------------------------------------------------


async def _swallows_cancel() -> AsyncIterator[dict[str, str]]:
    """A source that absorbs the stop and lets the pump end *normally*.

    That is what makes the two cancellations distinguishable in fact rather than only in the code:
    the pump task finishes with a result, so `self._task.cancelled()` is `False` and a
    `CancelledError` arriving at `await self._task` can only be the caller's own.
    """
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        return
    yield {"event": "token", "data": "unreachable"}  # pragma: no cover - the sleep never returns


def test_stop_re_raises_a_cancellation_addressed_to_its_own_caller() -> None:
    """`except CancelledError: pass` cannot tell the awaited task's cancel from its own.

    The stop route's handler is an ordinary task: a client that gives up on the stop request, or a
    pod draining, cancels *it* while it waits on the turn. Swallowing that returns a 200 to nobody
    and leaves a task that was asked to stop running as though it had not been — asyncio's rule is
    that a cancellation addressed to a frame propagates out of it.

    **The ordering here is the test, so it is spelled out.** A done callback registered *before*
    `stop()` awaits runs before the awaiting task's own `__wakeup`, so the cancel lands while the
    stopper is still suspended on an already-finished task. `Task.cancel()` then returns `False`
    (the pump is done), the stopper is marked `_must_cancel`, and the `CancelledError` is delivered
    at `await self._task` — from the caller, on a turn that was never cancelled. Timing this with
    sleeps instead would be a race dressed as a test.
    """

    async def _run() -> "asyncio.Task[None]":
        turn = DetachableTurn(_swallows_cancel(), session_id="s-stopper")
        holder: list[asyncio.Task[None]] = []
        turn._task.add_done_callback(lambda _t: holder[0].cancel())
        await asyncio.sleep(0.02)  # the pump is parked inside the source
        stopper = asyncio.create_task(turn.stop())
        holder.append(stopper)
        await asyncio.sleep(0.05)  # stop() cancels the pump, which ends by returning
        with contextlib.suppress(asyncio.CancelledError):
            await stopper
        assert not turn._task.cancelled(), "the pump was cancelled; the two are indistinguishable"
        return stopper

    stopper = asyncio.run(_run())
    assert stopper.cancelled(), (
        "stop() swallowed a cancellation addressed to its own caller and returned normally"
    )


def test_stop_still_swallows_the_turn_s_own_cancellation() -> None:
    """The other direction: an ordinary Stop must still return, not raise at its caller."""

    async def _run() -> None:
        turn = DetachableTurn(_forever(), session_id="s-ordinary")
        await asyncio.sleep(0.02)
        await turn.stop()
        assert turn._task.cancelled()

    asyncio.run(_run())
