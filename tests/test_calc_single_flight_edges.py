"""The edges of the calculation claim: a failure keeps its class, a lapse is survived, a wait ends.

`tests/test_calc_single_flight.py` proves the claim works; these prove it fails the way the rest of
the system expects. The ordering and exactly-once assertions are logical; the only wall-clock bounds
are lower bounds, which a slow runner cannot violate.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from chemclaw.connectors.calc.remote import CalcBusyError, CalcToolError
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.durable.publish import _BAD_DATA_TYPES
from chemclaw.science.calc import flight
from chemclaw.science.calc.flight import (
    Claim,
    PeerCalculationRefused,
    PeerComputationFailed,
    PeerWaitTimeout,
    PostgresClaims,
)
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import cached_compute
from tests.test_calc_single_flight import (
    _LEASE,
    _async,
    _claim_rows,
    _Contender,
    _fresh,
    _key,
    _never,
    _until,
    flight_on,  # noqa: F401 - a fixture, used by name
)


def _claims_reading(outcome: str) -> float:
    """`chemclaw_calc_claims_total{outcome}` as rendered."""
    wanted = f'chemclaw_calc_claims_total{{outcome="{outcome}"}} '
    for line in METRICS.render().splitlines():
        if line.startswith(wanted):
            return float(line.removeprefix(wanted))
    return 0.0


async def _dead_claim(key_text: str, *, lapses_in: float) -> None:
    """A claim whose holder died: nobody beats it, and it lapses `lapses_in` seconds from now."""
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO calculation_claims (key, attempt, lease_until) "
            "VALUES (%s, 'dead', now() + make_interval(secs => %s))",
            (key_text, lapses_in),
        )


# --- a failure keeps its retry class ----------------------------------------------------------


async def test_a_refusal_reaches_waiters_as_a_refusal_that_activities_do_not_retry(
    flight_on: None,  # noqa: F811
) -> None:
    """A deterministic refusal must not become a retryable error: each retry would recompute it."""
    key = _key()
    holder = _Contender(key, hold=1.0, fail=CalcToolError("no conformer below the energy window"))
    await _until(lambda: _async(holder.computes == 1))
    waiters = [_Contender(key, hold=1.0) for _ in range(3)]

    holder.join()
    outcomes = [waiter.join() for waiter in waiters]

    for kind, error in outcomes:
        assert kind == "error"
        assert isinstance(error, PeerCalculationRefused)
        assert type(error).__name__ in _BAD_DATA_TYPES, "Temporal would retry this by name"
        assert "no conformer below the energy window" in str(error)
        assert "CalcToolError" in str(error)
    assert sum(waiter.computes for waiter in waiters) == 0


async def test_an_outage_reaches_waiters_as_a_failure_that_may_be_retried(
    flight_on: None,  # noqa: F811
) -> None:
    """The holder met a full server: a retry can succeed, so the waiter's error stays retryable."""
    key = _key()
    holder = _Contender(key, hold=1.0, fail=CalcBusyError("every calculation slot is busy"))
    await _until(lambda: _async(holder.computes == 1))
    waiter = _Contender(key, hold=1.0)

    holder.join()
    kind, error = waiter.join()

    assert kind == "error" and isinstance(error, PeerComputationFailed)
    assert type(error).__name__ not in _BAD_DATA_TYPES
    assert "CalcBusyError" in str(error)
    assert waiter.computes == 0


# --- the heartbeat survives a database that goes away -----------------------------------------


def _outage(
    monkeypatch: pytest.MonkeyPatch, seconds: float, *, hang: bool
) -> Callable[[], Awaitable[None]]:
    """Make the heartbeat fail (or hang) for `seconds` after its first call, then work again."""
    original = PostgresClaims.heartbeat
    began: list[float] = []

    async def heartbeat(self: PostgresClaims, claim: Claim) -> bool:
        if not began:
            began.append(time.monotonic())
        if time.monotonic() - began[0] < seconds:
            if hang:
                await asyncio.sleep(3600)
            raise ConnectionError("the database is away")
        return await original(self, claim)

    monkeypatch.setattr(PostgresClaims, "heartbeat", heartbeat)

    async def over() -> None:
        await _until(lambda: _async(bool(began) and time.monotonic() - began[0] >= seconds))

    return over


