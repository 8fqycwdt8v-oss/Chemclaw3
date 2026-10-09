"""The durable per-user spend window (`D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`).

Every test builds a second `BudgetTracker` — a restart, an LRU eviction or a second pod — and
asserts against what the first one spent. Postgres-backed (`migrated_db_or_skip()`), with a
distinct actor prefix per test. The window is rolled by moving the row (`UPDATE ... window_start`),
not by sleeping.
"""

import asyncio

import pytest

from chemclaw.api import budget_store
from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.core import bookkeeping, db
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


async def test_a_turn_booked_by_one_tracker_binds_a_second_one(_durable: None) -> None:
    """The whole point: a restart, an eviction or a second pod does not hand back the allowance."""
    await migrated_db_or_skip()
    await _clean("window-restart")

    spender = BudgetTracker()
    spender.record("s1", "window-restart", tokens=900)
    await _drain()

    settings.budget_max_tokens_per_user = 500
    fresh = BudgetTracker()
    with pytest.raises(BudgetExceeded, match="user token budget"):
        await fresh.check("s2", "window-restart")


async def test_a_window_that_has_rolled_starts_the_principal_again(_durable: None) -> None:
    """A rolling window is a window: past it the counter resets, in place, on the next booking."""
    await migrated_db_or_skip()
    await _clean("window-rolls")

    tracker = BudgetTracker()
    tracker.record("s1", "window-rolls", tokens=900)
    await _drain()
    assert (await budget_store.usage("window-rolls"))[:2] == (1, 900)

    await _age_window("window-rolls", settings.budget_window_hours + 1)
    assert (await budget_store.usage("window-rolls"))[:2] == (0, 0), (
        "a window that has expired must read as zero without anything having rewritten it"
    )

    tracker.record("s2", "window-rolls", tokens=10)
    await _drain()
    assert (await budget_store.usage("window-rolls"))[:2] == (1, 10), (
        "the first booking after an expiry resets the counter rather than adding to it"
    )


async def test_a_window_that_has_not_rolled_accumulates(_durable: None) -> None:
    """The other half of the same statement — inside the window, bookings add up.

    Paired with the test above deliberately: one `CASE` arm decides both, so a change that made the
    reset unconditional would pass that test alone and lose every deployment's whole accounting.
    """
    await migrated_db_or_skip()
    await _clean("window-adds")

    tracker = BudgetTracker()
    for _ in range(3):
        tracker.record("s1", "window-adds", tokens=100)
    await _drain()

    assert (await budget_store.usage("window-adds"))[:2] == (3, 300)


async def test_an_unreachable_meter_admits_the_turn_rather_than_refusing_it(
    monkeypatch: pytest.MonkeyPatch, _durable: None
) -> None:
    """A budget that cannot be read must not become an outage amplifier.

    The in-process half still bounds this pod. Asserted by actually breaking the read.
    """

    async def _boom(actor: str) -> budget_store.Window:
        raise RuntimeError("no database")

    monkeypatch.setattr(budget_store, "usage", _boom)
    monkeypatch.setattr(settings, "budget_max_tokens_per_user", 500)

    await migrated_db_or_skip()
    await BudgetTracker().check("s1", "window-unreachable")


async def test_the_in_process_counter_still_binds_before_the_durable_write_lands(
    monkeypatch: pytest.MonkeyPatch, _durable: None
) -> None:
    """`record` is synchronous and its durable write is not, so the gap has to be covered.

    `max(in-process, durable)` refuses on what this pod knows. The durable read is forced to zero,
    since `check` yields to the pending write and could otherwise refuse from the durable half.
    """

    async def _silent(actor: str) -> budget_store.Window:
        return budget_store.Window(0, 0)

    monkeypatch.setattr(budget_store, "usage", _silent)

    await migrated_db_or_skip()
    await _clean("window-notyet")

    tracker = BudgetTracker()
    tracker.record("s1", "window-notyet", tokens=900)
    settings.budget_max_tokens_per_user = 500
    with pytest.raises(BudgetExceeded, match="user token budget"):
        await tracker.check("s2", "window-notyet")


def _age_counter(tracker: BudgetTracker, actor: str, hours: float) -> None:
    """Rewind a live in-process counter's window start, so elapsed time can be simulated.

    The in-process half uses `time.monotonic()`, which cannot be moved from outside; ageing both
    halves models real time passing.
    """
    counter = tracker._users.get(actor)
    assert counter is not None, "nothing was booked for this actor"
    counter.started -= hours * 3600.0


