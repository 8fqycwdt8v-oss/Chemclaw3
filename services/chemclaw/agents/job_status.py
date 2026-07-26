"""One status tool for every durable calculation job (plan step 1.6, xTB plan X3/X4).

Both job kinds — the HPC/DFT `QMJobWorkflow` and the xTB `XtbJobWorkflow` — are Temporal
workflows started with an id that names their kind, so a single tool can answer "how is
my calculation doing" for either. That is the whole reason this is not two tools: the
model asking the question does not care which engine is running, and giving it two
near-identical tools to choose between is a way to have it choose wrong.

The dispatch is on the **id prefix**, which the submitters own (`qm-…`, `xtb-…`) — not on
the workflow's result shape, so an unknown or foreign id is rejected before anything is
deserialized.
"""

from typing import Literal

from pydantic import ValidationError
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from agents.tool_registry import tool
from chemclaw.temporal_client import connect
from workflows.models import JobStatus, QMJobResult, XtbJobResult

_KINDS: dict[str, Literal["qm", "xtb"]] = {"qm-": "qm", "xtb-": "xtb"}


def _kind_of(job_id: str) -> Literal["qm", "xtb"]:
    """The job kind a submitted id encodes, or a `ValueError` naming what is valid."""
    for prefix, kind in _KINDS.items():
        if job_id.startswith(prefix):
            return kind
    raise ValueError(f"{job_id!r} is not a calculation job id (expected a 'qm-' or 'xtb-' prefix)")


@tool
async def get_job_status(job_id: str) -> JobStatus:
    """Return the current status of a calculation job, and its result once completed.

    Works for every job id this system hands out: quantum-mechanical jobs from
    `submit_qm_job`, and the larger xTB calculations (reaction energies, solvent
    comparisons, scans) that were too slow to run inside a single turn and returned a
    job id instead of a result.

    Args:
        job_id: The id that was returned when the job was submitted.

    Returns:
        The job's status, and — once it has completed — the result in the field matching
        its kind. Raises if the id is not a calculation job or does not exist.
    """
    kind = _kind_of(job_id)
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
            kind=kind,
            status=status.name if status is not None else "UNKNOWN",
            qm_result=QMJobResult.model_validate(result) if kind == "qm" and result else None,
            xtb_result=XtbJobResult.model_validate(result) if kind == "xtb" and result else None,
        )
    except ValidationError as exc:
        # A well-formed id whose workflow returns something else (a foreign workflow that
        # happens to share the prefix) → a clear error, not an opaque pydantic crash (G4).
        raise ValueError(f"workflow {job_id!r} is not a {kind} calculation job") from exc
