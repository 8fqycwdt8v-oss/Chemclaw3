"""One chemist may not hold the whole replica.

The admission semaphore is actor-blind, so a per-actor cap stops one principal taking every
permit. A cap that refuses the actor and also everybody else is the outage it should prevent, so
every refusal asserted here is paired with a second actor being served. No Postgres: the cap is
per process, and with the in-memory store the in-process lease is the whole mechanism.
"""

import asyncio
import contextlib
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from chemclaw.api.auth import Principal, require_principal
from chemclaw.core.config import settings
from tests.fakes import asgi_client
from tests.fakes_turn import ScriptedTurn
from tests.test_service import _app, _FakeOwnerStore

ALICE = Principal(oid="alice", upn="a@corp", roles=frozenset())
BOB = Principal(oid="bob", upn="b@corp", roles=frozenset())


class _ParkedTurn(ScriptedTurn):
    """A turn that starts, streams one piece, and then waits to be let go.

    Parking inside the stream keeps the turn admitted and running, the state a concurrency cap is
    about.
    """

    def __init__(self) -> None:
        """Hold the gate every parked turn waits on."""
        self.release = asyncio.Event()

    async def stream(self, message: str) -> Any:
        """Stream one piece, then hold the turn open until `release` is set."""
        yield "working"
        await self.release.wait()
        yield "done"


def _as(app: FastAPI, principal: Principal) -> None:
    """Speak as `principal` for every subsequent request on `app`."""
    app.dependency_overrides[require_principal] = lambda: principal


async def _hold_turns(
    app: FastAPI, client: httpx.AsyncClient, principal: Principal, count: int
) -> list[asyncio.Task[httpx.Response]]:
    """Start `count` turns for `principal`, each on its own session, and wait until all are live.

    One per session, since a session's line already forbids two concurrent turns.
    """
    _as(app, principal)
    held: list[asyncio.Task[httpx.Response]] = []
    for _ in range(count):
        session_id = (await client.post("/sessions")).json()["session_id"]
        held.append(
            asyncio.create_task(
                client.post(f"/sessions/{session_id}/messages", json={"message": "hold"})
            )
        )
    async with asyncio.timeout(10):
        while len(app.state.active_turns) < count:
            await asyncio.sleep(0.01)
    return held


async def _drain(held: list[asyncio.Task[httpx.Response]]) -> None:
    """Let every parked turn go and collect it, so no task outlives the test."""
    for task in held:
        task.cancel()
    for task in held:
        with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
            await task


async def _expect_refused(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    """POST a turn that must be refused, and fail fast rather than hang if it is not.

    A broken cap admits the turn, which parks and would otherwise block until the suite timeout;
    bounding it gives a named failure here.
    """
    try:
        return await asyncio.wait_for(
            client.post(f"/sessions/{session_id}/messages", json={"message": "hi"}), timeout=10
        )
    except TimeoutError:  # pragma: no cover - only when the cap is broken
        raise AssertionError(
            "the turn was admitted and is streaming; the per-actor cap did not refuse it"
        ) from None


def test_an_actor_at_the_cap_is_refused_while_another_actor_is_admitted(monkeypatch: Any) -> None:
    """An actor at the cap is refused while another actor is admitted.

    Asserting only the refusal would also pass for a guard that simply filled the pod.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 2)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 2)

            _as(app, ALICE)
            third = (await client.post("/sessions")).json()["session_id"]
            refused = await _expect_refused(client, third)
            assert refused.status_code == 429, "alice's third concurrent turn was admitted"
            assert "concurrent turns" in refused.json()["detail"]
            # The header is what the client classifies on, not the status — see the test below.
            assert refused.headers.get("retry-after")

            # The same pod, the same moment: a different chemist is unaffected. This is the
            # assertion that separates a fairness cap from a full replica.
            _as(app, BOB)
            bobs = (await client.post("/sessions")).json()["session_id"]
            bobs_turn = asyncio.create_task(
                client.post(f"/sessions/{bobs}/messages", json={"message": "hi"})
            )
            async with asyncio.timeout(10):
                while len(app.state.active_turns) < 3:
                    await asyncio.sleep(0.01)

            agent.release.set()
            assert (await bobs_turn).status_code == 200
            await _drain(held)

    asyncio.run(_run())


def test_a_finished_turn_frees_the_actors_slot(monkeypatch: Any) -> None:
    """A finished turn frees the actor's slot, and the started lease still carries the actor.

    `_start_turn_lease` restamps the lease at hand-off; dropping `actor` there would leave the cap
    counting nothing.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 2)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 2)
            # Wait for a started lease before reading `actor`: the reservation already carries it,
            # so only a started lease shows the restamp kept it.
            async with asyncio.timeout(10):
                while not any(
                    lease.deadline != float("inf") for lease in app.state.active_turns.values()
                ):
                    await asyncio.sleep(0.01)
            started = [
                lease for lease in app.state.active_turns.values() if lease.deadline != float("inf")
            ]
            assert all(lease.actor == "alice" for lease in started), (
                "_start_turn_lease dropped `actor`; the cap counts nothing once a turn streams"
            )

            _as(app, ALICE)
            third = (await client.post("/sessions")).json()["session_id"]
            assert (await _expect_refused(client, third)).status_code == 429

            agent.release.set()
            await _drain(held)
            async with asyncio.timeout(10):
                while app.state.active_turns:
                    await asyncio.sleep(0.01)

            # The slot came back with the turns, so the same request now succeeds.
            after = asyncio.create_task(
                client.post(f"/sessions/{third}/messages", json={"message": "hi"})
            )
            assert (await after).status_code == 200

    asyncio.run(_run())


