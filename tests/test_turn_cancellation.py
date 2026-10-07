"""An abandoned turn must not leak its permit or escape the budget.

A real client disconnect reaches the turn as `CancelledError` (sse-starlette cancels its task
group); `aclose()` and its `GeneratorExit` occur only on a send timeout. Both teardowns are
exercised. Pinned here:

  1. Abandoning a turn still books the tokens metered so far.
  2. Abandoning a turn releases the admission permit and the session's active-turn slot.
  3. A turn cut short has its session state rolled back under both teardowns, and a turn whose
     model run completed does not, however long the verifier or a job-result wait holds it open.
  4. The transcript is all-or-nothing across a teardown: a turn that answered keeps its
     exchange, a turn that did not writes none.
"""

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages.utils import count_tokens_approximately

from chemclaw.agent.session import TurnSession
from chemclaw.agent.turn_usage import _prompt_estimate
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import Event
from chemclaw.api.runner import run_turn
from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_correlation_id,
    get_current_roles,
)
from chemclaw.core.metrics import METRICS
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.turn_flags import is_dry_run
from tests.fakes_turn import Chunk, Piece, ScriptedTurn


def _closable(stream: AsyncIterator[Event]) -> AsyncGenerator[Event, None]:
    """Narrow `run_turn`'s declared AsyncIterator to the async generator it really is.

    sse-starlette calls `aclose()` on a send timeout, so this teardown is reachable.
    """
    return cast(AsyncGenerator[Event, None], stream)


async def _cancel_mid_turn(
    stream: AsyncIterator[Event], stalled: asyncio.Event, *, tokens: int = 0
) -> None:
    """Consume the turn until it stalls, then cancel it the way a real disconnect does.

    The consumer runs in its own task so it can be cancelled rather than closed. Waiting for
    `stalled` makes the cancel land while the consumer is suspended inside the turn; landing in the
    consumer's own frame would deliver `GeneratorExit` at shutdown instead and pass against the bug.
    Waiting for `tokens` too ensures earlier chunks queued in `astream` were metered first.
    """
    counted = asyncio.Event()
    seen = 0

    async def _consume() -> None:
        nonlocal seen
        async for event in stream:
            seen += event.type == "token"
            if seen >= tokens:
                counted.set()

    if not tokens:
        counted.set()
    task = asyncio.create_task(_consume())
    await stalled.wait()
    await counted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class _EndlessAgent(ScriptedTurn):
    """An agent whose turn never finishes on its own — so only cancellation ends the stream."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        while True:
            yield Chunk("tok", output_tokens=10)
            # A suspension point per chunk, so the consumer is scheduled between them. Under MAF
            # this only yields the loop; under LangGraph it is what lets the stream's queue be
            # drained at all, since a producer that never suspends fills it unboundedly.
            await asyncio.sleep(0)


class _StatePoisoningAgent(ScriptedTurn):
    """An agent that writes a tool call into session state and never returns its result.

    The shape of the real failure: a `tool_use` block with no matching `tool_result` when the client
    disconnects. What matters is that it lands in the state the runner snapshotted.
    """

    def __init__(self, session: TurnSession) -> None:
        """Poison `session`'s stored thread when the turn starts streaming."""
        self._session = session

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        messages = self._session.state.setdefault("messages", [])
        messages.append({"role": "assistant", "tool_use_id": "call_1"})
        while True:
            yield Chunk("tok", output_tokens=1)
            await asyncio.sleep(0)


class _AnsweringAgent(ScriptedTurn):
    """An agent that completes an ordinary turn: two tokens, then it returns.

    It stores nothing itself, so these tests drive the real `_record_transcript` write path.
    """

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield Chunk("the ", output_tokens=5)
        yield Chunk("answer", output_tokens=5)


class _RecordingHistory:
    """The transcript projection store, reduced to the one call the runner makes into it.

    Rows are kept so a test asserts on what a teardown left behind.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    async def save_messages(
        self, session_id: str, messages: list[Any], *, state: dict[str, Any] | None = None
    ) -> None:
        """Append this turn's exchange, the way `PostgresHistoryProvider` commits it."""
        for message in messages:
            role = "user" if message.type == "human" else "assistant"
            self.rows.append((session_id, f"{role}: {message.content}"))


