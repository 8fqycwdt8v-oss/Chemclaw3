"""A worker that nothing could scrape and nothing could probe.

Two findings that turned out to be one missing thing.

**Metrics went nowhere.** The chart scraped the front door alone, on the reasoning — written into
`metrics_bridge.py`'s docstring, the ServiceMonitor's comment and a chart test's assertion — that
recording a metric outside the front door "is a no-op: there is no registry and no HTTP surface in
those processes". The first half was never true. `core/metrics.py` is a stdlib-only module
singleton, so the registry exists in every process that imports it, and the background worker and
six connector workers have been incrementing a live registry that nothing could read.

**Probes did not exist.** "Liveness is the Temporal poll itself" was asserted in three chart
templates and enforced nowhere: a worker whose poll loop died kept its process open, so Kubernetes
reported `Running` and — per the paragraph above — no counter contradicted it either.

Both were waiting on the same missing HTTP surface, which is why `core/worker_http.py` is one
module and these are one test file.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient
from temporalio.worker import Worker

from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.worker_http import _build_app, worker_http
from tests.conftest import _free_port


def _client(ready: Callable[[], bool] = lambda: True) -> TestClient:
    """A client over the probe surface, without binding a port."""
    return TestClient(_build_app("test-worker", ready))


@pytest.fixture
def metrics_port(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Point `worker_http` at a free loopback port for the duration of a test."""
    port = _free_port()
    monkeypatch.setattr("chemclaw.core.config.settings.worker_metrics_host", "127.0.0.1")
    monkeypatch.setattr("chemclaw.core.config.settings.worker_metrics_port", port)
    yield port


def test_a_worker_serves_the_metrics_its_own_process_recorded() -> None:
    """The finding itself: a counter incremented off the front door had no reader.

    `record_metric` is the path every worker, activity and connector tool uses, and it resolves the
    same process-wide registry this route renders — which is precisely why the counters were never
    missing, only unreachable.
    """
    before = _client().get("/metrics")
    record_metric(lambda m: m.increment("chemclaw_jobs_started_total"))
    after = _client().get("/metrics")

    assert after.status_code == 200
    assert after.headers["content-type"].startswith("text/plain")
    assert "chemclaw_jobs_started_total" in after.text
    assert _total(after.text) == _total(before.text) + 1


def _total(exposition: str) -> float:
    """The `chemclaw_jobs_started_total` sample out of a rendered exposition."""
    for line in exposition.splitlines():
        if line.startswith("chemclaw_jobs_started_total "):
            return float(line.split()[-1])
    raise AssertionError("the counter is absent from the exposition")


def test_a_worker_that_has_stopped_polling_reports_not_ready() -> None:
    """Readiness is the worker's own state, not the fact that a process exists.

    The status *code* carries it, not the body: a probe reads the code and nothing else, so a 200
    carrying `"status": "not-ready"` is a pod reporting itself ready.
    """
    running = _client(lambda: True).get("/readyz")
    stopped = _client(lambda: False).get("/readyz")

    assert running.status_code == 200
    assert stopped.status_code == 503
    assert stopped.json()["status"] == "not-ready"


def test_liveness_answers_on_the_workers_own_event_loop() -> None:
    """`/healthz` is a stronger claim than "the process is up", which is what it replaced.

    It is served by the worker's own loop, so a loop wedged inside an activity stops answering and
    the kubelet restarts the pod — the failure the old comment ("liveness is the Temporal poll
    itself") named and no probe could see. The component is echoed so a probe response identifies
    which pod answered it.
    """
    body = _client().get("/healthz").json()
    assert body == {"status": "ok", "component": "test-worker"}


def test_the_surface_is_really_bound_while_the_worker_runs(metrics_port: int) -> None:
    """The context manager half: bound and answering before the body runs, gone after it.

    Asserted over a real socket rather than the ASGI app, because the thing that can break here is
    the binding — a `yield` that fires before the port accepts makes the first probe a connection
    refused, which a kubelet reads as a dead pod during every rollout.
    """
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{metrics_port}/healthz"

    async def _exercise() -> int:
        async with worker_http(component="bound", ready=lambda: True):
            return await asyncio.to_thread(
                lambda: int(urllib.request.urlopen(url, timeout=5).status)
            )

    assert asyncio.run(_exercise()) == 200

    # And the port is released, so a restarted worker in the same pod can bind it again.
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url, timeout=5)


