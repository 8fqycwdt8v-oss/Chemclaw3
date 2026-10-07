"""A turn cancelled during connector teardown must stay cancelled.

`HeldConnectorSession._shut_down` awaits the holder task, where a cancellation of the calling task
(a closed tab, or `asyncio.timeout(service_turn_timeout_seconds)`) is delivered. Swallowing it would
skip `run_turn`'s rollback and defeat the turn deadline, since `Task.cancel()` delivers once. The
holder's own `anyio` scope unwind must still be absorbed; `_is_really_cancelled()` (reading
`Task.cancelling()`) tells the two apart. Driven at the object, because only `_shut_down` decides
which task owns the exception.
"""

import asyncio

from chemclaw.connectors.transport import ConnectorSpec, HeldConnectorSession

_SPEC = ConnectorSpec(
    name="calc",
    connection={"transport": "streamable_http", "url": "http://127.0.0.1:1/mcp"},
    allowed_tools=("compute_xtb_energy",),
)


def _session_with_holder(holder: asyncio.Task[None]) -> HeldConnectorSession:
    """A holder whose task is `holder` — the state `__aenter__` leaves behind on a live turn."""
    session = HeldConnectorSession(_SPEC)
    session._task = holder
    return session


def test_a_caller_cancelled_inside_connector_teardown_stays_cancelled() -> None:
    """The defect: the cancelled turn finished normally and ran the code after the teardown."""

    async def scenario() -> tuple[bool, bool]:
        async def _slow_unwind() -> None:
            # A connector pod that takes its time closing the session. Longer than the caller
            # waits, so the cancellation is delivered while `_shut_down` is suspended on it.
            await asyncio.sleep(0.5)

        holder = asyncio.create_task(_slow_unwind())
        session = _session_with_holder(holder)
        ran_after_teardown = False

        async def caller() -> None:
            nonlocal ran_after_teardown
            await session._shut_down()
            # `run_turn`'s work after the exit stack closes. On a cancelled turn this must be
            # unreachable; reaching it is the audit's measured "code after the await ran = True".
            ran_after_teardown = True

        turn = asyncio.create_task(caller())
        # One loop pass is enough to get `caller` suspended on `await task` inside `_shut_down`.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        turn.cancel()
        try:
            await turn
        except asyncio.CancelledError:
            pass
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return turn.cancelled(), ran_after_teardown

    cancelled, ran_after_teardown = asyncio.run(scenario())

    assert not ran_after_teardown, (
        "the cancelled turn ran the code after connector teardown; `run_turn`'s rollback clause "
        "is skipped and `asyncio.timeout` never converts to TimeoutError"
    )
    assert cancelled, "the turn task completed normally after being cancelled"


def test_the_holders_own_scope_unwind_is_still_absorbed() -> None:
    """The holder's own scope unwind is still absorbed.

    Unwinding the MCP session's `anyio` cancel scope raises `CancelledError` on the holder without
    anyone cancelling the turn; re-raising it would fail every clean close.
    """

    async def scenario() -> bool:
        async def _scope_unwind() -> None:
            raise asyncio.CancelledError

        holder = asyncio.create_task(_scope_unwind())
        session = _session_with_holder(holder)
        await session._shut_down()
        return True

    assert asyncio.run(scenario()) is True


def test_a_holder_that_fails_on_the_way_out_is_still_absorbed() -> None:
    """The other half: a connector that errors while closing costs its close, never the turn."""

    async def scenario() -> bool:
        async def _broken_unwind() -> None:
            raise RuntimeError("streamable-http session already closed")

        holder = asyncio.create_task(_broken_unwind())
        session = _session_with_holder(holder)
        await session._shut_down()
        return True

    assert asyncio.run(scenario()) is True