class _StallingAgent(ScriptedTurn):
    """Emits a fixed number of updates and then blocks, announcing that it has.

    While it blocks, the consumer is suspended in the agent's frame, so `CancelledError` is
    delivered where a real disconnect delivers it; `stalled` makes that deterministic.
    """

    def __init__(self, session: TurnSession, *, updates: int = 1, poison: bool = False) -> None:
        """Stream `updates` metered chunks into `session`'s turn, then stall."""
        self.stalled = asyncio.Event()
        self._session = session
        self._updates = updates
        self._poison = poison

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        if self._poison:
            # The shape of the real failure (ISSUE-B-10): a `tool_use` block whose
            # `tool_result` never arrives, because the client left in between.
            self._session.state.setdefault("messages", []).append(
                {"role": "assistant", "tool_use_id": "call_1"}
            )
        for _ in range(self._updates):
            yield Chunk("tok", output_tokens=10)
        self.stalled.set()
        await asyncio.sleep(3600)


class _RecordingBudget(BudgetTracker):
    """A tracker that remembers what the runner booked, so the test can assert on it."""

    def __init__(self) -> None:
        super().__init__()
        self.booked: list[tuple[str, str | None, int]] = []

    def record(self, session_id: str, user_id: str | None, tokens: int) -> None:
        self.booked.append((session_id, user_id, tokens))
        super().record(session_id, user_id, tokens)


async def test_abandoned_turn_still_books_its_tokens() -> None:
    """Tokens spent before the client vanished count, otherwise abandon-and-retry is free.

    A gateway reports usage only on the terminal frame, so a cut-off turn bills the estimated prompt
    already handed to the provider (`agent/turn_usage.InFlightPrompts`). Asserted: it is non-zero,
    it lands in `chemclaw_estimated_tokens_total` and binds the budget, and `chemclaw_tokens_total`
    (measured spend) does not move. No figure is asserted, since it depends on the bound tools.
    """
    budget = _RecordingBudget()
    agent = _EndlessAgent()
    measured_before = METRICS.value("chemclaw_tokens_total")
    inferred_before = METRICS.value("chemclaw_estimated_tokens_total")

    stream = _closable(
        run_turn(
            TurnSession(session_id="s1"),
            "hi",
            actor="u1",
            budget=budget,
            graph_factory=agent.graph_factory,
            # Stated, because this test counts updates to decide when to abandon: defaulting
            # means every enabled connector, none of which is running in a test process, and
            # the resulting degradation event (D-139) is noise in that count.
            connectors=[],
        )
    )
    # Count tokens, not events: the number of non-token events a turn opens with is incidental.
    consumed = 0
    async for _event in stream:
        consumed += _event.type == "token"
        if consumed == 3:
            break
    await stream.aclose()  # sse-starlette's send-timeout teardown

    assert budget.booked, "an abandoned turn booked nothing at all"
    session_id, user_id, tokens = budget.booked[0]
    assert (session_id, user_id) == ("s1", "u1")
    assert tokens > 0, "the prompt this turn had already been billed for was booked as free"
    assert METRICS.value("chemclaw_tokens_total") == measured_before, (
        "an estimate was published as though a provider had reported it"
    )
    inferred = METRICS.value("chemclaw_estimated_tokens_total") - inferred_before
    assert inferred == tokens, (
        "the tokens this turn was billed for reached the budget and no counter, so the fleet-wide "
        "spend rate under-reports every abandoned turn by its whole prompt"
    )


