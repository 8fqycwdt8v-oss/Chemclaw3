"""A disconnect is a detach, not a stop (D-2026-08-27-a-disconnect-is-a-detach-not-a-stop).

A client disconnect must not cancel a turn; only the explicit stop does. Pinned from both ends:
the `DetachableTurn` pump directly, and the two routes (the stream that only detaches, the stop
that cancels).
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

from chemclaw.api.detach import DetachableTurn, RunningTurns
from chemclaw.core.config import settings
from tests.fakes_turn import Piece, ScriptedTurn
from tests.test_service import _app


async def _drip(
    produced: list[int], *, count: int = 5, gate: asyncio.Event | None = None
) -> AsyncIterator[dict[str, str]]:
    """A slow turn source: `count` events, recording how far it actually ran."""
    for index in range(count):
        if gate is not None:
            await gate.wait()
            gate.clear()
        else:
            await asyncio.sleep(0.01)
        produced.append(index)
        yield {"event": "token", "data": str(index)}


def test_a_cancelled_reader_detaches_and_the_turn_completes() -> None:
    """The pump outlives its reader — the whole point of the split."""

    async def _run() -> list[int]:
        produced: list[int] = []
        turn = DetachableTurn(_drip(produced), session_id="s-detach")

        async def _read_two() -> None:
            seen = 0
            async for _event in turn.events():
                seen += 1
                if seen == 2:
                    raise asyncio.CancelledError  # the client dropped mid-stream

        reader = asyncio.create_task(_read_two())
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        # The turn runs on: wait for the pump to finish the source.
        async with asyncio.timeout(5):
            while turn.running:
                await asyncio.sleep(0.01)
        return produced

    produced = asyncio.run(_run())
    assert produced == [0, 1, 2, 3, 4], (
        f"the source ran {produced}; a disconnect cancelled the turn instead of detaching"
    )


def test_the_old_posture_is_one_setting_away() -> None:
    """`survive_disconnect=False` restores disconnect-cancels-the-turn exactly."""

    async def _run() -> list[int]:
        produced: list[int] = []
        turn = DetachableTurn(_drip(produced), session_id="s-legacy", survive_disconnect=False)

        async def _read_two() -> None:
            seen = 0
            async for _event in turn.events():
                seen += 1
                if seen == 2:
                    raise asyncio.CancelledError

        reader = asyncio.create_task(_read_two())
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        async with asyncio.timeout(5):
            while turn.running:
                await asyncio.sleep(0.01)
        return produced

    produced = asyncio.run(_run())
    assert len(produced) < 5, "the legacy posture no longer stops the turn on disconnect"


def test_stop_cancels_the_turn_and_the_registry_forgets_it() -> None:
    """`stop()` is the explicit act, and a finished turn stops answering `running`."""

    async def _run() -> tuple[list[int], DetachableTurn | None]:
        produced: list[int] = []
        registry = RunningTurns()
        gate = asyncio.Event()
        turn = DetachableTurn(_drip(produced, gate=gate), session_id="s-stop")
        registry.register("s-stop", turn)
        assert registry.get("s-stop") is turn
        gate.set()
        await asyncio.sleep(0.05)  # let the first event through
        await turn.stop()
        return produced, registry.get("s-stop")

    produced, still_there = asyncio.run(_run())
    assert len(produced) < 5, "stop() did not cancel the running source"
    assert still_there is None, "a stopped turn is still registered as running"


class _SlowAgent(ScriptedTurn):
    """A turn slow enough to detach from, recording whether it finished."""

    def __init__(self) -> None:
        self.finished = False

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "part one "
        await asyncio.sleep(0.2)
        yield "part two"
        self.finished = True


class _Served:
    """The app under a real uvicorn server on loopback — the only honest transport here.

    `TestClient` and httpx's ASGI transport buffer a response whole and run each request on its own
    loop, so "the client dropped mid-stream" and "a second request while the stream is open" cannot
    be expressed through them.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.port = _free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "_Served":
        self._thread.start()
        for _ in range(200):  # ~10s worst case; a real start is tens of milliseconds
            if self._server.started:
                return self
            threading.Event().wait(0.05)
        raise RuntimeError("the app under test did not start")  # pragma: no cover

    def __exit__(self, *_exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    @property
    def base(self) -> str:
        """The server's loopback origin."""
        return f"http://127.0.0.1:{self.port}"

    def wait_for_slot_release(self, session_id: str, *, seconds: float = 10.0) -> None:
        """Block until the session's turn slot frees — the turn's true end."""
        deadline = time.monotonic() + seconds
        while session_id in self.app.state.active_turns:
            if time.monotonic() > deadline:  # pragma: no cover - only on a real regression
                raise AssertionError("the turn never released its slot")
            time.sleep(0.01)


def _free_port() -> int:
    """An ephemeral loopback port, released immediately for uvicorn to claim."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_dropped_stream_still_delivers_the_answer_to_the_transcript() -> None:
    """A dropped stream still delivers the answer to the transcript.

    The pump finishes the turn, `_record_transcript` runs, and a reconnecting client reads the
    answer from `GET /sessions/{id}/messages`.
    """
    agent = _SlowAgent()
    with _Served(_app(agent)) as served, httpx.Client(base_url=served.base) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "long job"}
        ) as response:
            for line in response.iter_lines():
                if line.startswith("data:"):
                    break  # the first streamed event is enough — then the connection drops
        # Leaving the `with` closed the socket mid-turn: the detach. The turn finishes on its
        # pump; wait for its true end, then read the answer back the way a reconnect would.
        served.wait_for_slot_release(session_id)
        assert agent.finished, "the turn was cancelled by the disconnect; detach is not working"
        transcript = client.get(f"/sessions/{session_id}/messages").json()
    texts = [str(entry) for entry in transcript]
    assert any("part two" in text for text in texts), (
        f"the detached turn's answer never reached the transcript: {transcript}"
    )


def test_the_stop_route_cancels_a_running_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Stop button's new home: an owner's POST ends the turn; without a turn it is a 404."""

    class _HangingAgent(ScriptedTurn):
        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "starting"
            await asyncio.sleep(60)
            yield "never"

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 30.0)
    with _Served(_app(_HangingAgent())) as served, httpx.Client(base_url=served.base) as client:
        session_id = client.post("/sessions").json()["session_id"]
        assert client.post(f"/sessions/{session_id}/turn/stop").status_code == 404, (
            "stopping a session with no running turn must say so, not pretend"
        )

        started = time.monotonic()
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "go"}
        ) as response:
            iterator = response.iter_lines()
            for line in iterator:
                if line.startswith("data:"):
                    break  # the turn is demonstrably running: press Stop
            stopped = client.post(f"/sessions/{session_id}/turn/stop")
            assert stopped.status_code == 200 and stopped.json() == {"stopped": True}
            for _line in iterator:
                pass  # the stream ends rather than hanging for the remaining 60 s
        assert time.monotonic() - started < 20, (
            "the stream outlived the stop; the route cancelled nothing"
        )
        served.wait_for_slot_release(session_id)


