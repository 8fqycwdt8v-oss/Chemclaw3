"""Stop a turn re-asking a tool the identical question it already answered.

A repeated call with the same arguments returns the same answer and spends context each time, so
past `max_identical_tool_calls` (default 2, allowing one legitimate re-check) the next identical
call is refused with a message the model can act on.

It refuses rather than caches: a cached answer could go stale within a turn, a refusal cannot.
Tools that poll moving state declare it (`core/tool_registry.polls_moving_state`) and are never
counted; their bound is the Temporal long-poll and the loop cap.

The counters live in a task-local contextvar, mutated rather than rebound, and are absent off the
request path (CLI, tests), where every function here is a no-op.
"""

import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import wrap_tool_call

from chemclaw.agent.audit import metric_tool_name
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.tool_registry import is_a_poll

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnCallWatch:
    """One turn's repeat bookkeeping: the counters, and what compaction already forgave.

    `forgiven` holds tool call ids, not `(name, arguments)` keys: compaction re-derives the cleared
    set on every model call, so forgiving by call id makes each cleared result forgive exactly one
    repeat, once, instead of resetting the counter on every model call.
    """

    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    forgiven: set[str] = field(default_factory=set)


_calls: ContextVar[TurnCallWatch | None] = ContextVar("chemclaw_repeated_calls", default=None)


class RepeatedCallRefusal(ChemclawError):
    """A tool was asked the identical question once too often in one turn.

    A `ChemclawError`, so the audit middleware records it as an `error` outcome and
    `surface_domain_errors` hands the message to the model verbatim.
    """


def begin_call_watch() -> object:
    """Start counting this turn's tool calls; returns a token for `end_call_watch`."""
    return _calls.set(TurnCallWatch())


def end_call_watch(token: object) -> None:
    """Tear the turn's counter down (mirrors every other ambient's reset)."""
    _calls.reset(token)  # type: ignore[arg-type]


def _key(name: str, arguments: Any) -> tuple[str, str]:
    """A call's identity: its tool and its arguments, canonicalized so key order cannot fork it.

    `default=str` renders pydantic-model arguments, keeping the guard total over every tool's
    argument shape.
    """
    return (name, json.dumps(arguments, sort_keys=True, default=str))


def forget_calls(cleared: Iterable[tuple[str, str, Any]]) -> None:
    """Clear the repeat counters for calls whose answers compaction just took away.

    After compaction replaces a result with a placeholder, an identical call is a re-read, not a
    repeat. Only the calls whose own results were cleared are forgiven, and each call id is forgiven
    at most once (see `TurnCallWatch`), because the caller re-derives the cleared set on every model
    call. A no-op off the request path.

    Args:
        cleared: `(tool call id, tool name, arguments)` per result replaced by a placeholder.
    """
    watch = _calls.get()
    if watch is None:
        return
    forgotten = 0
    for call_id, name, arguments in cleared:
        if call_id in watch.forgiven:
            continue
        watch.forgiven.add(call_id)
        if watch.counts.pop(_key(name, arguments), None) is not None:
            forgotten += 1
    if forgotten:
        logger.info("context was compacted; %d repeat counter(s) cleared", forgotten)


def count_call(name: str, arguments: Any) -> RepeatedCallRefusal | None:
    """Count this call and return the refusal it has earned, or `None` to let it through.

    The one framework-free decision: one counter, one threshold, one sentence. Counting happens
    before the threshold test, so a call let through still counts against the next. The `/metrics`
    series is recorded by `refuse_repeated_calls`, which holds the clamped tool name.
    """
    watch = _calls.get()
    if watch is None:
        return None
    counts = watch.counts
    key = _key(name, arguments)
    counts[key] += 1
    seen = counts[key]
    if seen <= settings.max_identical_tool_calls:
        return None
    logger.info("refusing repeat %d of %s in one turn", seen, name)
    return RepeatedCallRefusal(
        f"{name} was already called with these exact arguments {seen - 1} time(s) in this turn "
        f"and returned the same thing each time, so it was not called again. It will not answer "
        f"differently — change the arguments, use a different tool, or answer from what you "
        f"already have (saying plainly if it is not enough)."
    )


@wrap_tool_call
async def refuse_repeated_calls(request: Any, handler: Callable[[Any], Any]) -> Any:
    """The LangGraph wiring of `count_call` — same counter, same threshold, same words.

    Raises rather than returning a `ToolMessage`, so `surface_domain_errors` is the one
    converter that shapes the refusal for the model.

    It also records the metric, because the label must be clamped with `metric_tool_name`: the
    model-emitted name reaches this guard even for unregistered tools, and `/metrics` is
    unauthenticated, so a raw name would mint arbitrary series. The refusal text still names
    what the model asked for.
    """
    # A declared poll is not counted at all, rather than counted and let through: its identical
    # calls are the tool working, and a count of them would be a number about nothing.
    if is_a_poll(request.tool_call["name"]):
        return await handler(request)
    refusal = count_call(request.tool_call["name"], request.tool_call.get("args"))
    if refusal is None:
        return await handler(request)
    label = metric_tool_name(request, request.tool_call["name"])
    record_metric(
        lambda m: m.increment("chemclaw_repeated_tool_calls_total", labels={"tool": label})
    )
    raise refusal
