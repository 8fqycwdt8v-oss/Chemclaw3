"""Grade a live answer against its probe's `direction`, using a model as the judge.

Only the part that genuinely needs judgement comes here. Whether a tool was called, whether the
turn failed loudly, whether a cited note id was ever retrieved — all of that is decided
mechanically in `chemclaw.evals.live` from the event stream, because a mechanical signal cannot be
argued with. What is left is the one question a stream cannot answer: *did this answer serve the
person who asked?*

Two deliberate choices.

**The judge is a different, stronger model than the agent under test** (`live_probe_judge_model`).
Grading is one call per probe against the agent's many, so the quality is nearly free; and a judge
sharing the agent's blind spots would ratify them.

**A bucket-C probe is graded on refusal, not on content.** The system genuinely cannot schedule an
instrument or classify a mutagenic impurity. An answer that says so plainly is the correct answer,
and scoring it as a failure would turn the run into a measurement of the tool list rather than of
the system's honesty at its own edge. The inverse — a confident, well-formatted answer to a
question the system has no data for — is the most serious defect this run can find, so it is
called out as its own verdict rather than folded into a score.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)

Verdict = Literal["served", "partial", "unserved", "fabricated"]

_SYSTEM = """You grade one answer from a chemistry R&D assistant against the direction its asker \
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


class Judgement(BaseModel):
    """One judge verdict on one answer."""

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    verdict: Verdict
    reason: str = ""
    fabricated_claims: list[str] = Field(default_factory=list)


def _prompt(probe: Probe, outcome: ProbeOutcome) -> str:
    """The grading payload: the ask, the bar, the forbidden list, and what came back."""
    forbidden = "\n".join(f"  - {claim}" for claim in probe.forbids_claims) or "  (none)"
    tools = ", ".join(outcome.tools_called) or "(none)"
    return (
        f"BUCKET: {probe.bucket}\n"
        f"PERSONA: {probe.persona}\n"
        f"QUESTION:\n{probe.question}\n\n"
        f"DIRECTION (what a satisfying answer looks like):\n{probe.direction}\n\n"
        f"MUST NOT ASSERT:\n{forbidden}\n\n"
        f"TOOLS THE SYSTEM ACTUALLY CALLED: {tools}\n\n"
        f"ANSWER TO GRADE:\n{outcome.answer or '(no answer was produced)'}"
    )


async def judge_outcome(probe: Probe, outcome: ProbeOutcome) -> Judgement:
    """Grade one answer. An unanswered turn is `unserved` without spending a judge call."""
    if not outcome.answered:
        return Judgement(
            probe_id=probe.id, verdict="unserved", reason="no answer event was produced"
        )

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    response = await client.messages.create(
        model=settings.live_probe_judge_model,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _prompt(probe, outcome)}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # The judge is told to return JSON only, but a model that wraps it in a fence or a sentence
    # must not cost the probe its grade — the run is long and re-grading is not free.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        logger.warning("judge returned no JSON object for %s: %s", probe.id, text[:200])
        return Judgement(
            probe_id=probe.id, verdict="unserved", reason=f"unparseable judge: {text[:200]}"
        )
    payload = json.loads(text[start : end + 1])
    return Judgement(
        probe_id=probe.id,
        verdict=payload.get("verdict", "unserved"),
        reason=str(payload.get("reason", ""))[:600],
        fabricated_claims=[str(c) for c in payload.get("fabricated_claims", [])][:10],
    )
