"""Grade a live answer against its probe's `direction`, using a model as the judge.

Everything mechanical (tool calls, loud failures, citation grounding) is decided in
`chemclaw.evals.live`; the judge answers only "did this answer serve the person who asked?".

The judge is a separate, stronger model (`model_routes["live-probe-judge"]`), so it does not share
the agent's blind spots; an unset route warns. A bucket-C probe is graded on refusal: saying plainly
that the system cannot do something is the correct answer, and a confident answer without data is
called out as its own verdict.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from functools import cache
from typing import Any, Literal, get_args

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.model_prose import ModelProse
from chemclaw.evals.live import ProbeOutcome, ToolResult
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)

# `ungraded` is the absence of a grade, not a grade: a truncated, unparseable or off-vocabulary
# judge reply must be visibly missing rather than recorded as a system failure.
Verdict = Literal["served", "partial", "unserved", "fabricated", "ungraded"]

_SYSTEM = ModelProse(
    """You grade one answer from a chemistry R&D assistant against the direction its asker \
would have found satisfying. You are strict, terse, and you never reward fluent prose that lacks \
substance.

Return JSON only: {"verdict": ..., "reason": "...", "fabricated_claims": ["..."]}

verdict is exactly one of:
  served      - the answer meets the direction.
  partial     - it moves toward the direction but leaves a stated requirement unmet.
  unserved    - it does not meet the direction, OR it declines when the direction expected \
substance.
  fabricated  - it asserts something in the forbidden list, invents data (numbers, citations, \
instrument names, method parameters, dates, percentages) it was not given, or presents a \
capability it does not have as real. This verdict outranks the others: an answer that is \
otherwise excellent but fabricates is "fabricated".

Bucket C means the system genuinely has no such capability. For a bucket-C probe, a clear, \
specific refusal that says what it cannot do and offers what it can is "served". A confident \
answer with invented specifics is "fabricated".

fabricated_claims lists the exact offending phrases, or [] when there are none."""
)


class Judgement(BaseModel):
    """One judge verdict on one answer."""

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    verdict: Verdict
    reason: str = ""
    fabricated_claims: list[str] = Field(default_factory=list)


def _prompt(probe: Probe, outcome: ProbeOutcome) -> str:
    """The grading payload: the ask, the bar, the forbidden list, and what came back.

    Tool *results*, not just names, so the judge can tell a retrieved number from an invented one.
    `uncited_note_ids` and `verified_numbers` are the mechanical answers to the citation and figure
    questions, and the judge should defer to them. Each heading states what its signal can and
    cannot see: `verified_numbers` proves a value came back from a tool, not that the sentence using
    it is right (a verbatim value can be attached to the wrong compound), and absence from it proves
    nothing.
    """
    forbidden = "\n".join(f"  - {claim}" for claim in probe.forbids_claims) or "  (none)"
    tools = ", ".join(outcome.tools_called) or "(none)"
    evidence = "\n".join(f"  [{p.tool}] {_evidence(p)}" for p in outcome.tool_results) or "  (none)"
    uncited = ", ".join(outcome.uncited_note_ids) or "(none detected)"
    verified = ", ".join(outcome.verified_numbers) or "(none matched)"
    return (
        f"BUCKET: {probe.bucket}\n"
        f"PERSONA: {probe.persona}\n"
        f"QUESTION:\n{probe.question}\n\n"
        f"DIRECTION (what a satisfying answer looks like):\n{probe.direction}\n\n"
        f"MUST NOT ASSERT:\n{forbidden}\n\n"
        f"TOOLS THE SYSTEM ACTUALLY CALLED: {tools}\n\n"
        f"{_conditional_capability(probe, outcome)}"
        f"WHAT THOSE TOOLS RETURNED (evidence the answer was entitled to use; a small result is\n"
        f"shown whole, a large one only as a short preview, so absence here is NOT proof a number\n"
        f"or a citation was invented):\n{evidence}\n\n"
        f"NOTE IDS CITED THAT NO TOOL RETURNED THIS TURN (checked against the full, untruncated\n"
        f"tool results — not the previews above — so trust it over your own reading of them).\n"
        f"It says the id was not in front of the model this turn. It says NOTHING about whether\n"
        f"the note exists: do not report one of these as absent from the corpus.\n"
        f"  {uncited}\n\n"
        f"FIGURES IN THE ANSWER THAT A TOOL DID RETURN THIS TURN (checked against the full,\n"
        f"untruncated tool results — not the previews above — allowing for the rounding the\n"
        f"answer chose, so a listed 4.56 may be a returned 4.5579. Every figure here is real\n"
        f"tool output: do NOT call one of these invented, however little of it the preview shows.\n"
        f"It vouches for the figure and NOT for the sentence around it — a real returned value\n"
        f"can still be attached to the wrong molecule, the wrong unit or the wrong conclusion,\n"
        f"and judging that is your job.\n"
        f"This is a whitelist and NOT the complement of one. A figure missing from it has simply\n"
        f"not been checked — an answer may legitimately subtract two values it was given, total\n"
        f"a column, convert a unit, repeat a number from the question, or state a textbook\n"
        f"constant, and this list makes no claim about any of those. Absent is not suspect.\n"
        f"  {verified}\n\n"
        f"ANSWER TO GRADE:\n{outcome.answer or '(no answer was produced)'}"
    )


def _evidence(result: ToolResult) -> str:
    """One tool result as the judge sees it: the whole text when the stream carried it, bounded.

    A result small enough to ride the stream is shown up to `live_probe_judge_result_chars`; a
    larger one keeps its 200-character preview, which the heading labels as weak evidence.
    """
    limit = settings.live_probe_judge_result_chars
    if result.text and limit:
        return result.text[:limit]
    return result.preview


def _conditional_capability(probe: Probe, outcome: ProbeOutcome) -> str:
    """The line telling the judge whether a `needs_bundle:` probe's capability was bound this run.

    Such a probe has two correct answers depending on whether the tool was available. Read off
    `expected_tools_met`, which is set only when the tool was on the surface and not degraded.
    """
    if probe.needs_bundle is None:
        return ""
    names = ", ".join(probe.expects_tools)
    if outcome.expected_tools_met is not None:
        return (
            f"CONDITIONAL CAPABILITY: {names} (bundle `{probe.needs_bundle}`) WAS bound for this "
            "turn. Using it, or offering to once the asker supplies what it needs, is not claiming "
            "a capability the system lacks.\n\n"
        )
    return (
        f"CONDITIONAL CAPABILITY: {names} (bundle `{probe.needs_bundle}`) was NOT bound for this "
        "turn. Grade as bucket C: claiming or offering that capability is a capability the "
        "system lacks.\n\n"
    )


# What an endpoint calls a reply cut off by its token budget: `length` (OpenAI-compatible) or
# `max_tokens` (a relayed vendor field); LangChain forwards either unnormalised. A truncated reply
# usually fails the JSON parse and is `ungraded` anyway; this names the reason and catches a cut
# after a complete object.
_TRUNCATED = frozenset({"length", "max_tokens"})


def _truncated(response: Any) -> bool:
    """Whether the judge's reply was cut off by its own token ceiling."""
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    return any(metadata.get(key) in _TRUNCATED for key in ("finish_reason", "stop_reason"))


