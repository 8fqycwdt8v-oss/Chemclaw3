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
  3. A turn cut short has its session state rolled back under *both* teardowns, not just the one a
     test can reach by hand — and a turn whose model run completed does not, however long the
     verifier or a job-result wait then holds it open.
  4. The transcript is all-or-nothing across a teardown: a turn that answered keeps its exchange,
     a turn that did not writes none. That pair is what replaced the durable rollback the runner
     used to carry (D-2026-08-10 §2), and it is why the rollback could go.
"""

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

import pytest

from chemclaw.agent.session import TurnSession
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import Event
from chemclaw.api.runner import run_turn
from tests.fakes_turn import Chunk, Piece, ScriptedTurn


def _closable(stream: AsyncIterator[Event]) -> AsyncGenerator[Event, None]:
    """`run_turn` is typed as an AsyncIterator; the concrete object is an async *generator*.

    The cast narrows the declared type to the real one rather than papering over a mismatch:
    sse-starlette does call `aclose()` when a send times out, so this teardown is reachable — it
    is simply not the one a client disconnect takes (see the module docstring).
    """
    return cast(AsyncGenerator[Event, None], stream)


async def _cancel_mid_turn(
    stream: AsyncIterator[Event], stalled: asyncio.Event, *, tokens: int = 0
) -> None:
    """Consume the turn until it stalls, then tear it down the way a real disconnect does.

    The consumption runs in its own task so it can be *cancelled* rather than closed — the whole
    distinction this helper exists to preserve, since cancelling is what uvicorn plus sse-starlette
    actually produce.

    Waiting for `stalled` is not politeness, it is the test's correctness condition. The cancel has
    to land while the consumer is suspended *inside* the turn; if it lands in the consumer's own
    frame instead, the abandoned generator is finalised later by `asyncio.run`'s async-generator
    shutdown, which delivers `GeneratorExit` — and a test written that way passes against the very
    bug it is meant to catch. (It did. That is how this was found.)

    `tokens` is the second half of that condition, and it exists because the two engines deliver a
    stalled model's earlier chunks at different moments. Under MAF the runner consumes the model's
    generator directly, so every chunk before the stall has already been metered by the time
    `stalled` is set; under LangGraph they sit in `astream`'s queue until the producer suspends, so
    the stall itself is what releases them and the cancel could otherwise land first. A test that
    asserts on *metered* tokens therefore waits for both facts, which is deterministic on either
    engine rather than a race one of them happens to win.
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

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        while True:
            yield Chunk("tok", output_tokens=10)
            # A suspension point per chunk, so the consumer is scheduled between them. Under MAF
            # this only yields the loop; under LangGraph it is what lets the stream's queue be
            # drained at all, since a producer that never suspends fills it unboundedly.
            await asyncio.sleep(0)


class _StatePoisoningAgent(ScriptedTurn):
    """An agent that writes a tool call into session state and then never returns its result.

    This is the shape of the real failure (ISSUE-B-10): the model opens a `tool_use` block, and the
    client disconnects before the matching `tool_result` is ever appended.

    The session is held rather than taken from the run call, because the graph engine's model is
    handed messages and not a `TurnSession` — the poisoning is a stand-in for whatever the turn
    committed, and what matters is that it lands in the state the runner snapshotted.
    """

    def __init__(self, session: TurnSession) -> None:
        """Poison `session`'s stored thread when the turn starts streaming."""
        self._session = session

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        messages = self._session.state.setdefault("messages", [])
        messages.append({"role": "assistant", "tool_use_id": "call_1"})
        while True:
            yield Chunk("tok", output_tokens=1)
            await asyncio.sleep(0)


class _AnsweringAgent(ScriptedTurn):
    """An agent that completes an ordinary turn: two tokens, then it returns.

    It stores nothing itself, and that is what the projection changed. Under MAF this fake had to
    append its own rows, because the framework committed the thread as it went and the runner never
    saw the write. `_record_transcript` is the writer now, so leaving the fake mute makes these
    tests drive the *real* write path rather than a hand-placed imitation of it.
    """

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        yield Chunk("the ", output_tokens=5)
        yield Chunk("answer", output_tokens=5)


class _RecordingHistory:
    """The transcript projection store, reduced to the one call the runner makes into it.

    Holding the rows in a list is what lets a test assert on what a teardown *left behind* rather
    than on whether a method was called — which is the question these tests ask, and the one that
    used to be put to the deleted `rollback_to`.
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

    The block is what lets a test cancel the turn *from inside*: while it holds, the consumer is
    suspended in the agent's own frame, so `CancelledError` is delivered where a real disconnect
    delivers it. `stalled` makes that deterministic — no sleep long enough to "probably" be enough.
    """

    def __init__(self, session: TurnSession, *, updates: int = 1, poison: bool = False) -> None:
        """Stream `updates` metered chunks into `session`'s turn, then stall."""
        self.stalled = asyncio.Event()
        self._session = session
        self._updates = updates
        self._poison = poison

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
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


