"""How much durable work is open right now — asked of the broker, never of a workflow body.

A workflow execution is not "in" a process (between tasks it lives in the broker, and
termination, eviction or shutdown skip its `finally`), so the count comes from a broker
visibility query over open `ConnectorJobWorkflow` executions.

The reading is cached and refreshed on a timer by `serve_worker`, since a scrape must not make a
network call. It is fleet-wide: every worker publishes the same count, so dashboards take
`max()`, not `sum()`. The refresh loop also feeds `broker_seen_recently`, the readiness signal.
"""

import asyncio
import logging
import time

from temporalio.client import Client

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# Open executions of the connector-job wrapper. A literal so this module avoids the workflow
# package; `tests/test_durable_observability.py` pins it against the class.
_OPEN_JOBS_QUERY = "WorkflowType = 'ConnectorJobWorkflow' AND ExecutionStatus = 'Running'"

# The last count the broker gave, published as the gauge. Starts at zero until the first refresh.
_OPEN_JOBS = 0.0

# When the broker last answered this process, on `time.monotonic()` so a clock step cannot flip
# readiness. Zero means never: not-ready until the first refresh returns.
_LAST_BROKER_OK = 0.0

# How many missed refreshes make an outage rather than a blip. Derived from
# `jobs_in_flight_refresh_seconds` rather than a second setting.
_BROKER_STALE_INTERVALS = 3


async def refresh_open_jobs(client: Client) -> None:
    """Re-read the broker's count of open connector jobs into the gauge's reading.

    The count is the broker's visibility store, which trails an execution's own state by a moment,
    so a job that just closed may still be counted on the next refresh and not on the one after.
    Never raises: a failed query is counted (`chemclaw_degraded_total`) and the previous reading
    stands, so the drain and probe surface keep running.
    """
    global _OPEN_JOBS, _LAST_BROKER_OK
    try:
        count = await client.count_workflows(_OPEN_JOBS_QUERY)
    except Exception:
        degraded(
            logger,
            "jobs_in_flight",
            "could not count open durable jobs; the gauge still reads %.0f",
            _OPEN_JOBS,
        )
        return
    _OPEN_JOBS = float(count.count)
    _LAST_BROKER_OK = time.monotonic()


def broker_seen_recently() -> bool:
    """Whether this worker has had an answer from the broker inside the staleness window.

    Synchronous and allocation-free: it runs on the worker's event loop for every kubelet probe.
    """
    if not _LAST_BROKER_OK:
        return False
    window = settings.jobs_in_flight_refresh_seconds * _BROKER_STALE_INTERVALS
    return (time.monotonic() - _LAST_BROKER_OK) < window


async def poll_open_jobs(client: Client, stop: asyncio.Event) -> None:
    """Refresh the reading every `jobs_in_flight_refresh_seconds` until `stop` is set.

    A timer, so the gauge moves even while long jobs run and none completes.
    """
    await refresh_open_jobs(client)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), settings.jobs_in_flight_refresh_seconds)
        except TimeoutError:
            await refresh_open_jobs(client)


def jobs_in_flight() -> float:
    """The last count of open durable jobs this worker read from the broker."""
    return _OPEN_JOBS


def bind_job_gauges() -> None:
    """Publish the open-jobs reading on this process's `/metrics`.

    Called from `durable/serve.py`, the tail of every worker's `main()`, so no worker can skip it.
    """
    METRICS.bind_gauge("chemclaw_jobs_in_flight", jobs_in_flight)
