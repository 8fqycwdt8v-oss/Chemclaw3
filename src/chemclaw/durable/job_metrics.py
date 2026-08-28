"""How much durable work is open right now — asked of the broker, never of a workflow body.

**What this replaced, and the three measurements that retired it.** This module used to keep a
process-local `set` of the `ConnectorJobWorkflow` ids whose `run()` this process was "currently
inside", added at the top of the workflow body and discarded in its `finally`. Its own docstring
claimed neither call needed an `is_replaying` guard "because this is a statement about the
present". Driven against a live broker on 2026-08-28, the set was wrong in three directions and
raised in the fourth:

- **terminate** — `chemclaw_jobs_in_flight` read `1.0` for the life of the process. A termination
  never resumes workflow code, so the `finally` never ran.
- **eviction** (`max_cached_workflows=0`) — it read `0.0` while the workflow was still `RUNNING`,
  which is exactly the reading it must not give for the long idle-between-tasks parent workflows
  it existed to count.
- **worker shutdown**, the drain path `durable/serve.py` was written for — the id was still
  present after `worker.shutdown()` returned, *and* the `finally` raised
  `_NotInWorkflowEventLoopError: Not in workflow event loop` out of
  `job_ended(workflow.info().workflow_id)`, because a workflow being torn down is no longer in its
  own event loop.

None of that is a bug in the bracketing; it is the quantity being unmeasurable from inside a
workflow. A workflow execution is not "in" a process — between tasks it is in the broker, and
which worker picks up the next task is not this process's business. So the question is put to the
one component that can answer it: **the broker**, through a visibility count of open
`ConnectorJobWorkflow` executions.

**A cached reading refreshed on a timer, not a query per scrape** — the shape
`publish/outbox.py::bind_backlog_gauges` already uses, and for the same two reasons: a gauge source
is synchronous and a scrape must not make a network call. `serve_worker` drives the refresh, which
is the one tail every worker's `main()` runs through, so no entrypoint can wire the drain and
forget the gauge.

**The reading is fleet-wide, not per-pod, and that is a change in what the number means.** Every
worker publishes the same count, so a dashboard takes `max()` over the series rather than `sum()`.
That is the honest form: durable work in flight is a property of the deployment, and the per-pod
number the old set claimed to give never existed.
"""

import asyncio
import logging

from temporalio.client import Client

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# Open executions of the one wrapper every connector job runs inside. The type name is written as
# a literal rather than imported so this module stays free of the workflow package (and of its
# sandbox import block); `tests/test_durable_observability.py` pins it against the class.
_OPEN_JOBS_QUERY = "WorkflowType = 'ConnectorJobWorkflow' AND ExecutionStatus = 'Running'"

# The last count the broker gave, published as the gauge. Starts at zero rather than at "unknown",
# for the reason `publish/outbox.py` starts its families empty: a pod that has not refreshed yet
# is a pod carrying nothing anybody has told it about, and the first refresh is one interval away.
_OPEN_JOBS = 0.0


async def refresh_open_jobs(client: Client) -> None:
    """Re-read the broker's count of open connector jobs into the gauge's reading.

    Never raises. A visibility query that fails is a fact about the broker, not about the worker
    serving jobs, and failing the refresh loop would take the drain and the probe surface with it —
    so the failure is counted (`chemclaw_degraded_total`) and the previous reading stands, which is
    the same trade `notify_session_best_effort` and the outbox gauges make.
    """
    global _OPEN_JOBS
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


async def poll_open_jobs(client: Client, stop: asyncio.Event) -> None:
    """Refresh the reading every `jobs_in_flight_refresh_seconds` until `stop` is set.

    A timer rather than a query per scrape, and a timer rather than a refresh at each job's end:
    a gauge that only moves when a job *finishes* stands still for exactly the deployment that has
    eight long jobs running and nothing completing — the state it exists to show.
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

    Called from `durable/serve.py`, which is the tail of every worker's `main()` and therefore the
    one place a third worker cannot wire two of three cross-cutting concerns and look healthy — the
    same reason the pool, the probe surface and the drain are wired there rather than per
    entrypoint.
    """
    METRICS.bind_gauge("chemclaw_jobs_in_flight", jobs_in_flight)
