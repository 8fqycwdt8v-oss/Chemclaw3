"""Server-backed test for the durable hold on a challenged answer.

Proves the seam that carries an upheld objection past the end of a session: a candidate waits on the
time-skipping Temporal server until a `decide` signal, and every terminal state is distinguishable
from the others. Runs in CI; skips offline. The timeout path uses the server's time-skipping so the
seven-day hold resolves instantly.

The property worth a server-backed test rather than a unit test is that **"nobody looked" is not
filed as "somebody agreed"**. A hold that expired and a hold a human dismissed both end with no
objection outstanding, and only the workflow's own terminal state tells them apart — which is
exactly the distinction an audit trail is for.
"""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from chemclaw.durable.answer_review import (
    AnswerReviewCandidate,
    AnswerReviewOutcome,
    AnswerReviewWorkflow,
)
from tests.temporal_env import pydantic_client, start_env_or_skip

_CANDIDATE = AnswerReviewCandidate(
    correlation_id="corr-42",
    answer="Run the coupling at 1.0 mL/min on a Kinetex column.",
    objections=["[grounding] note-7 gives no flow rate; the figure is unsupported"],
    requested_by="oid-chemist",
)


def test_every_terminal_state_is_distinguishable() -> None:
    """Upheld, dismissed and expired are three outcomes, not two plus an absence.

    The expiry arm is the one that matters: an unanswered hold must not resolve to either ruling,
    because a record saying a human agreed with an objection nobody read is worse than no record.
    """

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-answer-review",
                workflows=[AnswerReviewWorkflow],
            ):
                upheld = await client.start_workflow(
                    AnswerReviewWorkflow.run,
                    _CANDIDATE,
                    id="review-upheld",
                    task_queue="test-answer-review",
                )
                # The queries a review surface reads before anyone rules.
                assert await upheld.query(AnswerReviewWorkflow.status) == "pending"
                assert await upheld.query(AnswerReviewWorkflow.owner) == "oid-chemist"
                assert "grounding" in await upheld.query(AnswerReviewWorkflow.summary)
                await upheld.signal(AnswerReviewWorkflow.decide, True)
                assert await upheld.result() == AnswerReviewOutcome(status="upheld")

                dismissed = await client.start_workflow(
                    AnswerReviewWorkflow.run,
                    _CANDIDATE,
                    id="review-dismissed",
                    task_queue="test-answer-review",
                )
                await dismissed.signal(AnswerReviewWorkflow.decide, False)
                assert await dismissed.result() == AnswerReviewOutcome(status="dismissed")

                # Nobody rules: time-skipping runs the hold out to its bound.
                expired = await client.start_workflow(
                    AnswerReviewWorkflow.run,
                    _CANDIDATE,
                    id="review-expired",
                    task_queue="test-answer-review",
                )
                assert await expired.result() == AnswerReviewOutcome(status="expired")
                assert await expired.query(AnswerReviewWorkflow.status) == "expired"

    asyncio.run(_run())


def test_the_first_ruling_wins() -> None:
    """A recorded sign-off a second click can flip is not a sign-off.

    The same rule `InteractionApprovalWorkflow.decide` keeps, and it matters more here: this hold
    exists to record that a named person resolved an objection, so a later signal overwriting that
    would rewrite the audit answer rather than update a preference.
    """

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-answer-review-first",
                workflows=[AnswerReviewWorkflow],
            ):
                handle = await client.start_workflow(
                    AnswerReviewWorkflow.run,
                    _CANDIDATE,
                    id="review-first-wins",
                    task_queue="test-answer-review-first",
                )
                await handle.signal(AnswerReviewWorkflow.decide, True)
                await handle.signal(AnswerReviewWorkflow.decide, False)
                assert await handle.result() == AnswerReviewOutcome(status="upheld")

    asyncio.run(_run())