class _SlowerThanAdmission(ScriptedTurn):
    """A turn that keeps streaming for longer than a queued turn is willing to wait.

    The gap makes the defect observable as a shed answer rather than a latency.
    """

    def __init__(self) -> None:
        """Count how many turns actually reached the model."""
        self.started = 0

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        self.started += 1
        yield "thinking "
        await asyncio.sleep(2.0)
        yield "done"


def _hang_up_mid_turn(client: httpx.Client, session_id: str) -> None:
    """Open a turn, read one event, and drop the socket — the detach a flaky network produces."""
    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": "long job"}
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                return


def test_a_hung_up_client_stops_charging_admission_for_a_turn_nobody_is_watching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung-up client stops charging admission for a turn nobody is watching.

    The admission permit is the process's shared `service_max_concurrent_turns` semaphore, not a
    per-session lock, so a detached turn holding it until its deadline would let ordinary hang-ups
    (a closed laptop, a retrying UI) shed every other chemist's turn. The permit is released at
    detach. Driven over a real socket (see `_Served`).
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 2)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.5)
    agent = _SlowerThanAdmission()

    with _Served(_app(agent)) as served, httpx.Client(base_url=served.base, timeout=30) as client:
        abandoned = [client.post("/sessions").json()["session_id"] for _ in range(2)]
        for session_id in abandoned:
            _hang_up_mid_turn(client, session_id)
        honest = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{honest}/messages", json={"message": "a real question"}
        ) as response:
            events = [line for line in response.iter_lines() if line.startswith("event:")]
        for session_id in (*abandoned, honest):
            served.wait_for_slot_release(session_id)

    assert "event: error" not in events, (
        f"an honest chemist was shed while {len(abandoned)} hung-up clients held every permit: "
        f"{events}"
    )
    assert agent.started == 3, (
        f"only {agent.started} of 3 turns reached the model; the shed one never ran"
    )


