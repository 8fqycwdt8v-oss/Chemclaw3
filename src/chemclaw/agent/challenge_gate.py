"""The gate that decides an answer gets challenged, and what happens when the panel is right.

`agent/challenge.py` holds the panel; this holds the *control flow*. It is an `after_model` hook, so
it sits where the turn has just produced a candidate answer and the graph has not yet ended — the
only point at which a critique can still change what the chemist reads rather than merely annotate
it.

**The trigger is the shape of the turn, not only its confidence.**

- **Two or more helpers ran → challenge, unconditionally.** Work split across agents is exactly the
  case where no single context saw the whole thing: each helper reported on its own piece, the
  supervisor stitched the pieces together, and the seams are where a contradiction survives. The
  verifier scores one finished answer against one turn's evidence and has no instrument for that.
- **One helper or none → challenge only when already flagged.** Here the existing checks keep their
  meaning: `verifier_enabled` scores citation faithfulness and `answer_shape_gate_enabled` scans for
  parameters and promises no tool backed. When one of them fires, the panel is the second opinion
  they cannot give themselves.

The count comes from `team.delegations()` — a counted tally, not a shape read off the message list,
for `agent/loop_cap.py`'s reason: the alternative to counting is an inference, and the inference is
what breaks quietly.

**No LLM decides whether to be challenged.** The trigger is arithmetic on a tally and a boolean, and
`can_jump_to` makes the branch a real graph edge — `enforce_loop_cap` is the precedent for both, and
for the reason the declaration matters: without `can_jump_to` the hook still runs, still decides
correctly, and the graph ignores it, because the conditional edge is built from the declaration.

**What happens when the panel reaches quorum**, in order:

1. **Revise, while attempts remain.** The panel's objections go back to the model as
   `challenge_feedback` and the graph jumps to `model`. This is the whole point of running before
   the answer ships: a stated, specific objection is usually something the model can fix, and fixing
   it is better than telling the chemist it exists. Bounded by `challenge_max_attempts` against a
   counted state field, because the model and the panel can disagree forever.
2. **Surface it, once they do not.** The objections ride out on the answer (`review_required`, with
   each rationale in `unsupported_claims`) and a durable hold is opened so a human decision outlives
   the session.

**`interrupt()` is deliberately not used here, and that is a scope decision rather than an
oversight.** LangGraph's interrupt is the right primitive for "stop and ask the chemist mid-turn",
and it is available (`langgraph.types.interrupt`, and the checkpointer under it already exists). But
it stops the graph by raising, and resuming it needs a front-door route that sends
`Command(resume=…)` plus a surface that renders the question — neither of which exists. An interrupt
nothing can resume is a turn that hangs, which is strictly worse than the annotated answer plus a
durable hold this ships. `docs/planning/DEFERRED.md` carries the row; its trigger is the same one
already recorded there, a surface that renders a hold.
"""

import logging
from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware import after_model
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chemclaw.agent.challenge import (
    ChallengeVerdict,
    corroborated,
    draft_briefs,
    panel_quorum,
    record_turn_review,
    run_panel,
    start_answer_review,
)
from chemclaw.agent.profiles import AgentProfile
from chemclaw.agent.team import delegations
from chemclaw.agent.verifier import score_answer, turn_evidence
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# How many helpers make a turn a *team*. Two, because one helper is an agent doing a scoped piece of
# work and reporting back — the supervisor still saw the whole question — while two is the first
# point at which the answer is assembled from pieces no single context held.
_TEAM_SIZE = 2


def _final_answer(messages: Sequence[Any]) -> str | None:
    """The text of the turn's finished answer, or `None` if this is not the end of one.

    `None` for anything that is not an `AIMessage` carrying text and *no* tool calls: a message that
    requested tools is a step, not an answer, and challenging it would put a panel on a half-formed
    thought and then jump the graph back to a model that was mid-work.
    """
    if not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage) or last.tool_calls:
        return None
    text = last.text
    return text or None