def test_an_abandoned_prompt_charges_the_system_message_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abandoned prompt charges the system message once.

    `prefix_tokens()` already includes the system message the prompt contains, so the estimate is
    `prompt + (prefix - system)`; pinned against a stubbed prefix so a flipped sign cannot pass. A
    prefix smaller than its system message clamps rather than going negative.
    """
    system = SystemMessage(content="you are a process chemist. " * 100)
    prompt = [system, HumanMessage(content="hello " * 50), AIMessage(content="hi " * 50)]
    system_tokens = int(count_tokens_approximately([system]))
    prompt_tokens = int(count_tokens_approximately(prompt))

    monkeypatch.setattr("chemclaw.agent.turn_usage.prefix_tokens", lambda: 5_000)

    assert _prompt_estimate([prompt]) == prompt_tokens + 5_000 - system_tokens, (
        "the system message is inside `prefix_tokens()` and inside the prompt, so charging it "
        "twice bills an abandoned turn its whole prefix over again"
    )

    monkeypatch.setattr("chemclaw.agent.turn_usage.prefix_tokens", lambda: 1)

    assert _prompt_estimate([prompt]) == prompt_tokens, (
        "a prefix smaller than the system message inside it booked a negative schema cost"
    )


def test_a_prompt_that_cannot_be_estimated_books_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt that cannot be estimated books exactly zero rather than ending the turn.

    A fabricated token would be spend nothing produced, charged against a budget.
    """

    class _Hostile:
        def __bool__(self) -> bool:
            return True

        def __getitem__(self, index: int) -> Any:
            raise RuntimeError("not the shape upstream promised")

    assert _prompt_estimate(_Hostile()) == 0


async def test_abandoned_turn_releases_its_permit_and_turn_slot() -> None:
    """The permit and the per-session turn slot come back, so capacity is not lost."""
    active: set[str] = set()
    # Created inside the running loop: an asyncio.Semaphore binds to the loop that first awaits it.
    semaphore = asyncio.Semaphore(1)

    async def _guarded() -> list[str]:
        """Mirror the front door's wrapper: acquire, stream, release in `finally`."""
        await semaphore.acquire()
        active.add("s1")
        seen: list[str] = []
        agent = _EndlessAgent()
        stream = _closable(
            run_turn(TurnSession(session_id="s1"), "hi", graph_factory=agent.graph_factory)
        )
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

    await _guarded()
    assert not semaphore.locked(), "the admission permit was not returned"
    assert active == set(), "the session stayed marked as having a live turn (409-bricked)"
    # And the freed permit is immediately reusable by the next turn.
    await asyncio.wait_for(semaphore.acquire(), timeout=1)


async def test_client_disconnect_rolls_back_a_half_written_turn() -> None:
    """A disconnect mid-tool-call does not leave a dangling `tool_use` in the thread.

    A `tool_use` with no `tool_result` is replayed every later turn and the model rejects the whole
    thread. Earlier turns must survive: this is a rollback, not a wipe.
    """
    session = TurnSession(session_id="s3")
    session.state["messages"] = [{"role": "user", "text": "an earlier, completed turn"}]
    before = copy.deepcopy(session.state)

    agent = _StatePoisoningAgent(session)

    stream = _closable(run_turn(session, "hi", graph_factory=agent.graph_factory))
    async for _event in stream:
        break  # the client goes away after the first token
    await stream.aclose()  # sse-starlette's send-timeout teardown

    assert session.state == before, "the half-written turn was left in the session thread"
    assert session.state["messages"] == [{"role": "user", "text": "an earlier, completed turn"}]


async def test_a_cancelled_turn_rolls_back_a_half_written_turn() -> None:
    """The same rollback, reached by cancellation as a real disconnect reaches it.

    With `except GeneratorExit:` alone, the poisoned `tool_use` would survive here while the
    `aclose()` test above still passes.
    """
    session = TurnSession(session_id="s4")
    session.state["messages"] = [{"role": "user", "text": "an earlier, completed turn"}]
    before = copy.deepcopy(session.state)

    agent = _StallingAgent(session, poison=True)
    await _cancel_mid_turn(
        run_turn(
            session,
            "hi",
            # Stated explicitly: the default means every enabled connector, none of which runs here.
            connectors=[],
            graph_factory=agent.graph_factory,
        ),
        agent.stalled,
    )
    # Asserted *inside* the loop. After `asyncio.run` returns, its async-generator shutdown has
    # closed every abandoned generator, which restores the state by the other path and would
    # make this pass no matter what the runner does with cancellation.
    assert session.state == before, "a cancelled turn left half-written state in the thread"
    assert session.state["messages"] == [{"role": "user", "text": "an earlier, completed turn"}]


