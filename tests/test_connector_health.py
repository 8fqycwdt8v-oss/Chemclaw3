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
import inspect
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
)
from temporalio.service import RPCError, RPCStatusCode, WorkflowService

from chemclaw.connectors.health import (
    ConnectorHealth,
    ConnectorsUnavailable,
    check_connectors_at_startup,
    probe_connectors,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError


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

    async def describe_task_queue(
        self,
        req: DescribeTaskQueueRequest,
        retry: bool = False,
        metadata: Any = None,
        timeout: timedelta | None = None,
    ) -> DescribeTaskQueueResponse:
        """Answer with the SDK's own response message — the poller list is the whole verdict."""
        self.requests.append(req)
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
    assert "\nchemclaw_connectors_unhealthy 1\n" in exposition, exposition


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
