"""The reachability sweep, including bundles that have no socket to open.

A bundle declaring `jobs:` and no `endpoint:` is probed by its task queue's pollers, so a worker
fleet at zero replicas is visible to the gauge and to `connectors_required`. These drive the real
sweep through the real registry with only the Temporal client replaced. The stand-in answers with
the SDK's own `DescribeTaskQueueResponse` and `RPCError`; the time-skipping test server answers
`UNIMPLEMENTED`, which is the "cannot tell" case.
"""

import asyncio
import contextlib
import inspect
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args
from unittest import mock

import pytest
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
)
from temporalio.service import RPCError, RPCStatusCode, WorkflowService

from chemclaw.connectors.health import (
    _SEVERITY,
    ConnectorHealth,
    ConnectorState,
    ConnectorsUnavailable,
    _folded,
    check_connectors_at_startup,
    probe_connectors,
)
from chemclaw.connectors.manifest import BearerAuth, ConnectorManifest, HttpEndpoint
from chemclaw.connectors.registry import _mcp_connection, open_connector_specs
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.metrics import Metrics


def _jobs_only(name: str) -> str:
    """A bundle whose whole capability is durable: a job, and no endpoint to probe."""
    return (
        f"name: {name}\n"
        f"description: the {name} capability, which is a durable job\n"
        "jobs:\n"
        f"  - name: run_{name.replace('-', '_')}_job\n"
        "    workflow: FixtureJobWorkflow\n"
        "    summary: Run the job.\n"
        "    description: A job whose worker fleet is the thing being probed.\n"
    )


def _http(name: str, *, health_route: bool) -> str:
    """An ordinary HTTP bundle, with or without the `/healthz` the sweep asks for."""
    route = f"  health_url: http://127.0.0.1:1/{name}/healthz\n" if health_route else ""
    return (
        f"name: {name}\n"
        f"description: the {name} capability\n"
        "endpoint:\n"
        "  transport: http\n"
        f"  url: http://127.0.0.1:1/{name}/mcp\n"
        f"{route}"
        "  tools:\n"
        f"    - {name}_lookup\n"
        "  read_only:\n"
        f"    - {name}_lookup\n"
    )


def _bundles(root: Path, monkeypatch: pytest.MonkeyPatch, **manifests: str) -> None:
    """Write each manifest as a bundle under `root` and point the registry at it, only at it."""
    for name, body in manifests.items():
        (root / name).mkdir(parents=True)
        (root / name / "connector.yaml").write_text(body)
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_dir", str(root))
    monkeypatch.setattr("chemclaw.core.config.settings.connectors_enabled", "")


class _FakeWorkflowService:
    """The one RPC the queue probe makes, scripted, with every request it received recorded."""

    def __init__(
        self,
        pollers: int = 0,
        error: Exception | None = None,
        polled: dict[str, int] | None = None,
    ) -> None:
        self.pollers = pollers
        self.error = error
        # Per-queue poller counts where a test needs two queues to disagree; any queue not named
        # answers with `pollers`.
        self.polled = polled or {}
        self.requests: list[DescribeTaskQueueRequest] = []
        # The deadline each call was given, because *which* budget reached the RPC is the thing the
        # startup sweep changes — and a `wait_for` above it would pass a test that only timed it.
        self.timeouts: list[timedelta | None] = []

    async def describe_task_queue(
        self,
        req: DescribeTaskQueueRequest,
        retry: bool = False,
        metadata: Any = None,
        timeout: timedelta | None = None,
    ) -> DescribeTaskQueueResponse:
        """Answer with the SDK's own response message — the poller list is the whole verdict."""
        self.requests.append(req)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        count = self.polled.get(req.task_queue.name, self.pollers)
        return DescribeTaskQueueResponse(
            pollers=[PollerInfo(identity=f"worker@pod-{i}") for i in range(count)]
        )


class _FakeClient:
    """A Temporal client stand-in exposing exactly what the probe uses: `workflow_service`."""

    def __init__(
        self,
        pollers: int = 0,
        error: Exception | None = None,
        polled: dict[str, int] | None = None,
    ) -> None:
        self.workflow_service = _FakeWorkflowService(pollers=pollers, error=error, polled=polled)


