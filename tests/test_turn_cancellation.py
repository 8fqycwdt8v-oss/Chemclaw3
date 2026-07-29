"""An abandoned turn must not leak its permit or escape the budget (gap AGT-1, corrected).

The gap analysis claimed cancellation was unhandled — that a chemist closing the tab left the turn
running, holding an admission permit and never booking its tokens. **That claim was wrong**: the
hardening in `4bc9b04` already made the counters cancellation-safe, and this suite is the evidence.
It is kept (rather than dropped as a non-finding) because nothing previously *proved* the behavior,
so a future refactor could silently reintroduce exactly the leak that was alleged — an
`await` added to the runner's `finally`, or an `except Exception` widened to `BaseException`,
would do it, and both look harmless in review.

**Correction (D-130): this suite used to simulate the disconnect wrongly, and hid a real defect.**
Every case below closed the stream with `aclose()` and called that "what sse-starlette does when
the client disconnects". It is not. sse-starlette answers `http.disconnect` by cancelling its task
group and never calls `aclose()` on the body iterator at all, so a real disconnect raises
`CancelledError` inside the turn — while `aclose()` raises `GeneratorExit`. The runner caught only
the latter, so its rollback was dead code on the only path that matters, and this suite reported
green throughout. Both teardowns are now exercised: `aclose()` is still reachable (sse-starlette
uses it on a send timeout) and cancellation is the common case.

What is pinned here:
  1. Abandoning a turn still books the tokens metered so far (no free abandoned turns).
  2. Abandoning a turn releases the admission permit and the session's active-turn slot, so the
     session is not 409-bricked and capacity is returned.
  3. A half-written turn is rolled back under *both* teardowns, not just the one a test can reach
     by hand.
"""

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agent_framework import AgentSession

from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import Event
from chemclaw.api.runner import run_turn


def _closable(stream: AsyncIterator[Event]) -> AsyncGenerator[Event, None]:
    """`run_turn` is typed as an AsyncIterator; the concrete object is an async *generator*.

    The cast narrows the declared type to the real one rather than papering over a mismatch:
    sse-starlette does call `aclose()` when a send times out, so this teardown is reachable — it
    is simply not the one a client disconnect takes (see the module docstring).
    """
    return cast(AsyncGenerator[Event, None], stream)


async def _cancel_mid_turn(stream: AsyncIterator[Event], stalled: asyncio.Event) -> None:
    """Consume the turn until it stalls, then tear it down the way a real disconnect does.

    The consumption runs in its own task so it can be *cancelled* rather than closed — the whole
    distinction this helper exists to preserve, since cancelling is what uvicorn plus sse-starlette
    actually produce.

    Waiting for `stalled` is not politeness, it is the test's correctness condition. The cancel has
    to land while the consumer is suspended *inside* the turn; if it lands in the consumer's own
    frame instead, the abandoned generator is finalised later by `asyncio.run`'s async-generator
    shutdown, which delivers `GeneratorExit` — and a test written that way passes against the very
    bug it is meant to catch. (It did. That is how this was found.)
    """

    async def _consume() -> None:
        async for _event in stream:
            pass

    task = asyncio.create_task(_consume())
    await stalled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _update(text: str, tokens: int) -> Any:
    """A streamed update carrying `tokens` of reported usage, shaped as MAF emits it."""
    usage = SimpleNamespace(usage_details={"total_token_count": tokens})
    return SimpleNamespace(text=text, contents=[usage], user_input_requests=[])


class _EndlessAgent:
    """An agent whose turn never finishes on its own — so only cancellation ends the stream."""

    mcp_tools: list[Any] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            while True:
                yield _update("tok", 10)
                await asyncio.sleep(0)

        return _gen()


class _StatePoisoningAgent:
    """An agent that writes a tool call into session state and then never returns its result.

    This is the shape of the real failure (ISSUE-B-10): the model opens a `tool_use` block, and the
    client disconnects before the matching `tool_result` is ever appended.
    """

    mcp_tools: list[Any] = []

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            messages = session.state.setdefault("messages", [])
            messages.append({"role": "assistant", "tool_use_id": "call_1"})
            while True:
                yield _update("tok", 1)
                await asyncio.sleep(0)

        return _gen()


class _StallingAgent:
    """Emits a fixed number of updates and then blocks, announcing that it has.

    The block is what lets a test cancel the turn *from inside*: while it holds, the consumer is
    suspended in the agent's own frame, so `CancelledError` is delivered where a real disconnect
    delivers it. `stalled` makes that deterministic — no sleep long enough to "probably" be enough.
    """

    mcp_tools: list[Any] = []

    def __init__(self, *, updates: int = 1, poison: bool = False) -> None:
        self.stalled = asyncio.Event()
        self._updates = updates
        self._poison = poison

    def run(  # noqa: D102 - a fake agent's run, documented by its class
        self,
        message: str,
        *,
        stream: bool,
        session: AgentSession,
        **_run_options: Any,
    ) -> Any:
        async def _gen() -> Any:
            if self._poison:
                # The shape of the real failure (ISSUE-B-10): a `tool_use` block whose
                # `tool_result` never arrives, because the client left in between.
                session.state.setdefault("messages", []).append(
                    {"role": "assistant", "tool_use_id": "call_1"}
                )
            for _ in range(self._updates):
                yield _update("tok", 10)
            self.stalled.set()
            await asyncio.sleep(3600)

        return _gen()


class _RecordingBudget(BudgetTracker):
    """A tracker that remembers what the runner booked, so the test can assert on it."""

    def __init__(self) -> None:
        super().__init__()
        self.booked: list[tuple[str, str | None, int]] = []

    def record(self, session_id: str, user_id: str | None, tokens: int) -> None:
        self.booked.append((session_id, user_id, tokens))
        super().record(session_id, user_id, tokens)


