"""The request budget and the concurrent-turn caps hold for the deployment, not for each replica.

Real front-door processes (`tests/replica_process.py`) on one database, driven over HTTP. Each
claim is measured twice: on replicas that keep their limits in memory, where the count multiplies
by the replica count, and on replicas that share them, where it does not. The first run is the
control that shows the second is not passing for a reason unrelated to sharing.
"""

import asyncio
import json
from collections.abc import Awaitable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from chemclaw.core import db
from tests.pg import create_checkpoint_tables, migrated_db_or_skip
from tests.replicas import marks, replica_env, replicas

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


async def _clear_leases() -> None:
    """Forget every turn lease in the test schema.

    The ceilings count admitted leases schema-wide, so a lease an earlier test left (60 s) would
    otherwise be counted by the next one.
    """
    from chemclaw.core.config import settings

    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM session_turns")


async def _expire(session_id: str) -> None:
    """Lapse a lease at once, as if its holder had not refreshed it for a whole lease."""
    from chemclaw.core.config import settings

    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        await conn.execute(
            "UPDATE session_turns SET expires_at = now() - interval '1 second' "
            "WHERE session_id = %s",
            (session_id,),
        )


async def _admitted(session_id: str) -> bool | None:
    """Whether the session's lease row is admitted; `None` when there is no row."""
    from chemclaw.core.config import settings

    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT admitted FROM session_turns WHERE session_id = %s", (session_id,)
        )
        row = await cursor.fetchone()
    return None if row is None else bool(row[0])


@pytest.fixture
def work(tmp_path: Path) -> Iterator[Path]:
    """A scratch directory for the replicas' marks and gate, over a migrated schema."""
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_checkpoint_tables())
    asyncio.run(_clear_leases())
    yield tmp_path


def _as(user: str) -> dict[str, str]:
    """The header that makes a request `user`'s."""
    return {"x-test-user": user}


# --- the request budget ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["memory", "postgres"])
def test_the_request_budget_is_spent_across_replicas(work: Path, store: str) -> None:
    """Two replicas share one principal's bucket: the burst is spent once, not once per replica.

    Forty requests alternate between the replicas against a burst of ten and a refill too slow to
    matter. In memory each replica holds its own bucket and admits ten, twenty in all; shared, ten.
    """
    env = replica_env(
        work,
        session_store=store,
        service_rate_limit_per_minute=6,
        service_rate_limit_burst=10,
    )
    with replicas(env, 2) as (a, b):
        responses = [
            httpx.get(f"{(a, b)[index % 2].base}/profiles", headers=_as("ana"))
            for index in range(40)
        ]
    admitted = sum(response.status_code == 200 for response in responses)
    refused = [response for response in responses if response.status_code == 429]

    assert admitted + len(refused) == 40
    assert admitted == (20 if store == "memory" else 10), f"{store}: {admitted} admitted"
    assert all(int(response.headers["Retry-After"]) >= 1 for response in refused)
    assert all(response.json() == {"detail": "too many requests"} for response in refused)


def test_one_principals_budget_does_not_spend_anothers(work: Path) -> None:
    """The shared bucket is keyed by principal: ana exhausting hers leaves ben's whole."""
    env = replica_env(
        work, service_rate_limit_per_minute=6, service_rate_limit_burst=3, session_store="postgres"
    )
    with replicas(env, 2) as (a, b):
        for index in range(6):
            httpx.get(f"{(a, b)[index % 2].base}/profiles", headers=_as("ana"))
        ana = httpx.get(f"{b.base}/profiles", headers=_as("ana"))
        ben = [httpx.get(f"{(a, b)[i % 2].base}/profiles", headers=_as("ben")) for i in range(3)]

    assert ana.status_code == 429
    assert [response.status_code for response in ben] == [200, 200, 200]


# --- the concurrent-turn caps ---------------------------------------------------------------------


async def _turn(base: str, user: str, hold: bool = True) -> tuple[int, list[dict[str, Any]]]:
    """Open a session as `user` on one replica and run one turn on it; status and events."""
    async with httpx.AsyncClient(base_url=base, timeout=60) as client:
        created = await client.post("/sessions", headers=_as(user))
        session_id = created.json()["session_id"]
        events: list[dict[str, Any]] = []
        async with client.stream(
            "POST",
            f"/sessions/{session_id}/messages",
            json={"message": f"hold {user}" if hold else f"quick {user}"},
            headers=_as(user),
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return response.status_code, []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line.removeprefix("data:")))
    return 200, events


