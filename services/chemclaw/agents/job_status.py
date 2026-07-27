"""The status tool for an HPC/DFT calculation job (plan step 1.6).

`submit_qm_job` is the last durable launcher core still owns — it needs an HPC identity bridge
rather than a connector, so it did not move — and this is its follow-up: given the id it handed
back, report where the run is and hand over the result once it lands.

**Every other durable job goes through `get_durable_job_status` instead.** This tool once served
the xTB jobs too, dispatching on an id prefix; those are `calc` connector jobs now
(`connectors/calc/connector.yaml`), and a connector job's result arrives in the standard envelope
which the generic status tool reads directly. Two tools, and the line between them is which
subsystem started the run — not something the model has to guess, because each launcher's docstring
names the one that collects it.

The id is still checked against the `qm-` prefix before anything is deserialized, so a foreign id is
rejected with a clear message rather than an opaque validation crash.
"""

from pydantic import ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from agents.tool_registry import tool
from chemclaw.temporal_client import connect
from workflows.models import JobStatus, QMJobResult

_QM_PREFIX = "qm-"


@tool
async def get_job_status(job_id: str) -> JobStatus:
    """Return the current status of an HPC/DFT job, and its result once completed.

    For the id `submit_qm_job` returned. Every *other* durable job — a calculation that was
    too slow to answer inside the turn, an optimization campaign, a report — is collected with
    `get_durable_job_status` instead, which reads the standard connector envelope.

    Args:
        job_id: The id that was returned when the job was submitted.

    Returns:
        The job's status, and — once it has completed — the QM result. Raises if the id is not a
        QM job or does not exist.
    """
    if not job_id.startswith(_QM_PREFIX):
        raise ValueError(
            f"{job_id!r} is not an HPC/DFT job id (expected a {_QM_PREFIX!r} prefix); "
            "collect any other durable job with get_durable_job_status"
        )
    client = await connect()
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except RPCError as exc:  # unknown id → a clear error, not a crash
        raise ValueError(f"no calculation job with id {job_id!r}") from exc

    status = description.status
    result = await handle.result() if status == WorkflowExecutionStatus.COMPLETED else None
    try:
        return JobStatus(
            job_id=job_id,
            kind="qm",
            status=status.name if status is not None else "UNKNOWN",
            qm_result=QMJobResult.model_validate(result) if result else None,
        )
    except ValidationError as exc:
        # A well-formed id whose workflow returns something else (a foreign workflow that
        # happens to share the prefix) → a clear error, not an opaque pydantic crash (G4).
        raise ValueError(f"workflow {job_id!r} is not a QM calculation job") from exc
