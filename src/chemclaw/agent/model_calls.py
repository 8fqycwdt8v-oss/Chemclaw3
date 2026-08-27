"""What happened at the model call — the one boundary in this system that recorded nothing.

Two middlewares, both `wrap_model_call`, because the model call is where two separate blind spots
met. Neither is a policy: remove them and a chemist gets the same answers.

**`RecordModelCalls` — there was no LLM error taxonomy anywhere.** No metric, no log, no span named
a provider failure. `agent/llm_provider.py` constructs a client and returns it, and nothing wrapped
what the client then did, so three distinct outages were one silence:

- `max_retries=settings.llm_max_retries` (3 by default) is passed *into* the provider SDK, so a
  single `ainvoke` covers up to four wire attempts with no callback and no log. A deployment
  retrying every call three times looked identical to one retrying none.
- A provider 429 had no counter of its own — the only rate-limit series in the process belonged to
  the front door's *inbound* limiter, which is a different actor being throttled by a different
  party.
- A context-length `BadRequestError` fell through `api/runner._classify` to `("internal", False)`,
  so the one failure mode `agent/compaction.py` exists to prevent was unmeasurable, and the chemist
  was told "internal error, do not retry" about the one failure a shorter question fixes.

The taxonomy itself is not invented here. `llm_provider._failover_exceptions` has always known
which failures mean "this endpoint is down", because failover depends on the distinction;
`llm_provider.classify_model_failure` reuses that set and names the neighbouring families beside
it, so there is one statement of what a provider failure *is*.

**What this still cannot see, stated rather than implied.** The SDK's own retries happen below
`ainvoke`, so one recorded call is between one and `llm_max_retries + 1` wire attempts and this
cannot say which. Making that observable means either `max_retries=0` plus a first-party retry
loop — which is re-implementing somebody else's tested backoff — or an `httpx` event hook on the
client this seam builds. Neither is done, so **the retry budget is configured and unmeasured**, and
a `timeout` or `transport` outcome here is the state *after* the SDK gave up rather than the first
failure.

A model call cancelled by a turn teardown is deliberately not counted: `CancelledError` is a
`BaseException`, the call did not fail, and the turn's own outcome series is where an abandoned
turn is recorded.

**`RepairInvalidToolCalls` — an unparseable tool call was a silent no-op.** LangChain puts a tool
call whose arguments do not parse onto `AIMessage.invalid_tool_calls` rather than `tool_calls`, and
nothing in `src/` read that field. The agent iterates `tool_calls`, so the call vanished: no
`tool_failed`, no `tool_result`, no audit row, no span. With no prose beside it the turn ended as
`empty_answer`; **with prose it proceeded as though no tool had been needed**, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` exactly. A truncated argument document
is what a real model emits when a stream is cut or a token budget runs out, so this is reachable in
production rather than only under a mock.

**It repairs from `wrap_model_call` and never jumps from `after_model`**, which is the constraint
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` leaves behind: a middleware
that jumps from `after_model` short-circuits every middleware that runs later, and the loop cap is
one of them. A jump back to the model from there would buy a correction by disarming the runaway
guard. Asking the model again from inside its own call has neither problem — the graph never sees
the discarded attempt, and the state it does see holds exactly one assistant message.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from chemclaw.agent.llm_provider import classify_model_failure
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import Metrics
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# What the model is told when its own tool call did not parse. Addressed to the model rather than to
# a log: it names each tool and the parse error verbatim (the error is the SDK's own sentence about
# the JSON, which is the only thing that identifies *where* the document broke), and it asks for the
# call again rather than for an apology, because the failure is a truncated argument document and
# the remedy is to re-emit it.
_CORRECTION = (
    "Your previous reply contained {count} tool call(s) whose arguments could not be parsed, so "
    "none of them ran and no results exist for them: {failures}. Re-issue the call(s) you still "
    "need with complete, valid JSON arguments. If you cannot, say what you were unable to do and "
    "continue with what you can — do not answer as though the tool had returned."
)


def model_call_middleware() -> list[Any]:
    """The two model-call observers, as the list `build_langgraph_agent` splices in.

    Order is nesting: the repair is outside the recorder, so a repaired turn books **two** model
    calls — which is what happened, and is the only way the cost of a malformed emission is
    visible. Spliced innermost of everything so the recorded duration is the provider call rather
    than the middleware above it: the context edits run in `wrap_model_call` too, and folding their
    token counting into `chemclaw_model_call_duration_seconds` would put first-party work into the
    histogram an operator reads as "how slow is the endpoint".
    """
    return [RepairInvalidToolCalls(), RecordModelCalls()]


def _observe(provider: str, outcome: str, seconds: float) -> None:
    """Book one finished model call: its outcome, and how long the provider took over it."""
    record_metric(
        lambda metrics: metrics.increment(
            "chemclaw_model_calls_total", labels={"provider": provider, "outcome": outcome}
        )
    )
    record_metric(
        lambda metrics: metrics.observe(
            "chemclaw_model_call_duration_seconds", seconds, labels={"provider": provider}
        )
    )


def _record_failure(provider: str, exc: BaseException, seconds: float) -> None:
    """Classify, count and log one failed model call, then let the caller re-raise.

    The WARNING carries the provider and the **exception class**, never the message: a provider's
    error text can quote the request, and the request is the chemist's question. The class and the
    outcome are what separate a rate limit from a dead endpoint from a thread that no longer fits,
    which is the whole distinction that was missing.
    """
    outcome = classify_model_failure(exc)
    _observe(provider, outcome, seconds)
    log_event(
        logger,
        "model.call_failed",
        "the %s endpoint failed after %.0f ms (%s: %s)",
        provider,
        seconds * 1000.0,
        outcome,
        type(exc).__name__,
        level=logging.WARNING,
        provider=provider,
        outcome=outcome,
        exception=type(exc).__name__,
        duration_ms=round(seconds * 1000.0, 1),
    )


class RecordModelCalls(AgentMiddleware[Any, Any, Any]):
    """Count and time every model call, by provider and by what went wrong.

    Both hooks declared, for the reason `agent/compaction.RecordContextCompaction` gives at length:
    `create_agent` puts a middleware declaring *either* into *both* chains, so an async-only
    middleware makes every synchronous `graph.invoke()` raise.

    Observation only — it re-raises whatever the model call raised, unchanged, so the front door's
    classification and the failover above it behave exactly as they did.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        """Record the call, then return what it produced (sync path)."""
        provider = settings.llm_provider
        start = time.perf_counter()
        try:
            response = handler(request)
        except Exception as exc:
            _record_failure(provider, exc, time.perf_counter() - start)
            raise
        _observe(provider, "ok", time.perf_counter() - start)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """Record the call — the path a turn actually takes."""
        provider = settings.llm_provider
        start = time.perf_counter()
        try:
            response = await handler(request)
        except Exception as exc:
            _record_failure(provider, exc, time.perf_counter() - start)
            raise
        _observe(provider, "ok", time.perf_counter() - start)
        return response


