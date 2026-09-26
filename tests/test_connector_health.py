"""The reachability sweep, and the half of it that has no socket to open.

`connectors/health.py` derived every target from `health_url(manifest)`, which is None for a bundle
that declares `jobs:` and no `endpoint:` — so `results` reported `unprobed` with its worker fleet at
two replicas and with it at zero, `chemclaw_connectors_unhealthy` counted neither, and
`connectors_required` — the posture whose whole point is refusing to serve degraded — could not see
the failure with the largest blast radius. These tests drive the real sweep through the real
registry (tmp bundles, real `connector.yaml`, real `ConnectorManifest`) with only the Temporal
client replaced, because the manifest → queue → verdict path is the thing being fixed.

The client stand-in answers with the SDK's **own** `DescribeTaskQueueResponse` and fails with its
own `RPCError`, rather than with a hand-shaped object: the two verdicts this change turns on are
"the poller list is empty" and "the call raised", and both are properties of that wire type. The
time-skipping test server cannot stand in here — measured, it answers `DescribeTaskQueue` with
`UNIMPLEMENTED`, which is precisely the "cannot tell" case rather than a poller count.
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

    def __init__(self, pollers: int = 0, error: Exception | None = None) -> None:
        self.pollers = pollers
        self.error = error
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
        return DescribeTaskQueueResponse(
            pollers=[PollerInfo(identity=f"worker@pod-{i}") for i in range(self.pollers)]
        )


class _FakeClient:
    """A Temporal client stand-in exposing exactly what the probe uses: `workflow_service`."""

    def __init__(self, pollers: int = 0, error: Exception | None = None) -> None:
        self.workflow_service = _FakeWorkflowService(pollers=pollers, error=error)


def _broker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pollers: int = 0,
    rpc_error: Exception | None = None,
    connect_error: Exception | None = None,
) -> _FakeClient:
    """Install a broker behind the sweep's `connect()` seam — the one every durable caller uses."""
    client = _FakeClient(pollers=pollers, error=rpc_error)

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
    """The call shape is upstream's, checked against upstream's own signature rather than believed.

    `describe_task_queue` is reached through `workflow_service`, which is generated: mypy sees the
    argument types and nothing sees a renamed keyword. Binding the real signature to the call this
    module makes turns an SDK rename into a failure here instead of into every durable bundle
    reporting `unknown` forever, which is the shape this change is least able to notice.
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
    """`connectors_required` refuses to serve on it, and says which bundle and why.

    The posture is "prefer death to degradation", and a bundle whose jobs nothing will run is the
    degradation it was opted into for: the job is accepted, the chemist is told "running", and the
    answer arrives when `connector_job_timeout_seconds` expires a day later.
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
    """Nothing to ask is still not an error, and the strictest posture must not invent one.

    A manifest with neither an endpoint nor a job cannot exist — `_contributes_capability` refuses
    it — so the realizable form of "nothing to probe" is an endpoint that declares no health route
    and owns no durable work. It is `unprobed`, it is not counted, and it does not gate: guessing a
    path on a third-party MCP server would manufacture the false alarm this state exists to avoid.
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
    """Temporal being down is a different fact from "nobody is polling", and is reported as one.

    Conflating them would make every broker restart a boot failure under `connectors_required`,
    which is the outage-as-a-different-fact defect D-2026-08-08 catalogued. `unknown` is also not
    `healthy`: the gate is not cleared by a check that did not run — it is told, in a WARNING of its
    own, that there is nothing to clear it with.
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
    """A reachable broker that refuses the call is still "we could not measure".

    UNIMPLEMENTED is not hypothetical: the time-skipping test server answers `DescribeTaskQueue`
    with it, and a namespace that does not exist answers NOT_FOUND. Neither is evidence about a
    poller, and only a successful response carries any.
    """
    _bundles(tmp_path, monkeypatch, durable=_jobs_only("durable"))
    _broker(monkeypatch, rpc_error=RPCError("unimplemented", RPCStatusCode.UNIMPLEMENTED, b""))

    (item,) = asyncio.run(probe_connectors())

    assert item.state == "unknown" and not item.unhealthy
    assert "connector-durable" in item.detail


def test_the_unhealthy_gauge_counts_an_unpolled_bundle_and_not_an_unknown_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real front door, because the gauge is a binding and bindings drift.

    `chemclaw_connectors_unhealthy` is the alerting half of this signal, and it read
    `state == "unreachable"` at a second site: a new down-state that the gate honoured and the gauge
    did not would be two definitions of "down" in one deployment.
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

    # Exactly one: `unpolled` counts, and `healthy`, `unprobed` and `unknown` do not.
    # `1.0` rather than `1` because a gauge holds a float and the exposition renders it with
    # `repr` — the same text `prometheus_client.floatToGoString` produces for 1.0. The old
    # `:g` spelling printed `1` and lost the counted digits past a million, which is why it
    # went (`D-2026-09-16-six-significant-digits-is-not-the-number-that-was-counted`).
    assert "\nchemclaw_connectors_unhealthy 1.0\n" in exposition, exposition


