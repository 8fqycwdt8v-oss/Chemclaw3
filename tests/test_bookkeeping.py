"""A turn's bookkeeping is awaited once, bounded, and can never fail or stall the turn.

Driven through the real `run_turn` with a scripted model. The cost sink and the durable budget
window are replaced by writes that fail or hang, because the claim is about what the turn does when
its bookkeeping misbehaves: it still answers, and it answers within the bound.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from chemclaw.agent.session import TurnSession
from chemclaw.agent.turn_cost import TurnCost
from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.api import budget_store
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.events import Event
from chemclaw.api.runner import run_turn
from chemclaw.core import bookkeeping
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.fakes_turn import Chunk, Piece, ScriptedTurn


class _Answers(ScriptedTurn):
    """A turn that answers, reporting some usage."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        """One reply."""
        yield Chunk(text="four", input_tokens=7, output_tokens=3)


class _Sink:
    """A cost sink whose write is whatever the test says."""

    def __init__(self, write: Any) -> None:
        """Remember what `record` does."""
        self.write = write
        self.recorded: list[TurnCost] = []

    async def record(self, cost: TurnCost) -> None:
        """Run the configured behaviour, then keep the row."""
        await self.write()
        self.recorded.append(cost)


async def _fails() -> None:
    raise RuntimeError("terminating connection due to administrator command")


async def _hangs() -> None:
    await asyncio.sleep(30)


async def _lands() -> None:
    return None


async def _turn(budget: BudgetTracker | None = None) -> tuple[list[Event], float]:
    """Run one turn and report its events and how long it took."""
    started = time.perf_counter()
    events = [
        event
        async for event in run_turn(
            TurnSession(session_id="bookkeeping-s"),
            "what is 2+2?",
            actor="bookkeeping-ana",
            budget=budget,
            connectors=[],
            graph_factory=_Answers().graph_factory,
        )
    ]
    return events, time.perf_counter() - started


def _counter(name: str) -> float:
    """The registry's current value of an unlabelled counter, 0 before its first increment."""
    for line in METRICS.render().splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[1])
    return 0.0


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> _Sink:
    """A cost sink that lands its rows, and a durable budget window, both under a 0.5 s bound."""
    landing = _Sink(_lands)
    monkeypatch.setattr("chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: landing)
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 100)
    monkeypatch.setattr("chemclaw.api.budget._durable", lambda: True)
    monkeypatch.setattr(settings, "service_turn_bookkeeping_timeout_seconds", 0.5)
    return landing


async def test_a_turn_that_books_cleanly_has_its_rows_before_it_answers(
    sink: _Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the turn yields its answer, the cost row and the budget booking have already landed."""
    window: list[tuple[str, int]] = []

    async def _book(actor: str, tokens: int) -> Any:
        window.append((actor, tokens))
        return budget_store.Window(1, tokens, 0.0, 0.0)

    monkeypatch.setattr(budget_store, "book", _book)
    seen_at_answer: list[tuple[int, int]] = []

    events: list[Event] = []
    async for event in run_turn(
        TurnSession(session_id="bookkeeping-s"),
        "what is 2+2?",
        actor="bookkeeping-ana",
        budget=BudgetTracker(),
        connectors=[],
        graph_factory=_Answers().graph_factory,
    ):
        if event.type == "answer":
            seen_at_answer.append((len(sink.recorded), len(window)))
        events.append(event)

    assert events[-1].type == "answer"
    assert seen_at_answer == [(1, 1)], "the answer was yielded before its bookkeeping landed"
    assert window == [("bookkeeping-ana", 10)]
    assert sink.recorded[0].completed is True


@pytest.mark.parametrize("which", ["cost", "budget", "both"])
async def test_a_bookkeeping_write_that_fails_never_fails_the_turn(
    sink: _Sink, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """The cost row, the budget window, or both raising still ends in an answer and no error."""
    if which in ("cost", "both"):
        sink.write = _fails

    async def _book(actor: str, tokens: int) -> Any:
        raise RuntimeError("the database went away")

    if which in ("budget", "both"):
        monkeypatch.setattr(budget_store, "book", _book)
    else:

        async def _fine(actor: str, tokens: int) -> Any:
            return budget_store.Window(1, tokens, 0.0, 0.0)

        monkeypatch.setattr(budget_store, "book", _fine)

    events, _ = await _turn(BudgetTracker())

    assert [event.type for event in events][-1] == "answer", events
    assert all(event.type != "error" for event in events)


async def test_a_bookkeeping_write_that_hangs_delays_the_answer_by_the_bound_and_no_more(
    sink: _Sink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write stuck for 30 s costs the answer 0.5 s, is counted, and keeps running behind it."""
    sink.write = _hangs
    monkeypatch.setattr(settings, "budget_enabled", False)
    before = _counter("chemclaw_bookkeeping_unsettled_total")

    events, elapsed = await _turn()
    try:
        assert events[-1].type == "answer"
        assert 0.4 < elapsed < 5.0, f"the answer took {elapsed:.2f}s against a 0.5s bound"
        assert bookkeeping.pending(), "the hung write was abandoned instead of left running"
        assert _counter("chemclaw_bookkeeping_unsettled_total") == before + 1
    finally:
        for task in bookkeeping.pending():
            task.cancel()
        await asyncio.gather(*bookkeeping.pending(), return_exceptions=True)


async def test_a_waiter_that_is_cancelled_leaves_the_write_running() -> None:
    """Cancelling the wait (a Stop, a disconnect) must not cancel the write it was waiting for."""
    landed = asyncio.Event()

    async def _slow() -> None:
        await asyncio.sleep(0.3)
        landed.set()

    task = bookkeeping.schedule(_slow())
    waiter = asyncio.create_task(bookkeeping.settle([task]))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(landed.wait(), 5)
    assert landed.is_set()


async def test_there_is_nothing_to_wait_for_without_a_loop_or_a_database() -> None:
    """`settle` of nothing returns at once, and a done write is not waited for."""
    started = time.perf_counter()
    await bookkeeping.settle([None, None])
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    await bookkeeping.settle([done])
    assert time.perf_counter() - started < 0.2


async def test_a_hung_approval_write_delays_the_terminal_frame_by_the_bound_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spent approval is bookkeeping like the cost row: bounded, and left running behind it."""
    from chemclaw.api import runner

    async def _hangs_for_ever(_session_id: str) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(runner, "consume_turn_approval", _hangs_for_ever)
    monkeypatch.setattr(settings, "service_turn_bookkeeping_timeout_seconds", 0.3)
    ledger = runner._TurnLedger(correlation_id="approval-hang", usage=TurnUsage())

    started = time.perf_counter()
    try:
        await runner._finish_turn(
            TurnSession(session_id="approval-hang-s"),
            ledger,
            actor="bookkeeping-ana",
            profile=None,
            budget=None,
            plan_gated=True,
        )
        elapsed = time.perf_counter() - started
        assert 0.25 < elapsed < 3.0, f"the terminal frame waited {elapsed:.2f}s on a 0.3s bound"
        assert bookkeeping.pending(), "the hung approval write was abandoned, not left running"
    finally:
        for task in bookkeeping.pending():
            task.cancel()
        await asyncio.gather(*bookkeeping.pending(), return_exceptions=True)
