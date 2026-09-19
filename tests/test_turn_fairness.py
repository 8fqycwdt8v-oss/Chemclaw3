"""One chemist may not hold the whole replica.

The admission semaphore (`service_max_concurrent_turns`) is actor-blind, so before this cap existed
one principal opening that many sessions took every permit on the pod and every other chemist was
shed `at_capacity`. `chemclaw.api.detach` carries the measurement from the hang-up direction — one
POST-and-hang-up per permit — and the fix applied there returns the *permit* at a detach, which
addresses that variant and not the general one: a client that keeps reading holds its permits for
the whole run.

What these tests pin is the pair, in both directions. A cap that refuses the actor at its limit and
*also* refuses everybody else is not a fairness guard, it is the outage it was meant to prevent — so
every case here that asserts a refusal asserts a second actor being served in the same breath.

None of this needs Postgres, and that absence is the design rather than a gap: the cap is
per-process (SCALE-1 keeps admission per-process), so there is no cross-replica behaviour an
integration test could reach. With the in-memory store `front.turn_claims` is `None` and the
in-process lease is the whole mechanism.
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

    Parking *inside* the stream is what makes the lease observable: the turn has been admitted, has
    taken its permit and its slot, and is still running — which is the state a concurrency cap is
    about. A turn that merely answered slowly would race the assertion.
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

    One turn per session because the per-session 409 already forbids two, so a per-actor cap can
    only ever be reached across sessions — which is also why this helper exists rather than a loop
    over one session id.
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


def test_an_actor_at_the_cap_is_refused_while_another_actor_is_admitted(monkeypatch: Any) -> None:
    """The case the whole change exists for — and both halves are the test.

    Alice at her cap is refused; bob, on the same pod with permits to spare, is served. Asserting
    only the refusal would pass just as well against a guard that had simply filled the pod, which
    is the behaviour being replaced rather than the one being added.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 2)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 2)

            _as(app, ALICE)
            third = (await client.post("/sessions")).json()["session_id"]
            refused = await client.post(f"/sessions/{third}/messages", json={"message": "hi"})
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
    """The release half, and the guard on `_start_turn_lease` carrying `actor` across its restamp.

    That restamp runs at the hand-off, so an `actor` dropped there would leave every lease
    anonymous from the moment a turn actually streams — the cap would count nothing and still read,
    in review, exactly as it does now. Asserting the live lease carries the actor is what makes the
    freeing assertion below mean something.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 2)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 2)
            assert [lease.actor for lease in app.state.active_turns.values()] == ["alice"] * 2

            _as(app, ALICE)
            third = (await client.post("/sessions")).json()["session_id"]
            assert (
                await client.post(f"/sessions/{third}/messages", json={"message": "hi"})
            ).status_code == 429

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


def test_a_double_submit_to_one_session_is_still_409_not_429(monkeypatch: Any) -> None:
    """A second POST to a *running* session answers the conflict that names what happened.

    This pins `besides=session_id`. Without it the status code a UI sees for a double-submit would
    be a function of how many other sessions the chemist has open — 409 below the cap, 429 at it —
    for one unchanged user action.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 2)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 2)
            running = next(iter(app.state.active_turns))

            _as(app, ALICE)
            again = await client.post(f"/sessions/{running}/messages", json={"message": "hi"})
            assert again.status_code == 409, "the per-actor cap swallowed the session conflict"
            assert "already running" in again.json()["detail"]

            agent.release.set()
            await _drain(held)

    asyncio.run(_run())


def test_the_actor_cap_is_off_in_code() -> None:
    """0 is the code default, and `chemclaw.cli.live_storm` is the concrete reason.

    That instrument drives 48 concurrent turns from one credential to measure this cap's own
    shedding curve; an on-by-default per-actor cap converts those sheds into 429s and breaks the
    one tool that validates admission control. The chart carries the production posture instead
    (D-142/REV-16), which `tests/test_helm_chart.py` holds.
    """
    from chemclaw.core.config.service import ServiceSettings

    assert ServiceSettings().service_max_concurrent_turns_per_actor == 0


def test_one_actor_can_still_fill_the_pod_when_the_cap_is_off(monkeypatch: Any) -> None:
    """With the default, nothing new refuses — the guard is genuinely inert rather than lenient.

    A cap that ships "off" by being set very high is a different thing from one that is not
    consulted, and only the second leaves `live_storm`'s offered-load sweep measuring what it says.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 0)
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 3)
    agent = _ParkedTurn()

    async def _run() -> None:
        app = _app(agent, owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            held = await _hold_turns(app, client, ALICE, 3)
            assert len(app.state.active_turns) == 3, "one actor could not fill the pod"
            agent.release.set()
            await _drain(held)

    asyncio.run(_run())


@pytest.mark.parametrize("label", ["actor", "oid", "session"])
def test_the_refusal_counter_refuses_an_identity_label(label: str) -> None:
    """The one label that must never exist on this counter, held mechanically.

    `/metrics` is unauthenticated and an `oid` is a caller-chosen, unbounded key — minting them is
    precisely how one would route around a per-principal limit — so a labelled series here would
    stop counting at the cardinality cap exactly when it mattered. The identity belongs in the
    WARNING beside the increment, and nowhere else.
    """
    from chemclaw.core.metrics import _COUNTER_LABELS, _COUNTERS, METRICS

    assert "chemclaw_turns_refused_actor_cap_total" in _COUNTERS
    assert "chemclaw_turns_refused_actor_cap_total" not in _COUNTER_LABELS
    with pytest.raises((KeyError, ValueError)):
        METRICS.increment("chemclaw_turns_refused_actor_cap_total", labels={label: "alice"})


def test_the_refusal_carries_retry_after_because_the_client_splits_429_on_it(
    monkeypatch: Any,
) -> None:
    """Without this header the shipped UI tells the chemist their usage budget is gone, for ever.

    `Chemclaw3_ui`'s `errorFromStatus` branches 429 on the **presence** of `Retry-After`: with one
    it raises a transient `rate_limited` banner carrying a countdown, and without one it raises
    `budget_exhausted` — "The usage budget for this service is exhausted." — which locks the
    composer and which that module's own comment says nothing in the UI clears. Both of those
    sentences would be false here: the cap lifts the moment one of the caller's own turns ends.

    A machine-readable `code` is not an alternative. `streamTurn.ts` calls `errorFromStatus` with
    four arguments and the discriminator is the fifth, so the code is dropped before it is read —
    which means the header is the only channel that reaches a client already deployed.

    The value is `service_turn_admission_timeout_seconds` rather than a number chosen here: it is
    already this system's answer to how long waiting for a turn permit is reasonable.
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
            refused = await client.post(f"/sessions/{second}/messages", json={"message": "hi"})

            assert refused.status_code == 429
            assert refused.headers["retry-after"] == "5", (
                "the hint must be the configured admission timeout, not a number invented here"
            )

            agent.release.set()
            await _drain(held)

    asyncio.run(_run())
