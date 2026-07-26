"""Frontend seam for the async confirmed-answer approval hold (plan step 5.5, D-032).

`record_confirmed_answer` proposes a note synchronously; the *asynchronous* "Save this
knowledge? [Yes]/[No]" button is served by `InteractionApprovalWorkflow`, which holds the
candidate durably until the click. These thin adapters are the one working reference caller
for that workflow — the seam a chat UI hooks onto: `start_approval` surfaces a candidate
(starts the hold), `decide_approval` delivers the click as the `decide` signal, and
`approval_status` reads the state for a polling UI. Like the QM tools they hold no durable
state (it lives in Temporal) and return immediately.
"""

from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.interaction_approval import InteractionApprovalWorkflow, InteractionCandidate


def _approval_id(interaction_id: str) -> str:
    """The workflow id for a candidate's hold — stable, so re-surfacing it is idempotent."""
    return f"approval-{interaction_id}"


async def start_approval(candidate: InteractionCandidate) -> str:
    """Start the durable approval hold for `candidate`; return its id (the button handle).

    The id is derived from the interaction id, so surfacing the same candidate twice returns
    the existing hold rather than starting a duplicate.
    """
    client = await connect()
    approval_id = _approval_id(candidate.interaction_id)
    try:
        handle = await client.start_workflow(
            InteractionApprovalWorkflow.run,
            candidate,
            id=approval_id,
            task_queue=settings.background_task_queue,
        )
    except WorkflowAlreadyStartedError:
        return approval_id  # the hold already exists — idempotent surface
    return handle.id


async def decide_approval(approval_id: str, approved: bool) -> None:
    """Deliver the human's Yes/No (the button click) to a pending approval hold."""
    client = await connect()
    handle = client.get_workflow_handle(approval_id)
    try:
        await handle.signal(InteractionApprovalWorkflow.decide, approved)
    except RPCError as exc:  # unknown id → a clear error, not a crash
        raise ValueError(f"no approval hold with id {approval_id!r}") from exc


async def approval_status(approval_id: str) -> str:
    """Return the hold's current state for a polling UI: pending/approved/rejected/expired."""
    client = await connect()
    handle = client.get_workflow_handle(approval_id)
    try:
        return await handle.query(InteractionApprovalWorkflow.status)
    except RPCError as exc:
        raise ValueError(f"no approval hold with id {approval_id!r}") from exc
