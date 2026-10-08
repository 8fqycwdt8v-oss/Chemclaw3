"""A calculation miss is computed once across processes: the rest wait for the holder's result.

D-011 extended to "a result being computed by someone else is awaited, not recomputed". Everything
here runs against real Postgres. The multi-process tests start real operating-system processes
(`tests/calc_flight_worker.py`), because a mock cannot show that two pods agree through the
database alone; the in-process tests give each contender its own thread and event loop, the
smallest thing that defeats the in-process future and leaves the claim table the only coordinator.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.science.calc import flight
from chemclaw.science.calc.flight import (
    PeerComputationFailed,
    PeerWaitTimeout,
    PostgresClaims,
)
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import (
    CalculationKey,
    InMemoryStore,
    ResultPayload,
    StoredResult,
    cached_compute,
)
from tests.pg import migrated_db_or_skip

_ROOT = Path(__file__).resolve().parents[1]

# Short enough to see a takeover within a test, long enough that a loaded runner's late beat is not
# mistaken for a dead holder: a beat every 0.4 s, a poll every 0.2 s.
_LEASE = 1.2


@pytest.fixture
async def flight_on(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Cross-process coordination on, a short lease, and the claim table migrated."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "calc_claim_lease_seconds", _LEASE)
    await migrated_db_or_skip()
    yield


def _key() -> CalculationKey:
    """A key no other test shares."""
    return CalculationKey(
        calc_type="flight", calc_version="1", input_hash=uuid.uuid4().hex, params_hash="p"
    )


async def _claim_rows(key: CalculationKey) -> list[tuple[str, str, str]]:
    """`(attempt, state, error)` of the claim rows for `key`."""
    async with db.connection(settings.postgres_dsn) as conn:
        cur = await conn.execute(
            "SELECT attempt, state, error FROM calculation_claims WHERE key = %s", (key.as_str(),)
        )
        return [(row[0], row[1], row[2]) for row in await cur.fetchall()]


async def _until(predicate: Callable[[], Awaitable[bool]], seconds: float = 20.0) -> None:
    """Poll `predicate` until it holds, or fail."""
    deadline = time.monotonic() + seconds
    while not await predicate():
        assert time.monotonic() < deadline, "the condition never held"
        await asyncio.sleep(0.05)


class _Contender:
    """One `cached_compute` on its own thread and event loop, with a computation it can count.

    A second loop does not share the first's in-process future, so the claim table is the only thing
    that can make two contenders agree — the situation of two pods.
    """

    def __init__(
        self,
        key: CalculationKey,
        *,
        hold: float = 0.0,
        fail: str = "",
        wait_seconds: float | None = None,
        value: int = 1,
    ) -> None:
        """Start missing `key` now."""
        self.key = key
        self.computes = 0
        self.outcome: tuple[str, Any] = ("pending", None)
        self._hold, self._fail, self._wait, self._value = hold, fail, wait_seconds, value
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[tuple[ResultPayload, bool]] | None = None
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._started.wait(10)

    async def _compute(self) -> ResultPayload:
        self.computes += 1
        await asyncio.sleep(self._hold)
        if self._fail:
            raise ValueError(self._fail)
        return {"value": self._value}

    async def _main(self) -> None:
        self._task = asyncio.current_task()
        self._started.set()
        try:
            result = await cached_compute(
                PostgresStore(), self.key, self._compute, wait_seconds=self._wait
            )
            self.outcome = ("ok", result)
        except asyncio.CancelledError:
            self.outcome = ("cancelled", None)
        except Exception as exc:
            self.outcome = ("error", exc)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    def cancel(self) -> None:
        """Cancel the caller from outside its loop."""
        assert self._loop is not None and self._task is not None
        self._loop.call_soon_threadsafe(self._task.cancel)

    def join(self, seconds: float = 60.0) -> tuple[str, Any]:
        """Wait for the call to end and return how it ended."""
        self._thread.join(seconds)
        assert not self._thread.is_alive(), "the contender never finished"
        return self.outcome


def _spawn(tmp_path: Path, name: str, **spec: Any) -> subprocess.Popen[str]:
    """Start `tests/calc_flight_worker.py` as a separate process against this test's database."""
    spec_file = tmp_path / f"{name}.json"
    spec_file.write_text(json.dumps(spec))
    env = {
        **os.environ,
        "CHEMCLAW_POSTGRES_DSN": settings.postgres_dsn,
        "CHEMCLAW_SESSION_STORE": "postgres",
        "CHEMCLAW_CALC_CLAIM_LEASE_SECONDS": str(settings.calc_claim_lease_seconds),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.calc_flight_worker", str(spec_file)],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(process: subprocess.Popen[str], seconds: float = 90.0) -> list[list[Any]]:
    """The worker's report, failing with its stderr if it did not produce one."""
    out, err = process.communicate(timeout=seconds)
    assert process.returncode == 0, f"worker failed:\n{err}"
    reports: list[list[Any]] = json.loads(out.strip().splitlines()[-1])
    return reports


def _lines(path: Path) -> list[str]:
    """The computations recorded in a counter file, one pid per line."""
    return path.read_text().split() if path.exists() else []


async def test_two_processes_with_eight_callers_each_compute_once(
    flight_on: None, tmp_path: Path
) -> None:
    """16 concurrent misses on one key, from two real processes, ran exactly one computation."""
    key = _key()
    counter = tmp_path / "computes"
    start = time.time() + 8.0  # both interpreters have imported by then
    spec = {
        "input_hash": key.input_hash,
        "counter": str(counter),
        "hold_seconds": 2.0,
        "value": 7,
        "start_at": start,
        "callers": 8,
    }
    first, second = _spawn(tmp_path, "a", **spec), _spawn(tmp_path, "b", **spec)
    reports = _finish(first) + _finish(second)

    assert len(_lines(counter)) == 1, f"the key was computed {_lines(counter)} times, not once"
    assert len(reports) == 16
    assert all(report[0]["value"] == 7 for report in reports), reports
    assert sum(1 for report in reports if report[1] is False) == 1, "exactly one caller computed"
    assert len({report[0]["computed_by"] for report in reports}) == 1
    stored = await PostgresStore().get(key)
    assert stored is not None and stored.result["value"] == 7
    assert await _claim_rows(key) == [], "a finished computation leaves no claim behind"


async def test_eight_loops_in_one_process_compute_once(flight_on: None) -> None:
    """Contenders that share no in-process future agree through the claim alone."""
    key = _key()
    contenders = [_Contender(key, hold=1.0) for _ in range(8)]
    outcomes = [contender.join() for contender in contenders]

    assert sum(contender.computes for contender in contenders) == 1
    assert all(kind == "ok" for kind, _ in outcomes), outcomes
    assert sum(1 for _, (_, cached) in outcomes if not cached) == 1
    assert await _claim_rows(key) == []


async def test_a_killed_holder_hands_the_key_to_exactly_one_waiter(
    flight_on: None, tmp_path: Path
) -> None:
    """Kill -9 on the computing process: its lease lapses and one waiter computes the rest."""
    key = _key()
    counter, announce = tmp_path / "computes", tmp_path / "announce"
    base = {"input_hash": key.input_hash, "counter": str(counter), "start_at": 0.0}
    holder = _spawn(
        tmp_path, "holder", **base, hold_seconds=600.0, value=1, callers=1, announce=str(announce)
    )
    try:
        await _until(lambda: _async(announce.exists()))
        assert len(await _claim_rows(key)) == 1
        # The waiters are already running (and blocked on the live holder) when it is killed.
        join_at = time.time() + 8.0
        waiters = [
            _spawn(
                tmp_path,
                f"waiter{i}",
                **{**base, "start_at": join_at},
                hold_seconds=0.2,
                value=2,
                callers=2,
            )
            for i in range(2)
        ]
        await asyncio.sleep(join_at + 1.0 - time.time())
        holder.kill()
        killed_at = time.monotonic()
        holder.communicate(timeout=10)

        contenders = [_Contender(key, hold=0.2, value=2) for _ in range(3)]
        outcomes = [contender.join() for contender in contenders]
        takeover_seconds = time.monotonic() - killed_at
        reports = [report for waiter in waiters for report in _finish(waiter)]
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=10)

    pids = _lines(counter)
    in_thread = sum(contender.computes for contender in contenders)
    assert (len(pids) - 1) + in_thread == 1, (
        f"after the kill, {len(pids) - 1} process(es) and {in_thread} thread(s) computed; "
        "exactly one waiter should have"
    )
    assert pids[0] == str(holder.pid)
    values = [report[0] for report in reports] + [result[0] for _, result in outcomes]
    assert all(value["value"] == 2 for value in values)
    callers = [report[1] for report in reports] + [result[1] for _, result in outcomes]
    assert callers.count(False) == 1, "exactly one caller reports having computed"
    # The lease lapses `_LEASE` after the last beat, and a waiter notices within one poll.
    assert takeover_seconds < _LEASE * 2, takeover_seconds
    assert await _claim_rows(key) == []
    print(f"\nkill -9 to takeover result: {takeover_seconds:.2f}s (lease {_LEASE}s)")


