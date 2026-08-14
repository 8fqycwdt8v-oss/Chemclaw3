"""Assembling a turn's final `AnswerEvent` from the verdict about its answer.

The last step of a turn is not "emit the text": it is a judgement about what the text is worth, and
then the projection of that judgement onto the wire contract a surface reads. This module owns the
second half. The first half is `chemclaw.agent.verifier.score_answer`, which is where it has to
live: **two callers need the same verdict and neither may own it.**

`agent/challenge_gate.py` computes it inside the graph, because it must decide whether to put the
answer to a review panel while the turn can still be revised. This module computes it for turns that
had no gate — it is off by default, and it never runs for a template step. One implementation, two
entry points; a second copy of the combination rules would let the two paths disagree about whether
the same answer is flagged, which is precisely the class of drift the LangGraph migration was
arranged to be incapable of.

So what is left here is small on purpose: take a verdict (given, or scored on demand) and build the
event. The reasoning about *which* checks run and how their findings combine lives beside the checks
themselves.
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
    review: TurnReview | None = None,
) -> AnswerEvent:
    """Assemble the turn's final `AnswerEvent`, scoring the answer first if nobody already did.

    `review_required` is the one routing signal a surface reads to flag an answer rather than
    present it as authoritative, and there is deliberately no second flag for the checks that can
    raise it: a reviewer needs to know *that* an answer wants a look, and `unsupported_claims`
    carries *why*, whichever check spoke. `challenged` is the one exception and it is not a second
    routing flag — it says the objection came from independent agents that went and looked, which is
    a different weight of evidence from a confidence score under a threshold.

    Args:
        answer: The finished answer text.
        tool_outputs: What this turn's tools returned, untruncated. This is the whole point of the
            checks: verification used to re-resolve an answer's citations from the graph on disk,
            which asked whether a cited note *exists* rather than whether this turn saw it.
        tools_called: Every tool this turn invoked, for the promised-but-uncalled scan.
        review: The in-graph challenge gate's verdict, when one ran (`challenge_enabled`). `None` is
            the ungated path and scores here instead — the original behaviour, unchanged.

    Returns:
        The event, which never carries a flag the caller has to interpret: every field is either
        what a check found or the `None`/`False` that says the check did not run.
    """
    if review is None:
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
