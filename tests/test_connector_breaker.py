"""A connector already known to be down is not dialled again (BS-18).

`connectors.health` verdicts are shared with the per-turn open path, so a dark connector does not
cost `connector_open_timeout_seconds` on every turn. These drive the real open path against a real
dark address and count dials, since the turn's outcome is identical either way. Both recovery
paths — a healthy readiness sweep and the verdict expiring — are tested, because a breaker with no
way back amplifies an outage.
"""

import asyncio
import socket
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from langchain_mcp_adapters.sessions import create_session

from chemclaw.connectors import health
from chemclaw.connectors.manifest import ConnectorManifest, HttpEndpoint
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.connectors.transport import ConnectorSpec
from chemclaw.core.config import settings
from tests.conftest import _free_port


@pytest.fixture
def dials(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record every real dial, wrapping `create_session` rather than replacing it.

    A spy: the session still opens for real against a dark port; the list records whether the dial
    was attempted.
    """
    attempted: list[str] = []

    def _spy(connection: Any) -> Any:
        attempted.append(str(connection.get("url", "")))
        return create_session(connection)

    # Patched by path rather than on an imported alias: `connectors.transport` resolves the name
    # from its own globals at call time, and that module is the one whose dial is being counted.
    monkeypatch.setattr("chemclaw.connectors.transport.create_session", _spy)
    yield attempted


def _dark_spec(name: str) -> ConnectorSpec:
    """A connector whose host is down: a spec pointing at a port nothing is listening on."""
    endpoint = HttpEndpoint(
        url=f"http://127.0.0.1:{_free_port()}/mcp",
        health_url=f"http://127.0.0.1:{_free_port()}/healthz",
        tools=["unreached"],
        read_only=["unreached"],
    )
    return _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name=name)), endpoint)


async def _open(spec: ConnectorSpec) -> list[str]:
    """Open one connector the way a turn does, and return the names that did not come up."""
    async with AsyncExitStack() as stack:
        _, unreachable = await open_connector_specs(stack, [spec])
    return unreachable


def test_a_dark_connector_is_dialled_once_and_skipped_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch, dials: list[str]
) -> None:
    """The failure of one open is a verdict, and the next open reads it instead of repeating it."""
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 60.0)
    spec = _dark_spec("dark")

    first = asyncio.run(_open(spec))
    second = asyncio.run(_open(spec))

    assert len(dials) == 1, "the second turn dialled a host the first turn had just found down"
    # The turn's outcome is unchanged, which is the property that makes the saving free: the
    # connector is still reported unreachable, so the degradation notice and the counter still fire.
    assert first == ["dark"]
    assert second == ["dark"]


def test_a_verdict_older_than_the_window_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, dials: list[str]
) -> None:
    """Recovery without any probe: past the window the next turn dials for real.

    This is the path a process with no readiness route takes — the CLI, a template activity on a
    worker — and it is why the window is a recovery bound rather than a savings one.
    """
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 0.05)
    spec = _dark_spec("dark")

    asyncio.run(_open(spec))
    time.sleep(0.06)
    asyncio.run(_open(spec))

    assert len(dials) == 2


def test_the_breaker_is_off_when_the_window_is_zero(
    monkeypatch: pytest.MonkeyPatch, dials: list[str]
) -> None:
    """0 restores the behaviour before this existed: every turn dials every connector."""
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 0.0)
    spec = _dark_spec("dark")

    asyncio.run(_open(spec))
    asyncio.run(_open(spec))

    assert len(dials) == 2


def test_a_healthy_readiness_sweep_readmits_a_connector_the_open_path_blocked(
    monkeypatch: pytest.MonkeyPatch, dials: list[str]
) -> None:
    """A healthy readiness sweep readmits a connector the open path blocked.

    Driven through the real `probe_connectors` with only the socket replaced: a restarting pod may
    answer `/healthz` before its MCP endpoint and must not wait out the window.
    """
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 60.0)
    spec = _dark_spec("dark")
    asyncio.run(_open(spec))
    assert len(dials) == 1

    manifest = SimpleNamespace(
        name="dark",
        # The sweep asks every manifest about its durable half as well as its endpoint, so a
        # stand-in that omits `jobs` is not one a real `ConnectorManifest` could be.
        jobs=[],
        endpoint=HttpEndpoint(
            url="http://127.0.0.1:1/mcp",
            health_url="http://127.0.0.1:1/healthz",
            tools=["unreached"],
            read_only=["unreached"],
        ),
    )
    monkeypatch.setattr(health, "enabled", lambda: [manifest])
    real_client = httpx.AsyncClient

    def _answering_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _answering_client)
    assert [item.state for item in asyncio.run(health.probe_connectors())] == ["healthy"]
    monkeypatch.setattr(httpx, "AsyncClient", real_client)

    asyncio.run(_open(spec))
    assert len(dials) == 2, "a connector the readiness sweep found healthy was still not dialled"


def test_a_failed_readiness_sweep_spares_the_next_turn_its_open_timeout(
    monkeypatch: pytest.MonkeyPatch, dials: list[str]
) -> None:
    """A failed readiness sweep spares the next turn its open timeout.

    The startup probe and `/readyz` run this sweep, so even an outage's first turn is spared.
    """
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 60.0)
    manifest = SimpleNamespace(
        name="dark",
        # The sweep asks every manifest about its durable half as well as its endpoint, so a
        # stand-in that omits `jobs` is not one a real `ConnectorManifest` could be.
        jobs=[],
        endpoint=HttpEndpoint(
            url=f"http://127.0.0.1:{_free_port()}/mcp",
            health_url=f"http://127.0.0.1:{_free_port()}/healthz",
            tools=["unreached"],
            read_only=["unreached"],
        ),
    )
    monkeypatch.setattr(health, "enabled", lambda: [manifest])
    assert [item.state for item in asyncio.run(health.probe_connectors())] == ["unreachable"]

    spec = _mcp_connection(cast(ConnectorManifest, manifest), manifest.endpoint)
    assert asyncio.run(_open(spec)) == ["dark"]
    assert dials == [], "the turn dialled a connector the readiness sweep had just found down"


def test_a_repeated_failing_sweep_does_not_restart_the_breaker_window(
    monkeypatch: pytest.MonkeyPatch, dials: list[str]
) -> None:
    """A repeated failing sweep does not restart the breaker window.

    `/readyz` re-observes every connector several times per window; if each observation re-dated the
    verdict it would never expire, and a connector whose `/healthz` disagrees with its MCP surface
    would lose its tools for the life of the process. Only a dial restarts the window: after the
    window a turn must dial, and the next must not.
    """
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 0.5)
    manifest = SimpleNamespace(
        name="dark",
        # The sweep asks every manifest about its durable half as well as its endpoint, so a
        # stand-in that omits `jobs` is not one a real `ConnectorManifest` could be.
        jobs=[],
        endpoint=HttpEndpoint(
            url=f"http://127.0.0.1:{_free_port()}/mcp",
            health_url=f"http://127.0.0.1:{_free_port()}/healthz",
            tools=["unreached"],
            read_only=["unreached"],
        ),
    )
    monkeypatch.setattr(health, "enabled", lambda: [manifest])
    spec = _mcp_connection(cast(ConnectorManifest, manifest), manifest.endpoint)

    assert [item.state for item in asyncio.run(health.probe_connectors())] == ["unreachable"]
    time.sleep(0.6)
    assert [item.state for item in asyncio.run(health.probe_connectors())] == ["unreachable"]

    assert asyncio.run(_open(spec)) == ["dark"]
    assert len(dials) == 1, (
        "the sweep re-dated a verdict it did not change, so the window never expired and no turn "
        "ever dialled — the breaker had become permanent"
    )

    assert asyncio.run(_open(spec)) == ["dark"]
    assert len(dials) == 1, "the dial did not restart the window, so every turn now pays the open"


@pytest.fixture
def hanging_port() -> Iterator[int]:
    """A port that completes the TCP handshake and then never speaks.

    The kernel accepts into the backlog, so `httpx` connects and waits out the read timeout on
    `initialize` — the only failure reaching `HeldConnectorSession.__aenter__`'s
    `except TimeoutError`. A refused connect fails fast through the other path.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    try:
        yield int(listener.getsockname()[1])
    finally:
        listener.close()


def test_a_connector_that_accepts_and_never_speaks_is_recorded_too(
    monkeypatch: pytest.MonkeyPatch, dials: list[str], hanging_port: int
) -> None:
    """A connector that accepts and never speaks is recorded too.

    This is the expensive failure (the full open bound per turn) and the `record_reachability` call
    in the `except TimeoutError` branch. Three turns: the first pays, the second is spared, the
    third (past the window) dials again, proving the connector is not dropped permanently.
    """
    monkeypatch.setattr(settings, "connector_open_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "connector_teardown_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 0.5)
    endpoint = HttpEndpoint(
        url=f"http://127.0.0.1:{hanging_port}/mcp",
        health_url=f"http://127.0.0.1:{hanging_port}/healthz",
        tools=["unreached"],
        read_only=["unreached"],
    )
    spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name="mute")), endpoint)

    started = time.monotonic()
    assert asyncio.run(_open(spec)) == ["mute"]
    first = time.monotonic() - started
    assert first >= 0.2, f"the open returned in {first:.3f}s, so it did not reach the open bound"
    assert len(dials) == 1

    assert asyncio.run(_open(spec)) == ["mute"]
    assert len(dials) == 1, (
        "the second turn paid the open bound again against a connector the first turn had just "
        "timed out on — the timeout branch records no verdict"
    )

    time.sleep(0.6)
    assert asyncio.run(_open(spec)) == ["mute"]
    assert len(dials) == 2, "past the window the connector was never dialled again"
