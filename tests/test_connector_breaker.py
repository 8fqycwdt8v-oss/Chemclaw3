"""A connector already known to be down is not dialled again (BS-18).

`connectors.health` has probed every enabled bundle at startup and on every `/readyz` since the
seam existed, and the per-turn open path read none of it. So a dark connector cost
`connector_open_timeout_seconds` on *every* turn for the whole outage, with no backoff, while a
fresh verdict sat in the readiness snapshot — and the readiness route and the open path could not
even see each other, because the snapshot lived on `app.state` in the front door.

These tests drive the real open path against a real dark address and count *dials*, because "the
dial did not happen" is the only observable this change is about: the turn's outcome (no tools, the
name in `unreachable`, the degradation notice) is deliberately identical either way. They fail on
the unfixed code, where the second open dials exactly like the first.

The recovery half is tested as carefully as the breaker itself, because a breaker with no way back
is an outage amplifier: both paths back — the readiness sweep recording a healthy probe, and the
verdict simply expiring — get a test of their own.
"""

import asyncio
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

    A spy, not a stub: the session is still opened for real against a dark port, so the failure
    these tests build on is the same `create_session` failure a dead sidecar produces. What the
    list adds is the one fact the outcome cannot show — whether the dial was attempted at all.
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
    """The fast path back: `/readyz` runs every ten seconds and its verdict wins.

    Driven through the real `probe_connectors`, with only the socket replaced — a connector that
    answers `/healthz` 200 while its MCP endpoint is dark is exactly the state a restarting pod
    passes through, and it must not have to wait out the window.
    """
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 60.0)
    spec = _dark_spec("dark")
    asyncio.run(_open(spec))
    assert len(dials) == 1

    manifest = SimpleNamespace(
        name="dark",
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
    """The verdict the breaker was built for: the *probe* found it down, and no turn had to.

    The startup probe and every `/readyz` run this sweep, so in a cluster the first turn of an
    outage is already spared — which is the half of the saving the open path cannot produce for
    itself.
    """
    monkeypatch.setattr(settings, "connector_breaker_window_seconds", 60.0)
    manifest = SimpleNamespace(
        name="dark",
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
