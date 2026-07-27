"""Agent tools bridging MAF to the durable conformer-ensemble job — research follow-up, D-092.

The same thin adapter shape as `agents.qm_tools`/`agents.durable_tools` (D-002): start and query
`ConformerEnsembleWorkflow` via the shared Temporal client and return immediately. The agent never
blocks on the job and holds no durable state — that lives in Temporal.
"""

from pydantic import ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from agents.authz import authorize_trigger, require_actor
from agents.dialogue_tools import dry_run_notice, is_dry_run
from agents.session_context import get_current_session_id
from agents.tool_registry import tool
from agents.turn_signals import record_job_started
from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.conformer_job import ConformerEnsembleWorkflow
from workflows.conformer_models import ConformerJobInput, ConformerJobStatus, conformer_job_key


@tool
async def submit_conformer_ensemble_job(smiles: str, charge: int = 0) -> str:
    """Start a Boltzmann-weighted GFN2-xTB conformer ensemble and return its job id immediately.

    Use this instead of `compute_xtb_energy` when a single rigid conformer is not representative
    enough — a flexible molecule's solution-phase behavior is better read from a population of
    conformers than one seeded geometry. Runs asynchronously as a durable Temporal job (an
    ensemble is tens of xTB single points, materially heavier than the fast calculators' sub-second
    budget); poll with `get_conformer_job_status`. Identical requests (same molecule, charge, and
    ensemble configuration) share one job id, so re-submitting is a safe no-op.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net molecular charge (0 = neutral).

    Returns:
        The job id to poll for progress and the result.
    """
    authorize_trigger("submit_conformer_ensemble_job")
    if is_dry_run():
        return dry_run_notice("run a conformer ensemble", f"{smiles} (charge {charge})")
    job = ConformerJobInput(
        molecule_smiles=smiles,
        charge=charge,
        requested_by=require_actor(),
        session_id=get_current_session_id(),
    )
    client = await connect()
    workflow_id = f"conformer-{conformer_job_key(job)}"
    try:
        handle = await client.start_workflow(
            ConformerEnsembleWorkflow.run,
            job,
            id=workflow_id,
            task_queue=settings.background_task_queue,
            # Same idempotency contract as `submit_qm_job`: a completed job is never recomputed
            # on re-submit, only a genuinely failed one may re-run.
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        return workflow_id  # existing job (running or completed) — see submit_qm_job's note
    record_job_started(handle.id, "conformer_ensemble")
    return handle.id


@tool
async def get_conformer_job_status(job_id: str) -> ConformerJobStatus:
    """Return the current status of a conformer-ensemble job, and its result once completed.

    Args:
        job_id: The id returned by `submit_conformer_ensemble_job`.

    Returns:
        The job's status; the parsed result is included only when it has completed. Raises if no
        job with this id exists.
    """
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:
        raise ValueError(f"no conformer job with id {job_id!r}") from exc

    status = description.status
    result = None
    if status == WorkflowExecutionStatus.COMPLETED:
        result = await handle.result()
    try:
        return ConformerJobStatus(
            job_id=job_id,
            status=status.name if status is not None else "UNKNOWN",
            result=result,
        )
    except ValidationError as exc:
        raise ValueError(f"workflow {job_id!r} is not a conformer job") from exc
