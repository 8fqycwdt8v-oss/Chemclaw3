"""The agent tool that bridges MAF to the Temporal QM job (plan step 1.5).

A *thin adapter* between the conversation layer and durable execution (D-002): it
starts a `QMJobWorkflow` via the shared Temporal client and returns immediately.
Polling moved to `agents.job_status`, which answers for every job kind rather than only
this one. The agent never blocks on a job and holds no durable state — that lives in
Temporal. MAF advertises this function as a tool, inferring its schema from the signature
and docstring, so the docstring below is also the tool description the model reads.
"""

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from agents.authz import authorize_trigger, require_actor
from agents.dialogue_tools import dry_run_notice, is_dry_run
from agents.harness_todo import mark_awaiting_job
from agents.session_context import get_current_session, get_current_session_id
from agents.tool_registry import tool
from agents.turn_signals import record_job_started
from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.models import QMJobInput, qm_job_key
from workflows.qm_job import QMJobWorkflow


@tool
async def submit_qm_job(molecule_smiles: str, method: str, basis_set: str) -> str:
    """Start a quantum-mechanical calculation and return its job id immediately.

    Runs asynchronously as a durable Temporal workflow; use the returned id with
    `get_job_status` to check progress. Identical requests (same molecule,
    method, and basis set) share one job id, so re-submitting is a safe no-op —
    whether the job is still running or already completed — that returns the
    existing id rather than launching a duplicate calculation (D-011: a stored
    result is never recomputed). Only a *failed* job is re-run on re-submit.

    Args:
        molecule_smiles: The molecule as a SMILES string.
        method: QM method / level of theory, e.g. "B3LYP".
        basis_set: Basis set, e.g. "def2-SVP".

    Returns:
        The job id to poll for status and results.
    """
    # Authorize the expensive HPC trigger against the turn's user before any durable work (F4-T5),
    # so an autonomously-planned todo can't launch a job outside the user's entitlements.
    authorize_trigger("submit_qm_job")
    if is_dry_run():
        return dry_run_notice("submit a QM job", f"{molecule_smiles} at {method}/{basis_set}")
    # Session (to notify on completion) and requested_by (the Entra actor) are ambient to the turn
    # (F3-T3/F4-T3), not model-supplied args. `require_actor` enforces the core rule: under Entra
    # this reject-if-absent guard refuses a job with no authenticated user before any durable work.
    job = QMJobInput(
        molecule_smiles=molecule_smiles,
        method=method,
        basis_set=basis_set,
        requested_by=require_actor(),
        session_id=get_current_session_id(),
    )
    client = await connect()
    try:
        handle = await client.start_workflow(
            QMJobWorkflow.run,
            job,
            id=f"qm-{qm_job_key(job)}",
            task_queue=settings.hpc_task_queue,
            # The default reuse policy only rejects while the workflow is OPEN;
            # a completed job would silently recompute. Allowing re-use only after
            # a *failure* makes submit idempotent across the job's whole lifetime.
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
    except WorkflowAlreadyStartedError:
        # Same id already running or completed: the identical calculation exists,
        # so return its id rather than launching a duplicate (idempotent submit). Not marked
        # awaiting again here — a re-submit of an already-*completed* job will never get another
        # push-back event, so a fresh awaiting todo for it would never be flipped and would block
        # `todos_remaining` forever.
        return f"qm-{qm_job_key(job)}"
    # Tell the streaming turn a job is now running (D-042), so the surface shows the launch instead
    # of silence until the push-back. Only on a genuine start: the re-submit branch above returns an
    # existing (possibly already completed) job, which will never emit a matching `job_completed`
    # event — announcing it would leave a permanently "running" row in the UI.
    await _mark_awaiting_if_harness(handle.id, molecule_smiles=molecule_smiles, method=method)
    # Surface the launch on the turn's event stream (gap RCH-5). Only on a *fresh* start, for the
    # same reason the awaiting todo is: a duplicate submit of a finished job never starts anything.
    record_job_started(handle.id, "qm")
    return handle.id


async def _mark_awaiting_if_harness(job_id: str, *, molecule_smiles: str, method: str) -> None:
    """Record the harness todo awaiting `job_id`, when the harness's todo list is in play.

    Silent no-op off the harness path (harness disabled, or no live session ambient — e.g. the CLI,
    which runs single-shot with no `AgentSession`): writing to a todo list nothing ever reads would
    just be dead state on the classic agent's turns.
    """
    if not settings.harness_enabled:
        return
    session = get_current_session()
    if session is None:
        return
    await mark_awaiting_job(
        session, job_id, title=f"Await QM job {job_id} ({molecule_smiles}, {method})"
    )