async def _async(value: bool) -> bool:
    """`value` as an awaitable, for `_until`."""
    return value


async def test_a_failed_computation_reaches_its_waiters_and_is_not_retried(
    flight_on: None,
) -> None:
    """The holder raises: waiters get that failure and nobody recomputes; the next call retries."""
    key = _key()
    holder = _Contender(key, hold=1.0, fail="boom: the calculator refused")
    await _until(lambda: _async(holder.computes == 1))
    waiters = [_Contender(key, hold=1.0) for _ in range(3)]

    holder_kind, holder_error = holder.join()
    outcomes = [waiter.join() for waiter in waiters]

    assert holder_kind == "error" and isinstance(holder_error, ValueError)
    assert all(kind == "error" for kind, _ in outcomes), outcomes
    for _, error in outcomes:
        assert isinstance(error, PeerComputationFailed)
        assert "boom: the calculator refused" in str(error)
    assert sum(waiter.computes for waiter in waiters) == 0, "a waiter retried a refused calculation"
    assert await PostgresStore().get(key) is None, "a failure is never cached"

    retry = _Contender(key, hold=0.0, value=9)
    assert retry.join() == ("ok", ({"value": 9}, False)), "the failed row is replaced, not stuck"
    assert await _claim_rows(key) == []


async def test_a_waiters_timeout_leaves_the_holder_and_its_claim_alone(flight_on: None) -> None:
    """A waiter whose budget runs out raises; the holder finishes and the result is cached."""
    key = _key()
    holder = _Contender(key, hold=2.5)
    await _until(lambda: _async(holder.computes == 1))
    (before,) = await _claim_rows(key)

    started = time.monotonic()
    with pytest.raises(PeerWaitTimeout, match="still computing"):
        await cached_compute(PostgresStore(), key, _never, wait_seconds=0.6)
    waited = time.monotonic() - started
    assert 0.5 <= waited < 1.5, f"waited {waited:.2f}s on a 0.6s budget"

    assert await _claim_rows(key) == [before], "the waiter must not have touched the claim"
    assert holder.join() == ("ok", ({"value": 1}, False))
    assert await _claim_rows(key) == []
    again = await cached_compute(PostgresStore(), key, _never)
    assert again == ({"value": 1}, True)