async def test_a_rolled_window_stops_binding_on_the_pod_that_spent_it(_durable: None) -> None:
    """The window has to roll on *both* halves, or `max()` is a ratchet instead of a floor.

    The tracker that did the spending must admit again once the durable row has rolled, as a fresh
    tracker would.
    """
    await migrated_db_or_skip()
    await _clean("window-both-halves")

    tracker = BudgetTracker()
    tracker.record("s1", "window-both-halves", tokens=900)
    await _drain()

    settings.budget_max_tokens_per_user = 500
    with pytest.raises(BudgetExceeded, match="user token budget"):
        await tracker.check("s2", "window-both-halves")

    past = settings.budget_window_hours + 1
    await _age_window("window-both-halves", past)
    _age_counter(tracker, "window-both-halves", past)

    await tracker.check("s3", "window-both-halves")


async def test_a_pod_that_joined_late_rolls_with_the_durable_window(_durable: None) -> None:
    """The in-process window is anchored to the durable row's, not to this pod's first booking.

    A pod that first booked a user late must roll when the durable row rolls, not a day after its
    own first booking. Modelled by building the late pod's counter by hand and backdating only the
    row.
    """
    await migrated_db_or_skip()
    actor = "window-late-pod"
    await _clean(actor)
    await budget_store.book(actor, 900)  # another pod opened the window and spent

    late = BudgetTracker()
    late.record("s1", actor, tokens=1000)
    await _drain()
    settings.budget_max_tokens_per_user = 1000
    with pytest.raises(BudgetExceeded, match="user token budget"):
        await late.check("s2", actor)

    # 24:00 for the row, 04:00 into this pod's own counter.
    await _age_window(actor, settings.budget_window_hours + 0.1)
    _age_counter(late, actor, 4.0)
    await late.check("s3", actor)


async def test_an_unwritten_turn_inside_the_window_still_binds_after_reconciling(
    monkeypatch: pytest.MonkeyPatch, _durable: None
) -> None:
    """Re-anchoring must not throw away what `max()` is for: this pod's turn not yet written.

    The counter lies inside the live durable window, so reconciling it adopts the window's start
    and keeps its counts — the durable row, which has not seen the turn, must not replace them.
    """
    await migrated_db_or_skip()
    actor = "window-inside"
    await _clean(actor)
    tracker = BudgetTracker()
    tracker.record("s1", actor, tokens=10)
    await _drain()

    async def _no_write(user: str, tokens: int) -> budget_store.Window:
        raise RuntimeError("not landed yet")

    monkeypatch.setattr(budget_store, "book", _no_write)
    tracker.record("s2", actor, tokens=900)
    await _drain()
    settings.budget_max_tokens_per_user = 500
    with pytest.raises(BudgetExceeded, match="user token budget"):
        await tracker.check("s3", actor)


async def _drain() -> None:
    """Let the durable write finish before reading it back.

    `record` schedules the write as a tracked task (it runs from teardown); awaiting the pending
    set avoids racing it.
    """
    while bookkeeping.pending():
        await asyncio.gather(*bookkeeping.pending(), return_exceptions=True)


async def test_two_concurrent_bookings_neither_lose_an_update_nor_reset_twice(
    _durable: None,
) -> None:
    """Two concurrent bookings neither lose an update nor reset twice.

    `_BOOK`'s `CASE` arms are safe because a conflicting writer re-evaluates against the committed
    row after the lock. Driven on both arms: a live window accumulates every booking; an expired one
    resets once. A pin on Postgres behaviour, like `tests/test_upstream_surface.py`.
    """
    await migrated_db_or_skip()

    for actor, age_hours in (("window-race-live", None), ("window-race-rolled", 25.0)):
        await _clean(actor)
        await budget_store.book(actor, 10)
        if age_hours is not None:
            await _age_window(actor, age_hours)

        await asyncio.gather(*(budget_store.book(actor, 10) for _ in range(32)))

        turns, tokens, _, _ = await budget_store.usage(actor)
        if age_hours is None:
            assert (turns, tokens) == (33, 330), (
                "a booking inside a live window was lost — the arms read a stale pre-image"
            )
        else:
            assert (turns, tokens) == (32, 320), (
                "an expired window must reset once for the batch, not once per writer"
            )
        await _clean(actor)
