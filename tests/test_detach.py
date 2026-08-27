"""A disconnect is a detach, not a stop (D-2026-08-27-a-disconnect-is-a-detach-not-a-stop).

The turn stream used to read any client disconnect as cancellation, so the Stop button and a
Wi-Fi handoff were the same event and a long multi-tool turn died with the connection carrying
it. These tests pin the split from both ends: the `DetachableTurn` pump directly, and the two
routes — the stream that only detaches, and the explicit stop that actually cancels.
"""

import asyncio
import contextlib
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
