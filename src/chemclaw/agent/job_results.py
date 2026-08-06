"""Wait, within one turn, for durable jobs that turn started (gap AGT-2).

The system's defining interaction — "compute this, then reason about the result" — was split in
two. A tool returned a job id, the turn ended, and the result arrived as a push-back event the
session only picked up on its *next* turn. For a research assistant built on durable execution,
that is the interaction, and it took two exchanges.

**Why bounded, and why opt-in.** Holding a turn open holds an admission permit and a session's
turn slot. A wait longer than the front door's whole-turn deadline would simply be cut off there,
so the bound here must be the shorter one and the caller stays responsible for the outer deadline.
A job that does not finish in time is not an error — the existing push-back path still delivers it
on the next turn, which is exactly the pre-existing behavior. The feature therefore degrades to
"what it did before" rather than to a failure.

**This waits on the jobs, not on the mailbox** (REV-7, D-153). It used to tail
`chemclaw.agent.session_events`, and that was the wrong source in a way that destroyed data. The
mailbox claim is *destructive*: claiming consumes every unconsumed `job_completed` row for the
session, so a resume waiting on job A also consumed the row for job B — a job this turn did not
start — and then dropped it on the floor, because it only kept what it was waiting for. The front
door's `/sessions/{id}/events` stream, the consumer those rows belong to, never saw them. The old
docstring half-admitted this and then argued the front door "would already have claimed" them,
which is a race, not a guarantee: both consumers poll the same rows.

Asking Temporal directly removes the whole class. "Did job X finish" is a question about durable
state, and the durable state is the authoritative answer; the mailbox exists to *wake a chat*, which
is a different job. So there is no shared queue left to race over, nothing to consume, and nothing
to discard. It is also strictly more informative: a `job_completed` payload carries a one-line
summary, while the envelope carries the result itself, so the model resumes with the data rather
than with a description of it.
"""

import asyncio
import logging
from typing import Any

from temporalio.client import WorkflowFailureError

from chemclaw.agent.durable_tools import completed_job_status, job_status
from chemclaw.core.temporal_client import connect

logger = logging.getLogger(__name__)


async def await_job_results(
    session_id: str,
    job_ids: list[str],
    *,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    """Wait up to `timeout_seconds` for `job_ids` to complete; return the results that arrived.

    Returns a partial map on timeout rather than raising: some results are strictly better than
    none, and the ones that did not arrive are still delivered by the ordinary push-back path on
    the session's next turn (the behavior before this existed).

    Each job is awaited on its own workflow handle — `handle.result()` is Temporal's own "tell me
    when this finishes", so there is no poll loop to tune and no mailbox to consume. They are
    gathered, because the jobs are independent and waiting for them in sequence would make the
    bound the *sum* of their durations rather than the longest.

    Args:
        session_id: The session the turn belongs to, for the log line only — the wait itself is
            per job now, and no longer touches anything session-scoped.
        job_ids: The jobs this turn started.
        timeout_seconds: The whole wait's bound, shared across the jobs.

    Returns:
        `{job_id: {"job_id": …, "status": …, "summary": …, "result": …}}` for every job that
        *reached a terminal state* in time — completed, failed, cancelled, timed_out or
        terminated. A job that failed is reported with its status rather than omitted: "your
        calculation failed" is an answer the chemist needs inside this turn, and dropping it would
        leave the model narrating a success that did not happen. A job still running is absent, and
        the ordinary push-back path delivers it on the next turn.
    """
    collected: dict[str, dict[str, Any]] = {}

    async def _collect(job_id: str) -> None:
        handle = (await connect()).get_workflow_handle(job_id)
        try:
            status = completed_job_status(job_id, await handle.result())
        except WorkflowFailureError:
            # The docstring above has always promised this and the code never did it:
            # `handle.result()` *raises* for a workflow that failed, was cancelled, timed out or
            # was terminated, `return_exceptions=True` swallowed the raise, and the job was
            # therefore dropped from the resume exactly like one that had not finished. The model
            # then resumed with a turn that mentioned no failure at all — the outcome the docstring
            # says must not happen, because the chemist reads silence as success.
            #
            # `job_status` rather than a status word composed here: it is the one place that maps a
            # terminal Temporal status onto the word this system reports, and a second mapping is
            # how the resume and `get_durable_job_status` would come to disagree about the same run.
            # It also distinguishes failed from cancelled from timed_out, which the raise does not.
            status = await job_status(job_id)
        collected[job_id] = status.model_dump()

    try:
        await asyncio.wait_for(
            # `return_exceptions` so one failed or undecodable job does not cancel the others'
            # waits — the turn should resume with whatever did land. The exceptions are logged
            # below rather than raised: this whole path is an optimization over waiting for the
            # next turn, and it must degrade to that rather than fail an answer.
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
    except Exception:
        # Temporal being unreachable must degrade the turn to its pre-AGT-2 behavior, not fail an
        # answer the model already has.
        logger.warning("mid-turn resume could not reach the durable jobs", exc_info=True)
    missing = [job_id for job_id in job_ids if job_id not in collected]
    if missing:
        logger.info("mid-turn resume has no result yet for %s", ", ".join(missing))
    return collected
