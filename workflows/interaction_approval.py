"""Durable hold-for-approval of a confirmed-answer note (plan step 5.5, async UI seam).

Why this exists: `record_confirmed_answer` proposes a note synchronously, inside one agent
turn. A chat "Save this knowledge? [Yes] [No]" button is *asynchronous* — the human may click
minutes later, after the turn (or session) has ended — so the pending candidate must outlive
the conversation. The architecture puts durability in Temporal, never in MAF, so the candidate
is held by this workflow: it starts when the agent surfaces a candidate, waits (bounded) for a
`decide` signal (the button click), and only on Yes runs the PR-gate activity. Reject or
timeout ends the workflow without proposing anything.

The write itself reuses `memory.interaction.propose_confirmed_answer` — the same build-and-gate
path as the synchronous tool — so the Yes button and the inline tool produce identical PRs.
The button gates the *proposal*, not the merge: an approved note still lands on a feature
branch for the real human PR review (D-005 stays intact).
"""

from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from kg.git_submitter import default_submitter
    from memory.interaction import propose_confirmed_answer

from workflows.publish import note_publish_retry


class InteractionCandidate(BaseModel):
    """The confirmed-answer a human is being asked to save (the held candidate)."""

    interaction_id: str
    question: str
    answer: str
    evidence_note_ids: list[str] = []


class ApprovalOutcome(BaseModel):
    """Terminal state of one approval hold, for the caller/UI to read.

    `reference` is the PR-gate reference and is set only when `status == "approved"`.
    """

    status: str  # "approved" | "rejected" | "expired"
    reference: str = ""


@activity.defn
async def propose_confirmed_answer_activity(candidate: InteractionCandidate) -> str:
    """Propose the approved candidate as an interaction note via the PR-gate (the write side)."""
    return await propose_confirmed_answer(
        candidate.interaction_id,
        candidate.question,
        candidate.answer,
        candidate.evidence_note_ids,
        default_submitter(),
    )


@workflow.defn
class InteractionApprovalWorkflow:
    """Hold a confirmed-answer note pending a human Yes/No, then publish on Yes.

    The frontend starts one workflow per candidate (its `id` is the button's handle),
    reads `status` to render the button, sends the click as the `decide` signal, and
    reads the returned `ApprovalOutcome` for the PR reference. Restarting a worker while
    a button is pending resumes the wait from history — the hold is durable.
    """

    def __init__(self) -> None:
        """Start with no decision recorded (button not yet clicked)."""
        self._approved: bool | None = None

    @workflow.run
    async def run(self, candidate: InteractionCandidate) -> ApprovalOutcome:
        """Wait (bounded) for the button click; propose the note only on Yes."""
        try:
            await workflow.wait_condition(
                lambda: self._approved is not None,
                timeout=timedelta(seconds=settings.interaction_approval_timeout_seconds),
            )
        except TimeoutError:
            # Nobody clicked in time: drop the candidate rather than pin the workflow.
            return ApprovalOutcome(status="expired")
        if not self._approved:
            return ApprovalOutcome(status="rejected")
        reference = await workflow.execute_activity(
            propose_confirmed_answer_activity,
            candidate,
            start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
            retry_policy=note_publish_retry(),
        )
        return ApprovalOutcome(status="approved", reference=reference)

    @workflow.signal
    def decide(self, approved: bool) -> None:
        """Record the human's Yes/No (the button click); the first decision wins."""
        if self._approved is None:
            self._approved = approved

    @workflow.query
    def status(self) -> str:
        """Current state for a polling UI: `pending`, `approved`, or `rejected`."""
        if self._approved is None:
            return "pending"
        return "approved" if self._approved else "rejected"
