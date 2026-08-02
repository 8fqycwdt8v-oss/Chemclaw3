"""Frontend seam for the async confirmed-answer approval hold (plan step 5.5, D-032).

`record_confirmed_answer` proposes a note synchronously; the *asynchronous* "Save this
knowledge? [Yes]/[No]" button is served by `InteractionApprovalWorkflow`, which holds the
candidate durably until the click. These thin adapters are the one working reference caller
for that workflow — the seam a chat UI hooks onto: `start_approval` surfaces a candidate
(starts the hold), `decide_approval` delivers the click as the `decide` signal, and
`approval_status` reads the state for a polling UI. Like the QM tools they hold no durable
state (it lives in Temporal) and return immediately.
"""

from pydantic import BaseModel
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from chemclaw.agent.authz import require_actor
from chemclaw.core.config import settings
from chemclaw.core.temporal_client import connect
from chemclaw.core.turn_signals import record_approval_request
from chemclaw.durable.interaction_approval import InteractionApprovalWorkflow, InteractionCandidate


class PendingApproval(BaseModel):
    """One open hold, as a review surface renders it: the handle plus what it is asking."""

    approval_id: str
    question: str
    requested_by: str


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
    # Stamp the turn's actor onto the hold if the caller did not, so the decision surface can
    # scope it to its owner (an unowned hold would be decidable by any authenticated user).
    owned = (
        candidate
        if candidate.requested_by
        else candidate.model_copy(update={"requested_by": require_actor()})
    )
    try:
        handle = await client.start_workflow(
            InteractionApprovalWorkflow.run,
            owned,
            id=approval_id,
            task_queue=settings.background_task_queue,
        )
    except WorkflowAlreadyStartedError:
        _announce(owned, approval_id)
        return approval_id  # the hold already exists — idempotent surface
    _announce(owned, handle.id)
    return handle.id


def _announce(candidate: InteractionCandidate, approval_id: str) -> None:
    """Surface the opened hold — with its handle — on the turn's event stream (gap RCH-3).

    `ApprovalRequestEvent.approval_id` has always documented itself as the handle a surface
    answers via `POST /approvals/{id}/decision`, but nothing populated it: `start_approval`
    returns the id into the *model's* context, and the runner sees only the model's streamed
    updates. So every approval reached the UI with an empty handle — renderable, unanswerable.

    Recorded as a turn signal rather than returned, for the same reason `JobSignal` is: the
    handle must come from the tool that opened the hold, never from anything the model authors.
    Announced on the already-started path too, so a re-surfaced candidate is still answerable.
    """
    record_approval_request(f"Save this to the knowledge graph? {candidate.question}", approval_id)


async def decide_approval(approval_id: str, approved: bool) -> None:
    """Deliver the human's Yes/No (the button click) to a pending approval hold."""
    client = await connect()
    handle = client.get_workflow_handle(approval_id)
    try:
        await handle.signal(InteractionApprovalWorkflow.decide, approved)
    except RPCError as exc:  # unknown id → a clear error, not a crash
        raise ValueError(f"no approval hold with id {approval_id!r}") from exc


async def approval_owner(approval_id: str) -> str:
    """The Entra oid a hold belongs to — the scope check the decision route applies."""
    client = await connect()
    handle = client.get_workflow_handle(approval_id)
    try:
        return await handle.query(InteractionApprovalWorkflow.owner)
    except RPCError as exc:
        raise ValueError(f"no approval hold with id {approval_id!r}") from exc


async def list_pending_approvals(owner: str | None = None) -> list[PendingApproval]:
    """Every hold still awaiting a click, optionally narrowed to one owner.

    Without this a hold could be *started* but never found again: the id was returned once, into
    a turn that has since ended. Listing is a Temporal visibility query over running workflows of
    this type — no second store, and the hold stays the single source of truth.
    """
    client = await connect()
    pending: list[PendingApproval] = []
    query = (
        f'WorkflowType = "{InteractionApprovalWorkflow.__name__}" AND ExecutionStatus = "Running"'
    )
    async for execution in client.list_workflows(query):
        handle = client.get_workflow_handle(execution.id)
        try:
            holder = await handle.query(InteractionApprovalWorkflow.owner)
            question = await handle.query(InteractionApprovalWorkflow.summary)
        except RPCError:
            # The hold completed between the visibility listing and the query — a natural race
            # on an eventually-consistent index, not an error. Drop it from the listing.
            continue
        if owner is not None and holder != owner:
            continue
        pending.append(
            PendingApproval(approval_id=execution.id, question=question, requested_by=holder)
        )
    return pending


async def approval_status(approval_id: str) -> str:
    """Return the hold's current state for a polling UI: pending/approved/rejected/expired."""
    client = await connect()
    handle = client.get_workflow_handle(approval_id)
    try:
        return await handle.query(InteractionApprovalWorkflow.status)
    except RPCError as exc:
        raise ValueError(f"no approval hold with id {approval_id!r}") from exc