def test_the_surface_can_be_switched_off_without_failing_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port 0 is for two workers on one developer machine, where the second cannot bind.

    A worker must still run — the escape hatch is about the observability surface, and turning it
    off to run a second worker locally must not be a way to stop the worker itself.
    """
    monkeypatch.setattr("chemclaw.core.config.settings.worker_metrics_port", 0)
    ran = False

    async def _exercise() -> None:
        nonlocal ran
        async with worker_http(component="off", ready=lambda: True):
            ran = True

    asyncio.run(_exercise())
    assert ran


def test_a_connector_serves_metrics_and_still_serves_mcp() -> None:
    """The connector half, and the ordering it depends on.

    `connector_app` mounts the MCP transport at `/`, so every route it declares has to be declared
    *before* the mount or fall through to a transport that answers it with a protocol error. That
    ordering has one comment and now two routes relying on it.
    """
    from chemclaw.connectors.server import connector_app

    app = connector_app(FastMCP("probe"), name="probe")
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/healthz", "/metrics"} <= paths

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "connector": "probe"}
        scrape = client.get("/metrics")
    assert scrape.status_code == 200
    assert "chemclaw_jobs_started_total" in scrape.text


def test_both_temporal_workers_go_through_the_one_runtime() -> None:
    """Every worker entrypoint, not just the one that was easy to reach.

    Core runs one worker and each bundle runs its own, and the second is the process doing the
    expensive science — so a fix that reached only `background_worker` would leave the most
    interesting fleet exactly as invisible, and exactly as un-drainable, as it was.

    The probe surface, the Postgres pool and the graceful shutdown are one call rather than three
    lines precisely because of this: a third worker wiring two of the three would be a pod that
    looks healthy and does the wrong thing on termination. Asserted on the source because starting
    a real Temporal worker needs a broker; what can go wrong offline is one forgotten entrypoint.
    """
    import inspect

    from chemclaw.connectors import worker as bundle_worker
    from chemclaw.durable import background_worker

    for module in (background_worker, bundle_worker):
        source = inspect.getsource(module)
        assert "serve_worker(" in source, (
            f"{module.__name__} runs its worker directly, so it is unobservable on the way up and "
            "SIGKILLed mid-activity on the way down"
        )
        assert "graceful_shutdown_timeout=" in source, (
            f"{module.__name__} builds a worker that cancels in-flight activities the instant it "
            "is asked to stop, which is a hard kill with extra steps"
        )
        assert "max_concurrent_activities=settings.worker_max_concurrent_activities" in source, (
            f"{module.__name__} builds a worker with temporalio's default of 100 concurrent "
            "activities, against a Postgres pool an order of magnitude smaller — the shortfall is "
            "not a crash but retry churn, since each starved activity spends one of "
            "activity_max_attempts on a ConnectionError before computing anything"
        )


def test_a_worker_may_not_admit_more_activities_than_its_pool_can_serve() -> None:
    """The default has to hold the invariant its own comment states, not merely be a number.

    An activity borrows a connection for a fraction of its runtime, so a ceiling *at* the pool
    width already leaves the pool mostly idle — equal is the point at which no activity can be the
    one that waits, and above it is where a shortage becomes retry churn. A deployment may still
    raise the ceiling deliberately (the `calc` bundle does, because a CREST search holds a slot
    rather than database work); what must not happen is the shipped default drifting above the
    shipped pool by accident.
    """
    from chemclaw.core.config import settings

    assert settings.worker_max_concurrent_activities <= settings.pg_pool_max_size, (
        f"a worker may run {settings.worker_max_concurrent_activities} activities against a pool "
        f"of {settings.pg_pool_max_size}"
    )


# There is deliberately no "the surface leaks no identity" test here. These routes are
# unauthenticated, so that rule matters — and it is already enforced where it belongs:
# `test_metrics_carry_no_identifiers_or_turn_content` is an allowlist over the *declared label
# names* of the one registry all three surfaces render, so it covers this exposition byte for byte.
# A second scan here could only be a weaker restatement of it (a substring sweep flags the word
# "session" inside a HELP string), and a weaker duplicate of a security check is worse than none:
# it is the copy people would trust.


def test_a_worker_whose_broker_has_gone_quiet_reports_not_ready() -> None:
    """`is_running` is a lifecycle flag, so readiness has to name the broker as well.

    Measured before this existed: a worker on a severed connection answered `/readyz` 200
    `{"status":"ready"}` for as long as it was left running, while the SDK core logged
    `poll_workflow_task_queue retried 8 times ... ConnectionRefused` — the pod stayed in the
    Service, a rollout in that window reported complete, and the PodDisruptionBudget counted it
    Available.

    **Driven through `worker_ready` and through the route, which this test used to only claim.**
    It called `broker_seen_recently()` directly — the *ingredient*, never the predicate — while its
    docstring said it would fail "if either half is dropped". Measured, it does not: with the
    freshness half removed the whole worker suite stayed green (75 passed, unchanged) and a severed
    worker answered `/readyz` 200 again, which is the regression the predicate exists to stop. So
    the lifecycle flag is held True here while the broker goes quiet, and the assertion is the
    status code a kubelet reads: a test that substitutes its own copy of the thing under test
    proves nothing about the thing under test.
    """
    from chemclaw.core.config import settings
    from chemclaw.durable import job_metrics
    from chemclaw.durable.serve import worker_ready

    # Stands in for the `Worker` only in the attribute the predicate reads, pinned True throughout:
    # what is under test is whether the *other* half can be reached at all.
    running_worker = cast(Worker, SimpleNamespace(is_running=True))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "jobs_in_flight_refresh_seconds", 10)
        patch.setattr(job_metrics, "_LAST_BROKER_OK", 0.0)
        assert not worker_ready(running_worker), (
            "a running worker that has never heard from the broker reports itself ready"
        )
        assert _client(lambda: worker_ready(running_worker)).get("/readyz").status_code == 503, (
            "the route answered ready for a worker whose every poll is failing — the pod stays in "
            "the Service and a rollout in that window reports complete"
        )
        patch.setattr(job_metrics, "_LAST_BROKER_OK", time.monotonic())
        assert worker_ready(running_worker)
        assert _client(lambda: worker_ready(running_worker)).get("/readyz").status_code == 200
        # Three missed refreshes at the configured interval.
        patch.setattr(job_metrics, "_LAST_BROKER_OK", time.monotonic() - 31)
        assert not worker_ready(running_worker), (
            "a worker whose last broker answer is three refresh intervals old still reports ready"
        )
        # And the lifecycle half still decides on its own, so neither is redundant.
        patch.setattr(job_metrics, "_LAST_BROKER_OK", time.monotonic())
        assert not worker_ready(cast(Worker, SimpleNamespace(is_running=False)))