def test_abandoned_turn_still_books_its_tokens() -> None:
    """Tokens spent before the client vanished count — otherwise abandon-and-retry is free.

    Without this, a user could bypass the token budget indefinitely by dropping each connection
    just before the answer, which is the cheapest possible attack on the runaway-cost guard.
    """
    budget = _RecordingBudget()

    async def _abandon() -> None:
        stream = _closable(
            run_turn(
                _EndlessAgent(), AgentSession(session_id="s1"), "hi", actor="u1", budget=budget
            )
        )
        consumed = 0
        async for _event in stream:
            consumed += 1
            if consumed == 3:
                break
        await stream.aclose()  # sse-starlette's send-timeout teardown

    asyncio.run(_abandon())

    assert budget.booked, "an abandoned turn booked nothing at all"
    session_id, user_id, tokens = budget.booked[0]
    assert (session_id, user_id) == ("s1", "u1")
    assert tokens >= 30, f"only {tokens} of the ~30 metered tokens were booked"


def test_abandoned_turn_releases_its_permit_and_turn_slot() -> None:
    """The permit and the per-session turn slot come back, so capacity is not lost."""
    active: set[str] = set()
    # Created inside the running loop: an asyncio.Semaphore binds to the loop that first awaits it.
    semaphore = asyncio.Semaphore(1)

    async def _guarded() -> list[str]:
        """Mirror the front door's wrapper: acquire, stream, release in `finally`."""
        await semaphore.acquire()
        active.add("s1")
        seen: list[str] = []
        stream = _closable(run_turn(_EndlessAgent(), AgentSession(session_id="s1"), "hi"))
        try:
            async for event in stream:
                seen.append(event.type)
                if len(seen) == 2:
                    break
        finally:
            await stream.aclose()
            semaphore.release()
            active.discard("s1")
        return seen

    async def _drive() -> None:
        await _guarded()
        assert not semaphore.locked(), "the admission permit was not returned"
        assert active == set(), "the session stayed marked as having a live turn (409-bricked)"
        # And the freed permit is immediately reusable by the next turn.
        await asyncio.wait_for(semaphore.acquire(), timeout=1)

    asyncio.run(_drive())


def test_client_disconnect_rolls_back_a_half_written_turn() -> None:
    """A disconnect mid-tool-call must not leave a dangling `tool_use` in the thread (ISSUE-B-10).

    Losing the interrupted turn is the cheap outcome. The expensive one is keeping it: a `tool_use`
    with no matching `tool_result` is replayed on every later turn, and the model rejects the whole
    thread, so one dropped connection would permanently brick the conversation rather than costing
    it a single answer. The earlier turns must survive — this is a rollback, not a wipe.
    """
    session = AgentSession(session_id="s3")
    session.state["messages"] = [{"role": "user", "text": "an earlier, completed turn"}]
    before = copy.deepcopy(session.state)

    async def _abandon() -> None:
        stream = _closable(run_turn(_StatePoisoningAgent(), session, "hi"))
        async for _event in stream:
            break  # the client goes away after the first token
        await stream.aclose()  # sse-starlette's send-timeout teardown

    asyncio.run(_abandon())

    assert session.state == before, "the half-written turn was left in the session thread"
    assert session.state["messages"] == [{"role": "user", "text": "an earlier, completed turn"}]


def test_a_cancelled_turn_rolls_back_a_half_written_turn() -> None:
    """The same rollback, reached the way a real disconnect reaches it: by cancellation.

    This is the case that was missing, and its absence is why the runner's rollback clause could
    catch `GeneratorExit` alone for as long as it did. Counterfactual: with
    `except GeneratorExit:` instead of `except (GeneratorExit, asyncio.CancelledError):`, the
    poisoned `tool_use` survives here while the `aclose()` test above still passes.
    """
    session = AgentSession(session_id="s4")
    session.state["messages"] = [{"role": "user", "text": "an earlier, completed turn"}]
    before = copy.deepcopy(session.state)

    async def _drive() -> None:
        agent = _StallingAgent(poison=True)
        await _cancel_mid_turn(run_turn(agent, session, "hi"), agent.stalled)
        # Asserted *inside* the loop. After `asyncio.run` returns, its async-generator shutdown has
        # closed every abandoned generator, which restores the state by the other path and would
        # make this pass no matter what the runner does with cancellation.
        assert session.state == before, "a cancelled turn left half-written state in the thread"
        assert session.state["messages"] == [{"role": "user", "text": "an earlier, completed turn"}]

    asyncio.run(_drive())


def test_a_cancelled_turn_still_books_its_tokens() -> None:
    """Cancellation is not a cheaper way to abandon a turn than closing the stream.

    The budget booking lives in the runner's `finally`, which runs under both teardowns — but
    "runs" is not "completes" when the task is cancelled, and that distinction is exactly what
    cost the durable claim release. Pinning it here means a future `await` added to that `finally`
    fails a test rather than silently making abandoned turns free.
    """
    budget = _RecordingBudget()
    session = AgentSession(session_id="s5")

    async def _drive() -> None:
        agent = _StallingAgent(updates=3)
        await _cancel_mid_turn(
            run_turn(agent, session, "hi", actor="u1", budget=budget), agent.stalled
        )
        assert budget.booked, "a cancelled turn booked nothing at all"
        booked_session, user_id, tokens = budget.booked[0]
        assert (booked_session, user_id) == ("s5", "u1")
        assert tokens >= 30, f"only {tokens} of the ~30 metered tokens were booked"

    asyncio.run(_drive())