async def test_an_outage_shorter_than_the_lease_costs_no_duplicate(
    flight_on: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed beats are retried at once, inside the lease, so the claim is never seen lapsed."""
    _outage(monkeypatch, _LEASE / 3, hang=False)
    key = _key()
    holder = _Contender(key, hold=_LEASE * 2)
    await _until(lambda: _async(holder.computes == 1))
    waiter = _Contender(key, hold=0.1, value=2)

    assert holder.join() == ("ok", ({"value": 1}, False))
    assert waiter.join() == ("ok", ({"value": 1}, True))
    assert waiter.computes == 0


async def test_a_hung_beat_is_cut_and_a_lapse_hands_the_key_over_without_clobbering(
    flight_on: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage longer than the lease: one waiter takes over; the returning holder cannot undo it.

    The hung beat has to be cut by its own bound, or the holder never learns the claim moved and
    never stops beating a row that is no longer its own.
    """
    lost = _claims_reading("lost")
    _outage(monkeypatch, _LEASE * 1.6, hang=True)
    key = _key()
    holder = _Contender(key, hold=_LEASE * 2.4)
    await _until(lambda: _async(holder.computes == 1))
    taker = _Contender(key, hold=_LEASE * 4, value=2)

    assert holder.join() == ("ok", ({"value": 1}, False))
    # The holder has finished and released; the taker is still computing and keeps its own row.
    assert taker.computes == 1
    rows = await _claim_rows(key)
    assert len(rows) == 1 and rows[0][1] == "running", rows
    assert _claims_reading("lost") == lost + 1, "the holder should have noticed the takeover"

    assert taker.join() == ("ok", ({"value": 2}, False))
    assert await _claim_rows(key) == []


# --- a waiter's budget ----------------------------------------------------------------------


async def test_a_waiter_with_less_budget_than_a_lease_does_not_take_over(
    flight_on: None,  # noqa: F811
) -> None:
    """Starting a calculation the waiter's own deadline will cancel would only orphan the run."""
    key = _key()
    await _dead_claim(key.as_str(), lapses_in=_LEASE / 2)
    before = await _claim_rows(key)

    waiter = _Contender(key, hold=0.1, wait_seconds=_LEASE * 1.2)
    kind, error = waiter.join()

    assert kind == "error" and isinstance(error, PeerWaitTimeout), (kind, error)
    assert waiter.computes == 0
    assert await _claim_rows(key) == before, "the lapsed claim is left for a caller with budget"


async def test_a_waiter_with_a_full_budget_takes_over_a_lapsed_claim(
    flight_on: None,  # noqa: F811
) -> None:
    """The control for the test above: the same lapse, a budget that can fund the calculation."""
    key = _key()
    await _dead_claim(key.as_str(), lapses_in=_LEASE / 2)

    waiter = _Contender(key, hold=0.1, value=4, wait_seconds=_LEASE * 20)

    assert waiter.join() == ("ok", ({"value": 4}, False))
    assert waiter.computes == 1


async def test_a_cancelled_holders_key_is_offered_after_a_cooling_off(
    flight_on: None,  # noqa: F811
) -> None:
    """The cancelled call is still unwinding server-side, so its key is not handed on at once."""
    key = _key()
    holder = _Contender(key, hold=60.0)
    await _until(lambda: _async(holder.computes == 1))
    waiter = _Contender(key, hold=0.1, value=5)
    await asyncio.sleep(0.5)  # the waiter is parked on the live claim

    cancelled_at = time.perf_counter()
    holder.cancel()
    assert holder.join()[0] == "cancelled"
    assert waiter.join() == ("ok", ({"value": 5}, False))

    cooling = _LEASE / 3
    gap = waiter.started_at[0] - cancelled_at
    assert gap >= cooling * 0.9, f"the key was handed over after {gap:.2f}s, before {cooling:.2f}s"
    assert await _claim_rows(key) == []


# --- settling a claim ---------------------------------------------------------------------------


async def test_a_beat_in_flight_when_the_computation_ends_is_not_a_lost_claim(
    flight_on: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The heartbeat stops before the claim is released, so a late beat cannot report a takeover."""
    original = PostgresClaims.heartbeat

    async def slow_beat(self: PostgresClaims, claim: Claim) -> bool:
        await asyncio.sleep(_LEASE / 3)
        return await original(self, claim)

    monkeypatch.setattr(PostgresClaims, "heartbeat", slow_beat)
    lost = _claims_reading("lost")
    key = _key()
    # Ends while the first beat (due at lease/3, taking lease/3) is mid-flight.
    holder = _Contender(key, hold=_LEASE / 3 + _LEASE / 6)

    assert holder.join() == ("ok", ({"value": 1}, False))
    await asyncio.sleep(_LEASE / 2)  # a surviving beat would land and be counted by now

    assert _claims_reading("lost") == lost
    assert "taken over while its holder" not in caplog.text


async def test_cancelling_a_caller_that_is_stopping_its_heartbeat_is_not_swallowed(
    flight_on: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's own cancellation must outlive the wait for the heartbeat to unwind."""
    stopping = asyncio.Event()

    async def stubborn(claims: PostgresClaims, claim: Claim) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopping.set()
            await asyncio.sleep(1.0)  # slow to unwind
            raise

    monkeypatch.setattr(flight, "_beat", stubborn)
    task = asyncio.create_task(cached_compute(PostgresStore(), _key(), _fresh))
    await asyncio.wait_for(stopping.wait(), 20)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_claim_won_while_the_caller_is_cancelled_is_not_stranded(
    flight_on: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled between winning a lapsed claim and starting the work: the claim is given back."""
    key = _key()
    await _dead_claim(key.as_str(), lapses_in=0.3)
    exiting = asyncio.Event()
    original: Any = flight._Listener.unsubscribe

    async def stuck_exit(self: flight._Listener, topic: str, event: asyncio.Event) -> None:
        try:
            exiting.set()
            await asyncio.sleep(3600)
        finally:
            await original(self, topic, event)

    monkeypatch.setattr(flight._Listener, "unsubscribe", stuck_exit)
    task = asyncio.create_task(cached_compute(PostgresStore(), key, _never, wait_seconds=60))
    await asyncio.wait_for(exiting.wait(), 20)  # the waiter has the claim and is leaving its wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _claim_rows(key) == [], "a claim nobody will heartbeat was left standing"


async def test_a_beat_cut_mid_statement_does_not_return_a_dirty_connection(
    flight_on: None,  # noqa: F811
) -> None:
    """The beat's bound is an `asyncio` timeout over the shared pool; it must leave it sound."""
    sleeping = "SELECT pg_sleep(30)"
    async with db.pooling():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                async with db.connection(settings.postgres_dsn, operation="calc_claim") as conn:
                    await conn.execute(sleeping)

        for _ in range(3):  # every later borrow answers: the connection was reset, not left busy
            async with db.connection(settings.postgres_dsn, operation="calc_claim") as conn:
                cur = await conn.execute("SELECT 1")
                assert await cur.fetchone() == (1,)

        async def still_running() -> bool:
            async with db.connection(settings.postgres_dsn, operation="calc_claim") as conn:
                cur = await conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE query = %s AND state = 'active'",
                    (sleeping,),
                )
                row = await cur.fetchone()
                return row is not None and row[0] > 0

        await _until(lambda: _async_not(still_running()), seconds=10)


async def _async_not(pending: Awaitable[bool]) -> bool:
    """The negation of an awaitable, for `_until`."""
    return not await pending