async def _until(predicate: Any, *, seconds: float = 20.0) -> None:
    """Poll until `predicate()`; fail loudly rather than hang."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("store", ["memory", "postgres"])
def test_the_turn_ceiling_is_the_deployments_not_each_replicas(work: Path, store: str) -> None:
    """Eight turns start at once across two replicas under a ceiling of three.

    Memory: each replica admits three of its four, six running at once. Shared: the ceiling of three
    is held across both replicas however the eight arrive, and the other five are shed with the
    retryable `at_capacity` frame once the admission timeout passes. Distinct people, so no
    per-person cap is involved.
    """
    env = replica_env(
        work,
        session_store=store,
        service_turn_admission_timeout_seconds=2,
        # Each replica may run three; two are running although one was declared, which startup
        # cannot see and the shared ceiling can.
        service_max_concurrent_turns=3,
        **({"service_fleet_max_concurrent_turns": 3} if store == "postgres" else {}),
    )

    async def _run(a_base: str, b_base: str) -> list[tuple[int, list[dict[str, Any]]]]:
        turns = [
            asyncio.create_task(_turn((a_base, b_base)[index % 2], f"u{index}"))
            for index in range(8)
        ]
        await _until(lambda: len(marks(work)) >= (6 if store == "memory" else 3))
        await asyncio.sleep(3.5)  # past the admission timeout: every turn that can be shed has been
        started_while_waiting = len(marks(work))
        (work / "gate").write_text("open")
        results = await asyncio.gather(*turns)
        assert started_while_waiting == (6 if store == "memory" else 3), marks(work)
        return list(results)

    with replicas(env, 2) as (a, b):
        results = asyncio.run(_run(a.base, b.base))

    answered = [events for status, events in results if any(e["type"] == "answer" for e in events)]
    shed = [events for _, events in results if any(e.get("code") == "at_capacity" for e in events)]
    assert len(answered) == (6 if store == "memory" else 3)
    assert len(shed) == 8 - len(answered)
    assert all(
        next(e for e in events if e.get("code") == "at_capacity")["retryable"] for events in shed
    )


@pytest.mark.parametrize("store", ["memory", "postgres"])
def test_the_per_person_cap_counts_every_replica(work: Path, store: str) -> None:
    """One person's third and fourth turn are refused with 429 wherever they land.

    The cap is two. In memory each replica lets the person run two, four in all. Shared, the
    count is read across replicas: the third turn is refused on either, with `Retry-After`.
    """
    env = replica_env(work, session_store=store, service_max_concurrent_turns_per_actor=2)

    async def _run(a_base: str, b_base: str) -> list[int]:
        statuses: list[int] = []
        running: list[asyncio.Task[Any]] = []
        for index in range(4):
            before = len(marks(work))
            task = asyncio.create_task(_turn((a_base, b_base)[index % 2], "ana"))
            running.append(task)
            # One at a time, so each turn has registered before the next asks.
            await _until(lambda t=task, n=before: t.done() or len(marks(work)) > n)
            statuses.append(task.result()[0] if task.done() else 200)
        (work / "gate").write_text("open")
        await asyncio.gather(*running)
        return statuses

    with replicas(env, 2) as (a, b):
        statuses = asyncio.run(_run(a.base, b.base))

    assert statuses == ([200, 200, 200, 200] if store == "memory" else [200, 200, 429, 429])


# --- the statements themselves, under concurrency ---------------------------------------------


async def _claimed(count: int, *, actor: str | None = None, lease: float = 60.0) -> list[str]:
    """Claim `count` fresh sessions the way turns do; the holder of session `i` is `h-i`."""
    from uuid import uuid4

    from chemclaw.agent.session_store import SessionTurnClaims

    claims = SessionTurnClaims()
    ids = [f"limits-{uuid4().hex}" for _ in range(count)]
    taken = await asyncio.gather(
        *(claims.claim(sid, f"h-{sid}", lease, actor=actor) for sid in ids)
    )
    assert all(taken)
    return ids


async def test_simultaneous_admissions_never_exceed_the_ceiling() -> None:
    """Forty replicas' worth of turns ask for five slots at once; exactly five are granted.

    Each admission is its own connection, so the count is only right if the advisory lock
    serialises the count-and-take. Repeated, because a lost race shows up as a rate, not a case.
    """
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    await _clear_leases()
    claims = SessionTurnClaims()
    async with db.pooling():  # as a replica runs: connections are the pool's, not one per request
        for _ in range(3):
            ids = await _claimed(40)
            granted = await asyncio.gather(
                *(
                    claims.admit(sid, f"h-{sid}", fleet_cap=5, actor=None, actor_cap=0)
                    for sid in ids
                )
            )
            assert sum(granted) == 5, f"{sum(granted)} slots granted of 5"
            await asyncio.gather(*(claims.release(sid, f"h-{sid}") for sid in ids))


async def test_simultaneous_admissions_never_exceed_one_persons_cap() -> None:
    """Twelve of one person's turns ask at once under a cap of three; exactly three are granted."""
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    await _clear_leases()
    claims = SessionTurnClaims()
    async with db.pooling():
        ids = await _claimed(12, actor="ana-race")
        granted = await asyncio.gather(
            *(
                claims.admit(sid, f"h-{sid}", fleet_cap=0, actor="ana-race", actor_cap=3)
                for sid in ids
            )
        )
        assert sum(granted) == 3, f"{sum(granted)} granted of 3"
        await asyncio.gather(*(claims.release(sid, f"h-{sid}") for sid in ids))


