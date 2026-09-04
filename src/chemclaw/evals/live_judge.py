"""Grade a live answer against its probe's `direction`, using a model as the judge.

Only the part that genuinely needs judgement comes here. Whether a tool was called, whether the
turn failed loudly, whether a cited note id was ever retrieved — all of that is decided
mechanically in `chemclaw.evals.live` from the event stream, because a mechanical signal cannot be
argued with. What is left is the one question a stream cannot answer: *did this answer serve the
person who asked?*

Two deliberate choices.

**The judge is a different, stronger model than the agent under test**
(`model_routes["live-probe-judge"]`). Grading is one call per probe against the agent's many, so the
quality is nearly free; and a judge sharing the agent's blind spots would ratify them — which is why
an unset route is a WARNING rather than a silent fallback to the agent's own model.

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
from collections.abc import Mapping
from functools import cache
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.probe import Probe

logger = logging.getLogger(__name__)

# `ungraded` is not a grade — it is the absence of one, and it exists because the first version of
# this module did not have it. A truncated or unparseable judge reply fell through to `unserved`,
# so a *grading crash* was recorded as a *system failure*, indistinguishable in the output from a
# real one. That mislabelled 65 of 190 probes in the first run and inflated the headline
# unserved rate from at most 22 to 87. A verdict that cannot be obtained must be visibly missing.
Verdict = Literal["served", "partial", "unserved", "fabricated", "ungraded"]

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
    """The grading payload: the ask, the bar, the forbidden list, and what came back.

    The tool *results* are here, not just the tool names, and that is the difference between a
    judge that can tell a retrieved number from an invented one and a judge that guesses. The
    first version passed names alone and called verbatim quotations from merged notes
    "fabricated" at a 40% false-positive rate — it had no way to see that the number was in the
    evidence. `uncited_note_ids` is passed for the same reason: it is the mechanical answer to the
    citation question, and the judge should defer to it rather than re-derive it from prose.
    `verified_numbers` is the same move for figures, and it exists because fixing the ids alone did
    not stop the grader calling verbatim tool output invented: it went on writing "the tool results
    shown are truncated previews that do not display the numerical limits" about six ICH PDEs the
    tool had returned in full. It is presented as a whitelist and labelled as one — the harness can
    prove a figure is in the evidence and cannot prove the reverse, because subtraction, the
    question itself and textbook constants all produce numbers no tool returned (`_verified_numbers`
    has the measurement).

    It also cannot prove the *sentence*, and the heading says so because the live data contains the
    case. gr-18 quoted a dipole of 5.67 D for a para-CF₃ sulfonyl fluoride whose SMILES it printed;
    the tool had been called on the *meta* isomer, which really does return 5.67, while the para
    one returns 1.86. Every figure was verbatim and the comparison it was built into was not, so a
    heading claiming more than "this value came back" would launder that.

    **Telling the judge to trust a signal obliges us to say what the signal can see.** It did not,
    and both halves went wrong at once. The list was derived from the same truncated previews the
    prompt warns are weak evidence, so "trust this over your own reading" was an instruction to
    trust a broken number — and a grader duly escalated it, reporting four ids as "mechanically
    verified as absent from the corpus" when all four were on disk and all four came back from a
    single retrieval call. `_score_citations` now reads the untruncated ids, and the heading says
    in the prompt itself what the signal does and does not claim, because a caveat the judge has to
    infer is a caveat the judge will not apply (`docs/archive/live-grounded-2026-08-03.md`).
    """
    forbidden = "\n".join(f"  - {claim}" for claim in probe.forbids_claims) or "  (none)"
    tools = ", ".join(outcome.tools_called) or "(none)"
    evidence = "\n".join(f"  [{p.tool}] {p.preview}" for p in outcome.tool_results) or "  (none)"
    uncited = ", ".join(outcome.uncited_note_ids) or "(none detected)"
    verified = ", ".join(outcome.verified_numbers) or "(none matched)"
    return (
        f"BUCKET: {probe.bucket}\n"
        f"PERSONA: {probe.persona}\n"
        f"QUESTION:\n{probe.question}\n\n"
        f"DIRECTION (what a satisfying answer looks like):\n{probe.direction}\n\n"
        f"MUST NOT ASSERT:\n{forbidden}\n\n"
        f"TOOLS THE SYSTEM ACTUALLY CALLED: {tools}\n\n"
        f"WHAT THOSE TOOLS RETURNED (evidence the answer was entitled to use; previews are\n"
        f"truncated, so absence here is NOT proof a number was invented):\n{evidence}\n\n"
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


# What an endpoint calls a reply that stopped because it ran out of budget. Two spellings, for the
# reason `agent/llm_provider._CONTEXT_LENGTH_MARKERS` keeps two: an OpenAI-compatible gateway says
# `length`, and one that relays a vendor's own field verbatim can say `max_tokens`. LangChain does
# not normalise this — it forwards whatever `finish_reason`/`stop_reason` the response carried — so
# recognising both is cheaper than being wrong about which gateway a site runs.
#
# **Missing it does not fabricate a verdict, and that is deliberate belt-and-braces.** A truncated
# reply has no closing brace, so the JSON parse below fails and the probe is already `ungraded`
# rather than `unserved`; this exists to say *why* in the reason, and to catch the case where a
# reply is cut after a syntactically complete object. Conflating the two mislabelled 65 of 190
# probes once, which is the whole reason `ungraded` is a verdict.
_TRUNCATED = frozenset({"length", "max_tokens"})


def _truncated(response: Any) -> bool:
    """Whether the judge's reply was cut off by its own token ceiling."""
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    return any(metadata.get(key) in _TRUNCATED for key in ("finish_reason", "stop_reason"))