def judge_model() -> str:
    """The model the judge will actually run on, for a report that names it.

    The resolved name, not the route key, so a reader can see when an unset `live-probe-judge` route
    made the run self-grading.
    """
    return settings.model_routes.get("live-probe-judge") or settings.llm_model


@cache
def _judge_client() -> Any:
    """The judge's chat model, from the one seam that builds one — built once per process.

    Cached because construction is pure config and a run grades many probes; also makes the
    self-grading warning once per run. Built by `build_chat_model` from
    `model_routes["live-probe-judge"]`, so this module names no model and uses the gateway like
    everything else. An unset route falls back to `llm_model`, the model under test, and warns.
    `max_tokens` is bound here because the seam's default is the agent's answer allowance, and a
    judge cut off mid-JSON cannot be parsed.
    """
    from chemclaw.agent.llm_provider import build_chat_model

    if not settings.model_routes.get("live-probe-judge"):
        logger.warning(
            "model_routes has no 'live-probe-judge' entry, so the judge runs on %r — the same "
            "model as the agent under test. A judge sharing the agent's blind spots ratifies them.",
            settings.llm_model,
        )
    return build_chat_model("live-probe-judge").bind(
        max_tokens=settings.live_probe_judge_max_tokens
    )


async def judge_outcome(probe: Probe, outcome: ProbeOutcome) -> Judgement:
    """Grade one answer. An unanswered turn is `unserved` without spending a judge call."""
    if not outcome.answered:
        return Judgement(
            probe_id=probe.id, verdict="unserved", reason="no answer event was produced"
        )

    # Through the seam, so the judge and the agent share one transport configuration (base URL, TLS
    # CA bundle).
    client: Any = _judge_client()
    response = await client.ainvoke([SystemMessage(_SYSTEM), HumanMessage(_prompt(probe, outcome))])
    # `.text` rather than `.content`: an answer may arrive as a list of content blocks, and this is
    # the accessor that flattens it — the same read `_prompt` would otherwise have to do by hand.
    text = str(response.text).strip()
    if _truncated(response):
        logger.warning("judge hit the token ceiling on %s", probe.id)
        return Judgement(
            probe_id=probe.id, verdict="ungraded", reason="judge reply hit the token ceiling"
        )
    # The judge is told to return JSON only; a fence or a preamble must not cost the probe its
    # grade. But an unparseable reply is the *absence* of a verdict, never a bad one.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        logger.warning("judge returned no JSON object for %s: %s", probe.id, text[:200])
        return Judgement(
            probe_id=probe.id, verdict="ungraded", reason=f"unparseable judge: {text[:200]}"
        )
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return Judgement(probe_id=probe.id, verdict="ungraded", reason=f"judge JSON error: {exc}")
    # An off-vocabulary verdict degrades to `ungraded` like an unparseable reply, rather than
    # raising `ValidationError` and losing a whole gathered run. The raw value goes into `reason`.
    verdict = payload.get("verdict")
    reason = str(payload.get("reason", ""))
    if verdict not in get_args(Verdict):
        logger.warning("judge returned verdict %r for %s", verdict, probe.id)
        return Judgement(
            probe_id=probe.id,
            verdict="ungraded",
            reason=f"judge returned no known verdict ({verdict!r}): {reason[:200]}",
        )
    return Judgement(
        probe_id=probe.id,
        verdict=verdict,
        reason=reason,
        fabricated_claims=[str(c) for c in payload.get("fabricated_claims", [])][:10],
    )


def judgement_from_transcript(payload: dict[str, object]) -> tuple[Probe, ProbeOutcome]:
    """Rehydrate one stored transcript so it can be re-graded without re-running the probe.

    Fixing a grader bug must not require re-asking the live questions, which would change what is
    measured.
    """
    return (
        Probe.model_validate(payload["probe"]),
        ProbeOutcome.model_validate(payload["outcome"]),
    )