async def test_a_slot_is_freed_by_release_and_by_a_lapsed_lease() -> None:
    """A finished turn frees its slot at once; a dead pod's turn frees it when its lease lapses."""
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    await _clear_leases()
    claims = SessionTurnClaims()
    (first, doomed, third, fourth) = await _claimed(4)

    def admit(session_id: str) -> Awaitable[bool]:
        return claims.admit(session_id, f"h-{session_id}", fleet_cap=2, actor=None, actor_cap=0)

    assert await admit(first)
    assert await admit(doomed)
    assert not await admit(third)

    await _expire(doomed)  # its pod never refreshed it
    assert await admit(third)
    await claims.release(first, f"h-{first}")
    assert await admit(fourth)
    await asyncio.gather(*(claims.release(s, f"h-{s}") for s in (third, fourth)))


async def test_a_lease_that_lapsed_is_not_refreshed_and_does_not_take_the_slot_back() -> None:
    """A holder whose refresh lands after its lease lapsed gets neither the session nor the slot.

    While the lease was lapsed the slot was free and another turn took it; a late refresh that
    re-asserted `admitted` would pass the ceiling, and one that re-extended the lease would take
    the session from whoever is about to resume it. It is refused, and the holder's turn ends.
    """
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    await _clear_leases()
    claims = SessionTurnClaims()
    (slow, other) = await _claimed(2)

    def admit(session_id: str) -> Awaitable[bool]:
        return claims.admit(session_id, f"h-{session_id}", fleet_cap=1, actor=None, actor_cap=0)

    assert await admit(slow)

    await _expire(slow)  # the database was unreachable for longer than a lease
    assert await admit(other)
    assert not await claims.refresh(slow, f"h-{slow}", 60.0), "a lapsed lease was extended"
    assert await _admitted(other) is True
    (third,) = await _claimed(1)
    assert not await admit(third), "the ceiling of one was passed"

    # A refresh inside the lease changes nothing.
    assert await claims.refresh(other, f"h-{other}", 60.0)
    assert await _admitted(other) is True


async def test_taking_over_a_lapsed_lease_starts_unadmitted() -> None:
    """A new claim on a lapsed, admitted row must not inherit the dead turn's slot."""
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    await _clear_leases()
    claims = SessionTurnClaims()
    (session,) = await _claimed(1)
    assert await claims.admit(session, f"h-{session}", fleet_cap=0, actor=None, actor_cap=0)
    assert await _admitted(session) is True
    await _expire(session)

    assert await claims.claim(session, "successor", 60.0, actor="ana")
    assert await _admitted(session) is False


async def test_a_claim_that_is_no_longer_ours_is_not_admitted() -> None:
    """Admission is for the holder of a live claim; a stale holder cannot take a slot with it."""
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    claims = SessionTurnClaims()
    (session,) = await _claimed(1)
    assert not await claims.admit(session, "someone-else", fleet_cap=0, actor=None, actor_cap=0)


