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
    # **And the workflow's latest run, because a reset keeps the row but not the run.** An
    # operator's reset — the documented remedy for a nondeterminism failure on exactly this
    # workflow — terminates the run the row names and continues the wait in a new run that replays
    # past the activity which wrote `run_id`, so the row still names the dead one while the wait
    # is alive and answerable. Whichever run of this id is running owns the question.
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


#: The share of the activity's `start_to_close` a pass may spend walking pages before it stops.
#: Below one so the pass returns what it settled rather than being cancelled mid-page, which would
#: lose the report and retry the whole walk. Where it stopped is the next pass's starting point
#: (`OrphanSweep.resume_after`), so a table longer than one pass is walked across passes.
_PASS_BUDGET_FRACTION = 0.5


@durable_activity("background")
@activity.defn
async def settle_orphaned_waits(after: pending_store.WaitingRow | None = None) -> OrphanSweep:
    """Settle every examined `waiting` row whose run the broker says is gone.

    **Walks the table page by page from `after`**, `awaiting_orphan_batch` rows at a time, until
    it is exhausted or the pass has spent `_PASS_BUDGET_FRACTION` of its timeout — and always at
    least one page, so a pass makes progress however small the budget. One page per pass with no
    cursor re-examined the same oldest rows every hour: live waits are left `waiting`, so a batch
    of them older than an orphan starved it for as long as they stayed open. Restarting every pass
    at the oldest row only moved that starvation to a longer table, so where a pass stops is
    returned as `resume_after` and the next pass continues from it, wrapping to the start once the
    table is exhausted — within the same pass when `after` already sits past the last row, which a
    pass that ran out of budget exactly on the table's last full page leaves behind.

    Args:
        after: the keyset position the previous pass stopped at; `None` starts at the oldest row.

    A broker error on one row stops the sweep rather than skipping the row: an unreachable broker
    reads the same as "not found" to a loop that swallowed it, and settling a live question because
    Temporal was briefly down is the one outcome this must never produce.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.retention_timeout_seconds * _PASS_BUDGET_FRACTION
    sweep = OrphanSweep()
    client: Any = None
    # A cursor the previous pass left on what turned out to be the table's last full page finds
    # nothing after it. Wrapping here, once, keeps that pass from being an interval that sweeps
    # nothing; once, so an empty table cannot loop.
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


# Schedule-only, and a bug here should park rather than fail: nothing reads the run, the sweep is
# idempotent, and a parked run finishes once a fix ships — the stance `ArtifactEvictionWorkflow`
# and `RetentionWorkflow` hold for the same reasons.
@durable_workflow("background")
@workflow.defn
class OrphanedWaitsWorkflow:
    """Settle the durable waits whose runs ended without settling them, on a cadence."""

    @workflow.run
    async def run(self) -> OrphanSweep:
        """Run one sweep from where the previous run stopped, and return what it settled.

        The cursor rides on the Schedule's last completion result rather than on an input or a
        table: it is read from the run's start event, so it issues no command, and a previous run
        with no `resume_after` (or none at all) starts this one at the oldest row.
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
