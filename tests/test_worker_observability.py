"""Every worker serves metrics and health probes over `core/worker_http.py`.

The metrics registry exists in every process that imports `core/metrics.py`, so workers record
counters that only an HTTP surface makes scrapeable. The same surface serves the probes, so a
worker whose poll loop or broker connection has died stops reporting healthy.
"""

import asyncio
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import cast

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient
from temporalio.worker import Worker

from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.worker_http import _build_app, _QuietServer, worker_http
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
    """A worker serves the counters its own process recorded.

    `record_metric` resolves the same process-wide registry this route renders.
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
    """`/healthz` is answered on the worker's own event loop.

    A loop wedged inside an activity stops answering, so the kubelet restarts the pod. The component
    is echoed so a probe response identifies which pod answered it.
    """
    body = _client().get("/healthz").json()
    assert body == {"status": "ok", "component": "test-worker"}


def test_the_surface_is_really_bound_while_the_worker_runs(metrics_port: int) -> None:
    """The context manager half: bound and answering before the body runs, gone after it.

    Asserted over a real socket, because a `yield` that fires before the port accepts makes the
    first probe a refused connection, read as a dead pod during every rollout.
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

    `connector_app` mounts the MCP transport at `/`, so every route must be declared before the
    mount or the transport answers it with a protocol error.
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
    """Every Temporal worker entrypoint goes through the one runtime.

    The bundle worker runs the expensive science, so a fix reaching only `background_worker` would
    leave it invisible and un-drainable. Probe surface, pool and graceful shutdown are one call so a
    new worker cannot wire two of three. Asserted on source, because a real worker needs a broker.
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
    """The default activity ceiling must not exceed the default pool width.

    At or below the pool width no activity waits for a connection; above it a shortage becomes retry
    churn. A deployment may raise the ceiling deliberately (the `calc` bundle does); the shipped
    defaults must not drift apart by accident.
    """
    from chemclaw.core.config import settings

    assert settings.worker_max_concurrent_activities <= settings.pg_pool_max_size, (
        f"a worker may run {settings.worker_max_concurrent_activities} activities against a pool "
        f"of {settings.pg_pool_max_size}"
    )


# No "the surface leaks no identity" test here: these routes are unauthenticated, and
# `test_metrics_carry_no_identifiers_or_turn_content` already allowlists the declared label names
# of the one registry they render. A weaker duplicate of a security check is worse than none.


def test_a_worker_whose_broker_has_gone_quiet_reports_not_ready() -> None:
    """`is_running` is a lifecycle flag, so readiness has to name the broker as well.

    A worker on a severed connection must answer `/readyz` not-ready, so it leaves the Service and a
    rollout does not count it Available. The lifecycle flag is held True while the broker goes
    quiet, and the assertion is the status code a kubelet reads, driven through `worker_ready` and
    the route.
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


async def test_the_bind_flag_wait_is_cancelled_when_the_bind_does_not_win_the_race(
    monkeypatch: pytest.MonkeyPatch, metrics_port: int
) -> None:
    """The surface races two awaitables and must not walk away from the loser.

    When the bind fails, `serving` wins and the task waiting on `bound` must be cancelled, not left
    pending forever. Uvicorn's own failed bind exits the process, which hides the leak, so this
    injects a `startup` that declines and returns, keeping the loop running so the leak is
    observable.
    """
    servers: list[_QuietServer] = []
    original_init = _QuietServer.__init__

    def _remember(self: _QuietServer, config: uvicorn.Config) -> None:
        """Hold the real server, so the event keeping the orphan alive cannot be collected."""
        original_init(self, config)
        servers.append(self)

    async def _declined(self: _QuietServer, sockets: list[object] | None = None) -> None:
        """Uvicorn refusing to start without raising: `serve()` returns, `bound` unset."""
        self.should_exit = True

    monkeypatch.setattr(_QuietServer, "__init__", _remember)
    monkeypatch.setattr(_QuietServer, "startup", _declined)

    async with worker_http(component="declined", ready=lambda: True):
        assert servers, "the surface did not build a server"
        # One loop turn, because `Task.cancel()` schedules the throw; `Event.wait`'s `finally`
        # removes the waiter when the task next runs. Without the cancel no number of turns removes
        # it.
        await asyncio.sleep(0)
        assert not servers[0].bound._waiters, (
            "the bind-flag wait is still queued on an event nothing will set"
        )
