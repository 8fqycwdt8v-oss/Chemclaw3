"""Stop a turn re-asking a tool the identical question it already answered.

**The measurement.** A live full-stack run (2026-08-04) recorded `find_past_jobs` called **7-8
times in a single turn** across three separate probes — the same tool, the same arguments, the same
answer — alongside `load_skill` x6 and `find_notes` x5. Every call was cheap on its own, which is
why nothing failed; what it cost was the turn. Median turn ran 128-142 s against 16.9 s on the
archived comparison run, and each repeat also spends its result back into the context window, so a
turn that loops on a fruitless retrieval crowds out the evidence it needed to answer from.

A repeat is not a bug in any tool. It is the model doing the one thing a tool call cannot tell it is
useless: `find_past_jobs` returning nothing looks, from the model's side, exactly like a call that
has not been made yet. So the correction belongs in the loop, not in the tool.

**Why this refuses rather than caches.** Serving the first call's result would be faster still, and
wrong: `get_durable_job_status` is read-only and legitimately changes *within* one turn, so a cached
answer would pin a job at "running" for a model that was correctly re-checking it. Refusing never
fabricates and never goes stale — it says what happened and hands the decision back.

**Why the third call and not the second.** One re-check is a real pattern (a job polled after a
wait, a note re-read after a write). Seven is a loop. `max_identical_tool_calls` is the boundary
and defaults to 2, so a legitimate re-check still goes through and the measured shape does not.

The carrier is a contextvar, for the reasons `chemclaw.core.turn_signals` gives for its buffer: it
is task-local (concurrent turns on one worker cannot see each other's calls), empty off the request
path (CLI, tests, the classic agent), and mutated rather than rebound, so it is visible even when
the agent's stream is driven from a task of its own.
"""

import json
import logging
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from langchain.agents.middleware import wrap_tool_call

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

_calls: ContextVar[Counter[tuple[str, str]] | None] = ContextVar(
    "chemclaw_repeated_calls", default=None
)


class RepeatedCallRefusal(ChemclawError):
    """A tool was asked the identical question once too often in one turn.

    A `ChemclawError` so the two mechanisms that already exist do the work: the audit middleware
    records it as an `error` outcome, and `surface_domain_errors` hands the message to the model
    verbatim instead of an opaque "Function failed." — which matters more here than anywhere
    else, since the whole point is to tell the model something it can act on.
    """


def begin_call_watch() -> object:
    """Start counting this turn's tool calls; returns a token for `end_call_watch`."""
    return _calls.set(Counter())


def end_call_watch(token: object) -> None:
    """Tear the turn's counter down (mirrors every other ambient's reset)."""
    _calls.reset(token)  # type: ignore[arg-type]


def _key(name: str, arguments: Any) -> tuple[str, str]:
    """A call's identity: its tool and its arguments, canonicalized so key order cannot fork it.

    `sort_keys` because a model that emits the same call twice is under no obligation to serialize
    its arguments in the same order, and two spellings of one question are one question.

    `default=str` because a hand-written tool declares a pydantic model rather than a JSON object
    (`start_optimization_campaign(spec: CampaignSpec)`), and `json.dumps` refuses one. Rendering it
    keeps the guard total: a middleware that raised on the argument shape half this system's tools
    use would fail the calls it exists to protect, which is worse than the repetition. There is no
    fallback beyond it because there is nothing left to fall back from — tool arguments are either
    a decoded JSON object or a pydantic model, and neither can be circular.
    """
    return (name, json.dumps(arguments, sort_keys=True, default=str))


def count_call(name: str, arguments: Any) -> RepeatedCallRefusal | None:
    """Count this call and return the refusal it has earned, or `None` to let it through.

    The decision, framework-free, so there is one counter, one threshold and one sentence however
    the plumbing around it is written. Splitting it would let a turn's repeat budget drift — and the
    number that matters here was measured (7–8 identical `find_past_jobs` calls in one turn, a
    median of 128–142 s against 16.9 s), so a second copy free to drift from it would quietly undo
    the finding.

    **Counting is the side effect**, and it happens before the threshold test, so a call that is
    let through is still recorded against the next one. Off the request path there is no counter
    and this is a no-op — the CLI, the tests and the classic agent all take that branch.
    """
    counts = _calls.get()
    if counts is None:
        return None
    key = _key(name, arguments)
    counts[key] += 1
    seen = counts[key]
    if seen <= settings.max_identical_tool_calls:
        return None
    logger.info("refusing repeat %d of %s in one turn", seen, name)
    record_metric(
        lambda m: m.increment("chemclaw_repeated_tool_calls_total", labels={"tool": name})
    )
    return RepeatedCallRefusal(
        f"{name} was already called with these exact arguments {seen - 1} time(s) in this turn "
        f"and returned the same thing each time, so it was not called again. It will not answer "
        f"differently — change the arguments, use a different tool, or answer from what you "
        f"already have (saying plainly if it is not enough)."
    )


@wrap_tool_call
async def refuse_repeated_calls(request: Any, handler: Callable[[Any], Any]) -> Any:
    """The LangGraph wiring of `refuse_repeated_calls` — same counter, same threshold, same words.

    Raised rather than returned as a `ToolMessage`, unlike the gates in `tool_authz`: a
    `RepeatedCallRefusal` is a `ChemclawError`, so `surface_domain_errors` is what turns it into
    the message the model reads. That keeps one converter responsible for how a refusal reaches the
    model, instead of this gate having its own opinion about it.
    """
    refusal = count_call(request.tool_call["name"], request.tool_call.get("args"))
    if refusal is not None:
        raise refusal
    return await handler(request)