async def _never() -> ResultPayload:
    raise AssertionError("a waiter must not compute")


async def test_a_cancelled_waiter_leaks_neither_claim_nor_listener(flight_on: None) -> None:
    """Cancelling a waiting caller drops its wake-up registration and leaves the holder running."""
    key = _key()
    holder = _Contender(key, hold=2.0)
    await _until(lambda: _async(holder.computes == 1))

    waiter = asyncio.create_task(cached_compute(PostgresStore(), key, _never))
    await asyncio.sleep(0.5)
    listener = flight._listener_for(settings.postgres_dsn)
    assert listener._events, "the waiter should be registered for a wake-up"
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert not listener._events and listener._task is None, "the listener outlived its last waiter"
    assert len(await _claim_rows(key)) == 1, "the holder's claim is untouched"
    assert holder.join() == ("ok", ({"value": 1}, False))


async def test_a_cancelled_holder_releases_the_key_to_a_waiter(flight_on: None) -> None:
    """Cancelling the computing caller removes its claim, so a waiter takes the key over."""
    key = _key()
    holder = _Contender(key, hold=30.0)
    await _until(lambda: _async(holder.computes == 1))
    waiter = _Contender(key, hold=0.1, value=5)
    await asyncio.sleep(0.5)
    holder.cancel()

    assert holder.join()[0] == "cancelled"
    assert waiter.join() == ("ok", ({"value": 5}, False))
    assert waiter.computes == 1
    assert await _claim_rows(key) == []


