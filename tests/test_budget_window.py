"""The durable per-user spend window (`D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`).

`tests/test_budget.py` proves the in-process tracker against monkeypatched settings and no database,
which is the right shape for what it covers and is structurally unable to cover this: the defect
being fixed is that the counters *are* the process, so a test that builds one tracker can never see
it. Every test here builds a **second** `BudgetTracker` — the stand-in for a restart, an LRU
eviction or a second pod — and asserts against what the first one spent.

Postgres-backed, in the style of `tests/test_postgres_turn_cost_store.py`: `migrated_db_or_skip()`
first, a distinct actor prefix per test so tests sharing the session schema cannot see each other's
rows, and the read-back written here rather than in production code.

**The window is rolled by moving the row, not by sleeping.** A test that waited for a real window to
expire would either take the window's length or need the window set so short that the assertion
races the statement; `UPDATE ... window_start = now() - interval` puts the row in the state the next
booking has to recognise, which is the thing under test.
"""

import asyncio

import pytest

from chemclaw.api import budget_store
from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


def _dsn() -> str:
    """The DSN the store itself resolves to, so the read-back lands in the same schema."""
    return settings.session_store_dsn or settings.postgres_dsn


async def _clean(actor: str) -> None:
    """Drop this test's own row, so a re-run starts from nothing."""
    async with db.connection(_dsn()) as conn:
        await conn.execute("DELETE FROM budget_usage WHERE actor = %s", (actor,))


async def _age_window(actor: str, hours: float) -> None:
    """Backdate a principal's window so the next booking sees it as expired."""
    async with db.connection(_dsn()) as conn:
        await conn.execute(
            "UPDATE budget_usage SET window_start = now() - %s * INTERVAL '1 hour'"
            " WHERE actor = %s",
            (hours, actor),
        )


@pytest.fixture
def _durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budgets on, the durable half engaged, every cap unlimited until a test tightens one."""
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "session_store", "postgres")
    for field in (
        "budget_max_turns_per_session",
        "budget_max_tokens_per_session",
        "budget_max_turns_per_user",
        "budget_max_tokens_per_user",
    ):
        monkeypatch.setattr(settings, field, 0)


def test_a_turn_booked_by_one_tracker_binds_a_second_one(_durable: None) -> None:
    """The whole point: a restart, an eviction or a second pod does not hand back the allowance.

    Two `BudgetTracker`s share nothing in memory — this is exactly the state a pod roll leaves, and
    the state an LRU eviction leaves for one user without any roll at all. Before this window
    existed the second tracker admitted the turn, because the only counter that had ever seen the
    spend died with the first.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean("window-restart")

        spender = BudgetTracker()
        spender.record("s1", "window-restart", tokens=900)
        await _drain()

        settings.budget_max_tokens_per_user = 500
        fresh = BudgetTracker()
        with pytest.raises(BudgetExceeded, match="user token budget"):
            await fresh.check("s2", "window-restart")

    asyncio.run(_run())


def test_a_window_that_has_rolled_starts_the_principal_again(_durable: None) -> None:
    """A rolling window is a window: past it the counter resets, in place, on the next booking."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean("window-rolls")

        tracker = BudgetTracker()
        tracker.record("s1", "window-rolls", tokens=900)
        await _drain()
        assert await budget_store.usage("window-rolls") == (1, 900)

        await _age_window("window-rolls", settings.budget_window_hours + 1)
        assert await budget_store.usage("window-rolls") == (0, 0), (
            "a window that has expired must read as zero without anything having rewritten it"
        )

        tracker.record("s2", "window-rolls", tokens=10)
        await _drain()
        assert await budget_store.usage("window-rolls") == (1, 10), (
            "the first booking after an expiry resets the counter rather than adding to it"
        )

    asyncio.run(_run())


def test_a_window_that_has_not_rolled_accumulates(_durable: None) -> None:
    """The other half of the same statement — inside the window, bookings add up.

    Paired with the test above deliberately: one `CASE` arm decides both, so a change that made the
    reset unconditional would pass that test alone and lose every deployment's whole accounting.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean("window-adds")

        tracker = BudgetTracker()
        for _ in range(3):
            tracker.record("s1", "window-adds", tokens=100)
        await _drain()

        assert await budget_store.usage("window-adds") == (3, 300)

    asyncio.run(_run())


def test_an_unreachable_meter_admits_the_turn_rather_than_refusing_it(
    monkeypatch: pytest.MonkeyPatch, _durable: None
) -> None:
    """A budget that cannot be read must not become an outage amplifier.

    The in-process half still bounds this pod, so the honest degrade is "bound on what I can see"
    rather than "refuse every chemist's question because the meter is down". Asserted by breaking
    the read, not by trusting the `except` — a test that patched nothing would pass against a
    version with no error handling at all.
    """

    async def _boom(actor: str) -> tuple[int, int]:
        raise RuntimeError("no database")

    monkeypatch.setattr(budget_store, "usage", _boom)
    monkeypatch.setattr(settings, "budget_max_tokens_per_user", 500)

    async def _run() -> None:
        await migrated_db_or_skip()
        await BudgetTracker().check("s1", "window-unreachable")

    asyncio.run(_run())


def test_the_in_process_counter_still_binds_before_the_durable_write_lands(_durable: None) -> None:
    """`record` is synchronous and its durable write is not, so the gap has to be covered.

    This is the case `max(in-process, durable)` exists for. The write is deliberately *not* drained
    here: immediately after `record` returns, the row may hold nothing, and the guard must still
    refuse on what this pod knows. Booking and checking through one tracker is what a single pod
    does on every turn, so a regression here is not an edge case.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _clean("window-notyet")

        tracker = BudgetTracker()
        tracker.record("s1", "window-notyet", tokens=900)
        settings.budget_max_tokens_per_user = 500
        with pytest.raises(BudgetExceeded, match="user token budget"):
            await tracker.check("s2", "window-notyet")

    asyncio.run(_run())


async def _drain() -> None:
    """Let the fire-and-forget durable write finish before reading it back.

    `record` schedules its write as a task for the reason its docstring gives (it is called from a
    teardown where an `await` would lose the rest of the frame), so a test that read immediately
    would be racing it. Awaiting the module's own pending set is the honest wait — a fixed sleep
    would be a slower version of the same race.
    """
    from chemclaw.api.budget import _PENDING

    while _PENDING:
        await asyncio.gather(*tuple(_PENDING), return_exceptions=True)
