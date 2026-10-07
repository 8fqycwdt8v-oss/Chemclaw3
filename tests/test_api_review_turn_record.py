"""What a turn books about itself: whose booking runs first, when the clock is read, and by whom.

The budget record and cost row are booked before anything that can fail, the deadline is sampled
when the cancellation lands, TTFT counts only root-agent tokens, and the template step writes a
real `turn_costs.outcome`.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, suppress
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from chemclaw.agent.session import TurnSession
from chemclaw.agent.turn_cost import TurnCost
from chemclaw.agent.turn_usage import TurnUsage
from chemclaw.api import runner
from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.api.events import TokenEvent
from chemclaw.api.runner import _book_turn_spend, _settle_outcome, _TurnLedger, run_turn
from chemclaw.core.config import settings
from tests.fakes_langgraph import ScriptedChatModel
from tests.fakes_turn import Piece, ScriptedTurn
from tests.test_template_agent_step import _USAGE, _CostRecorder, _scripted, _step


@pytest.fixture
def booked(monkeypatch: pytest.MonkeyPatch) -> list[TurnCost]:
    """Every `turn_costs` row this test books, without a database under it.

    `record_turn_cost` writes from a task it deliberately does not await, so a test reading these
    yields once afterwards — the same seam and the same reason as `tests/test_template_agent_step`.
    """
    rows: list[TurnCost] = []
    monkeypatch.setattr(
        "chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: _CostRecorder(rows)
    )
    return rows


# --------------------------------------------------------------------------------------------
# 11 — the budget record must not sit behind work that can fail.
# --------------------------------------------------------------------------------------------


def test_a_failed_settle_still_books_the_budget_and_the_row(
    booked: list[TurnCost], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed settle still books the budget and the row.

    The booking is a dict write and goes first; `_settle_outcome` and `_resolved_model` run after,
    so a failure there costs one row's precision, not the row, and does not replace an unwinding
    `CancelledError`.
    """
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 1)

    def _explode(_ledger: _TurnLedger) -> str:
        raise RuntimeError("a ledger this function did not expect")

    monkeypatch.setattr(runner, "_settle_outcome", _explode)
    budget = BudgetTracker()
    ledger = _TurnLedger(correlation_id="c" * 32, usage=TurnUsage(input=90, output=10, total=100))

    async def _book() -> None:
        _book_turn_spend(
            ledger,
            session=TurnSession(session_id="s-book"),
            actor="chemist-1",
            profile=None,
            budget=budget,
        )
        await asyncio.sleep(0)

    asyncio.run(_book())

    with pytest.raises(BudgetExceeded):
        asyncio.run(budget.check("s-book", "chemist-1"))
    (row,) = booked
    assert (row.input_tokens, row.output_tokens) == (90, 10), "the spend was lost with the outcome"
    assert row.outcome == "unknown", (
        "a row whose outcome could not be settled must say so rather than claim an ending"
    )


