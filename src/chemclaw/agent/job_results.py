"""Wait, within one turn, for durable jobs that turn started.

Lets "compute this, then reason about the result" happen in one exchange. The wait is bounded and
opt-in: holding a turn open holds an admission permit and a turn slot, so the bound must be shorter
than the front door's whole-turn deadline. A job that does not finish in time is not an error; the
push-back path still delivers it on the next turn.

It waits on the jobs' Temporal workflow handles, not on the session mailbox: claiming mailbox rows
is destructive and would consume completions belonging to other consumers. The handle also carries
the result itself rather than a one-line summary (D-153).
"""

import asyncio
import logging
from typing import Any

from temporalio.client import WorkflowFailureError

from chemclaw.agent.durable_tools import completed_job_status
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.temporal_client import connect
from chemclaw.durable.connector_job import failure_reason

logger = logging.getLogger(__name__)


async def await_job_results(
    session_id: str,
    job_ids: list[str],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    """Wait up to `timeout_seconds` for `job_ids` to complete; return the results that arrived.

    Returns a partial map on timeout rather than raising; jobs that did not arrive are delivered on
    the next turn. Jobs are awaited concurrently so the bound is the longest job, not the sum.
    `session_id` is used for logging only.

    Returns:
        `{job_id: {"job_id": …, "status": …, "summary": …, "result": …}}` for jobs that finished in
        time. A failed job is reported with its status rather than omitted, so the model does not
        narrate a success that did not happen.
    """
    collected: dict[str, dict[str, Any]] = {}

    async def _collect(job_id: str) -> None:
        handle = (await connect()).get_workflow_handle(job_id)
        try:
            collected[job_id] = completed_job_status(job_id, await handle.result()).model_dump()
        except WorkflowFailureError as exc:
            # A failed job is a result, not an absence: report it so the turn resumes with the
            # failure.
            logger.info("mid-turn resume: job %s failed; reporting it in this turn", job_id)
            collected[job_id] = {
                "job_id": job_id,
                "status": "failed",
                # `exc.__cause__ or exc`: the client-side `WorkflowFailureError` wraps the product's
                # own sentence; the wrapper alone reads "Workflow execution failed".
                "summary": failure_reason(exc.__cause__ or exc),
            }

    try:
        outcomes = await asyncio.wait_for(
            # `return_exceptions` so one failed job does not cancel the others' waits; this path
            # must degrade to waiting for the next turn rather than fail an answer.
            asyncio.gather(*(_collect(job_id) for job_id in job_ids), return_exceptions=True),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.info(
            "mid-turn resume timed out for session %s; %d/%d job(s) arrived, the rest "
            "will surface on the next turn",
            session_id,
            len(collected),
            len(job_ids),
        )
    else:
        # Exceptions that are not a failed workflow (broker unreachable, result not a connector
        # envelope) are not reported to the model, since the job may still be running and push-back
        # delivers it next turn. They are logged per job for the operator, with `exc_info=False`
        # because no exception is active here.
        for job_id, outcome in zip(job_ids, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                degraded(
                    logger,
                    "job_resume",
                    "mid-turn resume could not collect job %s from the durable subsystem: %s",
                    job_id,
                    outcome,
                    exc_info=False,
                )
    missing = [job_id for job_id in job_ids if job_id not in collected]
    if missing:
        logger.info("mid-turn resume has no result yet for %s", ", ".join(missing))
    return collected