def _question(messages: Sequence[Any]) -> str:
    """What the chemist asked this turn — the newest human message.

    Newest rather than first: a checkpointed thread carries every turn of the session, so the first
    `HumanMessage` is the question that opened the conversation and usually not the one being
    answered now. The panel is briefed on the wrong problem if this picks the wrong end.
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.text
    return ""


def _tool_outputs(messages: Sequence[Any]) -> list[str]:
    """Everything this turn's tools returned, untruncated, in call order.

    The in-graph equivalent of `api/runner_trace.ToolCallTrace.outputs`, read off the thread rather
    than off the stream because a middleware has the messages and not the events. Untruncated for
    that class's own stated reason: a grounding check run against a 200-character preview called 39
    of 40 citations fabricated in a live run.
    """
    return [m.text for m in messages if isinstance(m, ToolMessage) and m.text]


def _called_tools(messages: Sequence[Any]) -> list[str]:
    """Every tool this turn actually called, for the promised-but-uncalled scan."""
    return [
        call["name"]
        for m in messages
        if isinstance(m, AIMessage)
        for call in m.tool_calls
        if call.get("name")
    ]


def _feedback(upheld: Sequence[ChallengeVerdict]) -> str:
    """The panel's objections, worded for the model that has to answer them.

    Named by angle so the model can tell independent objections apart rather than reading one long
    complaint, and explicit that a disagreement is a legitimate response: a model bullied into
    "fixing" a correct answer by an over-confident challenger is the failure mode a revision loop
    introduces, and the instruction against it is the cheapest guard available.
    """
    points = "\n".join(
        f"- [{v.angle}] {v.rationale}"
        + (f" (claim at issue: {v.challenged_claim})" if v.challenged_claim else "")
        for v in upheld
    )
    return (
        "A review panel examined your answer independently and raised the following. Revise the "
        "answer to address each point — check the record again where that settles it, drop or "
        "qualify a claim you cannot support, and keep the citations accurate.\n\n"
        f"{points}\n\n"
        "If you believe an objection is mistaken, say so in the answer and state what supports "
        "your "
        "original claim. Do not weaken a correct answer to satisfy a reviewer."
    )


def build_challenge_gate(
    caller_profile: AgentProfile,
    **build_kwargs: Any,
) -> Any:
    """The `after_model` middleware that challenges this agent's answers.

    A factory rather than a module-level hook because the panel needs what only the graph builder
    holds — the turn's connectors, actor, correlation id and audit sink, and the profile every
    challenger is checked against. The four `wrap_tool_call` wrappers beside it read ambient state
    and need no factory; this one does, for `tool_authz.refuse_undeclared_writes`'s reason.

    Args:
        caller_profile: The answering agent's profile. Every challenger is an attenuation of it.
        **build_kwargs: Forwarded to each challenger's `build_langgraph_agent`.

    Returns:
        The middleware, ready to attach.
    """

    @after_model(can_jump_to=["model", "end"])
    async def challenge_answer(state: Any, runtime: Any) -> dict[str, Any] | None:
        """Challenge a finished answer; revise while attempts remain, else surface the objection."""
        messages = state.get("messages") or []
        answer = _final_answer(messages)
        if answer is None:
            return None
        outputs = _tool_outputs(messages)
        review = await score_answer(answer, outputs, _called_tools(messages))
        team = delegations() >= _TEAM_SIZE
        if not team and not review.review_required:
            # Nothing to add: a solo turn the existing checks were happy with. The score is still
            # published, so the runner stamps the event from it rather than re-judging the answer.
            record_turn_review(review)
            return None
        record_metric(lambda metrics: metrics.increment("chemclaw_challenge_rounds_total"))
        question = _question(messages)
        briefs = await draft_briefs(question, answer, turn_evidence(answer, outputs))
        verdicts = await run_panel(
            question,
            answer,
            briefs,
            caller_profile=caller_profile,
            **build_kwargs,
        )
        upheld = corroborated(verdicts)
        if len(upheld) < panel_quorum(len(briefs) or 1):
            # The panel looked and did not agree there is a problem. That is a result, not a
            # non-event: the answer keeps whatever the existing checks said about it and goes out.
            record_turn_review(review)
            return None
        record_metric(lambda metrics: metrics.increment("chemclaw_challenge_upheld_total"))
        attempts = int(state.get("challenge_attempts", 0))
        if attempts < settings.challenge_max_attempts:
            logger.info(
                "challenge upheld by %d/%d; revising (attempt %d)",
                len(upheld),
                len(verdicts),
                attempts + 1,
            )
            # **The critique goes back as a message, not as a state field**, and that is what makes
            # the revision happen at all: jumping to `model` re-runs it over `messages`, so a
            # critique parked anywhere else would leave the model looking at exactly the input that
            # produced the answer under objection — and produce it again. Appending is also the
            # honest record: the revision's reason stays in the thread the checkpointer persists,
            # where an auditor reading the session can see why the answer changed.
            # Published before the jump too: if the revision round then fails or the turn is torn
            # down, the runner still stamps the event with what was known rather than with nothing.
            # The next pass replaces it with the verdict on the revised answer.
            record_turn_review(review)
            return {
                "jump_to": "model",
                "challenge_attempts": attempts + 1,
                "messages": [HumanMessage(content=_feedback(upheld))],
            }
        # Out of revisions and the panel still objects. The answer goes out marked, and a durable
        # hold carries the decision past the end of this session.
        logger.warning(
            "challenge upheld by %d/%d after %d revision(s); surfacing",
            len(upheld),
            len(verdicts),
            attempts,
        )
        review.unsupported = [*review.unsupported, *(f"[{v.angle}] {v.rationale}" for v in upheld)]
        review.review_required = True
        review.challenged = True
        review.hold_id = await start_answer_review(answer, upheld)
        record_turn_review(review)
        return None

    return challenge_answer
