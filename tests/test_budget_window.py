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


async def test_a_turn_booked_by_one_tracker_binds_a_second_one(_durable: None) -> None:
    """The whole point: a restart, an eviction or a second pod does not hand back the allowance.

    Two `BudgetTracker`s share nothing in memory — this is exactly the state a pod roll leaves, and
    the state an LRU eviction leaves for one user without any roll at all. Before this window
    existed the second tracker admitted the turn, because the only counter that had ever seen the
    spend died with the first.
    """
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

    The in-process half still bounds this pod, so the honest degrade is "bound on what I can see"
    rather than "refuse every chemist's question because the meter is down". Asserted by breaking
    the read, not by trusting the `except` — a test that patched nothing would pass against a
    version with no error handling at all.
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

    This is the case `max(in-process, durable)` exists for: immediately after `record` returns the
    row may hold nothing, and the guard must still refuse on what this pod knows.

    **The durable half is silenced rather than merely un-drained, and that is the whole fixture.**
    This test used to skip `_drain()` and trust that the write had not landed — but `check` awaits
    before it reads, which yields to the very task `record` just scheduled, so the row was often
    already written and the refusal came from the half the test is not about. Measured over 20
    runs: the write had landed in 2, and with the in-process half deleted the assertion still held
    in 2 — a test that passes a broken implementation one run in ten, and unpredictably more under
    `PYTEST_WORKERS`. Forcing the read to zero makes the refusal attributable to one source.
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

    Reaching into `_users` deliberately: the in-process half keeps its window on `time.monotonic()`,
    which nothing can move from outside, and the alternative — a window set to a second and a real
    sleep — is the race this file's header argues against for the durable half. Ageing *both* halves
    is what makes a test model 25 hours passing rather than a database edit.
    """
    counter = tracker._users.get(actor)
    assert counter is not None, "nothing was booked for this actor"
    counter.started -= hours * 3600.0


async def test_a_rolled_window_stops_binding_on_the_pod_that_spent_it(_durable: None) -> None:
    """The window has to roll on *both* halves, or `max()` is a ratchet instead of a floor.

    The defect this pins shipped: the durable row rolled and the in-process counter never did, so
    `max()` held the principal at their lifetime spend for as long as the pod stayed up. Measured,
    a tracker that had booked 900 tokens still refused against a 500-token cap after the durable
    row had correctly read (0, 0) — while a *freshly built* tracker admitted the same turn. That
    inverts this feature's premise: a restart became the only thing that handed the allowance back,
    and the test that was supposed to cover the roll never re-checked the tracker that did the
    spending, only `budget_store.usage()`.
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

    The review of 2026-09-26 scenario: the durable row opened at 00:00, this pod first booked the
    user at 20:00 (a restart, an eviction, a second replica) and the user hit the cap here. At
    24:00 the row expires; the pod's own counter, anchored at 20:00, would have kept refusing
    until 20:00 the next day while a sibling pod admitted. Modelled by building the late pod's
    counter by hand and backdating only the durable row — the pod's counter is 4 hours old, the row
    past its window — which is exactly the state `_rolled` alone cannot see.
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
    """Let the fire-and-forget durable write finish before reading it back.

    `record` schedules its write as a task for the reason its docstring gives (it is called from a
    teardown where an `await` would lose the rest of the frame), so a test that read immediately
    would be racing it. Awaiting the module's own pending set is the honest wait — a fixed sleep
    would be a slower version of the same race.
    """
    from chemclaw.api.budget import _PENDING

    while _PENDING:
        await asyncio.gather(*tuple(_PENDING), return_exceptions=True)


async def test_two_concurrent_bookings_neither_lose_an_update_nor_reset_twice(
    _durable: None,
) -> None:
    """The upstream behaviour the whole durable window rests on, pinned rather than believed.

    `_BOOK` is one `INSERT ... ON CONFLICT DO UPDATE` whose three `CASE` arms each test
    `budget_usage.window_start`. That is only safe because a conflicting writer blocks on the row
    lock and then re-evaluates against what the first writer *committed* — not against its own
    command snapshot, which is what this module's comment asserted until it was measured. Under the
    snapshot reading, two concurrent bookings would each add 1 to the same pre-image and the window
    would be reset once per writer.

    Driven over the pool on both arms of the `CASE`: a live window must accumulate every booking,
    and an expired one must reset exactly once no matter how many writers arrive together.

    **This is a pin on Postgres, not a mutation-provable assertion about our code**, and saying so
    is the point — there is no edit to `_BOOK` that produces the snapshot semantics the old comment
    described, because the re-check is unconditional. It fails if a future server changes that, or
    if somebody splits the reset into a second statement. `tests/test_upstream_surface.py` keeps
    the same kind of assertion for the same reason: a promise nothing in this repository owns.
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
