"""How many durable jobs this worker is carrying right now, and who is allowed to say so.

`chemclaw_jobs_started_total` counted launches and nothing counted endings, so "durable work in
flight" was a subtraction that could not be done. The completion counter now exists
(`chemclaw_jobs_finished_total`, booked in `durable/job_record.py::record_job`), and it is booked
in a *different process* from the launcher — a job is started by the front door and run by a
worker — so the subtraction still would not answer for either pod. This is the direct reading
instead: the set of `ConnectorJobWorkflow` executions whose `run()` this process is currently
inside.

**A set of workflow ids rather than a counter, and that is what makes it replay-safe.** A workflow
body is replayed from history after a worker restart or a cache eviction, so an `increment` at the
top would count one job many times. Adding an id that is already present is a no-op, and the id is
the run's own workflow id, so a replay of a job this worker is already carrying changes nothing
while a replay on a *fresh* worker correctly re-adds it. Neither call needs an `is_replaying`
guard, because neither is an accumulation — this is a statement about the present, and under replay
the present is genuinely "this workflow is executing here".

Imported inside `connector_job.py`'s `imports_passed_through()` block for the reason
`core.metrics_bridge` is: the workflow sandbox re-imports a module it is not told to pass through,
and a sandbox copy of `_IN_FLIGHT` is a set the gauge would never read.
"""

import logging

from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

# The workflow ids of the durable jobs this process is currently running. A `set`, so the same run
# re-entered by a replay occupies one entry.
_IN_FLIGHT: set[str] = set()


def job_running(job_id: str) -> None:
    """Note that this process is now carrying durable job `job_id`."""
    _IN_FLIGHT.add(job_id)


def job_ended(job_id: str) -> None:
    """Note that `job_id` has left this process, however it ended."""
    _IN_FLIGHT.discard(job_id)


def jobs_in_flight() -> float:
    """How many durable jobs this process is carrying — the gauge's reading."""
    return float(len(_IN_FLIGHT))


def bind_job_gauges() -> None:
    """Publish the in-flight reading on this process's `/metrics`.

    Called from `durable/serve.py`, which is the tail of every worker's `main()` and therefore the
    one place a third worker cannot wire two of three cross-cutting concerns and look healthy — the
    same reason the pool, the probe surface and the drain are wired there rather than per
    entrypoint.
    """
    METRICS.bind_gauge("chemclaw_jobs_in_flight", jobs_in_flight)
