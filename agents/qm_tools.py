"""Agent tools that bridge MAF to the Temporal QM job (plan steps 1.5, 1.6).

These two async functions are the *thin adapter* between the conversation layer
and durable execution (D-002): they start and query a `QMJobWorkflow` via the
shared Temporal client and return immediately. The agent never blocks on a job
and holds no durable state — that lives in Temporal. MAF advertises these
functions as tools, inferring their schema from the signature and docstring, so
the docstrings below are also the tool descriptions the model reads.
"""

from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.models import QMJobInput, QMJobStatus, qm_job_key
from workflows.qm_job import QMJobWorkflow


async def submit_qm_job(molecule_smiles: str, method: str, basis_set: str) -> str:
    """Start a quantum-mechanical calculation and return its job id immediately.

    Runs asynchronously as a durable Temporal workflow; use the returned id with
    `get_qm_job_status` to check progress. Identical requests (same molecule,
    method, and basis set) share one job id, so re-submitting is a safe no-op
    that returns the existing id rather than launching a duplicate calculation.

    Args:
        molecule_smiles: The molecule as a SMILES string.
        method: QM method / level of theory, e.g. "B3LYP".
        basis_set: Basis set, e.g. "def2-SVP".

    Returns:
        The job id to poll for status and results.
    """
    job = QMJobInput(molecule_smiles=molecule_smiles, method=method, basis_set=basis_set)
    client = await connect()
    try:
        handle = await client.start_workflow(
            QMJobWorkflow.run,
            job,
            id=f"qm-{qm_job_key(job)}",
            task_queue=settings.hpc_task_queue,
        )
    except WorkflowAlreadyStartedError:
        # Same id already running or done: the identical calculation is in flight,
        # so return its id rather than launching a duplicate (idempotent submit).
        return f"qm-{qm_job_key(job)}"
    return handle.id


async def get_qm_job_status(job_id: str) -> QMJobStatus:
    """Return the current status of a QM job, and its result once completed.

    Args:
        job_id: The id returned by `submit_qm_job`.

    Returns:
        The job's status; the parsed result is included only when it has
        completed. Raises if no job with this id exists.
    """
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:  # unknown id → surface a clear error, not a crash
        raise ValueError(f"no QM job with id {job_id!r}") from exc

    status = description.status
    result = None
    if status == WorkflowExecutionStatus.COMPLETED:
        result = await handle.result()
    return QMJobStatus(
        job_id=job_id,
        status=status.name if status is not None else "UNKNOWN",
        result=result,
    )