async def test_a_disconnect_after_the_answer_keeps_the_completed_turn() -> None:
    """A turn that answered keeps its transcript, however the stream is then torn down.

    `_record_transcript` writes the exchange in one call once the answer exists, so no later step
    can remove it. Pinned under `aclose()` and under cancellation delivered into the yield the
    answer is suspended in.
    """
    history = _RecordingHistory()
    session = TurnSession(session_id="s6")

    for teardown in ("aclose", "cancel"):
        agent = _AnsweringAgent()
        stream = _closable(
            run_turn(
                session,
                f"hi ({teardown})",
                history=history,
                connectors=[],
                graph_factory=agent.graph_factory,
            )
        )
        seen: list[str] = []
        async for event in stream:
            seen.append(event.type)
            if event.type == "answer":
                break  # the client goes away while the answer is being sent
        assert seen[-1] == "answer", f"the turn never answered: {seen}"
        if teardown == "aclose":
            await stream.aclose()
        else:
            with pytest.raises(asyncio.CancelledError):
                await stream.athrow(asyncio.CancelledError())
        assert history.rows[-1][1] == "assistant: the answer", (
            f"the answered turn's committed rows did not survive a {teardown} teardown: "
            f"{history.rows}"
        )

    assert len(history.rows) == 4, f"both completed turns should be stored: {history.rows}"


async def test_a_disconnect_after_the_answer_is_billed_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect after the answer is billed as completed.

    `TurnCost.completed` means "the turn answered", not "the turn was never torn down"; otherwise
    every disconnect-after-answer would look abandoned in the spend ledger.
    """
    from chemclaw.agent.turn_cost import TurnCost

    booked: list[TurnCost] = []

    class _CapturingSink:
        async def record(self, cost: TurnCost) -> None:
            booked.append(cost)

    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", _CapturingSink)
    history = _RecordingHistory()

    agent = _AnsweringAgent()
    stream = _closable(
        run_turn(
            TurnSession(session_id="s-answered-cancel"),
            "hi",
            history=history,
            connectors=[],
            graph_factory=agent.graph_factory,
        )
    )
    async for event in stream:
        if event.type == "answer":
            break  # the client goes away while the answer is being sent
    with pytest.raises(asyncio.CancelledError):
        await stream.athrow(asyncio.CancelledError())
    # The ledger write is scheduled on the loop rather than awaited (see `record_turn_cost`).
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(booked) == 1, "a turn torn down after answering never reached the cost ledger"
    assert booked[0].completed is True, (
        "a turn that answered was billed as incomplete because its client then disconnected"
    )


async def test_a_cancelled_turn_still_books_its_tokens() -> None:
    """A cancelled turn still books its tokens.

    The booking lives in the runner's `finally`, which a cancellation may not complete if an `await`
    is added there; this fails if that happens.
    """
    budget = _RecordingBudget()
    session = TurnSession(session_id="s5")

    agent = _StallingAgent(session, updates=3)
    await _cancel_mid_turn(
        run_turn(
            session,
            "hi",
            actor="u1",
            budget=budget,
            connectors=[],
            graph_factory=agent.graph_factory,
        ),
        agent.stalled,
        # The assertion below is about metered tokens, so the cancel waits for all three to
        # have reached the runner as well as for the model to have stalled.
        tokens=3,
    )
    assert budget.booked, "a cancelled turn booked nothing at all"
    booked_session, user_id, tokens = budget.booked[0]
    assert (booked_session, user_id) == ("s5", "u1")
    assert tokens > 0, "a cancelled turn was billed nothing for the prompt it had sent"


async def test_a_cancelled_turn_unstamps_every_ambient_it_stamped() -> None:
    """A cancelled turn unstamps every ambient it stamped.

    Otherwise the next turn on the worker would run under the disconnected user's identity. Driven
    directly rather than through `_cancel_mid_turn`: that helper runs in a task, which copies the
    context and would make the "after" assertion vacuous, whereas an async generator runs in its
    driver's context. `athrow` delivers `CancelledError` at the suspension point inside the turn.
    """
    session = TurnSession(session_id="s-ambient")

    agent = _StallingAgent(session)
    # `_closable` for its narrowing rather than for `aclose`: `run_turn` is declared
    # `AsyncIterator`, and `athrow` — the delivery a real disconnect makes — is on the concrete
    # async *generator*. The same cast every sibling here uses, for the same reason.
    stream = _closable(
        run_turn(
            session,
            "hi",
            actor="oid-ambient",
            roles=frozenset({"chemist"}),
            dry_run=True,
            connectors=[],
            graph_factory=agent.graph_factory,
        )
    )
    await stream.__anext__()
    assert get_current_session_id() == "s-ambient"
    assert get_current_actor() == "oid-ambient"
    assert get_current_roles() == frozenset({"chemist"})
    assert get_current_correlation_id(), "the turn stamped no correlation id"
    assert is_dry_run() is True
    with pytest.raises(asyncio.CancelledError):
        await stream.athrow(asyncio.CancelledError())
    assert get_current_session_id() is None, "the session id outlived its turn"
    assert get_current_actor() is None, "the turn's identity leaked past its teardown"
    assert get_current_roles() == frozenset(), "the turn's roles leaked past its teardown"
    assert get_current_correlation_id() is None, "the correlation id outlived its turn"
    assert is_dry_run() is False, "the dry-run flag leaked past its turn"


async def test_a_turn_torn_down_before_answering_writes_no_transcript_row() -> None:
    """A turn torn down before answering writes no transcript row.

    `_record_transcript` runs once, after the answer, writing question and answer together, so a
    teardown leaves either nothing or a whole exchange; no half-written `tool_use` can be stored.
    """
    history = _RecordingHistory()
    history.rows = [
        ("s-blind", "user: an earlier question"),
        ("s-blind", "assistant: an earlier answer"),
    ]
    before = list(history.rows)
    session = TurnSession(session_id="s-blind")

    agent = _StallingAgent(session, poison=True)
    await _cancel_mid_turn(
        run_turn(
            session,
            "hi",
            history=history,
            connectors=[],
            graph_factory=agent.graph_factory,
        ),
        agent.stalled,
    )
    assert history.rows == before, (
        f"a turn that never answered still committed a transcript row: {history.rows}"
    )


class _StateWritingAgent(ScriptedTurn):
    """An answering agent that also advances `session.state`, the way the harness does.

    The write stands in for whatever the model run legitimately settled before the post-run wait.
    """

    def __init__(self, session: TurnSession) -> None:
        """Advance `session`'s state as the turn streams."""
        self._session = session

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        self._session.state["todos"] = ["done"]
        yield Chunk("the ", output_tokens=5)
        yield Chunk("answer", output_tokens=5)


