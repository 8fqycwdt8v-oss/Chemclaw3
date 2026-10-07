"""Settle a durable wait whose own run can no longer settle it.

`pending_requests` rows leave `waiting` only through their `AwaitAnswerWorkflow` run. A
terminate (never resumes workflow code) or a failed or timed-out run leaves the row `waiting`
forever: it cannot be answered, and retention never prunes it.

The broker decides, not the clock: a row is settled only when Temporal says its run is not
running or has no record of it. A running wait past its deadline is the run's own business, and
a lost worker is not an orphan. Rows are settled `cancelled`, never deleted, and only for the
run the sweep examined (`pending_store.settle_orphan`).
"""

import asyncio
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
    """What one sweep did: how many rows it asked about, which it settled, and where it stopped.

    `resume_after` is the keyset position the next pass starts from, carried between the
    Schedule's runs as the previous run's completion result: `None` when this pass reached the end
    of the table, so the next one wraps around to the oldest rows.
    """

    examined: int = 0
    settled: list[str] = []
    resume_after: pending_store.WaitingRow | None = None


async def _run_is_gone(client: Any, request_id: str, run_id: str) -> str | None:
    """Why the run owning this row can no longer settle it, or `None` if it still can.

    The request id is the wait's workflow id (`awaiting.request_id_for`). A row with no `run_id` is
    asked about the latest run.
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
    # Also check the latest run: an operator's reset continues the wait in a new run while the row
    # still names the old one, and whichever run is running owns the question.
    if run_id and await _latest_is_running(client, request_id):
        return None
    status = described.status.name.lower() if described.status else "closed"
    return f"the wait's run ended ({status}) without settling this request"


async def _latest_is_running(client: Any, request_id: str) -> bool:
    """Whether the newest run under this workflow id is still running."""
    try:
        latest = await client.get_workflow_handle(request_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return bool(latest.status == WorkflowExecutionStatus.RUNNING)


# The share of the activity's `start_to_close` a pass may spend walking pages before it stops, so
# it returns its report rather than being cancelled mid-page. The next pass resumes from
# `OrphanSweep.resume_after`.
_PASS_BUDGET_FRACTION = 0.5


@durable_activity("background")
@activity.defn
async def settle_orphaned_waits(after: pending_store.WaitingRow | None = None) -> OrphanSweep:
    """Settle every examined `waiting` row whose run the broker says is gone.

    Walks the table page by page from `after` (`awaiting_orphan_batch` rows each) until exhausted
    or out of budget, always at least one page. Where it stops is returned as `resume_after`, so
    live waits at the front cannot starve orphans behind them; the walk wraps to the start once the
    table is exhausted, including within a pass whose `after` sits past the last row.

    A broker error stops the sweep rather than skipping the row: settling a live question because
    Temporal was briefly unreachable must never happen.

    Args:
        after: the keyset position the previous pass stopped at; `None` starts at the oldest row.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.retention_timeout_seconds * _PASS_BUDGET_FRACTION
    sweep = OrphanSweep()
    client: Any = None
    # A cursor left on the table's last full page finds nothing after it; wrap once (not repeatedly,
    # so an empty table cannot loop).
    may_wrap = after is not None
    while True:
        rows = await pending_store.waiting_rows(
            older_than_seconds=settings.awaiting_orphan_grace_seconds,
            limit=settings.awaiting_orphan_batch,
            after=after,
        )
        if not rows and may_wrap:
            may_wrap, after = False, None
            continue
        may_wrap = False
        if not rows:
            break
        client = client or await connect()
        for row in rows:
            sweep.examined += 1
            reason = await _run_is_gone(client, row.request_id, row.run_id)
            if reason is not None and await pending_store.settle_orphan(
                row.request_id, row.run_id, reason
            ):
                logger.warning(
                    "awaiting.orphan_settled: request %s cancelled — %s", row.request_id, reason
                )
                sweep.settled.append(row.request_id)
        if len(rows) < settings.awaiting_orphan_batch:
            break
        after = rows[-1]
        if loop.time() >= deadline:
            sweep.resume_after = after
            break
    return sweep


# Schedule-only and idempotent, and nothing reads the run, so a bug parks rather than fails.
@durable_workflow("background")
@workflow.defn
class OrphanedWaitsWorkflow:
    """Settle the durable waits whose runs ended without settling them, on a cadence."""

    @workflow.run
    async def run(self) -> OrphanSweep:
        """Run one sweep from where the previous run stopped, and return what it settled.

        The cursor comes from the Schedule's last completion result, read from the start event, so
        it
        issues no command; with none, the sweep starts at the oldest row.
        """
        previous = (
            workflow.get_last_completion_result(OrphanSweep)
            if workflow.has_last_completion_result()
            else None
        )
        return await workflow.execute_activity(
            settle_orphaned_waits,
            previous.resume_after if previous else None,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