async def test_a_live_holder_keeps_its_claim_across_many_leases(flight_on: None) -> None:
    """The heartbeat renews the lease, so a computation longer than it is not taken over."""
    key = _key()
    holder = _Contender(key, hold=_LEASE * 3.5)
    await _until(lambda: _async(holder.computes == 1))
    waiter = _Contender(key, hold=0.1, value=2)

    assert holder.join() == ("ok", ({"value": 1}, False))
    assert waiter.join() == ("ok", ({"value": 1}, True))
    assert waiter.computes == 0


async def test_liveness_is_the_database_clock_not_the_process_clock(
    flight_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process whose clock is ten years ahead does not see a live claim as lapsed."""
    key = _key()
    holder = _Contender(key, hold=_LEASE * 2)
    await _until(lambda: _async(holder.computes == 1))
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 10 * 365 * 86400)
    waiter = _Contender(key, hold=0.1, value=2)

    assert holder.join() == ("ok", ({"value": 1}, False))
    assert waiter.join() == ("ok", ({"value": 1}, True))
    assert waiter.computes == 0


async def test_only_a_lapsed_claim_or_a_failed_one_to_a_fresh_caller_can_be_taken(
    flight_on: None,
) -> None:
    """The claim statement compares against `now()` in the database, row by row."""
    claims = PostgresClaims(settings.postgres_dsn)
    live, lapsed, failed = _key().as_str(), _key().as_str(), _key().as_str()
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO calculation_claims (key, attempt, lease_until) VALUES "
            "(%s, 'a', now() + interval '1 hour'), (%s, 'b', now() - interval '1 second')",
            (live, lapsed),
        )
        await conn.execute(
            "INSERT INTO calculation_claims (key, attempt, state, lease_until) "
            "VALUES (%s, 'c', 'failed', now() + interval '1 hour')",
            (failed,),
        )

    assert await claims.claim(live, retry_failed=True) is None
    taken = await claims.claim(lapsed, retry_failed=False)
    assert taken is not None and taken.taken_over
    assert await claims.claim(failed, retry_failed=False) is None, "a waiter must get the failure"
    replaced = await claims.claim(failed, retry_failed=True)
    assert replaced is not None and replaced.taken_over
    vacant = await claims.claim(_key().as_str(), retry_failed=False)
    assert vacant is not None and not vacant.taken_over


async def test_a_waiter_wakes_on_the_release_notification(
    flight_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the poll pushed out to minutes, only NOTIFY can wake the waiter; measure how fast."""
    monkeypatch.setattr(settings, "calc_claim_lease_seconds", 600.0)  # poll every 100 s
    key = _key()
    released: list[float] = []
    original = PostgresClaims.release

    async def timed_release(self: PostgresClaims, claim: flight.Claim) -> None:
        await original(self, claim)
        released.append(time.perf_counter())

    monkeypatch.setattr(PostgresClaims, "release", timed_release)
    holder = _Contender(key, hold=1.0)
    await _until(lambda: _async(holder.computes == 1))

    result = await cached_compute(PostgresStore(), key, _never, wait_seconds=30.0)
    woke = time.perf_counter()

    assert result == ({"value": 1}, True)
    latency = woke - released[0]
    assert latency < 2.0, f"woken {latency:.3f}s after the release committed; the poll is 100s"
    print(f"\nrelease committed to waiter returned: {latency * 1000:.0f} ms")
    holder.join()


async def test_a_waiter_cancelled_while_the_listener_connects_leaves_no_registration(
    flight_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling during the `LISTEN` handshake does not strand a registration or a connection."""

    async def slow(self: flight._Listener, ready: asyncio.Event) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(flight._Listener, "_run", slow)
    key = _key()
    holder = _Contender(key, hold=1.5)
    await _until(lambda: _async(holder.computes == 1))

    waiter = asyncio.create_task(cached_compute(PostgresStore(), key, _never))
    await asyncio.sleep(0.4)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    listener = flight._listener_for(settings.postgres_dsn)
    assert not listener._events and listener._task is None
    holder.join()


async def test_a_missed_notification_costs_one_poll_not_a_hang(
    flight_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the listener never delivering, the poll finds the finished result."""

    async def deaf(self: flight._Listener, ready: asyncio.Event) -> None:
        ready.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(flight._Listener, "_run", deaf)
    key = _key()
    holder = _Contender(key, hold=1.0)
    await _until(lambda: _async(holder.computes == 1))

    started = time.monotonic()
    result = await cached_compute(PostgresStore(), key, _never, wait_seconds=30.0)

    assert result == ({"value": 1}, True)
    assert time.monotonic() - started < 1.0 + _LEASE / 6 + 1.5
    holder.join()


async def test_claims_are_counted_by_outcome(flight_on: None) -> None:
    """`won`, `awaited` and `taken_over` each move on the path that earns them."""

    def reading(outcome: str) -> float:
        wanted = f'chemclaw_calc_claims_total{{outcome="{outcome}"}} '
        for line in METRICS.render().splitlines():
            if line.startswith(wanted):
                return float(line.removeprefix(wanted))
        return 0.0

    won, awaited, taken = reading("won"), reading("awaited"), reading("taken_over")
    waits = METRICS.observations("chemclaw_calc_claim_wait_seconds")[0]
    key = _key()
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO calculation_claims (key, attempt, lease_until) "
            "VALUES (%s, 'dead', now() - interval '1 second')",
            (key.as_str(),),
        )
    first = _Contender(key, hold=0.8)
    await _until(lambda: _async(first.computes == 1))
    second = _Contender(key, hold=0.1)
    first.join()
    second.join()
    other = _key()
    await cached_compute(PostgresStore(), other, _fresh)

    assert reading("taken_over") == taken + 1
    assert reading("awaited") == awaited + 1
    assert reading("won") == won + 1
    assert METRICS.observations("chemclaw_calc_claim_wait_seconds")[0] == waits + 1


async def _fresh() -> ResultPayload:
    return {"value": 3}


class _Spy(InMemoryStore):
    """A store that can coordinate, and fails if asked to."""

    def claims(self) -> PostgresClaims | None:
        raise AssertionError("the claim ledger was consulted")


async def test_a_hit_never_touches_the_claim_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hit path is one read: no claim, no listener, whatever the session store is."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    store = _Spy()
    key = _key()
    await store.put(StoredResult(key=key, result={"value": 1}))

    assert await cached_compute(store, key, _never) == ({"value": 1}, True)


class _Offering(InMemoryStore):
    """Results held in memory, claims decided by the Postgres store's own selection rule."""

    def claims(self) -> PostgresClaims | None:
        return PostgresStore().claims()


async def test_memory_mode_coordinates_in_process_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`session_store=memory` offers no ledger, so a miss never touches the claim table."""
    monkeypatch.setattr(settings, "session_store", "memory")
    assert PostgresStore().claims() is None

    result = await cached_compute(_Offering(), _key(), _fresh)

    assert result == ({"value": 3}, False)


async def test_the_postgres_session_store_offers_a_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same store, under the postgres selection, coordinates."""
    monkeypatch.setattr(settings, "session_store", "postgres")

    assert isinstance(PostgresStore().claims(), PostgresClaims)
