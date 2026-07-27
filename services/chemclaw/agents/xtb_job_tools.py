"""Submitting an expensive xTB task as a durable job (xTB plan X3/X4).

The routing seam between `agents.calc_tools` (which runs cheap xTB work inline) and
Temporal. A calculator tool asks `submit_or_none` whether its request is over the inline
budget; if it is, it gets back a job id to report and returns immediately, and if it is
not, it computes in the turn.

This mirrors `agents.qm_tools` deliberately — the same idempotent-submit rule, the same
ambient `requested_by`/`session_id`, the same announce-and-mark-awaiting behaviour — but
it is *not* the same tool. Submitting an HPC/DFT job is an expensive, authorization-gated
decision the model makes on purpose; routing an xTB request to a worker because it is
predicted to take 40 seconds is a mechanical consequence of its size, and putting that
behind a separate model-facing tool would make the model responsible for a cost estimate
it cannot make.
"""

from pydantic import BaseModel
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from agents.authz import require_actor
from agents.job_events import announce_job_started
from agents.session_context import get_current_session_id
from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.models import XtbJobInput, XtbJobSpec, xtb_job_key
from workflows.xtb_job import XtbJobWorkflow


class DeferredJob(BaseModel):
    """What a calculator tool returns instead of a result when the request is too big.

    Not an error and not a refusal: the calculation is running, and `job_id` is how to
    collect it. `predicted_seconds` is the estimate that made the decision, reported so
    the reason is visible rather than mysterious.
    """

    job_id: str
    predicted_seconds: float
    message: str


async def defer_to_job(spec: XtbJobSpec, predicted_seconds: float) -> DeferredJob:
    """Submit `spec` as a durable job and describe it for the model to report."""
    job_id = await submit_xtb_job(spec)
    return DeferredJob(
        job_id=job_id,
        predicted_seconds=round(predicted_seconds, 1),
        message=(
            f"This calculation is predicted to take about {predicted_seconds:.0f} seconds, "
            "so it is running as a background job rather than holding up the conversation. "
            f"Tell the user it is running and give them the job id; poll it with "
            f"get_job_status({job_id!r})."
        ),
    )


async def submit_xtb_job(spec: XtbJobSpec) -> str:
    """Start (or re-join) the durable job for `spec` and return its id.

    Idempotent across the job's whole lifetime, exactly as `submit_qm_job`: the workflow
    id is derived from the spec alone, and re-use is allowed only after a *failure*, so
    resubmitting an identical request while it runs — or after it completed — returns
    the existing job instead of recomputing (D-011).
    """
    job = XtbJobInput(
        spec=spec,
        requested_by=require_actor(),
        session_id=get_current_session_id(),
    )
    job_id = f"xtb-{xtb_job_key(job)}"
    client = await connect()
    try:
        handle = await client.start_workflow(
            XtbJobWorkflow.run,
            job,
            id=job_id,
            task_queue=settings.hpc_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        return job_id
    announce_job_started(handle.id)
    return handle.id
