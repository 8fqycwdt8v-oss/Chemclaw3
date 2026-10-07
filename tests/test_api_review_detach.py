"""The detachable turn: pump failures are retrieved and logged, and `stop` propagates its own
cancel.

`api/detach.py` runs on a path nobody watches once a client detaches, so a control there can stop
working unnoticed; these tests drive `_note_pump_failure` and `stop` directly.
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
    """A pump that raises is logged and its exception retrieved.

    The retrieval must not live on a reader branch: `_pump`'s `finally` offers `_DONE` first, so a
    reader never reaches it. `task._log_traceback` is private but asserted, because it is the only
    thing that distinguishes "retrieved" from "also printed at GC".
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
    """A detached turn that raises is still retrieved.

    It has no reader, so retrieval on any reader-side path would report nothing.
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

    The pump then finishes with a result, so a `CancelledError` at `await self._task` can only be
    the caller's own.
    """
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        return
    yield {"event": "token", "data": "unreachable"}  # pragma: no cover - the sleep never returns


def test_stop_re_raises_a_cancellation_addressed_to_its_own_caller() -> None:
    """`stop` re-raises a cancellation addressed to its own caller.

    A client abandoning the stop request, or a draining pod, cancels the stop handler; swallowing it
    would break asyncio's rule that a cancellation propagates. A done callback registered before
    `stop()` awaits delivers the cancel while the stopper waits on an already-finished task, so the
    ordering is deterministic without sleeps.
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


async def test_stop_still_swallows_the_turn_s_own_cancellation() -> None:
    """The other direction: an ordinary Stop must still return, not raise at its caller."""
    turn = DetachableTurn(_forever(), session_id="s-ordinary")
    await asyncio.sleep(0.02)
    await turn.stop()
    assert turn._task.cancelled()