def _broker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pollers: int = 0,
    rpc_error: Exception | None = None,
    connect_error: Exception | None = None,
    polled: dict[str, int] | None = None,
) -> _FakeClient:
    """Install a broker behind the sweep's `connect()` seam — the one every durable caller uses."""
    client = _FakeClient(pollers=pollers, error=rpc_error, polled=polled)

    async def _connect() -> _FakeClient:
        if connect_error is not None:
            raise connect_error
        return client

    monkeypatch.setattr("chemclaw.connectors.health.connect", _connect)
    return client


def _states(health_list: list[ConnectorHealth]) -> dict[str, str]:
    """The sweep's verdict as `{name: state}`, which is what every consumer reads it for."""
    return {item.name: item.state for item in health_list}


def test_the_probe_asks_the_rpc_the_sdk_actually_offers() -> None:
    """The probe's call matches the SDK's own `describe_task_queue` signature.

    `workflow_service` is generated, so a renamed keyword would otherwise leave every durable bundle
    reporting `unknown`.
    """
    signature = inspect.signature(WorkflowService.describe_task_queue)
    signature.bind(
        cast(Any, None),
        DescribeTaskQueueRequest(),
        timeout=timedelta(seconds=settings.connector_health_timeout_seconds),
    )


def test_a_jobs_only_bundle_whose_queue_is_polled_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal that did not exist: a durable bundle is up when something polls its queue.

    Swept beside an ordinary HTTP bundle, because the two halves run concurrently and a change that
    reported the queue correctly while dropping the endpoint sweep would pass a test with only one.
    """
    _bundles(
        tmp_path,
        monkeypatch,
        durable=_jobs_only("durable"),
        alpha=_http("alpha", health_route=True),
    )
    client = _broker(monkeypatch, pollers=2)

    result = asyncio.run(probe_connectors())

    # `alpha`'s health route is a dark loopback port, so the HTTP half still ran and still failed.
    assert _states(result) == {"alpha": "unreachable", "durable": "healthy"}
    (request,) = client.workflow_service.requests
    assert request.task_queue.name == "connector-durable"
    assert request.namespace == settings.temporal_namespace
    # The workflow queue, not the activity queue: every job declares a workflow, while a bundle
    # whose activities live elsewhere would have an idle activity queue and a healthy fleet.
    assert request.task_queue_type == 1  # TASK_QUEUE_TYPE_WORKFLOW


def test_a_jobs_only_bundle_whose_queue_has_no_poller_is_unpolled_and_counts_as_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero pollers is the fleet at zero replicas: reported, and counted where `unreachable` is."""
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    _broker(monkeypatch, pollers=0)

    (item,) = asyncio.run(probe_connectors())

    assert item.state == "unpolled"
    assert item.unhealthy, "an unpolled queue must count in chemclaw_connectors_unhealthy"
    assert "connector-durable" in item.detail


def test_an_unpolled_queue_trips_the_fail_fast_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpolled queue trips the fail-fast gate, naming the bundle and why.

    A bundle whose jobs nothing will run is the degradation `connectors_required` exists to refuse.
    """
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    _broker(monkeypatch, pollers=0)
    monkeypatch.setattr(settings, "connectors_required", True)

    with pytest.raises(ConnectorsUnavailable) as raised:
        asyncio.run(check_connectors_at_startup())
    assert "durable" in str(raised.value) and "unpolled" in str(raised.value)


def test_a_polled_queue_clears_the_same_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so the test above is about the poller count and not about the state."""
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    _broker(monkeypatch, pollers=1)
    monkeypatch.setattr(settings, "connectors_required", True)

    assert _states(asyncio.run(check_connectors_at_startup())) == {"durable": "healthy"}


