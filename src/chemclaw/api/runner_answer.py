"""Assembling a turn's final `AnswerEvent` from the verdict about its answer.

`chemclaw.agent.verifier.score_answer` judges the answer; this module projects that verdict onto the
wire contract a surface reads.
"""

import logging
from collections.abc import Sequence

from chemclaw.agent.verifier import TurnReview, score_answer
from chemclaw.api.events import AnswerEvent

logger = logging.getLogger(__name__)


async def build_answer_event(
    answer: str,
    tool_outputs: Sequence[str],
    tools_called: Sequence[str] = (),
) -> tuple[AnswerEvent, TurnReview]:
    """Assemble the turn's final `AnswerEvent`, scoring the answer first.

    `tool_outputs` are this turn's untruncated tool results, so the checks ask what this turn saw;
    `tools_called` feeds the promised-but-uncalled scan. Returns the event and the verdict: the wire
    merges unsupported claims with review notes, and the revision loop in `api/runner.py` needs only
    the claims. `review_required` is the one flag a surface reads; `checks_run` says which checks
    ran at all.
    """
    review = await score_answer(answer, tool_outputs, tools_called)
    return (
        AnswerEvent(
            text=answer,
            checks_run=review.checks_run,
            confidence=review.confidence,
            verified_by=review.verified_by,
            # Findings first, then the note saying which check produced the verdict.
            unsupported_claims=[*review.unsupported, *review.review_notes],
            review_required=review.review_required,
            challenged=review.challenged,
            review_hold_id=review.hold_id,
        ),
        review,
    )