@cache
def _judge_client() -> Any:
    """The judge's chat model, from the one seam that builds one — built once per process.

    `@cache`d for the reason `agent/verifier._default_client` is: construction is pure config, a
    run grades ~190 probes, and rebuilding the client per probe would redo TLS and transport setup
    on every one. It also fixes the frequency of the warning below — a property of the *run*, said
    once, rather than 190 identical lines.

    Routed rather than named: `build_chat_model` resolves `model_routes["live-probe-judge"]`
    against the gateway, so this module names no model and imports no provider client
    (`D-2026-09-04-a-gateway-is-the-only-provider` — it was the last first-party importer of the
    `anthropic` SDK, posting that vendor's protocol to `<gateway>/v1/messages`, which against an
    OpenAI-compatible gateway is a doubled path and a 404 degraded to `ungraded` on every probe).

    An unset route falls back to `llm_model` — the model under test — which quietly turns the run
    into self-grading. That is the one property of this judge worth a log line, so it gets one.

    `max_tokens` is bound rather than configured, because the seam's ceiling is the *agent's*
    answer allowance and this call needs its own: at 1024 the judge ran out of budget mid-JSON on
    long answers, the closing brace was never emitted, and the parse failure was recorded as a
    verdict of `unserved` on 65 of 190 probes.
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
    return Judgement(
        probe_id=probe.id,
        verdict=payload.get("verdict", "ungraded"),
        reason=str(payload.get("reason", "")),
        fabricated_claims=[str(c) for c in payload.get("fabricated_claims", [])][:10],
    )


def judgement_from_transcript(payload: dict[str, object]) -> tuple[Probe, ProbeOutcome]:
    """Rehydrate one stored transcript so it can be re-graded without re-running the probe.

    Re-grading has to be possible offline. The first run's verdicts were wrong for a reason that
    had nothing to do with the system under test, and re-asking 190 live questions to correct a
    grader bug would have changed the thing being measured as well as the measurement.
    """
    return (
        Probe.model_validate(payload["probe"]),
        ProbeOutcome.model_validate(payload["outcome"]),
    )