def invalid_tool_calls(response: Any) -> list[tuple[str, str]]:
    """`(tool name, parse error)` per tool call in `response` whose arguments did not parse.

    Reads the `AIMessage`s out of whichever shape the handler returned — LangChain lets a
    `wrap_model_call` handler answer with a `ModelResponse`, a bare `AIMessage` or an
    `ExtendedModelResponse`, and this must see the same thing on all three.

    The name falls back to `"unknown"` because `invalid_tool_calls` is exactly the case where the
    model's output was malformed: an entry can carry a parse error and no usable name, and a counter
    that dropped those would under-report the failure it exists to surface. It is a bounded literal,
    so the metric's label space stays the tool surface plus one.
    """
    messages: Sequence[BaseMessage]
    if isinstance(response, AIMessage):
        messages = [response]
    else:
        inner = getattr(response, "model_response", response)
        messages = getattr(inner, "result", None) or []
    return [
        (str(call.get("name") or "unknown"), str(call.get("error") or "arguments did not parse"))
        for message in messages
        if isinstance(message, AIMessage)
        for call in (message.invalid_tool_calls or [])
    ]


class RepairInvalidToolCalls(AgentMiddleware[Any, Any, Any]):
    """Ask again when the model emitted a tool call nobody could parse, and count that it did.

    One repair attempt, never a loop. The bound is deliberate and it is not a tuning knob: a second
    unparseable reply to a corrective instruction is a model or a budget problem, and asking a third
    time spends tokens on the same answer while the turn's own runaway cap — which counts in
    `before_model` and therefore does not see a retry taken from inside one model call — cannot
    bound it. The second attempt is returned whatever it holds, with an ERROR beside it, because
    returning the *first* attempt instead would be choosing the reply that is known to be broken.

    The corrective instruction is appended to the request only, so the discarded attempt never
    reaches graph state, the transcript or the checkpoint: what the session records is one assistant
    message, the one the model meant to send.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        """Repair once, then return (sync path — declared for `RecordModelCalls`'s reason)."""
        response = handler(request)
        failures = invalid_tool_calls(response)
        if not failures:
            return response
        _count_invalid(failures, attempt="first")
        repaired = handler(_retry_request(request, failures))
        _report_repair(invalid_tool_calls(repaired))
        return repaired

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """Repair once — the path a turn actually takes."""
        response = await handler(request)
        failures = invalid_tool_calls(response)
        if not failures:
            return response
        _count_invalid(failures, attempt="first")
        repaired = await handler(_retry_request(request, failures))
        _report_repair(invalid_tool_calls(repaired))
        return repaired


def _retry_request(
    request: ModelRequest[Any], failures: list[tuple[str, str]]
) -> ModelRequest[Any]:
    """The same request with the correction appended — `override`, so nothing is mutated."""
    described = "; ".join(f"{name} ({error})" for name, error in failures)
    correction = _CORRECTION.format(count=len(failures), failures=described)
    return request.override(messages=[*request.messages, HumanMessage(content=correction)])


def _bump_invalid(tool: str, metrics: Metrics) -> None:
    """Increment the unparseable-call counter for one tool.

    A named function bound with `partial` rather than a closure over the loop variable, which is
    the idiom `agent/audit_store.py` already uses: a `lambda` capturing `name` in a loop captures
    the *variable*, so every deferred update would book the last tool in the list.
    """
    metrics.increment("chemclaw_invalid_tool_calls_total", labels={"tool": tool})


def _count_invalid(failures: list[tuple[str, str]], *, attempt: str) -> None:
    """Count each unparseable call under its tool, and say so once per model call."""
    for name, _error in failures:
        record_metric(partial(_bump_invalid, name))
    log_event(
        logger,
        "model.invalid_tool_calls",
        "the model emitted %d tool call(s) with unparseable arguments (%s attempt): %s",
        len(failures),
        attempt,
        ", ".join(f"{name}: {error}" for name, error in failures),
        level=logging.WARNING,
        attempt=attempt,
        count=len(failures),
        # A comma-joined string rather than a list, because a log stack indexes scalars — the same
        # rule `log_event` states for every field it takes.
        tools=", ".join(sorted({name for name, _ in failures})),
    )


def _report_repair(failures: list[tuple[str, str]]) -> None:
    """Close the repair out: silence when it worked, an ERROR and a count when it did not."""
    if not failures:
        return
    _count_invalid(failures, attempt="second")
    log_event(
        logger,
        "model.invalid_tool_calls_unrepaired",
        "the model emitted unparseable tool-call arguments twice; the turn continues with the "
        "second reply, in which %d call(s) still cannot run",
        len(failures),
        level=logging.ERROR,
        count=len(failures),
    )