def test_a_double_submit_to_one_session_joins_its_line_not_429(monkeypatch: Any) -> None:
    """A second POST to a running session waits in its line; the per-actor cap does not refuse it.

    Pins `besides=session_id`, so a double-submit's answer does not depend on how many other
    sessions the chemist has open.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 2)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 2)
            running = next(iter(app.state.active_turns))

            _as(app, ALICE)
            again = asyncio.create_task(
                client.post(f"/sessions/{running}/messages", json={"message": "hi"})
            )
            async with asyncio.timeout(10):
                while not await app.state.turn_queue.waiting(running):
                    assert not again.done(), (await again).text
                    await asyncio.sleep(0.01)

            agent.release.set()
            await _drain([*held, again])

    asyncio.run(_run())


def test_the_actor_cap_is_off_in_code(monkeypatch: Any) -> None:
    """The actor cap is off in code by default.

    `chemclaw.cli.live_storm` drives many concurrent turns from one credential to sweep the
    admission cap, which an on-by-default per-actor cap would break. The chart carries the
    production setting, and startup refuses a cap not strictly below the pod cap.
    """
    from chemclaw.core.config.service import ServiceSettings

    # `_env_file=None` does not stop pydantic-settings reading the process environment, so an
    # exported override would fail this for a reason that is not the code's.
    monkeypatch.delenv("CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS_PER_ACTOR", raising=False)
    assert ServiceSettings().service_max_concurrent_turns_per_actor == 0


def test_one_actor_can_still_fill_the_pod_when_the_cap_is_off(monkeypatch: Any) -> None:
    """With the default, nothing new refuses — the guard is genuinely inert rather than lenient.

    A cap that ships "off" by being set very high is a different thing from one that is not
    consulted, and only the second leaves `live_storm`'s offered-load sweep measuring what it says.
    """
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 0)
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 3)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            before = METRICS.value("chemclaw_turns_refused_actor_cap_total")
            held = await _hold_turns(app, client, ALICE, 3)
            # The count is implied by `_hold_turns` returning at all; what this test means is that
            # the guard refused nothing on the way.
            assert METRICS.value("chemclaw_turns_refused_actor_cap_total") == before, (
                "the guard refused a turn while configured off"
            )
            agent.release.set()
            await _drain(held)

    asyncio.run(_run())


@pytest.mark.parametrize("label", ["actor", "oid", "session"])
def test_the_refusal_counter_refuses_an_identity_label(label: str) -> None:
    """The refusal counter refuses an identity label.

    `/metrics` is unauthenticated and an `oid` is an unbounded caller-chosen key, so a labelled
    series would hit the cardinality cap when it mattered. The identity goes in the WARNING log
    only.
    """
    from chemclaw.core.metrics import _COUNTER_LABELS, _COUNTERS, METRICS

    assert "chemclaw_turns_refused_actor_cap_total" in _COUNTERS
    assert "chemclaw_turns_refused_actor_cap_total" not in _COUNTER_LABELS
    with pytest.raises((KeyError, ValueError)):
        METRICS.increment("chemclaw_turns_refused_actor_cap_total", labels={label: "alice"})


def test_the_refusal_carries_retry_after_because_the_client_splits_429_on_it(
    monkeypatch: Any,
) -> None:
    """The refusal carries `Retry-After`, because the client splits 429 on it.

    `Chemclaw3_ui`'s `errorFromStatus` treats a 429 without `Retry-After` as an exhausted usage
    budget that locks the composer, while this cap lifts as soon as one of the caller's turns ends.
    The header is the only channel a deployed client reads. Its value is
    `service_turn_admission_timeout_seconds`.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 5.0)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 1)

            _as(app, ALICE)
            second = (await client.post("/sessions")).json()["session_id"]
            refused = await _expect_refused(client, second)

            assert refused.status_code == 429
            hint = int(refused.headers["retry-after"])
            # Jittered over one interval, so refused clients do not re-converge on one cadence and
            # arrive together at the pod that just refused them. Never 0, which would mean "retry
            # immediately" and turn the hint into a spin.
            assert 1 <= hint <= 10, f"hint {hint} is outside base..2x base for a 5s admission wait"

            # Derived from the setting rather than written here: move the setting, move the hint.
            monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 30.0)
            third = (await client.post("/sessions")).json()["session_id"]
            again = await _expect_refused(client, third)
            assert again.status_code == 429
            assert int(again.headers["retry-after"]) > 10, (
                "the hint is a literal, not the configured admission wait"
            )

            agent.release.set()
            await _drain(held)

    asyncio.run(_run())


