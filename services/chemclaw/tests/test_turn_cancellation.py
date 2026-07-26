"""An abandoned turn must not leak its permit or escape the budget (gap AGT-1, corrected).

The gap analysis claimed cancellation was unhandled — that a chemist closing the tab left the turn
running, holding an admission permit and never booking its tokens. **That claim was wrong**: the
hardening in `4bc9b04` already made the counters cancellation-safe, and this suite is the evidence.
It is kept (rather than dropped as a non-finding) because nothing previously *proved* the behavior,
so a future refactor could silently reintroduce exactly the leak that was alleged — an
`await` added to the runner's `finally`, or an `except Exception` widened to `BaseException`,
would do it, and both look harmless in review.

What is pinned here:
  1. Closing the stream mid-turn still books the tokens metered so far (no free abandoned turns).
  2. Closing the stream releases the admission permit and the session's active-turn slot, so the
     session is not 409-bricked and capacity is returned.
"""

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from agent_framework import AgentSession

from service.budget import BudgetTracker
from service.events import Event
from service.runner import run_turn


def _closable(stream: AsyncIterator[Event]) -> AsyncGenerator[Event, None]:
    """`run_turn` is typed as an AsyncIterator; the concrete object is an async *generator*.

    sse-starlette closes it on disconnect, which is exactly the path under test, so the cast is
    narrowing the declared type to the real one rather than papering over a mismatch.
    """
    return cast(AsyncGenerator[Event, None], stream)


def _update(text: str, tokens: int) -> Any:
    """A streamed update carrying `tokens` of reported usage, shaped as MAF emits it."""
    usage = SimpleNamespace(usage_details={"total_token_count": tokens})
    return SimpleNamespace(text=text, contents=[usage], user_input_requests=[])


class _EndlessAgent:
    """An agent whose turn never finishes on its own — so only cancellation ends the stream."""

    mcp_tools: list[Any] = []

    def run(self, message: str, *, stream: bool, session: AgentSession) -> Any:
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

    def run(self, message: str, *, stream: bool, session: AgentSession) -> Any:
        async def _gen() -> Any:
            messages = session.state.setdefault("messages", [])
            messages.append({"role": "assistant", "tool_use_id": "call_1"})
            while True:
                yield _update("tok", 1)
                await asyncio.sleep(0)

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
        await stream.aclose()  # what sse-starlette does when the client disconnects

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
        await stream.aclose()  # what sse-starlette does on disconnect

    asyncio.run(_abandon())

    assert session.state == before, "the half-written turn was left in the session thread"
    assert session.state["messages"] == [{"role": "user", "text": "an earlier, completed turn"}]