def test_a_failed_settle_is_loud(
    booked: list[TurnCost], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`unknown` is also the pre-migration default, so this arm must never be quiet.

    Two populations in one value is the defect item 9 is about; the fallback is allowed to write it
    only because it shouts, which is what makes the two tellable apart in the log if not in SQL.
    """

    def _explode(_ledger: _TurnLedger) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_settle_outcome", _explode)

    async def _book() -> None:
        _book_turn_spend(
            _TurnLedger(correlation_id="c" * 32, usage=TurnUsage()),
            session=TurnSession(session_id="s-loud"),
            actor=None,
            profile=None,
            budget=None,
        )
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR):
        asyncio.run(_book())

    assert [r for r in caplog.records if "settling the turn record" in r.getMessage()]


# --------------------------------------------------------------------------------------------
# 12 — `timed_out` is an exact test only where it is taken.
# --------------------------------------------------------------------------------------------


class _OneTokenAgent(ScriptedTurn):
    """A turn that produces one event and then waits to be torn down."""

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "thinking"
        await asyncio.sleep(30)
        yield " never"  # pragma: no cover - the turn is always torn down first


async def test_a_stop_just_short_of_the_deadline_is_not_a_timeout(
    booked: list[TurnCost], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop just short of the deadline is not a timeout.

    `_settle_outcome` runs after the rollback, so the deadline must be sampled when the cancellation
    lands; otherwise a slow teardown books `timed_out` for a kill that never happened. The rollback
    blocks until 50 ms past a deadline computed far out, so the reproduction is deterministic.
    """
    horizon = 2.0

    def _slow_rollback(*_args: Any, **_kwargs: Any) -> None:
        overshoot = deadline_box[0] - asyncio.get_running_loop().time() + 0.05
        time.sleep(max(overshoot, 0.0))

    monkeypatch.setattr(runner, "_roll_back_unfinished", _slow_rollback)
    deadline_box: list[float] = []

    deadline = asyncio.get_running_loop().time() + horizon
    deadline_box.append(deadline)
    turn = cast(
        "AsyncGenerator[Any, None]",
        run_turn(
            TurnSession(session_id="s-stop"),
            "go",
            connectors=[],
            graph_factory=_OneTokenAgent().graph_factory,
            deadline=deadline,
        ),
    )
    await turn.asend(None)  # the first event: the turn is demonstrably running
    # The Stop button, delivered exactly as the pump delivers it — inside the deadline.
    assert asyncio.get_running_loop().time() < deadline, (
        f"building the graph took longer than the {horizon}s horizon; raise it"
    )
    with suppress(asyncio.CancelledError):
        await turn.athrow(asyncio.CancelledError())
    await asyncio.sleep(0)

    (row,) = booked
    assert row.outcome == "abandoned", (
        "a Stop inside the deadline booked a wall-clock kill, because the clock was read after "
        "the teardown rather than at the cancellation"
    )


def test_the_deadline_reading_is_still_what_names_a_wall_clock_kill() -> None:
    """The other direction: a cancellation delivered past the deadline is still `timed_out`."""
    ledger = _TurnLedger(correlation_id="c" * 32, usage=TurnUsage())
    ledger.cancelled = True
    ledger.timed_out = True
    assert _settle_outcome(ledger) == "timed_out"


# --------------------------------------------------------------------------------------------
# 13 — one definition of "a token of this turn".
# --------------------------------------------------------------------------------------------


def test_a_subagent_only_turn_reports_no_time_to_first_token() -> None:
    """A subagent-only turn reports no time to first token.

    `answer_parts` takes only root-agent tokens, so `first_token` must apply the same filter, or an
    `empty_answer` turn could carry a non-null `ttft_seconds`.
    """
    ledger = _TurnLedger(correlation_id="c" * 32, usage=TurnUsage())
    ledger.note_event(TokenEvent(text="working on it", agent="subagent"))

    assert ledger.ttft_seconds is None, (
        "a subagent's working prose was booked as this turn's first answer token"
    )
    assert _settle_outcome(ledger) == "empty_answer"


def test_the_supervisor_s_own_first_token_is_still_the_first_token() -> None:
    """And the field still measures what it exists to measure."""
    ledger = _TurnLedger(correlation_id="c" * 32, usage=TurnUsage())
    ledger.note_event(TokenEvent(text="a subagent speaks", agent="subagent"))
    ledger.note_event(TokenEvent(text="the answer begins", agent=""))
    assert ledger.ttft_seconds is not None and ledger.ttft_seconds >= 0


# --------------------------------------------------------------------------------------------
# 9 — the second writer of `turn_costs`, which wrote no outcome at all.
# --------------------------------------------------------------------------------------------


def _drive_step(
    monkeypatch: pytest.MonkeyPatch, script: Any, rows: list[TurnCost]
) -> BaseException | None:
    """Run the real `run_agent_step` and keep the cost row **even when the step raises**."""
    from chemclaw.durable import template_activities

    monkeypatch.setattr("chemclaw.agent.audit.default_audit_sink", lambda: _NullSink())
    monkeypatch.setattr(
        "chemclaw.agent.langgraph_agent.build_chat_model", lambda *_a, **_k: _scripted(script)
    )
    monkeypatch.setattr(
        "chemclaw.agent.turn_cost.default_turn_cost_sink", lambda: _CostRecorder(rows)
    )

    async def _open(_stack: AsyncExitStack, _specs: Any) -> tuple[list[Any], list[str]]:
        return [], []

    monkeypatch.setattr(template_activities, "open_connector_specs", _open)

    async def _run() -> BaseException | None:
        try:
            await template_activities.run_agent_step(_step())
        except BaseException as exc:
            return exc
        finally:
            await asyncio.sleep(0)
        return None

    return asyncio.run(_run())


class _NullSink:
    """An audit sink that keeps nothing — this file asserts on cost rows, not on the trail."""

    async def record(self, event: Any) -> None:
        """Drop one audit event."""


def test_a_template_step_books_a_real_outcome(
    monkeypatch: pytest.MonkeyPatch, booked: list[TurnCost]
) -> None:
    """A template step books a real outcome.

    `outcome='unknown'` is the column default, meaning "written before the column existed", so a
    second writer must not leave it there.
    """
    rows: list[TurnCost] = []
    assert (
        _drive_step(monkeypatch, [AIMessage(content="all clear", usage_metadata=_USAGE)], rows)
        is None
    )

    (row,) = rows
    assert row.outcome == "answered", f"a template step still books {row.outcome!r}"
    assert row.completed is True


def test_a_template_step_that_raises_books_errored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A step that broke is `errored`, not an ending nobody can query for."""
    rows: list[TurnCost] = []
    raised = _drive_step(monkeypatch, _Erroring(messages=iter([])), rows)

    assert isinstance(raised, _Outage)
    (row,) = rows
    assert (row.outcome, row.completed) == ("errored", False)


class _Outage(Exception):
    """The provider dying mid-step — the failure a template run actually sees."""


class _Erroring(ScriptedChatModel):
    """A model that raises instead of answering.

    `*args, **kwargs` on both hooks: upstream calls `_generate` and `_stream` with different
    arities.
    """

    def _generate(self, *_args: Any, **_kwargs: Any) -> Any:
        """Fail the way a provider outage fails."""
        raise _Outage("the provider died")

    def _stream(self, *_args: Any, **_kwargs: Any) -> Any:
        """Fail the same way when the caller streams."""
        raise _Outage("the provider died")