def test_a_bundle_with_no_durable_work_and_no_health_route_stays_unprobed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle with no durable work and no health route stays unprobed.

    It is not counted and does not gate; guessing a path on a third-party MCP server would invent
    false alarms. (A manifest with neither endpoint nor jobs is refused by
    `_contributes_capability`.)
    """
    _bundles(tmp_path, monkeypatch, quiet=_http("quiet", health_route=False))
    client = _broker(monkeypatch, pollers=0)
    monkeypatch.setattr(settings, "connectors_required", True)

    (item,) = asyncio.run(check_connectors_at_startup())

    assert item.state == "unprobed" and not item.unhealthy
    assert client.workflow_service.requests == [], (
        "a bundle with no jobs owns no queue to ask about"
    )


def test_a_broker_outage_does_not_masquerade_as_a_queue_with_no_poller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker outage does not masquerade as a queue with no poller.

    Otherwise every broker restart is a boot failure under `connectors_required`. `unknown` is not
    `healthy` either; the gate is warned that it has nothing to clear it with.
    """
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    _broker(monkeypatch, connect_error=SubsystemUnavailableError("Temporal is unreachable"))
    monkeypatch.setattr(settings, "connectors_required", True)

    with caplog.at_level(logging.WARNING, logger="chemclaw.connectors.health"):
        (item,) = asyncio.run(check_connectors_at_startup())

    assert item.state == "unknown", "an outage was reported as a fleet at zero replicas"
    assert not item.unhealthy, "a broker blip must not fail the pod's startup"
    assert "could not be determined" in caplog.text and "durable" in caplog.text


def test_a_failed_describe_is_unknown_rather_than_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed describe is `unknown`, not a verdict.

    UNIMPLEMENTED and NOT_FOUND carry no evidence about pollers; only a successful response does.
    """
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    _broker(monkeypatch, rpc_error=RPCError("unimplemented", RPCStatusCode.UNIMPLEMENTED, b""))

    (item,) = asyncio.run(probe_connectors())

    assert item.state == "unknown" and not item.unhealthy
    assert "connector-durable" in item.detail


def test_the_unhealthy_gauge_counts_an_unpolled_bundle_and_not_an_unknown_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unhealthy gauge counts an unpolled bundle and not an unknown one.

    Driven through the real front door, so the gauge and the gate share one definition of "down".
    """
    from fastapi.testclient import TestClient

    from chemclaw.api import app as service_app
    from tests.test_service import _no_connectors

    async def _swept() -> list[ConnectorHealth]:
        return [
            ConnectorHealth(name="alpha", state="healthy"),
            ConnectorHealth(name="durable", state="unpolled", detail="no worker is polling"),
            ConnectorHealth(name="quiet", state="unprobed"),
            ConnectorHealth(name="slow", state="unknown", detail="broker unreachable"),
        ]

    monkeypatch.setattr(service_app, "probe_connectors", _swept)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _swept)
    monkeypatch.setattr(settings, "session_store", "memory")

    with TestClient(service_app.create_app(connector_factory=_no_connectors)) as client:
        exposition = client.get("/metrics").text

    # Exactly one: `unpolled` counts; `healthy`, `unprobed` and `unknown` do not. `1.0` because the
    # exposition renders gauge floats with `repr`, as `prometheus_client.floatToGoString` does.
    assert "\nchemclaw_connectors_unhealthy 1.0\n" in exposition, exposition


