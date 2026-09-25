"""Settle a durable wait whose own run can no longer settle it.

`pending_requests` is a projection of `AwaitAnswerWorkflow`, written by the run's own activities,
and the run is what moves a row out of `waiting`. Two things end a run without that happening
(`D-2026-09-25-a-wait-nobody-can-settle-is-settled-by-a-sweep`):

- **a terminate**, which never resumes workflow code — `ParentClosePolicy.TERMINATE` on a parent
  that died (`tests/test_awaiting.py::test_a_wait_started_as_a_child_settles_when_its_parent_dies`
  pins that it reaches no `except`), or an operator terminating the wait directly;
- **a run that failed or timed out**, including a settle cancelled after the run had already
  closed — the race that test documents and deliberately does not assert on.

Such a row stays in every entitled person's inbox and cannot be answered:
`POST /pending/{id}/answer` reads `waiting`, signals a run that is gone and answers 503. It is
also in `retention._NOT_PRUNED` on purpose, so nothing else would ever collect it.

**The broker decides, not the clock.** A row is settled only when Temporal says the run that owns it
is not running — or has no record of it. `due_at` is not the test: a running wait past its deadline
is the run's own business (its timer settles it `expired`, with the notice that goes with that), and
a sweep that acted on the deadline would race the one party that knows. A lost *worker* is not an
orphan either: the run is still `RUNNING` and resumes on the next worker, so it is left alone.

**Settled `cancelled`, never deleted**, and only for the run the sweep examined
(`pending_store.settle_orphan`) — a reopen under a new run is that run's question, not this one's.
"""

import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    from chemclaw.core.config import settings
    from chemclaw.core.temporal_client import connect
    from chemclaw.durable import pending_store
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)


class OrphanSweep(BaseModel):
    """What one sweep did: how many rows it asked about, and which it settled."""

    examined: int = 0
    settled: list[str] = []


async def _run_is_gone(client: Any, request_id: str, run_id: str) -> str | None:
    """Why the run owning this row can no longer settle it, or `None` if it still can.

    The request id *is* the wait's workflow id (`awaiting.request_id_for`). A row with no `run_id`
    (written before the column, or by a caller with no run to name) is asked about the latest run.
    """
    handle = client.get_workflow_handle(request_id, run_id=run_id or None)
    try:
        described = await handle.describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return "the wait's run is no longer known to the broker"
        # A run id the broker refuses as malformed names no run that could ever settle the row, and
        # raising here would stop every later row behind this one on every pass, for good.
        if exc.status == RPCStatusCode.INVALID_ARGUMENT:
            return "the row names a run id the broker does not accept"
        raise
    if described.status == WorkflowExecutionStatus.RUNNING:
        return None
    status = described.status.name.lower() if described.status else "closed"
    return f"the wait's run ended ({status}) without settling this request"


@durable_activity("background")
@activity.defn
async def settle_orphaned_waits() -> OrphanSweep:
    """Settle every examined `waiting` row whose run the broker says is gone.

    A broker error on one row stops the sweep rather than skipping the row: an unreachable broker
    reads the same as "not found" to a loop that swallowed it, and settling a live question because
    Temporal was briefly down is the one outcome this must never produce.
    """
    rows = await pending_store.waiting_rows(
        older_than_seconds=settings.awaiting_orphan_grace_seconds,
        limit=settings.awaiting_orphan_batch,
    )
    sweep = OrphanSweep(examined=len(rows))
    if not rows:
        return sweep
    client = await connect()
    for request_id, run_id in rows:
        reason = await _run_is_gone(client, request_id, run_id)
        if reason is not None and await pending_store.settle_orphan(request_id, run_id, reason):
            logger.warning("awaiting.orphan_settled: request %s cancelled — %s", request_id, reason)
            sweep.settled.append(request_id)
    return sweep


# Schedule-only, and a bug here should park rather than fail: nothing reads the run, the sweep is
# idempotent, and a parked run finishes once a fix ships — the stance `ArtifactEvictionWorkflow`
# and `RetentionWorkflow` hold for the same reasons.
@durable_workflow("background")
@workflow.defn
class OrphanedWaitsWorkflow:
    """Settle the durable waits whose runs ended without settling them, on a cadence."""

    @workflow.run
    async def run(self) -> OrphanSweep:
        """Run one sweep and return what it settled."""
        return await workflow.execute_activity(
            settle_orphaned_waits,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
