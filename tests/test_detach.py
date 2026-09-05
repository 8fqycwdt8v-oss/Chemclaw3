"""A disconnect is a detach, not a stop (D-2026-08-27-a-disconnect-is-a-detach-not-a-stop).

The turn stream used to read any client disconnect as cancellation, so the Stop button and a
Wi-Fi handoff were the same event and a long multi-tool turn died with the connection carrying
it. These tests pin the split from both ends: the `DetachableTurn` pump directly, and the two
routes — the stream that only detaches, and the explicit stop that actually cancels.
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

    Both `TestClient` and httpx's ASGI transport buffer a response whole and run each request on
    a loop of its own, so "the client dropped mid-stream" and "a second request while the stream
    is open" are inexpressible through either: a detach test against them passes whatever the
    route does. A real socket on the process's one server loop is what the production stack is,
    and it is the shape `tests/test_verifier.py` already uses for its fake model endpoint.
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
    """End to end through the route: the client vanishes, the turn answers anyway.

    The failure this closes was measured in prose all the way down: a 20-minute multi-tool turn
    died with a network blip, its partial answer lost from the live view *and* the transcript
    (written only after the answer). Now the pump finishes the turn, `_record_transcript` runs,
    and a reconnecting client reads the answer from `GET /sessions/{id}/messages`.
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

    The gap is what makes the defect observable: while a detached turn holds its admission permit,
    an honest client's turn is shed rather than merely delayed, so the assertion is on an answer
    rather than on a latency.
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
    """A disconnect must not cost the replica a permit until the turn's 600-second deadline.

    The permit is taken inside the turn generator and released in its `finally`, which since
    `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` runs at the pump's *true* end rather than
    when the client goes away. `detach.py` framed that per session — "a session stays 409-locked
    for exactly as long as a turn is running" — but the permit is not per session: it is the
    process's shared `service_max_concurrent_turns` semaphore. Measured on the real app with the
    shipped cap of 8: eight fresh sessions POSTed and hung up left **0** permits free and every
    other chemist's turn on that replica got `queued` then a shed `error`, for up to
    `service_turn_timeout_seconds`. It needs no malice — a closed laptop, a Wi-Fi handoff or a UI
    that retries on disconnect produces it, and the retry *adds* a holder rather than replacing
    one. Before that ADR a disconnect returned the permit immediately, so the failure was
    self-limiting.

    Driven over a real socket for the reason `_Served` gives: neither `TestClient` nor httpx's ASGI
    transport can express "the client dropped mid-stream".
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


def test_shutdown_waits_for_the_turns_detaching_promised_to_finish() -> None:
    """A rolling update must not destroy exactly the work detaching exists to preserve.

    A pump task is not an in-flight HTTP request, so uvicorn's own drain cannot see one and
    nothing in the process did either: on SIGTERM the lifespan `finally` ran immediately —
    `close_memory_store()`, `close_checkpointer()`, then `db.pooling()` closing the store pool —
    while detached turns were still mid-flight. Measured against the real `_lifespan` with a real
    checkpointer: shutdown returned in **0.001 s** with the turn still running, and that turn's
    next checkpoint write raised `PoolClosed` and booked itself `abandoned`. The client had been
    told to recover its answer from `GET /sessions/{id}/messages`, and it was not there.

    The chart makes it worse by believing it is handled: `terminationGracePeriodSeconds` is
    derived as `CHEMCLAW_SERVICE_TURN_TIMEOUT_SECONDS + service.drainSeconds` specifically so "a
    drain that outlasts it cannot cut one short" — grace the process did not use.
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

    The bound is `service_turn_timeout_seconds` because that is the number the chart's grace period
    is already derived from — and because a turn's own deadline is measured from when it started,
    so in a healthy configuration the turn's timeout always fires first and this bound is never the
    binding one. It exists for the turn whose deadline is not enforceable (a teardown that itself
    hangs), where the honest outcome is a warning and a shutdown, not a pod that never exits.
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

    # On the module's own logger rather than through `caplog`: `_lifespan` calls
    # `configure_logging()`, whose `logging.basicConfig(force=True)` removes every root handler —
    # pytest's capture handler included — so a root-attached capture sees nothing from inside a
    # lifespan.
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