def test_the_queue_half_spends_one_budget_rather_than_one_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue half spends one budget rather than one per step.

    `/readyz` runs this sweep inside a kubelet probe whose timeout is derived from
    `connector_health_timeout_seconds`, so the connect and the RPC share it. Both fakes hang for ten
    times a 0.1 s budget, so the assertion is about the bound, not machine speed.
    """
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"), other=_jobs_only("other"))
    budget = 0.1
    monkeypatch.setattr(settings, "connector_health_timeout_seconds", budget)

    class _HangingService:
        async def describe_task_queue(self, request: Any, timeout: Any = None) -> Any:
            await asyncio.sleep(budget * 10)
            raise AssertionError("the RPC outlived the sweep's budget")

    async def _slow_connect() -> Any:
        await asyncio.sleep(budget)  # reachable, but only just
        return type("_Client", (), {"workflow_service": _HangingService()})()

    monkeypatch.setattr("chemclaw.connectors.health.connect", _slow_connect)

    started = time.monotonic()
    result = asyncio.run(probe_connectors())
    elapsed = time.monotonic() - started

    assert _states(result) == {"durable": "unknown", "other": "unknown"}
    assert elapsed < budget * 2, (
        f"the sweep took {elapsed:.3f}s against a {budget}s budget: the connect and the RPC are "
        "spending one each, so a probe timeout derived from that number cannot bound it"
    )
    # The reason has to reach the operator: a bare `TimeoutError` renders as the empty string.
    assert "TimeoutError" in result[0].detail and "connector-durable" in result[0].detail


def _http_serving(name: str, port: int) -> str:
    """An HTTP bundle whose `/healthz` is a real socket on `port`, rather than a dark loopback."""
    return (
        f"name: {name}\n"
        f"description: the {name} capability\n"
        "endpoint:\n"
        "  transport: http\n"
        f"  url: http://127.0.0.1:{port}/{name}/mcp\n"
        f"  health_url: http://127.0.0.1:{port}/{name}/healthz\n"
        "  tools:\n"
        f"    - {name}_lookup\n"
        "  read_only:\n"
        f"    - {name}_lookup\n"
    )


async def _trickle(interval: float, reader: Any, writer: Any) -> None:
    """A `/healthz` that answers, slowly, forever: one byte of the body every `interval`.

    Each read lands inside a per-read timeout, so httpx's `timeout=` (which restarts per read) never
    fires.
    """
    try:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 64\r\n\r\n")
        await writer.drain()
        for _ in range(64):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(interval)
    except (OSError, asyncio.IncompleteReadError):  # pragma: no cover - the client hung up
        pass


def test_the_http_half_bounds_the_answer_rather_than_each_socket_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP half bounds the whole answer rather than each socket read.

    `httpx.AsyncClient(timeout=...)` restarts per read, so a trickling `/healthz` could hold the
    sweep far past the budget the chart's probe timeout is derived from. Two bundles on the same
    slow server, because the sweep bound holds only if probes run concurrently.
    """
    budget = 0.2

    async def _measure() -> tuple[list[ConnectorHealth], float]:
        # Half the budget: each read is inside a per-read timeout of `budget`, while the whole
        # response takes far longer; an interval longer than the timeout would be caught either way.
        server = await asyncio.start_server(
            lambda r, w: _trickle(budget * 0.5, r, w), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        _bundles(
            tmp_path,
            monkeypatch,
            slow=_http_serving("slow", port),
            slower=_http_serving("slower", port),
        )
        monkeypatch.setattr(settings, "connector_health_timeout_seconds", budget)
        started = time.monotonic()
        result = await probe_connectors()
        elapsed = time.monotonic() - started
        server.close()
        return result, elapsed

    result, elapsed = asyncio.run(_measure())

    assert _states(result) == {"slow": "unreachable", "slower": "unreachable"}
    assert elapsed < budget * 3, (
        f"the sweep took {elapsed:.3f}s against a {budget}s budget: a trickling health route "
        "restarts httpx's per-read timeout forever, so only a wall clock bounds it"
    )
    # A bare `TimeoutError` renders as the empty string, so the budget is named rather than shown.
    assert f"{budget}s" in result[0].detail, result[0].detail


def test_a_trickling_health_route_is_not_reported_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trickling health route is not reported healthy.

    A connector that cannot answer within a turn must not be counted healthy by the gauge, the gate
    or the breaker. Asserted separately from the timing, which a fix reporting `unknown` would also
    pass.
    """
    budget = 0.2

    async def _measure() -> list[ConnectorHealth]:
        server = await asyncio.start_server(
            lambda r, w: _trickle(budget * 0.5, r, w), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        _bundles(tmp_path, monkeypatch, slow=_http_serving("slow", port))
        monkeypatch.setattr(settings, "connector_health_timeout_seconds", budget)
        result = await probe_connectors()
        server.close()
        return result

    (item,) = asyncio.run(_measure())

    assert item.state == "unreachable" and item.unhealthy


def test_the_startup_sweep_gets_its_own_budget_so_a_cold_connect_cannot_hide_an_empty_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup sweep gets its own budget, so a cold connect cannot hide an empty queue.

    The first check parses PEM files and does an mTLS handshake from the same budget; running out
    yields `unknown`, which neither counts nor gates, so a zero-replica worker fleet would pass
    `connectors_required`. One broker: `unknown` at the poll budget, `unpolled` (and refused) at the
    startup budget.
    """
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    poll, cold, boot = 0.1, 0.3, 2.0
    monkeypatch.setattr(settings, "connector_health_timeout_seconds", poll)
    monkeypatch.setattr(settings, "connector_startup_health_timeout_seconds", boot)
    monkeypatch.setattr(settings, "connectors_required", True)
    client = _broker(monkeypatch, pollers=0)
    connected = _FakeClient(pollers=0)
    connected.workflow_service = client.workflow_service

    async def _cold_connect() -> _FakeClient:
        await asyncio.sleep(cold)  # PEM parsing and the mTLS handshake, once per process
        return connected

    monkeypatch.setattr("chemclaw.connectors.health.connect", _cold_connect)

    # The poll: the connect eats the budget, so the RPC never answers and nothing is learned.
    assert _states(asyncio.run(probe_connectors())) == {"durable": "unknown"}

    # The boot: the same broker, the same empty queue, and a budget that lets the RPC finish.
    with pytest.raises(ConnectorsUnavailable) as raised:
        asyncio.run(check_connectors_at_startup())
    assert "durable" in str(raised.value) and "unpolled" in str(raised.value)
    # And the budget reached the RPC itself rather than only the `wait_for` above it.
    assert client.workflow_service.timeouts[-1] == timedelta(seconds=boot)


def test_the_startup_budget_is_materially_larger_than_the_polls() -> None:
    """The startup budget is materially larger than the poll's; the defaults are pinned.

    The poll budget bounds a kubelet probe; the startup budget is paid once under a startup probe
    already granting 300 s.
    """
    assert (
        settings.connector_startup_health_timeout_seconds
        >= settings.connector_health_timeout_seconds * 2
    )


async def _ok(reader: Any, writer: Any) -> None:
    """A `/healthz` that answers 200 at once, so the endpoint half is the *healthy* one."""
    try:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()
    except (OSError, asyncio.IncompleteReadError):  # pragma: no cover - the client hung up
        pass


def _http_and_jobs(name: str, port: int) -> str:
    """The shipped shape this sweep had no verdict for: an endpoint *and* durable work.

    `calc` (twelve jobs) and `bo` (one) are both this, which is 13 of the fleet's 14 declared jobs.
    """
    return _http_serving(name, port) + (
        "jobs:\n"
        f"  - name: run_{name}_job\n"
        "    workflow: FixtureJobWorkflow\n"
        "    summary: Run the job.\n"
        "    description: A job whose worker fleet is the thing being probed.\n"
    )


def test_a_bundle_that_serves_an_endpoint_and_owns_jobs_has_its_queue_probed_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle that serves an endpoint and owns jobs has its queue probed too.

    The two questions are additive and the worse answer is the verdict, so a live MCP pod in front
    of a zero-replica worker fleet is not `healthy`.
    """

    async def _measure() -> tuple[list[ConnectorHealth], _FakeClient]:
        server = await asyncio.start_server(_ok, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _bundles(tmp_path, monkeypatch, both=_http_and_jobs("both", port))
        client = _broker(monkeypatch, pollers=0)
        result = await probe_connectors()
        server.close()
        return result, client

    result, client = asyncio.run(_measure())

    (item,) = result  # one row per connector, not one per half
    assert [req.task_queue.name for req in client.workflow_service.requests] == ["connector-both"]
    assert item.state == "unpolled", "the endpoint answered for the whole bundle again"
    assert item.unhealthy, "an unpolled queue must count in chemclaw_connectors_unhealthy"
    assert "connector-both" in item.detail


def test_a_dark_endpoint_still_decides_when_the_queue_half_is_the_healthy_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so the fold is "the worse half" rather than "the queue wins".

    `_http`'s health route is a dark loopback port and the queue has a poller: the bundle is still
    down, because a polled worker fleet does not make an MCP endpoint dialable.
    """
    _bundles(
        tmp_path,
        monkeypatch,
        both=_http("both", health_route=True) + "jobs:\n"
        "  - name: run_both_job\n"
        "    workflow: FixtureJobWorkflow\n"
        "    summary: Run the job.\n"
        "    description: A job whose worker fleet is polled.\n",
    )
    _broker(monkeypatch, pollers=1)

    (item,) = asyncio.run(probe_connectors())

    assert item.state == "unreachable"
    assert item.detail, "the endpoint's reason is what an operator acts on"


def test_the_severity_order_names_every_state_a_connector_can_be_in() -> None:
    """`_SEVERITY` names every state in `ConnectorState`.

    A state missing from the ordering would raise out of `probe_connectors`, which must never raise
    (it backs the boot gate and `/readyz`).
    """
    assert set(_SEVERITY) == set(get_args(ConnectorState))


def test_a_state_the_severity_order_has_not_been_taught_still_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state the severity order has not been taught still folds.

    The runtime half: ranking is total, so with only `unknown` ranked the fold still produces its
    row.
    """
    monkeypatch.setattr("chemclaw.connectors.health._SEVERITY_RANK", {"unknown": 0})

    folded = _folded(
        [
            ConnectorHealth(name="calc", state="healthy", detail="endpoint up"),
            ConnectorHealth(name="calc", state="unreachable", detail="queue dark"),
        ]
    )

    assert [health.name for health in folded] == ["calc"]
    assert "queue dark" in folded[0].detail


class _HealthyButBroken(BaseHTTPRequestHandler):
    """`GET /healthz` answers 200; `POST /mcp` answers the failure the class name promises.

    The shape this whole pair of tests is about: a pod that is up, listening, and passing its
    readiness route while every MCP call to it fails. `mode` is set on the class by `_serving`.
    """

    mode = "500"

    def _body(self, code: int, body: str, ctype: str = "application/json") -> None:
        """Write one bounded response — the handler does nothing else."""
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        """The readiness route an operator and the sweep both believe."""
        if self.path.endswith("/healthz"):
            self._body(200, '{"status":"ok"}')
        else:
            self._body(404, "{}")

    def do_POST(self) -> None:
        """The MCP route, broken in one of the two ways a real pod breaks."""
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if type(self).mode == "500":
            self._body(500, '{"error":"internal"}')
        else:
            self._body(200, "<html>not json at all</html>", "text/html")

    def log_message(self, *_args: Any) -> None:
        """Silence the handler's own stderr logging, which is not what these tests read."""


@contextlib.contextmanager
def _serving(mode: str) -> Iterator[int]:
    """A real listener on an ephemeral port: 200 on `/healthz`, `mode` on `/mcp`."""
    handler = type("_Handler", (_HealthyButBroken,), {"mode": mode})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _turn_open(name: str, port: int) -> tuple[int, list[str]]:
    """Open one connector the way a turn does, and report (tool count, names that did not come up).

    The spec is built through `_mcp_connection` off a real `HttpEndpoint`, which is what a turn
    uses, so the bearer declaration and the timeouts are the deployment's rather than a fixture's.
    """
    endpoint = HttpEndpoint(
        url=f"http://127.0.0.1:{port}/{name}/mcp",
        health_url=f"http://127.0.0.1:{port}/{name}/healthz",
        tools=[f"{name}_lookup"],
        read_only=[f"{name}_lookup"],
    )
    spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name=name)), endpoint)

    async def _open() -> tuple[int, list[str]]:
        async with AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, [spec])
            return len(tools), unreachable
        raise AssertionError("unreachable")  # pragma: no cover

    return asyncio.run(_open())


