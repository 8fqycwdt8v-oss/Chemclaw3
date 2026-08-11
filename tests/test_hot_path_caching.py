"""Per-process resources that were being rebuilt per call.

Three of these showed up as steady background cost in a 50-user load test, all of the same shape:
something expensive to construct was constructed inside a hot path instead of held for the process.

- A Temporal `Client` per call — every connector-job launch, every status poll, every approval
  route. Production runs Temporal over mTLS, so each was a TLS handshake plus three blocking PEM
  reads on the loop serving the chat surface.
- An `httpx.AsyncClient` per *connector* on every `/readyz`, and `/readyz` re-probed the whole
  fleet on every call — a route the kubelet hits every 10 s per pod and that any unauthenticated
  caller can hit as fast as it likes.
"""

import asyncio
from typing import Any

import httpx
import pytest
from temporalio.client import Client

from chemclaw.connectors.manifest import HttpEndpoint
from chemclaw.core import temporal_client
from chemclaw.core.config import settings


def test_the_temporal_client_is_built_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated `connect()` calls share one client instead of opening a channel each time."""
    monkeypatch.setattr(temporal_client, "_CLIENT", None)
    connects = 0

    async def _fake_connect(target: str, **options: Any) -> object:
        nonlocal connects
        connects += 1
        return object()

    monkeypatch.setattr(Client, "connect", _fake_connect)

    async def _run() -> list[object]:
        return list(await asyncio.gather(*(temporal_client.connect() for _ in range(5))))

    clients = asyncio.run(_run())
    assert connects == 1
    assert len({id(client) for client in clients}) == 1


def test_connector_probes_share_one_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `AsyncClient` per sweep, not per connector — a fleet of N cost N TCP setups a probe."""
    from chemclaw.connectors import health

    built = 0
    real_client = httpx.AsyncClient

    def _counting_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        nonlocal built
        built += 1
        return real_client(*args, **kwargs)

    class _Manifest:
        """A minimal enabled-connector stand-in carrying only what the probe reads."""

        def __init__(self, name: str) -> None:
            self.name = name
            self.endpoint = HttpEndpoint(
                url=f"http://127.0.0.1:1/{name}/mcp",
                health_url=f"http://127.0.0.1:1/{name}/healthz",
            )

    monkeypatch.setattr(httpx, "AsyncClient", _counting_client)
    monkeypatch.setattr(health, "enabled", lambda: [_Manifest("a"), _Manifest("b"), _Manifest("c")])

    states = asyncio.run(health.probe_connectors())
    # All three are unreachable (nothing listens on port 1) — the point is how many clients it took.
    assert [item.state for item in states] == ["unreachable"] * 3
    assert built == 1


def test_readyz_reuses_its_connector_sweep_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of readiness probes costs one connector sweep, not one per request."""
    from fastapi.testclient import TestClient

    from chemclaw.api import app as service_app
    from tests.test_service import _no_connectors

    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    sweeps = 0

    async def _counting_probe() -> list[Any]:
        nonlocal sweeps
        sweeps += 1
        return []

    monkeypatch.setattr(service_app, "probe_connectors", _counting_probe)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _counting_probe)

    app = service_app.create_app(connector_factory=_no_connectors)
    with TestClient(app) as client:
        before = sweeps  # the lifespan's own startup probe
        for _ in range(5):
            assert client.get("/readyz").json()["status"] == "ready"
    assert sweeps == before  # every request served from the startup snapshot


def test_readyz_probes_every_request_when_the_window_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero restores the pre-cache behavior, so a deployment can opt out of the staleness."""
    from fastapi.testclient import TestClient

    from chemclaw.api import app as service_app
    from tests.test_service import _no_connectors

    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    sweeps = 0

    async def _counting_probe() -> list[Any]:
        nonlocal sweeps
        sweeps += 1
        return []

    monkeypatch.setattr(service_app, "probe_connectors", _counting_probe)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _counting_probe)

    app = service_app.create_app(connector_factory=_no_connectors)
    with TestClient(app) as client:
        before = sweeps
        for _ in range(3):
            client.get("/readyz")
    assert sweeps == before + 3
