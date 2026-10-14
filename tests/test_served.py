"""The test server helper waits for readiness, scales its bound, and says why a start failed.

`tests/served.py` replaced eight hand-rolled loops that polled `Server.started` for ten seconds and
raised "did not start" whatever had happened, a lifespan that raised and a port already taken
included (uvicorn exits both with `sys.exit(3)`, which a thread drops silently).
"""

import asyncio
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from tests import served
from tests.served import ServerThread


def _app(lifespan_body: object = None) -> FastAPI:
    """A one-route app whose lifespan runs `lifespan_body` (an async callable) before serving."""

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if lifespan_body is not None:
            await lifespan_body()  # type: ignore[operator]
        yield

    app = FastAPI(lifespan=_lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_a_started_server_answers_and_is_stopped_on_exit() -> None:
    """Entering returns once the server accepts; leaving ends its thread."""
    server = ServerThread(_app())
    with server:
        assert httpx.get(f"http://127.0.0.1:{server.port}/ping").json() == {"ok": "yes"}
    assert not server._thread.is_alive()


def test_a_start_slower_than_any_poll_interval_is_waited_for() -> None:
    """The wait is on the readiness event, so a slow lifespan is waited out, not given up on."""
    release = threading.Event()
    threading.Timer(0.4, release.set).start()

    async def _until_released() -> None:
        while not release.is_set():
            await asyncio.sleep(0.01)

    with ServerThread(_app(_until_released)) as server:
        assert httpx.get(f"http://127.0.0.1:{server.port}/ping").status_code == 200


def test_a_lifespan_that_raises_is_reported_with_its_own_message() -> None:
    """The failure carries the lifespan's exception, not a bare "did not start"."""

    async def _refuse() -> None:
        raise RuntimeError("the pool is down: connection refused")

    with pytest.raises(RuntimeError) as failure, ServerThread(_app(_refuse)):
        pytest.fail("a server whose lifespan raised must not start")
    assert "the pool is down: connection refused" in str(failure.value), failure.value
    assert isinstance(failure.value.__cause__, SystemExit), "uvicorn's own exit is the cause"


def test_the_start_bound_is_one_constant_scaled_by_the_suites_timeout_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that never finishes fails at `STARTUP_SECONDS` x `PYTEST_TIMEOUT_SCALE`."""
    release = threading.Event()

    async def _never_until_released() -> None:
        while not release.is_set():
            await asyncio.sleep(0.01)

    monkeypatch.setattr(served, "STARTUP_SECONDS", 0.2)
    monkeypatch.setenv("PYTEST_TIMEOUT_SCALE", "2")
    try:
        with pytest.raises(TimeoutError) as failure, ServerThread(_app(_never_until_released)):
            pytest.fail("a server whose lifespan is parked must not report started")
    finally:
        release.set()
    assert "after 0.4s" in str(failure.value), failure.value


def test_a_port_is_held_from_construction_so_nothing_can_take_it_before_the_listen() -> None:
    """The window between "find a free port" and "listen on it" is closed, not narrowed."""
    server = ServerThread(_app())
    with socket.socket() as intruder, pytest.raises(OSError):
        intruder.bind(("127.0.0.1", server.port))
    with server:
        assert httpx.get(f"http://127.0.0.1:{server.port}/ping").status_code == 200