async def test_a_disconnect_during_a_slow_verifier_keeps_the_run_s_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect during a slow verifier keeps the run's state.

    The teardown predicate is "the model run returned" (`run_complete`), not "the answer was
    yielded"; otherwise the verifier window would roll back a completed run. This fails if the
    predicate is simplified to `answered`.
    """
    from chemclaw.agent.verifier import VerificationResult
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "verifier_enabled", True)
    session = TurnSession(session_id="s-slow-verify")
    stalled = asyncio.Event()

    async def _stalling_verify(answer: str, *_args: Any, **_kwargs: Any) -> VerificationResult:
        stalled.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr("chemclaw.agent.verifier.verify_turn_answer", _stalling_verify)

    agent = _StateWritingAgent(session)
    await _cancel_mid_turn(
        run_turn(session, "hi", connectors=[], graph_factory=agent.graph_factory),
        stalled,
    )
    assert session.state.get("todos") == ["done"], (
        f"a slow verifier made the teardown roll a finished run's state back: {session.state}"
    )


async def test_a_disconnect_during_a_slow_job_result_wait_keeps_the_run_s_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect during a slow job-result wait keeps the run's state.

    `await_job_results` may hold the turn open after its run completed. Once a resumed second run
    starts, `run_complete` is reset and the rollback re-arms.
    """
    from chemclaw.core.config import settings
    from chemclaw.core.turn_signals import record_job_started

    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)
    session = TurnSession(session_id="s-slow-resume")
    stalled = asyncio.Event()

    class _JobAgent(_StateWritingAgent):
        """A state-writing agent whose turn also launched a durable job, so the wait runs."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            record_job_started("job-slow", "qm")
            async for piece in super().stream(message):
                yield piece

    async def _stalling_wait(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
        stalled.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _stalling_wait)

    agent = _JobAgent(session)
    await _cancel_mid_turn(
        run_turn(session, "hi", connectors=[], graph_factory=agent.graph_factory),
        stalled,
    )
    assert session.state.get("todos") == ["done"], (
        f"a slow job-result wait rolled a finished run's state back: {session.state}"
    )