def _gauge(exposition: str, name: str) -> float:
    """One unlabelled gauge's value out of a Prometheus exposition."""
    for line in exposition.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split(" ", 1)[1])
    raise AssertionError(f"{name} is not in the exposition")


def test_the_in_flight_gauge_counts_demand_and_can_exceed_the_permit_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chemclaw_turns_in_flight` is leases, not permits — the HPA's signal, named honestly.

    The gauge reads `app.state.active_turns`, populated at POST before admission and kept by a
    detached turn after its permit returns: permits held plus queued plus detached. So
    `in_flight / capacity` is a demand ratio that may exceed 1.0. Driven: two detached and one live
    turn against a cap of two must read 3. Demand rather than permits is argued in
    `src/chemclaw/api/detach.py` (`D-2026-09-19-a-pod-wide-cap-is-not-a-fair-one`): a detached turn
    still spends this pod's CPU, tokens and connection.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 2)
    agent = _SlowerThanAdmission()

    with _Served(_app(agent)) as served, httpx.Client(base_url=served.base, timeout=30) as client:
        abandoned = [client.post("/sessions").json()["session_id"] for _ in range(2)]
        for session_id in abandoned:
            _hang_up_mid_turn(client, session_id)
        live = client.post("/sessions").json()["session_id"]
        # A second connection, because the scrape has to happen *while* the third turn's stream is
        # open and the first client is blocked reading it.
        with (
            httpx.Client(base_url=served.base, timeout=30) as scraper,
            client.stream(
                "POST", f"/sessions/{live}/messages", json={"message": "a real question"}
            ) as response,
        ):
            for line in response.iter_lines():
                if line.startswith("data:"):
                    break
            exposition = scraper.get("/metrics").text
            contended = served.app.state.turn_semaphore.locked()
        for session_id in (*abandoned, live):
            served.wait_for_slot_release(session_id)

    in_flight = _gauge(exposition, "chemclaw_turns_in_flight")
    capacity = _gauge(exposition, "chemclaw_turn_capacity")
    assert (in_flight, capacity) == (3.0, 2.0), (
        f"chemclaw_turns_in_flight read {in_flight} against a capacity of {capacity}; two detached "
        "turns plus one live one is three leases against two permits, and any reading of this "
        "series as an occupancy fraction is wrong by exactly the detached turns"
    )
    assert not contended, (
        "the semaphore was contended, so this run did not distinguish leases from permits"
    )


def test_shutdown_waits_for_the_turns_detaching_promised_to_finish() -> None:
    """Shutdown waits for the turns detaching promised to finish.

    A pump task is not an in-flight HTTP request, so uvicorn's drain cannot see it; without waiting,
    the lifespan `finally` closes the stores and pool under running turns, whose answers then never
    reach the transcript the client was told to read. The chart's grace period is derived to cover
    this drain.
    """

    async def _run() -> tuple[list[int], bool]:
        produced: list[int] = []
        app = _app()
        async with app.router.lifespan_context(app):
            registry: RunningTurns = app.state.running_turns
            # Registered and never read: the detached shape, which is the one nothing could see.
            turn = DetachableTurn(_drip(produced, count=20), session_id="s-drain")
            registry.register("s-drain", turn)
        return list(produced), bool(app.state.running_turns.get("s-drain"))

    produced, still_registered = asyncio.run(_run())

    assert produced == list(range(20)), (
        f"the lifespan returned with the detached turn {len(produced)}/20 events in; a rolling "
        "update closes both Postgres pools underneath it"
    )
    assert not still_registered, (
        "the turn was still running when the process finished shutting down"
    )


def test_shutdown_gives_up_on_a_turn_that_outlasts_the_grace_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain is bounded, and says what it left behind rather than hanging the pod.

    The bound is `service_turn_timeout_seconds` (what the chart's grace period derives from); a
    turn's own deadline fires first in a healthy configuration, so this only catches a hung
    teardown, with a warning and a shutdown.
    """
    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)

    async def _forever() -> AsyncIterator[dict[str, str]]:
        while True:
            await asyncio.sleep(0.05)
            yield {"event": "token", "data": "."}

    async def _run() -> float:
        app = _app()
        turn = DetachableTurn(_forever(), session_id="s-stuck")
        started = time.monotonic()
        async with app.router.lifespan_context(app):
            registry: RunningTurns = app.state.running_turns
            registry.register("s-stuck", turn)
        elapsed = time.monotonic() - started
        await turn.stop()
        return elapsed

    # On the module's own logger rather than `caplog`: `_lifespan` calls `configure_logging()`,
    # whose `basicConfig(force=True)` removes pytest's capture handler.
    said: list[str] = []

    class _Collect(logging.Handler):
        """Keep every record this module emits, whatever the root handlers are doing."""

        def emit(self, record: logging.LogRecord) -> None:
            said.append(record.getMessage())

    handler = _Collect(level=logging.WARNING)
    detach_log = logging.getLogger("chemclaw.api.detach")
    detach_log.addHandler(handler)
    try:
        elapsed = asyncio.run(_run())
    finally:
        detach_log.removeHandler(handler)

    assert elapsed < 5, f"shutdown blocked {elapsed:.1f}s on a turn that never ends"
    assert any("did not finish" in message for message in said), (
        f"a turn abandoned at shutdown left no line an operator could find it by: {said}"
    )


def test_a_detached_turn_still_holds_its_actors_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The permit comes back at a detach and the per-actor slot deliberately does not.

    The permit is fairness to a *waiting client*, which a detached turn no longer has. The per-actor
    slot rations one principal's share of the replica, which a detached turn is still spending until
    the loop cap or turn timeout stops it; releasing it would let POST-and-hang-up bypass the cap.
    """
    # One permit, not two: `Semaphore.locked()` is `value == 0`, so with a cap of two this assertion
    # could not distinguish a returned permit from a kept one.
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 1)
    monkeypatch.setattr(settings, "service_max_concurrent_turns_per_actor", 1)
    agent = _SlowerThanAdmission()

    # A *named* principal, because the cap skips the shared dev principal (with `entra_required`
    # off, "per actor" would mean "per pod"). Installed before the real uvicorn starts.
    from chemclaw.api.auth import Principal, require_principal

    app = _app(agent)
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="detach-alice", upn="a@corp", roles=frozenset()
    )

    with _Served(app) as served, httpx.Client(base_url=served.base, timeout=30) as client:
        abandoned = client.post("/sessions").json()["session_id"]
        _hang_up_mid_turn(client, abandoned)

        # Polled, because the detach hook runs in the server's task and `_hang_up_mid_turn` returns
        # once the socket closes. At a cap of one, `locked()` clears only if the permit was really
        # released.
        deadline = time.monotonic() + 10.0
        while served.app.state.turn_semaphore.locked():
            if time.monotonic() > deadline:  # pragma: no cover - only on a real regression
                raise AssertionError(
                    "the detached turn kept its admission permit; the guard above regressed"
                )
            time.sleep(0.01)
        # ...and the lease did not, so the actor is still counted as running a turn.
        assert abandoned in served.app.state.active_turns

        follow_up = client.post("/sessions").json()["session_id"]
        refused = client.post(f"/sessions/{follow_up}/messages", json={"message": "hi"})
        assert refused.status_code == 429, (
            "hanging up freed the actor's slot, so POST-and-hang-up is unbounded again"
        )

        served.wait_for_slot_release(abandoned)
        # Once the detached turn genuinely ends, the slot comes back with it.
        served_again = client.post(f"/sessions/{follow_up}/messages", json={"message": "hi"})
        assert served_again.status_code == 200
        served.wait_for_slot_release(follow_up)
