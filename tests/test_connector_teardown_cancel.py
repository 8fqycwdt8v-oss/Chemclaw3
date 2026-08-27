"""A turn cancelled *during* connector teardown must stay cancelled.

`HeldConnectorSession._shut_down` awaits the holder task, and that `await` is the suspension point
at which a cancellation of the **calling** task is delivered — a chemist closing the tab, or
`asyncio.timeout(service_turn_timeout_seconds)` in `api/routes/turns.py` expiring while a wedged
connector pod is slow to close its streamable-HTTP session. The clause around it suppressed
`asyncio.CancelledError` along with `Exception`, so the caller's own cancellation was swallowed and
the turn ran on: `run_turn`'s `except (GeneratorExit, asyncio.CancelledError)` clause — the one that
rolls back the half-written exchange — was never entered, and `asyncio.timeout.__aexit__` never saw
the `CancelledError` it converts into `TimeoutError`, so the front door's wall-clock deadline did
nothing at all. A `Task.cancel()` delivers its exception once; swallowing it does not re-arm it.

The suppression itself is right for what it was written for, and the second test pins that half:
the *holder* task's unwind raises `CancelledError` out of the MCP client's own `anyio` cancel
scope, and that must still be absorbed or every clean teardown would fail the turn. The module
already owns the discriminator between the two — `_is_really_cancelled()`, which reads
`Task.cancelling()` — and `absorb_connect_failure` four lines up already uses it.

Driven at the object rather than through a live server, because the property under test is which
task the exception belongs to, and that is decided by `_shut_down` alone; a real connector adds a
socket to the picture and nothing to the question.
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
    """The half the suppression was written for, kept: an inner scope's unwind is not the turn's.

    The MCP session is an `anyio` cancel scope and unwinding it raises `CancelledError` on the
    holder task without anyone having cancelled *this* one. Re-raising that blanket would turn
    every clean connector close into a cancelled turn — which is why the fix reuses
    `_is_really_cancelled()` rather than simply dropping `CancelledError` from the clause.
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