def test_abandoned_turn_still_books_its_tokens() -> None:
    """Tokens spent before the client vanished count — otherwise abandon-and-retry is free.

    Without this, a user could bypass the token budget indefinitely by dropping each connection
    just before the answer, which is the cheapest possible attack on the runaway-cost guard.
    """
    budget = _RecordingBudget()
    agent = _EndlessAgent()

    async def _abandon() -> None:
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
        # Tokens, not events. Counting every event coupled the cut-off to how many *non*-token
        # events a turn happens to open with — the capability announcement alone moved it twice —
        # so the number of metered updates the assertion below depends on silently changed with
        # each. The turn's spend is carried by its tokens; count those.
        consumed = 0
        async for _event in stream:
            consumed += _event.type == "token"
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
    session = TurnSession(session_id="s3")
    session.state["messages"] = [{"role": "user", "text": "an earlier, completed turn"}]
    before = copy.deepcopy(session.state)

    agent = _StatePoisoningAgent(session)

    async def _abandon() -> None:
        stream = _closable(run_turn(session, "hi", graph_factory=agent.graph_factory))
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
    session = TurnSession(session_id="s4")
    session.state["messages"] = [{"role": "user", "text": "an earlier, completed turn"}]
    before = copy.deepcopy(session.state)

    async def _drive() -> None:
        agent = _StallingAgent(session, poison=True)
        await _cancel_mid_turn(
            run_turn(
                session,
                "hi",
                # Stated, as every sibling in this file states it: defaulting means every enabled
                # connector, none of which is running in a test process. It is no longer merely
                # noise — the runner hands `connectors` straight to `build_langgraph_agent`, and
                # the default is MAF's connector representation, which that builder cannot accept.
                # See the M13 note in `tasks/todo.md`; the engines' connector wiring is a defect of
                # its own and not this test's subject.
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

    asyncio.run(_drive())


def test_a_disconnect_after_the_answer_keeps_the_completed_turn() -> None:
    """A turn that answered keeps its transcript, however the stream is then torn down.

    The window is one send plus one round trip and it was open on the only path production takes:
    the client drops while sse-starlette is writing the `AnswerEvent`, and the runner's teardown
    clause runs unconditionally. Under MAF that clause ran `rollback_to`, which DELETEd the user
    and assistant rows the finished turn had already committed — the turn billed `completed=True`
    and its output gone, silent loss of conversation history, which in a GxP system is the
    expensive outcome rather than the cheap one.

    The rollback is gone, and this is the test that has to keep holding without it — which is
    exactly why it survives the deletion rather than going with it. `_record_transcript` writes the
    exchange in one call once the answer exists, so the property is now structural: there is no
    later step that could remove what this turn committed. Pinned under both teardowns — `aclose()`,
    which sse-starlette uses on a send timeout, and the cancellation a real disconnect delivers
    into the yield the answer is suspended in.
    """
    history = _RecordingHistory()
    session = TurnSession(session_id="s6")

    async def _drive() -> None:
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

    asyncio.run(_drive())
    assert len(history.rows) == 4, f"both completed turns should be stored: {history.rows}"


def test_a_disconnect_after_the_answer_is_billed_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TurnCost.completed` means "the turn answered", not "the turn was never torn down".

    The runner books `completed=answered`, so the flag draws its line at the answer and not at the
    teardown: a client dropping while sse-starlette writes the `AnswerEvent` produces a cancelled
    turn that nonetheless answered, kept its history, and must be billed as the completed turn it
    is. The neighbouring test pins the history half of that; this pins the ledger half, which had
    only the *failed*-turn direction pinned (`tests/test_turn_observability.py`) — so a change
    booking `completed=False` for every torn-down turn passed the suite and quietly made every
    disconnected-after-answering turn look abandoned in the spend ledger.
    """
    from chemclaw.agent.turn_cost import TurnCost

    booked: list[TurnCost] = []

    class _CapturingSink:
        async def record(self, cost: TurnCost) -> None:
            booked.append(cost)

    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", _CapturingSink)
    history = _RecordingHistory()

    async def _drive() -> None:
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

    asyncio.run(_drive())

    assert len(booked) == 1, "a turn torn down after answering never reached the cost ledger"
    assert booked[0].completed is True, (
        "a turn that answered was billed as incomplete because its client then disconnected"
    )


def test_a_cancelled_turn_still_books_its_tokens() -> None:
    """Cancellation is not a cheaper way to abandon a turn than closing the stream.

    The budget booking lives in the runner's `finally`, which runs under both teardowns — but
    "runs" is not "completes" when the task is cancelled, and that distinction is exactly what
    cost the durable claim release. Pinning it here means a future `await` added to that `finally`
    fails a test rather than silently making abandoned turns free.
    """
    budget = _RecordingBudget()
    session = TurnSession(session_id="s5")

    async def _drive() -> None:
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
        assert tokens >= 30, f"only {tokens} of the ~30 metered tokens were booked"

    asyncio.run(_drive())


def test_a_turn_torn_down_before_answering_writes_no_transcript_row() -> None:
    """Nothing to roll back, because nothing was written — the other half of the deleted guard.

    The durable rollback existed for the opposite arrangement: MAF committed the thread as the turn
    went, so a disconnect mid-tool-call left a `tool_use` with no `tool_result`, every later turn
    replayed it, the model rejected the thread outright, and one dropped connection permanently
    bricked the conversation. Deleting the rollback is only safe if that half-written state cannot
    occur, so this is the claim the deletion rests on and it has to be pinned rather than argued.

    `_record_transcript` runs once, after the answer exists, and writes the user message and the
    answer in a single call. A teardown therefore lands on one side or the other: before it, and
    the store is untouched (here), or after it, and the exchange is whole (the two tests above).
    Nothing moves this test to green except moving that write, which is precisely the change that
    would reintroduce the failure.
    """
    history = _RecordingHistory()
    history.rows = [
        ("s-blind", "user: an earlier question"),
        ("s-blind", "assistant: an earlier answer"),
    ]
    before = list(history.rows)
    session = TurnSession(session_id="s-blind")

    async def _drive() -> None:
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

    asyncio.run(_drive())


class _StateWritingAgent(ScriptedTurn):
    """An answering agent that also advances `session.state`, the way the harness does.

    The state write is the thing under test in the two windows below. It stands in for a completed
    todo, a consumed approval, a recorded plan hash — whatever the turn's model run legitimately
    settled before the post-run wait began.
    """

    def __init__(self, session: TurnSession) -> None:
        """Advance `session`'s state as the turn streams."""
        self._session = session

    async def stream(self, message: str) -> AsyncIterator[Piece]:  # noqa: D102 - see the base class
        self._session.state["todos"] = ["done"]
        yield Chunk("the ", output_tokens=5)
        yield Chunk("answer", output_tokens=5)


def test_a_disconnect_during_a_slow_verifier_keeps_the_run_s_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown predicate is "the model run returned", not "the answer was yielded".

    Between the run finishing and the AnswerEvent the turn still awaits the verifier — an LLM
    call. A disconnect or the front door's wall-clock deadline landing in that window finds
    `answered` still False, and on the `answered`-only predicate would roll the session state back
    over a run that had genuinely completed: the verifier, a scoring aid, silently undoing the work
    it was scoring. `run_complete` is what draws the line in the right place, and this is the test
    that fails if someone simplifies the predicate to the flag that reads like the obvious one.

    This used to assert on `history.rows` instead, because MAF committed the stored thread during
    the run and the teardown then DELETEd it. Both halves of that are gone — the projection writes
    once, after the answer — so the surviving guard is the state rollback, and that is what it now
    asks about. The window and the predicate are unchanged.
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

    async def _drive() -> None:
        agent = _StateWritingAgent(session)
        await _cancel_mid_turn(
            run_turn(session, "hi", connectors=[], graph_factory=agent.graph_factory),
            stalled,
        )
        assert session.state.get("todos") == ["done"], (
            f"a slow verifier made the teardown roll a finished run's state back: {session.state}"
        )

    asyncio.run(_drive())


def test_a_disconnect_during_a_slow_job_result_wait_keeps_the_run_s_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other window between the run and the answer: `await_job_results` under mid-turn resume.

    The wait can hold the turn open for `mid_turn_resume_timeout_seconds`, and the run whose state
    it holds has completed — so a teardown inside it has nothing to undo. (Once the resume's
    *second* run starts the turn is genuinely mid-flight again and the rollback re-arms: that is
    the `run_complete = False` around the resume.)
    """
    from chemclaw.core.config import settings
    from chemclaw.core.turn_signals import record_job_started

    monkeypatch.setattr(settings, "mid_turn_resume_enabled", True)
    session = TurnSession(session_id="s-slow-resume")
    stalled = asyncio.Event()

    class _JobAgent(_StateWritingAgent):
        """A state-writing agent whose turn also launched a durable job, so the wait runs."""

        async def stream(  # noqa: D102 - see `ScriptedTurn`
            self, message: str
        ) -> AsyncIterator[Piece]:
            record_job_started("job-slow", "qm")
            async for piece in super().stream(message):
                yield piece

    async def _stalling_wait(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
        stalled.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr("chemclaw.api.runner.await_job_results", _stalling_wait)

    async def _drive() -> None:
        agent = _JobAgent(session)
        await _cancel_mid_turn(
            run_turn(session, "hi", connectors=[], graph_factory=agent.graph_factory),
            stalled,
        )
        assert session.state.get("todos") == ["done"], (
            f"a slow job-result wait rolled a finished run's state back: {session.state}"
        )

    asyncio.run(_drive())
