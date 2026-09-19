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


async def _expect_refused(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    """POST a turn that must be refused, and fail fast rather than hang if it is not.

    A working cap answers from a dict scan, so this returns in milliseconds. A *broken* cap admits
    the turn, which then parks on `_ParkedTurn.release` and blocks this await until the suite-wide
    timeout — and that timeout's own epilogue says "these assertions never ran … nothing above is
    evidence about the code under test", so the shipped diagnostic for the one defect these tests
    exist to catch is a message that disclaims itself. Bounding it here turns that into a named
    failure on the line that meant it.
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
            # **Wait for a *started* lease before reading `actor`.** At claim time the field is
            # already "alice", so asserting it on a reservation passes whether or not
            # `_start_turn_lease` carried it across the restamp — which is the thing this test
            # says it pins. A finite deadline is what "the restamp ran" looks like from here.
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
            again = await _expect_refused(client, running)
            assert again.status_code == 409, "the per-actor cap swallowed the session conflict"
            assert "already running" in again.json()["detail"]

            agent.release.set()
            await _drain(held)

    asyncio.run(_run())


def test_the_actor_cap_is_off_in_code(monkeypatch: Any) -> None:
    """0 is the code default, and `chemclaw.cli.live_storm` is the concrete reason.

    That instrument's family A sweeps the *admission* cap end to end, driving 48 concurrent turns
    from one credential at each value; an on-by-default per-actor cap converts those sheds into
    429s and breaks the
    one tool that validates admission control. The chart carries the production posture instead
    (D-142/REV-16), which `tests/test_deploy_chart.py` holds, and which
    `core/config/__init__.py` refuses outright at startup when it is not strictly below the pod
    cap.
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
    """Delete the increment and this goes red; nothing else in the suite did.

    The shipped dashboard panel reads `chemclaw_turns_refused_actor_cap_total` against
    `chemclaw_turns_shed_total`, and its own description says a flat zero has to be
    distinguishable from "the cap is off". Asserting only that the counter is *declared* — which is
    what this file did first — leaves the panel reading zero for ever if the call site is dropped,
    which is the one failure a metric cannot report about itself.
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
    """The half of the expiry argument that was false until it was driven.

    `TurnLease.actor`'s docstring justifies deriving the count from the lease map because a lease
    expires and a counter does not. That is true of a *started* lease and was not true of a
    reservation: `_claim_turn_slot` stamps `deadline=math.inf` and only `post_message`'s `finally`
    ends it, so a handler parked on one of the reservation phase's three store round trips — none
    of which carries a statement timeout — held the slot with no expiry at all. Inherited by the
    per-actor count, one wedged store call refused that chemist on *every* session until the pod
    restarted, which is exactly the brick the design claims to avoid.

    So an un-started reservation ages from `claimed_at`, at the width `_start_turn_lease` would
    have stamped. The session's own 409 keeps reading `deadline` and is deliberately unchanged.
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
    """With one oid for everybody, "per actor" would mean "per pod" — so the guard stands down.

    `entra_required=False` hands every caller the same `Principal`, which is exactly the shape this
    cap must not act on: it would stop dividing the replica between chemists and start capping the
    replica itself at this number, so the first client to reach it would refuse every other client.
    That is the starvation the guard exists to prevent, inverted, and the configuration is
    reachable — `service_allow_insecure` permits it, and a deployment fronting the API with one
    service credential for many humans has the same shape with no such switch.

    Driven rather than reasoned: this was found by `tests/test_detach.py`, which serves a real
    uvicorn under the dev principal and went red the moment the guard learned to stand down.
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
    """Both properties, because a mutation removing them killed no test.

    The jitter is the whole reason this is not a constant: every client refused by this guard is
    handed the same hint, and without a spread they re-converge on one cadence and arrive together
    at the pod that just refused them. The ceiling is why `Retry-After: 0` — which means "retry
    immediately", turning a hint into a spin — cannot be produced from a sub-second setting.
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
    """Deleting the log line killed no test, and the line is where attribution lives.

    The counter carries no actor label by design, so this record is the only place a refusal is
    tied to a principal. It reports `held` rather than `actor_cap` on purpose: the predicate is
    `>=`, so a count *above* the cap is legitimate and is the one observable symptom of a lease
    outliving its turn — logging the configured number in its place would hide exactly that.
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