def test_the_refusal_actually_increments_its_counter(monkeypatch: Any) -> None:
    """The refusal actually increments its counter.

    The dashboard reads it against `chemclaw_turns_shed_total`; a dropped call site would read zero
    forever, which a metric cannot report about itself.
    """
    from chemclaw.core.metrics import METRICS

    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            before = METRICS.value("chemclaw_turns_refused_actor_cap_total")
            held = await _hold_turns(app, client, ALICE, 1)

            _as(app, ALICE)
            second = (await client.post("/sessions")).json()["session_id"]
            assert (await _expect_refused(client, second)).status_code == 429

            after = METRICS.value("chemclaw_turns_refused_actor_cap_total")
            assert after == before + 1, "the refusal did not reach its counter"

            agent.release.set()
            await _drain(held)

    asyncio.run(_run())


def test_a_reservation_that_never_starts_its_lease_ages_out_of_the_actor_count() -> None:
    """A reservation that never starts its lease ages out of the actor count.

    A reservation has `deadline=math.inf` and ends only in `post_message`'s `finally`, so a wedged
    store call would hold the slot forever and refuse that chemist on every session. An un-started
    reservation therefore ages from `claimed_at`; the session's own 409 still reads `deadline`.
    """
    from chemclaw.api.state import _actor_turns_in_flight, _claim_turn_slot

    active: dict[str, Any] = {}
    assert _claim_turn_slot(active, "s1", actor="alice") is not None
    assert active["s1"].deadline == float("inf"), "the reservation is still the 409's to own"
    assert _actor_turns_in_flight(active, "alice", besides="other") == 1

    # Age the reservation past the widest a live turn could hold it, leaving `deadline` inf.
    stale = active["s1"]
    width = settings.service_turn_timeout_seconds + settings.service_turn_admission_timeout_seconds
    active["s1"] = type(stale)(
        token=stale.token,
        deadline=float("inf"),
        actor="alice",
        claimed_at=stale.claimed_at - width - 1.0,
    )
    assert _actor_turns_in_flight(active, "alice", besides="other") == 0, (
        "a parked reservation still holds this actor's slot; one wedged store call bricks them"
    )
    assert "s1" in active, "the session's own 409 must be unaffected by the actor count's view"


def test_the_cap_is_inert_under_the_shared_dev_principal(monkeypatch: Any) -> None:
    """The cap is inert under the shared dev principal.

    With one oid for every caller, "per actor" would mean "per pod", and the first client to reach
    the cap would refuse everyone else.
    """
    from chemclaw.api.auth import _DEV_PRINCIPAL_OID

    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
    shared = Principal(oid=_DEV_PRINCIPAL_OID, upn="dev@localhost", roles=frozenset())
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, shared, 1)

            _as(app, shared)
            second = (await client.post("/sessions")).json()["session_id"]
            second_turn = asyncio.create_task(
                client.post(f"/sessions/{second}/messages", json={"message": "hi"})
            )
            async with asyncio.timeout(10):
                while len(app.state.active_turns) < 2:
                    await asyncio.sleep(0.01)

            agent.release.set()
            assert (await second_turn).status_code == 200, (
                "the cap acted on the shared principal and refused the whole pod"
            )
            await _drain(held)

    asyncio.run(_run())


def test_the_retry_hint_is_jittered_and_never_zero(monkeypatch: Any) -> None:
    """The retry hint is jittered and never zero.

    Without jitter refused clients re-converge and arrive together; `Retry-After: 0` would turn the
    hint into a spin.
    """
    from chemclaw.api.routes.turns import _retry_after_hint

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 5.0)
    draws = {_retry_after_hint() for _ in range(50)}
    assert len(draws) > 1, f"the hint is a constant, not a jittered cadence: {draws}"
    assert all(5 <= int(d) <= 10 for d in draws), f"outside base..2x base: {sorted(draws)}"

    # A sub-second setting must still round up to a real wait rather than to "immediately".
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.4)
    assert {int(_retry_after_hint()) for _ in range(20)} == {1}


def test_the_refusal_names_the_measured_count_not_the_configured_cap(
    monkeypatch: Any, caplog: Any
) -> None:
    """The refusal log names the measured count, not the configured cap.

    The log is the only place a refusal is tied to a principal. The predicate is `>=`, so a count
    above the cap is the visible symptom of a lease outliving its turn.
    """
    import logging

    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 1)

            _as(app, ALICE)
            second = (await client.post("/sessions")).json()["session_id"]
            with caplog.at_level(logging.INFO, logger="chemclaw.api.routes.turns"):
                refused = await _expect_refused(client, second)
            assert refused.status_code == 429

            records = [
                r.getMessage() for r in caplog.records if "refusing a turn" in r.getMessage()
            ]
            assert records, "the refusal reached no log record; attribution has nowhere to live"
            assert "alice" in records[0], "the refusal does not name the principal"
            assert "holding 1 concurrent turn(s)" in records[0], (
                f"the refusal does not report the measured count: {records[0]}"
            )

            agent.release.set()
            await _drain(held)

    asyncio.run(_run())
