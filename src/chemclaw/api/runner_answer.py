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

    `review_required` is the one signal a surface reads to flag an answer; `unsupported_claims`
    carries why, whichever check spoke.

    Args:
        answer: The finished answer text.
        tool_outputs: What this turn's tools returned, untruncated, so checks ask what this turn
        saw.
        tools_called: Every tool this turn invoked, for the promised-but-uncalled scan.

    Returns:
        The event and the verdict. The verdict keeps unsupported claims apart from review notes,
        which the wire merges; the revision loop in `api/runner.py` needs only the actionable half.
        Every finding field is what a check found or the `None`/`False` meaning nothing was found,
        and `checks_run` says which checks ran at all.
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
