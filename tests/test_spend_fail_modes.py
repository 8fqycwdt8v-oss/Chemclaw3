"""Where the spend modules read their limits and where a broken install must not be swallowed.

Two properties of the pre-merge code that a move can lose without a decision changing on any
ordinary input: the per-user caps are read after the durable window has been awaited, and a failed
import of the pool module raises where it is written instead of being booked as a database that
cannot answer (which would admit the turn on this pod's counters alone).
"""

import asyncio
import sys
from typing import Any

import pytest

import chemclaw.core as core_package
from chemclaw.core.config import settings
from chemclaw.core.turn_cost import TurnCost
from chemclaw.spend import limits
from chemclaw.spend.ledger import record_turn_cost
from chemclaw.spend.limits import BudgetExceeded, BudgetTracker, Window


@pytest.fixture
def _durable_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budgets on, the per-user window durable, and no cap that would refuse by itself."""
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 0)
    monkeypatch.setattr(settings, "budget_max_tokens_per_session", 0)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 0)
    monkeypatch.setattr(settings, "budget_max_tokens_per_user", 1_000)
    monkeypatch.setattr(limits, "_durable", lambda: True)


def _without_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `from chemclaw.core import db` raise `ImportError`, as a broken install would."""
    monkeypatch.delattr(core_package, "db")
    monkeypatch.setitem(sys.modules, "chemclaw.core.db", None)


def test_the_user_caps_are_read_after_the_durable_window_is_awaited(
    monkeypatch: pytest.MonkeyPatch, _durable_budget: None
) -> None:
    """A cap changed while the window was being read is the cap the turn is held to."""

    async def _window_read_while_the_cap_is_lowered(actor: str) -> Window:
        monkeypatch.setattr(settings, "budget_max_tokens_per_user", 50)
        return Window(0, 60)

    monkeypatch.setattr(limits, "usage", _window_read_while_the_cap_is_lowered)
    with pytest.raises(BudgetExceeded, match=r"user token budget exhausted \(60 tokens\)"):
        asyncio.run(BudgetTracker().check("s1", "alice"))


def test_a_broken_pool_import_stops_the_budget_check_instead_of_admitting_the_turn(
    monkeypatch: pytest.MonkeyPatch, _durable_budget: None
) -> None:
    """`check` lets the import failure out; only an unanswering database is read as `None`."""
    _without_the_pool(monkeypatch)
    with pytest.raises(ImportError):
        asyncio.run(BudgetTracker().check("s1", "alice"))


def test_a_broken_pool_import_stops_the_booking_where_it_is_scheduled(
    monkeypatch: pytest.MonkeyPatch, _durable_budget: None
) -> None:
    """`record` is synchronous and raises at the call, not inside the task it would have started."""
    _without_the_pool(monkeypatch)
    with pytest.raises(ImportError):
        BudgetTracker().record("s1", "alice", 10)


def test_a_broken_pool_import_stops_the_cost_booking_where_the_sink_is_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a Postgres session store, choosing the sink raises instead of booking a lost ledger."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    _without_the_pool(monkeypatch)
    cost: Any = TurnCost(correlation_id="c-1")
    with pytest.raises(ImportError):
        record_turn_cost(cost)