#: How `/mcp` is broken, and the words the WARNING must carry for it. A `500` raises
#: `httpx.HTTPStatusError` inside the handshake's `TaskGroup`; a `200` with `text/html` (an ingress
#: error page) leaves the client waiting until the open times out. Neither may render as a bare
#: `ExceptionGroup` or an empty `TimeoutError`.
_BROKEN_MCP: dict[str, tuple[str, ...]] = {
    "500": ("HTTPStatusError", "500"),
    "garbage": ("TimeoutError", "handshake did not complete"),
}


@pytest.mark.parametrize("mode", sorted(_BROKEN_MCP))
def test_a_connector_healthy_on_healthz_and_broken_on_mcp_is_reported_by_the_turn(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connector healthy on `/healthz` and broken on `/mcp` is reported by the turn.

    Driven against a real listener. The sweep's `healthy` verdict is asserted, not changed:
    `/healthz` stays the whole probe because a full MCP handshake costs over ten times more on a
    route the kubelet runs every 10 s (no-traffic detection is a `docs/planning/BACKLOG.md` row).
    The turn must:

    - put the connector in `unreachable`, so the turn degrades and says so;
    - label `chemclaw_connectors_unreachable_total` with `connector`;
    - log the leaf error, not the enclosing `ExceptionGroup` (see `_BROKEN_MCP`).
    """
    metrics = Metrics()
    with _serving(mode) as port:
        with (
            mock.patch("chemclaw.core.metrics_bridge.METRICS", metrics),
            caplog.at_level(logging.WARNING, logger="chemclaw.connectors.transport"),
        ):
            tools, unreachable = _turn_open("probe", port)
        # The sweep, over the same live listener, through the real registry.
        _bundles(tmp_path, monkeypatch, probe=_http_serving("probe", port))
        sweep = asyncio.run(probe_connectors())

    assert _states(sweep) == {"probe": "healthy"}, (
        f"the readiness sweep no longer calls this connector healthy ({_states(sweep)}); if that "
        "was deliberate, the decision recorded in this docstring and in BACKLOG.md changed, and "
        "the alert pair needs revisiting"
    )
    assert sum(1 for item in sweep if item.unhealthy) == 0, (
        "`ChemclawConnectorsUnhealthy` reads `max(chemclaw_connectors_unhealthy) > 0`, so a "
        "non-zero count here would mean this test is no longer about the case that alert misses"
    )

    assert (tools, unreachable) == (0, ["probe"]), (
        f"the turn got {tools} tool(s) and reported {unreachable}; a connector that fails every "
        "call must contribute nothing and be named"
    )
    rendered = metrics.render()
    assert 'chemclaw_connectors_unreachable_total{connector="probe"} 1' in rendered, (
        "`ChemclawConnectorsDegradingTurns` reads "
        "`increase(chemclaw_connectors_unreachable_total[15m]) > 0` and its runbook entry tells an "
        "operator to read the `connector` label; an unlabelled sample says only that something "
        f"went dark. Got:\n{rendered}"
    )

    lines = [record.getMessage() for record in caplog.records if "is unreachable" in record.msg]
    assert len(lines) == 1, f"expected one unreachable WARNING, got {lines}"
    assert "ExceptionGroup" not in lines[0], (
        f"the line still reports the group rather than what failed: {lines[0]}"
    )
    missing = [word for word in _BROKEN_MCP[mode] if word not in lines[0]]
    assert not missing, (
        f"the line omits {missing}, so the operator is sent after a network fault while "
        f"`/healthz` says the pod is fine: {lines[0]}"
    )


def test_a_connector_whose_token_is_unset_names_the_variable_rather_than_the_group(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connector whose token is unset names the variable rather than the exception group.

    The open fails before a byte is sent; its remedy is the opposite of a broken pod's. Tested apart
    from the `httpx` cases because the leaf is first-party
    (`connectors/identity.MissingConnectorCredential`).
    """
    monkeypatch.delenv("CHEMCLAW_PROBE_MCP_TOKEN", raising=False)
    with _serving("500") as port:
        endpoint = HttpEndpoint(
            url=f"http://127.0.0.1:{port}/probe/mcp",
            tools=["probe_lookup"],
            read_only=["probe_lookup"],
            auth=BearerAuth(token_env="CHEMCLAW_PROBE_MCP_TOKEN"),
        )
        spec = _mcp_connection(cast(ConnectorManifest, SimpleNamespace(name="probe")), endpoint)

        async def _open() -> list[str]:
            async with AsyncExitStack() as stack:
                _, unreachable = await open_connector_specs(stack, [spec])
                return unreachable
            raise AssertionError("unreachable")  # pragma: no cover

        with caplog.at_level(logging.WARNING, logger="chemclaw.connectors.transport"):
            unreachable = asyncio.run(_open())

    assert unreachable == ["probe"]
    line = next(record.getMessage() for record in caplog.records if "is unreachable" in record.msg)
    assert "ExceptionGroup" not in line, line
    assert "MissingConnectorCredential" in line and "CHEMCLAW_PROBE_MCP_TOKEN" in line, (
        "an unset credential and a broken pod are different remedies and must not be one sentence: "
        f"{line}"
    )


def _http_jobs_and_queued(name: str, port: int) -> str:
    """`bo`'s shipped shape: an endpoint, durable jobs, and a `queued:` tool on the endpoint."""
    return (
        _http_serving(name, port).replace(
            "  read_only:\n",
            f"  queued:\n    inline_wait_seconds: 45\n    tools:\n      - {name}_lookup\n"
            "  read_only:\n",
        )
        + "jobs:\n"
        f"  - name: run_{name}_job\n"
        "    workflow: FixtureJobWorkflow\n"
        "    summary: Run the job.\n"
        "    description: A job whose worker fleet is polled.\n"
    )


def _sweep_queued_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, polled: dict[str, int], *, startup: bool
) -> tuple[list[ConnectorHealth], _FakeClient]:
    """Sweep one healthy-endpoint bundle with jobs and a queued tool, against `polled`."""

    async def _measure() -> tuple[list[ConnectorHealth], _FakeClient]:
        server = await asyncio.start_server(_ok, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _bundles(tmp_path, monkeypatch, heavy=_http_jobs_and_queued("heavy", port))
        client = _broker(monkeypatch, polled=polled)
        try:
            sweep = check_connectors_at_startup() if startup else probe_connectors()
            return await sweep, client
        finally:
            server.close()

    return asyncio.run(_measure())


def test_a_queued_tools_interactive_queue_with_no_poller_is_unpolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued tool's interactive queue with no poller is `unpolled`.

    Otherwise queued calls become jobs nothing polls while the endpoint and the jobs worker both
    look healthy; the interactive queue is the third question.
    """
    result, client = _sweep_queued_bundle(
        tmp_path,
        monkeypatch,
        {"connector-heavy": 1, "connector-heavy-interactive": 0},
        startup=False,
    )

    asked = sorted(req.task_queue.name for req in client.workflow_service.requests)
    assert asked == ["connector-heavy", "connector-heavy-interactive"]
    (item,) = result
    assert item.state == "unpolled", "an unpolled interactive queue was hidden by the other halves"
    assert item.unhealthy
    assert "connector-heavy-interactive" in item.detail and "interactive-worker" in item.detail


def test_an_unpolled_interactive_queue_trips_the_startup_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`connectors_required` refuses on it, as it does on an unpolled jobs queue."""
    monkeypatch.setattr(settings, "connectors_required", True)
    with pytest.raises(ConnectorsUnavailable) as raised:
        _sweep_queued_bundle(
            tmp_path,
            monkeypatch,
            {"connector-heavy": 1, "connector-heavy-interactive": 0},
            startup=True,
        )
    assert "heavy (unpolled)" in str(raised.value)


def test_a_polled_interactive_queue_clears_the_startup_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so the test above is about the poller and not the bundle's shape.

    With every queue polled, the same bundle passes the same gate.
    """
    monkeypatch.setattr(settings, "connectors_required", True)
    result, _ = _sweep_queued_bundle(
        tmp_path,
        monkeypatch,
        {"connector-heavy": 1, "connector-heavy-interactive": 1},
        startup=True,
    )
    assert _states(result) == {"heavy": "healthy"}


def test_a_bundle_that_queues_nothing_is_not_asked_about_an_interactive_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `queued:`, no interactive worker in the chart, so no question about one.

    Asked anyway, every such bundle would read `unpolled` against a queue nothing is meant to poll.
    """

    async def _measure() -> _FakeClient:
        server = await asyncio.start_server(_ok, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _bundles(tmp_path, monkeypatch, both=_http_and_jobs("both", port))
        client = _broker(monkeypatch, pollers=1)
        try:
            assert _states(await probe_connectors()) == {"both": "healthy"}
        finally:
            server.close()
        return client

    client = asyncio.run(_measure())
    assert [req.task_queue.name for req in client.workflow_service.requests] == ["connector-both"]
