"""Assembling a turn's final `AnswerEvent`, scored against what that turn actually retrieved.

The last step of a turn is not "emit the text": it is two honesty checks over the text and the
turn's own tool outputs, each able to mark the answer for review. That is a self-contained
judgement over two arguments, so it sits beside `chemclaw.api.runner` rather than inside it — the
runner's module owns the turn's *lifecycle*, and this owns what the turn's answer is worth.

Scoring lives in `chemclaw.agent.verifier`; this module only decides which checks run for this
deployment and how their verdicts combine into the one routing flag a surface reads.
"""

import logging
from collections.abc import Sequence
from typing import Literal

from chemclaw.agent.verifier import (
    promised_uncalled_tools,
    ungrounded_parameter_shapes,
    verify_turn_answer,
)
from chemclaw.api.events import AnswerEvent
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


async def build_answer_event(
    answer: str, tool_outputs: Sequence[str], tools_called: Sequence[str] = ()
) -> AnswerEvent:
    """Assemble the turn's final `AnswerEvent`, scoring it against what this turn retrieved (F10-B).

    Two independent checks, each behind its own knob and each able to set `review_required` — the
    one routing signal a surface (or a future D-032 hold) reads to flag an answer rather than
    present it as authoritative. There is deliberately no second flag: a reviewer needs to know
    *that* an answer wants a look, and `unsupported_claims` carries *why*, whichever check spoke.

    - `verifier_enabled`: citation faithfulness against `tool_outputs`, stamping the aggregate
      confidence and flagging below `verifier_confidence_threshold`.
    - `answer_shape_gate_enabled`: two deterministic scans over the finished text — for method
      parameters no tool in this turn produced, and for tools the answer *names* but never called.
      Both flag outright rather than moving a confidence, because neither is a measure of anything:
      each either found something or did not. Off by default and a deployment decision rather than
      a default-on behaviour, because the shape half is a heuristic that both misses and over-fires
      (see `ungrounded_parameter_shapes`) and the naming half has its own honest false positive —
      an answer *about* the toolset — and an answer marked for review that did not need it costs a
      chemist trust in every mark that follows. They share one knob because they share a purpose:
      catching in the text what an instruction failed to prevent in the generation.

    `tool_outputs` is what the turn's tools actually returned, untruncated, and it is the whole
    point of both checks. Verification used to re-resolve the answer's citations from the graph on
    disk, which asked whether a cited note exists rather than whether this turn saw it.

    Neither check may sink the turn: a verifier failure degrades to the unscored answer.
    """
    confidence: float | None = None
    verified_by: Literal["judge", "citation-gate"] | None = None
    unsupported: list[str] = []
    review = False
    if settings.verifier_enabled:
        try:
            result = await verify_turn_answer(answer, tool_outputs)
        except Exception:
            # A check that was configured on and did not complete must not be indistinguishable
            # from one that ran and passed. Leaving `review` False here made a crashed verification
            # emit the same routing flag as a clean verdict — the outer twin of the degrade defect
            # below, with two nested guards and neither marking the result.
            logger.exception("answer verification crashed; routing the turn to review")
            unsupported = ["verification did not run"]
            review = True
        else:
            confidence = result.confidence
            verified_by = result.verified_by
            unsupported = [claim.text for claim in result.unsupported]
            review = result.confidence < settings.verifier_confidence_threshold
            # The citation gate scores *resolvability* and the judge scores *faithfulness*, so when
            # the judge is unreachable the substitute answers a different question — and answers it
            # more generously: measured, the same cited-but-contradicted answer scored 1.0/supported
            # degraded against 0.0/unsupported judged. A verdict that could not be taken must not
            # clear the review gate on the strength of a check that was never run.
            if result.verified_by != "judge" and answer.strip():
                # A reason, not a bare flag. `confidence` is the citation gate's score and the
                # event documents the flag as "confidence fell below the threshold", so a 1.0 next
                # to a review affordance and an empty `unsupported_claims` is a contradiction a
                # reviewer cannot act on. `verified_by` carries the detail; this carries the why.
                #
                # `answer.strip()` because an empty turn already emits its own `empty_answer`
                # error event, and "review this empty answer, maximum confidence" is not a
                # judgement anyone can use.
                unsupported = [
                    *unsupported,
                    "verified by the citation gate only; the judge did not run",
                ]
                review = True
    if settings.answer_shape_gate_enabled:
        shapes = [
            *ungrounded_parameter_shapes(answer, tool_outputs),
            *promised_uncalled_tools(answer, tools_called),
        ]
        if shapes:
            # WARNING because this is the signal an operator tunes the gate on — how often it
            # fires, and on what — and the matched text is in the message so a false positive is
            # diagnosable without reading the transcript. A counter would be the better home for
            # the rate, but `core/metrics` refuses an undeclared name and declaring one is a
            # cross-package edit this change does not own.
            logger.warning(
                "answer marked for review: claims no tool in this turn supports (%s)",
                "; ".join(shapes),
            )
            unsupported = [*unsupported, *shapes]
            review = True
    return AnswerEvent(
        text=answer,
        confidence=confidence,
        verified_by=verified_by,
        unsupported_claims=unsupported,
        review_required=review,
    )