async def test_a_persons_cap_counts_only_their_own_admitted_turns() -> None:
    """The per-person cap is about the person: others' turns neither fill nor free their share."""
    from chemclaw.agent.session_store import SessionTurnClaims

    await migrated_db_or_skip()
    claims = SessionTurnClaims()
    ana = await _claimed(3, actor="ana-cap")
    ben = await _claimed(2, actor="ben-cap")
    for sid in ana[:2]:
        assert await claims.admit(sid, f"h-{sid}", fleet_cap=0, actor="ana-cap", actor_cap=2)
    assert not await claims.admit(ana[2], f"h-{ana[2]}", fleet_cap=0, actor="ana-cap", actor_cap=2)
    for sid in ben:
        assert await claims.admit(sid, f"h-{sid}", fleet_cap=0, actor="ben-cap", actor_cap=2)
    assert await claims.actor_turns("ana-cap", ana[2]) == 2
    assert await claims.actor_turns("ana-cap", ana[0]) == 2  # ana[2] is claimed, if not admitted
    await asyncio.gather(*(claims.release(s, f"h-{s}") for s in ana + ben))


async def test_simultaneous_spends_take_exactly_the_tokens_there_are() -> None:
    """Sixty requests from one principal race for a burst of ten; ten win, wherever they run."""
    from uuid import uuid4

    from chemclaw.api import rate_limit_store

    await migrated_db_or_skip()
    principal = f"bucket-{uuid4().hex}"
    async with db.pooling():
        spends = await asyncio.gather(
            *(rate_limit_store.spend(principal, per_minute=0.6, burst=10) for _ in range(60))
        )
    assert sum(spend.allowed for spend in spends) == 10
    refused = next(spend for spend in spends if not spend.allowed)
    # 0.6 a minute is a token per 100 s; a drained bucket is told to wait the best part of that.
    assert 90 < refused.retry_after(0.01) <= 100


async def test_a_spent_bucket_refills_at_the_configured_rate() -> None:
    """A token returns after 1/rate seconds, by the database's clock, and not before."""
    from uuid import uuid4

    from chemclaw.api import rate_limit_store

    await migrated_db_or_skip()
    principal = f"bucket-{uuid4().hex}"
    rate = 120.0  # per minute: a token every half second
    assert (await rate_limit_store.spend(principal, per_minute=rate, burst=1)).allowed
    assert not (await rate_limit_store.spend(principal, per_minute=rate, burst=1)).allowed
    await asyncio.sleep(0.6)
    assert (await rate_limit_store.spend(principal, per_minute=rate, burst=1)).allowed


async def test_a_database_that_cannot_answer_leaves_the_replicas_own_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the shared bucket is unreachable the limit degrades to per replica, never to none."""
    from chemclaw.api import rate_limit, rate_limit_store
    from chemclaw.core.config import settings

    async def _unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("Postgres unreachable")

    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_rate_limit_per_minute", 6.0)
    monkeypatch.setattr(settings, "service_rate_limit_burst", 2.0)
    monkeypatch.setattr(rate_limit_store, "spend", _unreachable)
    rate_limit.reset_limiter()

    await rate_limit.enforce_request_budget("ana")
    await rate_limit.enforce_request_budget("ana")
    with pytest.raises(rate_limit.RateLimited):
        await rate_limit.enforce_request_budget("ana")
    rate_limit.reset_limiter()


async def test_an_outage_costs_one_wait_not_one_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the shared bucket is down, requests after the first skip the database altogether.

    Each failed spend takes its pool timeout; without a backoff every authenticated request would
    wait that long before falling back. Five requests against a spend that takes 0.3 s to fail cost
    one wait, and the database is asked once; after the window it is asked again (the probe).
    """
    import time

    from chemclaw.api import rate_limit, rate_limit_store
    from chemclaw.core.config import settings

    asked: list[str] = []

    async def _slow_failure(principal: str, **_kwargs: Any) -> Any:
        asked.append(principal)
        await asyncio.sleep(0.3)
        raise ConnectionError("Postgres unreachable")

    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_rate_limit_per_minute", 600.0)
    monkeypatch.setattr(settings, "service_rate_limit_burst", 100.0)
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 1.0)
    monkeypatch.setattr(rate_limit_store, "spend", _slow_failure)
    rate_limit.reset_limiter()

    started = time.perf_counter()
    for _ in range(5):
        await rate_limit.enforce_request_budget("ana")
    elapsed = time.perf_counter() - started
    assert asked == ["ana"], "later requests asked the unreachable database again"
    assert elapsed < 0.9, f"five requests took {elapsed:.2f}s against one 0.3s wait"

    await asyncio.sleep(1.1)  # past the window: the next request is the probe
    await rate_limit.enforce_request_budget("ana")
    assert asked == ["ana", "ana"]
    rate_limit.reset_limiter()
