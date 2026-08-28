"""What a turn books about itself: whose booking runs first, when the clock is read, and by whom.

Three of the four defects here are about *ordering* rather than about a value — new work placed
ahead of a booking that must not be prevented, a clock read after the work that moves it, and a
counter taken over events the answer does not contain. The fourth is a second writer of a column
whose comment says it has one, which is why `turn_costs.outcome` could not be queried honestly.
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
    """`_settle_outcome` and `_resolved_model` were computed *before* `budget.record`.

    So anything raising in either lost both the budget record and the `turn_costs` row — the two
    instruments a runaway is found with — and, because this function runs from a `finally` that is
    usually unwinding a `CancelledError`, replaced that cancellation with its own exception. The
    booking is a dict write; it goes first, and the derivations are settled where a failure costs
    one row's precision instead of the row.
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
        budget.check("s-book", "chemist-1")
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


def test_a_stop_just_short_of_the_deadline_is_not_a_timeout(
    booked: list[TurnCost], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim was "an exact test rather than a tolerance"; the reading was taken too late.

    `_settle_outcome` runs in the `finally`, *after* `_roll_back_unfinished` and after an approval
    spend — so a Stop delivered at `deadline − ε` behind a slow teardown crossed the deadline while
    being torn down and booked `timed_out`, a wall-clock kill that never happened.

    **No sleep races anything here.** The deadline is set a whole horizon out, the cancellation is
    delivered inside it (asserted), and the rollback then blocks until 50 ms *past* it — computed
    from the deadline rather than guessed at, so the reproduction is the same on a loaded machine
    as on an idle one. The horizon only has to outlast building the real graph, and the assertion
    says so if it ever does not.
    """
    horizon = 2.0

    def _slow_rollback(*_args: Any, **_kwargs: Any) -> None:
        overshoot = deadline_box[0] - asyncio.get_running_loop().time() + 0.05
        time.sleep(max(overshoot, 0.0))

    monkeypatch.setattr(runner, "_roll_back_unfinished", _slow_rollback)
    deadline_box: list[float] = []

    async def _run() -> None:
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

    asyncio.run(_run())

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
    """TTFT and `answer_text` disagreed about what a token is, in one row.

    `_stream_into` appends to `answer_parts` only for `not event.agent` — the filter its docstring
    calls load-bearing — while `note_event` set `first_token` for *any* `TokenEvent`, and
    `graph_stream.py` marks every chunk from below the root `agent="subagent"`. A turn in which
    only a subagent ever spoke therefore booked `outcome="empty_answer"` beside a non-null
    `ttft_seconds`: a time-to-first-token for an answer that never had a first token.
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
    """Run the real `run_agent_step` and keep the cost row **even when the step raises**.

    `tests/test_template_agent_step._drive` returns its rows only on the success path, and the
    outcomes worth pinning here are the failing ones — so this is the same three substitutions with
    the exception handed back instead of propagated.
    """
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
    """Every row this second writer has ever written carried `outcome='unknown'`.

    That is the column's *default*, meaning "written before the column existed" — so an outcome
    query could not tell a backfilled row from a step booked today, and the index on that column
    indexed a value with two meanings. `infra/sql/060_turn_outcome.sql` and `core/turn_cost.py`
    both still say `_settle_outcome` is the only producer; this is what makes the second one honest
    about how it ends.
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

    `*args, **kwargs` on both hooks for the reason `tests/test_template_agent_step._ProviderOutage`
    gives: upstream calls `_generate` and `_stream` with different arities, and pinning either here
    would make this fake fail on a LangChain bump for a reason unrelated to what it tests.
    """

    def _generate(self, *_args: Any, **_kwargs: Any) -> Any:
        """Fail the way a provider outage fails."""
        raise _Outage("the provider died")

    def _stream(self, *_args: Any, **_kwargs: Any) -> Any:
        """Fail the same way when the caller streams."""
        raise _Outage("the provider died")
