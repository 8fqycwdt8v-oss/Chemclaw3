"""Assembling a turn's final `AnswerEvent` from the verdict about its answer.

The last step of a turn is not "emit the text": it is a judgement about what the text is worth, and
then the projection of that judgement onto the wire contract a surface reads. This module owns the
second half; `chemclaw.agent.verifier.score_answer` owns the first.

**The split used to be load-bearing and is now only tidy, which is worth saying rather than
leaving as an unexplained seam.** `score_answer` lived apart because two callers needed one verdict:
this module for an ordinary turn, and an in-graph gate that had to decide whether to put the answer
to a review panel while the turn could still be revised. That gate is gone (D-2026-08-15), so there
is one caller again. The split stays because the reasoning about *which* checks run and how their
findings combine belongs beside the checks themselves — not because a second entry point exists.

So what is left here is small on purpose: score the answer and build the event.
"""

import logging
from collections.abc import Sequence

from chemclaw.agent.verifier import score_answer
from chemclaw.api.events import AnswerEvent

logger = logging.getLogger(__name__)


async def build_answer_event(
    answer: str,
    tool_outputs: Sequence[str],
    tools_called: Sequence[str] = (),
) -> AnswerEvent:
    """Assemble the turn's final `AnswerEvent`, scoring the answer first.

    `review_required` is the one routing signal a surface reads to flag an answer rather than
    present it as authoritative, and there is deliberately no second flag for the checks that can
    raise it: a reviewer needs to know *that* an answer wants a look, and `unsupported_claims`
    carries *why*, whichever check spoke.

    Args:
        answer: The finished answer text.
        tool_outputs: What this turn's tools returned, untruncated. This is the whole point of the
            checks: verification used to re-resolve an answer's citations from the graph on disk,
            which asked whether a cited note *exists* rather than whether this turn saw it.
        tools_called: Every tool this turn invoked, for the promised-but-uncalled scan.

    Returns:
        The event, which never carries a flag the caller has to interpret: every field is either
        what a check found or the `None`/`False` that says the check did not run.
    """
    review = await score_answer(answer, tool_outputs, tools_called)
    return AnswerEvent(
        text=answer,
        confidence=review.confidence,
        verified_by=review.verified_by,
        unsupported_claims=review.unsupported,
        review_required=review.review_required,
        challenged=review.challenged,
        review_hold_id=review.hold_id,
    )
