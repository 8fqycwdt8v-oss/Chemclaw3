"""Durable hold for an answer a review panel objected to and the model could not fix.

Why this exists: `agent/challenge_gate.py` runs its panel inside a turn, and a turn ends. When the
panel reaches quorum and the revision budget is spent, the objection is a thing a *human* has to
settle — and that decision does not fit inside the conversation that produced it. The chemist may be
gone; the session may be over; the pod may be replaced. The architecture puts durability in
Temporal, never in layer 1, so the pending decision is held here.

**Not `InteractionApprovalWorkflow`, and the difference is what approval *means*.** That workflow
(D-032) holds a confirmed-answer note and, on Yes, runs the PR-gate activity that proposes it — its
terminal action is a knowledge *write*, and its question is "should this become part of the record".
This one's question is "was this answer sound", and Yes means a human read the objection and
disagreed with it. Reusing the first for the second would make an approval here propose a note
nobody asked to save, which is a worse defect than the duplication it avoids. What they genuinely
share — a bounded wait on a signal, an owner-scoped decision, a status query for a polling surface —
is a shape worth repeating, not a workflow worth overloading.

**Nothing here writes anything, on either branch.** The hold records that a human made a call; it
does not act on it. That is deliberate at this stage: the answer has already been delivered to the
chemist marked for review (`AnswerEvent.review_required`), so there is no pending artifact for an
approval to release. What a decision buys is the audit trail — a record that an objection was
raised, and that a named person resolved it — which is exactly the substrate
`docs/reference/architektur.md` §12 asks this system to keep emitting rather than to self-certify.
"""

from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.registry import durable_workflow


class AnswerReviewCandidate(BaseModel):
    """The answer a human is being asked to rule on, and what the panel said about it.

    `requested_by` is the Entra `oid` of the chemist whose turn produced the answer (F4-T3's
    `require_actor`), so the decision surface can be owner-scoped for
    `InteractionApprovalWorkflow.owner`'s reason: a review is a judgement about someone's work, and
    any authenticated user being able to clear another person's objection would make the record
    meaningless. Empty only on the dev path, where `entra_required` is off and no actor exists.

    `objections` are the upheld verdicts, already rendered as `[angle] rationale`. Strings rather
    than the `ChallengeVerdict` model because a workflow argument is a serialization contract that
    outlives a deploy: this history has to replay years from now, and a pydantic model in the agent
    layer is free to change in a way a list of strings is not.
    """

    correlation_id: str
    answer: str
    objections: list[str] = Field(default_factory=list)
    requested_by: str = ""


class AnswerReviewOutcome(BaseModel):
    """Terminal state of one review hold, for the caller or a surface to read."""

    status: str  # "upheld" | "dismissed" | "expired"


@durable_workflow("background")
@workflow.defn
class AnswerReviewWorkflow:
    """Hold a challenged answer pending a human's ruling on the panel's objection.

    A surface starts one per challenged answer (its `id` is the handle), reads `status` to render
    the pending item, sends the ruling as the `decide` signal, and reads `AnswerReviewOutcome`.
    Restarting a worker mid-wait resumes from history — the hold is durable.

    `decide(upheld=True)` means the human agreed the objection was real; `False` means they read it
    and judged the answer sound. **Both are decisions and both are recorded**; only the timeout is
    an absence, and it is reported as `expired` rather than as either ruling, because "nobody
    looked" must never be filed as "somebody agreed".
    """

    def __init__(self) -> None:
        """Start with no ruling recorded (nobody has looked yet)."""
        self._upheld: bool | None = None
        # Mirrored at run start so `owner`/`summary` can answer before any ruling — a review queue
        # needs both to scope and to render the pending item.
        self._candidate: AnswerReviewCandidate | None = None
        # Set when the hold times out, so `status` reports `expired` after completion rather than a
        # stale `pending`.
        self._expired: bool = False

    @workflow.run
    async def run(self, candidate: AnswerReviewCandidate) -> AnswerReviewOutcome:
        """Wait (bounded) for a human ruling on the panel's objection."""
        self._candidate = candidate
        try:
            await workflow.wait_condition(
                lambda: self._upheld is not None,
                timeout=timedelta(seconds=settings.interaction_approval_timeout_seconds),
            )
        except TimeoutError:
            # Nobody ruled in time. The answer was already delivered marked for review, so letting
            # the hold expire drops the *pending decision* rather than any work.
            self._expired = True
            return AnswerReviewOutcome(status="expired")
        return AnswerReviewOutcome(status="upheld" if self._upheld else "dismissed")

    @workflow.query
    def owner(self) -> str:
        """The Entra oid this hold belongs to, so a decision route can scope it to its owner."""
        return self._candidate.requested_by if self._candidate is not None else ""

    @workflow.query
    def summary(self) -> str:
        """What the panel objected to — what a review list renders."""
        if self._candidate is None:
            return ""
        return "; ".join(self._candidate.objections)

    @workflow.signal
    def decide(self, upheld: bool) -> None:
        """Record the human's ruling; the first decision wins.

        First-wins rather than last-wins for `InteractionApprovalWorkflow.decide`'s reason: a
        recorded sign-off that a second click can flip is not a sign-off.
        """
        if self._upheld is None:
            self._upheld = upheld

    @workflow.query
    def status(self) -> str:
        """Current state for a polling surface: `pending`, `upheld`, `dismissed`, or `expired`."""
        if self._expired:
            return "expired"
        if self._upheld is None:
            return "pending"
        return "upheld" if self._upheld else "dismissed"