def test_the_queue_half_spends_one_budget_rather_than_one_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/readyz` runs this sweep, and a kubelet probe's default timeout is one second.

    The connect and the RPC each used to carry `connector_health_timeout_seconds`, so a broker
    reachable enough to accept a connection and then blackhole the call cost *twice* the number the
    deployment's `timeoutSeconds` is derived from. Measured here rather than reasoned about: both
    steps hang, and the sweep still has to come back inside one budget with every bundle `unknown`.

    The budget is squeezed to a tenth of a second so the assertion is about the bound rather than
    about how fast this machine is; the fakes hang for ten times it, in both places at once.
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

    The pathology this exists to reproduce, and it is a realistic one — an overloaded pod behind an
    ingress that flushes as it goes. Every individual read lands well inside a per-read timeout, so
    httpx's `timeout=` never fires: the deadline it enforces restarts on each byte.
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
    """The endpoint half had the defect the queue half was fixed for, one layer down.

    `httpx.AsyncClient(timeout=...)` is a **per-operation** timeout, not a budget: the read leg
    restarts it on every socket read. Measured against the shipped 2 s number before this change, a
    `/healthz` trickling one byte every 1.5 s held `_probe_endpoints` for **16.6 s** — and then
    reported `healthy`, because the response did eventually arrive. `/readyz` runs this sweep
    inside a kubelet probe whose `timeoutSeconds` the chart *derives* from that same number, so the
    derivation was describing a bound that did not exist.

    Two bundles rather than one, pointed at the same slow server: the per-endpoint bound is only a
    sweep bound because the probes run concurrently, and a serialising regression (a connection
    pool that queues them, a `gather` turned into a loop) would double the wall clock while every
    single-endpoint assertion still passed.
    """
    budget = 0.2

    async def _measure() -> tuple[list[ConnectorHealth], float]:
        # Half the budget: every read lands comfortably inside a per-read timeout of `budget`,
        # so httpx's own deadline never fires while the response takes 64 x 0.1 s to complete —
        # which is exactly the case the old bound could not see. An interval *longer* than the
        # per-read timeout is caught by either form and would prove nothing.
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
    """The half of the defect that was not about latency: the verdict was wrong too.

    The old form waited out the whole trickle and then read a 200, so a connector nobody could get
    an answer from inside a turn was published as `healthy` — counted healthy by the gauge, cleared
    by `connectors_required`, and readmitted by the breaker. Asserted separately from the timing
    above because a fix that bounded the wait and then reported `unprobed`, or `unknown`, would
    satisfy that test while leaving the gauge as wrong as it was.
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
    """The cost of sharing one budget across the connect and the RPC, paid where it is worst.

    Sharing is right on the hot path — `/readyz` every 10 s per pod, off a cached client. The
    *first* check after process start has no cached client: it parses PEM files and does an mTLS
    handshake, and whatever that costs comes out of the same budget the `DescribeTaskQueue` needs
    to answer in. Run out of it and the sweep reports `unknown`, which neither counts in the gauge
    nor trips the gate — so a worker fleet at zero replicas clears `connectors_required`, the one
    posture that exists to refuse it, and the verdict is final for that boot.

    Both directions in one test, against one broker: at the poll's budget the cold connect leaves
    nothing and the answer is `unknown`; at the startup budget the same broker answers and the same
    empty poller list is `unpolled` — and the gate refuses.
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
    """The two numbers are only worth having apart if they are apart, so the defaults are pinned.

    Not an arbitrary ratio: the poll's budget is what a kubelet waits for and the chart derives its
    `timeoutSeconds` from, while the startup budget is paid once and bounded by a startup probe
    already granting 300 s. A deployment that sets them equal has re-created the defect — a cold
    connect charged to the RPC that decides `unpolled` — and this is where that shows up.
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
    """The two questions are additive, and the worse answer is the verdict.

    They used to be an `elif` on the health route, so a bundle with both halves was judged on its
    endpoint alone and its queue was asked about by nobody — `connector-worker-calc` at zero
    replicas behind a live MCP pod read as `healthy`, the gauge stayed at 0, and
    `connectors_required` started a service whose every launched job would sit in a queue until the
    job ceiling expired. The endpoint here answers 200 on a real socket, so the old code has a
    verdict to report and reports the wrong one.
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
    """`_SEVERITY` is a hand-written restatement of `ConnectorState`, and nothing pinned it.

    The fold ranked a state by looking it up in that tuple, so a sixth member added to the `Literal`
    would have raised `ValueError` out of `probe_connectors` — a function whose docstring promises
    it never raises, called by the boot gate (`api/app.py`) and `/readyz`. The consequence of the
    drift was not "one connector reported oddly"; it was the front door refusing to come up, for a
    change to an unrelated enum.
    """
    assert set(_SEVERITY) == set(get_args(ConnectorState))


def test_a_state_the_severity_order_has_not_been_taught_still_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime half of the same guard: ranking is total, so the promise holds through a drift.

    The test above catches the drift in CI; this one says what happens in the window before someone
    runs CI. The rank map is emptied of everything but `unknown` — exactly the shape of a `Literal`
    that has gained members this ordering has not — and the fold must still produce its one row
    rather than take the process's readiness route with it.
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


#: How `/mcp` is broken, and the words the WARNING must carry for it. Two shapes, because they
#: reach the log line by different routes and only one of them has a status code to report: a `500`
#: raises `httpx.HTTPStatusError` inside the handshake's `TaskGroup`, while a `200` carrying
#: `text/html` — an ingress error page, which is what a real cluster serves — leaves the MCP client
#: waiting for a stream that never becomes one, so the open times out. Both used to render as
#: `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)`, and the timeout then
#: rendered as `TimeoutError: ` — the type with the reason missing.
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
    """The sweep calls it healthy; the *turn* is what names it — so the turn's series must carry it.

    **Driven against a real listener on a real socket, because the gap is between two probes and no
    stub of either one has it.** Measured on 2026-09-19 against exactly this handler:
    `/readyz` answered `{"status":"ready","connectors_unhealthy":0}`, the startup line said
    `molfp=healthy`, and `chemclaw_connectors_unhealthy` held **0** while every MCP call failed —
    so `ChemclawConnectorsUnhealthy` (`max(chemclaw_connectors_unhealthy) > 0`) could not fire on a
    connector that was contributing nothing.

    The sweep's verdict is asserted rather than fixed, and that is the decision this test records:
    `GET /healthz` stays the whole probe. Measured against the four connector apps this repository
    serves, on loopback with no TLS, `GET /healthz` costs 3.6–4.2 ms and a full MCP
    handshake + `tools/list` + teardown costs 50–71 ms — **12–19x** — on a route the kubelet runs
    every 10 s with a 5 s timeout derived from a 2 s per-endpoint budget. And it would buy *less*
    speed, not more: `ChemclawConnectorsDegradingTurns` fires at `for: 0m` on the first degraded
    turn, where a sweep-based gauge sits behind `for: 10m`. What a `tools/list` probe would buy is
    detection with **no traffic**, which is a real gap and is a `docs/planning/DEFERRED.md` row with
    its own trigger rather than a change made here.

    So what had to change is the *turn's* report, and the three things asserted here are the three
    that were missing:

    - the connector is in `unreachable`, so the turn degrades and says so (this already held);
    - `chemclaw_connectors_unreachable_total` carries `connector`, which it did not — it was one
      bulk increment of an unlabelled series, so neither the new alert nor a dashboard could name
      which connector had gone dark while its sibling gauge `chemclaw_connector_unhealthy` could;
    - the WARNING names the leaf. It printed the enclosing `ExceptionGroup` —
      `unhandled errors in a TaskGroup (1 sub-exception)` — which reads as a network fault whatever
      happened, while `/healthz` said the pod was fine. The status code was inside the group all
      along; see `_BROKEN_MCP` for why the `garbage` arm has no status code to name and what it
      names instead.
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
        "was deliberate, the decision recorded in this docstring and in DEFERRED.md changed, and "
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
    """The other leaf this path reaches, and the one where nothing about the pod is wrong.

    A manifest declaring `auth: {mode: bearer, token_env: …}` whose variable is unset fails the
    open before a byte is sent. Driven on 2026-09-19 it produced the *identical* line to the
    broken-`/mcp` case — `connector … is unreachable (ExceptionGroup: unhandled errors in a
    TaskGroup (1 sub-exception))` — so two faults with opposite remedies were one sentence, and
    the sweep called the pod healthy in both.

    Asserted separately from the test above rather than as a third parametrization, because it is
    the part of the boundary a fix that special-cased `httpx` would have left behind: the leaf here
    is first-party (`connectors/identity.MissingConnectorCredential`) and reaches the same line
    through the same group.
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
