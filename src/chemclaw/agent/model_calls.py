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
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` exactly.

**What reaches that field is genuinely malformed JSON, and not — as this said — a truncated
document.** LangChain runs a streamed tool call's argument fragments through `parse_partial_json`,
which closes an unterminated string and an unclosed brace, so the cut stream this docstring named
as the reachable case repairs itself before anything sees it: measured, `'{"smiles": "CC'` arrives
as a valid entry on `tool_calls` with `invalid_tool_calls` empty. The field is reached by output
that is not JSON at all, or that is JSON-shaped and broken in a way partial parsing cannot close —
which a model emits rarely and does emit, and which nothing recovered from.

**It repairs from `wrap_model_call` and never jumps from `after_model`**, which is the constraint
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` leaves behind: a middleware
that jumps from `after_model` short-circuits every middleware that runs later, and the loop cap is
one of them. A jump back to the model from there would buy a correction by disarming the runaway
guard. Asking the model again from inside its own call has neither problem — the loop cap counts in
`before_model` and is not skipped, and the state the graph is handed holds exactly one assistant
message.

**But the graph is not the only reader, and the stream was the one this module got wrong.** The
docstring here used to say the discarded attempt "never reaches graph state, the transcript or the
checkpoint", which is true, and rested it on "the graph never sees the discarded attempt", which is
false for the one reader a chemist actually is. `graph.astream(stream_mode=["messages"])` emits per
*model call*, not per returned message, so both attempts stream — measured on a compiled
`create_agent` graph: a first reply of `"Let me compute that. "` beside an unparseable call, a
second of `"Here it is: pKa 4.2."`, and a client that received
`"Let me compute that. Here it is: pKa 4.2."`. `api/runner._stream_into` concatenates exactly those
root `TokenEvent`s into the turn's persisted answer, so the discarded attempt's prose was in the
answer, in the transcript the front door stores and in what the corpus `score_answer` grades —
while the *message* the graph recorded held only the second attempt. Two records of one turn,
disagreeing.

Neither of the two obvious repairs is available, and the reason is worth stating because it is
what shapes what this does instead. The attempt that gets discarded is the **first** one, which on
every turn that needs no repair is also the only one — so running it off the run's callbacks, or
tagging it `langsmith:nostream`, does not suppress a discarded attempt, it suppresses *the*
streaming path. And a token already emitted cannot be recalled: nothing in either stream mode
retracts.

So the divergence is closed from the other end. The discarded attempt's **call** is discarded, and
its **prose is carried into the message the graph records** (`_carrying_prose`), because that prose
has already reached the chemist and the record's job is to say what happened. What this module
guarantees is therefore the invariant that was actually broken and is now testable: *the assistant
message the turn records is exactly the text the turn streamed.*
"""

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from chemclaw.agent.audit import UNKNOWN_TOOL, bounded_name, bounded_repr
from chemclaw.agent.llm_provider import classify_model_failure
from chemclaw.agent.tool_authz import FAILURE_CHARS
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import Metrics
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.core.turn_signals import record_tool_failure

logger = logging.getLogger(__name__)

# What the model is told when its own tool call did not parse. Addressed to the model rather than to
# a log, and it asks for the call again rather than for an apology, because the remedy for a
# malformed argument document is to re-emit it.
#
# **It carries the arguments, because on the streaming path the parse error is empty.** This said it
# "names each tool and the parse error verbatim"; measured on the aggregated `AIMessageChunk`
# LangChain actually produces for a streamed reply, `error` is `None` and `invalid_tool_calls` falls
# back to the literal `"arguments did not parse"` — so the model was handed a tool name and nothing
# else about a reply it cannot see, and asked to fix it. The one field that *is* populated is
# `args`, the malformed document itself, and it was read and thrown away. It is quoted back
# (bounded by the same budget that bounds an audit row's arguments) because it is the only thing in
# either record that says what broke.
_CORRECTION = (
    "Your previous reply contained {count} tool call(s) whose arguments could not be parsed, so "
    "no tool call from that reply ran and no results exist for any of them: {failures}."
    "{discarded} Re-issue the call(s) you still need with complete, valid JSON arguments. If you "
    "cannot, say what you were unable to do and continue with what you can — do not answer as "
    "though the tool had returned."
)

# The other half of "no tool call from that reply ran", and it is a separate sentence because it
# describes calls the model has no reason to suspect. A reply carrying one unparseable call *and*
# one valid one is discarded whole — the repair returns the second attempt, so the valid call never
# reaches `ToolNode` — and the correction used to be formatted with the count of the *failures*
# alone while asserting "so none of them ran". Measured: a valid `predict_pka` beside one broken
# call was dropped with no log, no metric, no audit row and no word to the model, which is
# `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` — the ADR this middleware cites as
# its reason to exist.
#
# Naming them rather than letting them run is the choice with one code path: short-circuiting the
# repair when `tool_calls` is non-empty would leave the *unparseable* call silently dropped
# instead, which is the same defect wearing the other face.
_DISCARDED_VALID = (
    " The {count} call(s) in that same reply whose arguments were valid were discarded with it and "
    "did not run either: {names}."
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


def _messages_of(response: Any) -> Sequence[BaseMessage]:
    """The messages in whatever shape a `wrap_model_call` handler answered with.

    LangChain lets a handler return a `ModelResponse`, a bare `AIMessage` or an
    `ExtendedModelResponse`, and every reader here — the failures, the discarded valid calls, the
    prose — must see the same thing on all three. One reading, so the three cannot disagree about
    what the model said.
    """
    if isinstance(response, AIMessage):
        return [response]
    inner = getattr(response, "model_response", response)
    result = getattr(inner, "result", None) or []
    return cast(Sequence[BaseMessage], result)


@dataclass(frozen=True, slots=True)
class BrokenCall:
    """One tool call the model emitted whose arguments could not be parsed.

    A record rather than a tuple because the third field arrived late and the two-tuple's call
    sites read `name, error` positionally: what the model *sent* is the field that survives the
    streaming path, and a reader has to be able to tell it from the parse error, which does not.
    """

    name: str
    """The tool the model named, bounded — it is the model's own string and may be anything."""

    error: str
    """The SDK's sentence about the JSON, or a stand-in: empty on the streamed shape."""

    arguments: str
    """The malformed argument document, bounded. The one field the streamed shape populates."""


def invalid_tool_calls(response: Any) -> list[BrokenCall]:
    """Every tool call in `response` whose arguments did not parse, as bounded strings.

    The name falls back to `UNKNOWN_TOOL` because `invalid_tool_calls` is exactly the case where
    the model's output was malformed: an entry can carry a parse error and no usable name, and a
    counter that dropped those would under-report the failure it exists to surface.

    **Every field here is the model's own output and is bounded on the way out**, by the budget an
    audit row's arguments are bounded by (`settings.agent_audit_max_arg_chars`). The name is
    bounded too, not only the arguments: it reaches a log field and a sentence sent back to the
    model, and nothing upstream limits what a model may call a tool.
    """
    return [
        BrokenCall(
            name=bounded_name(call.get("name") or UNKNOWN_TOOL),
            error=str(call.get("error") or ""),
            arguments=bounded_repr(call.get("args")),
        )
        for message in _messages_of(response)
        if isinstance(message, AIMessage)
        for call in (message.invalid_tool_calls or [])
    ]


def valid_tool_calls(response: Any) -> list[str]:
    """The names of the parseable tool calls in `response` — the ones a repair discards with it.

    Bounded for the reason `invalid_tool_calls` bounds its own: this is model output, it reaches a
    log field and the correction, and a *parseable* argument document says nothing about whether
    the name beside it is a reasonable length.
    """
    return [
        bounded_name(call.get("name") or UNKNOWN_TOOL)
        for message in _messages_of(response)
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
    ]


def _prose_of(response: Any) -> str:
    """The text the model streamed in `response` — what a chemist has already read."""
    return "".join(
        message.text for message in _messages_of(response) if isinstance(message, AIMessage)
    )


class RepairInvalidToolCalls(AgentMiddleware[Any, Any, Any]):
    """Ask again when the model emitted a tool call nobody could parse, and count that it did.

    One repair attempt, never a loop. The bound is deliberate and it is not a tuning knob: a second
    unparseable reply to a corrective instruction is a model or a budget problem, and asking a third
    time spends tokens on the same answer while the turn's own runaway cap — which counts in
    `before_model` and therefore does not see a retry taken from inside one model call — cannot
    bound it. The second attempt is returned whatever it holds, with an ERROR beside it, because
    returning the *first* attempt instead would be choosing the reply that is known to be broken.

    The corrective instruction is appended to the request only, so the discarded attempt's *tool
    calls* never reach graph state, the transcript or the checkpoint: what the session records is
    one assistant message, holding the calls the model meant to make.

    **Its prose does reach them, on purpose, because it has already reached the chemist.** Both
    attempts stream — `stream_mode="messages"` emits per model call — and a token cannot be
    recalled, so discarding the first attempt's text from the record while the front door had
    already concatenated it into the answer left two records of one turn that disagreed. The module
    docstring has the measurement and why suppressing the first attempt's stream is not available.
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
        discarded = valid_tool_calls(response)
        _count_invalid(request, failures, discarded, attempt="first")
        repaired = handler(_retry_request(request, failures, discarded))
        _report_repair(request, repaired)
        return _carrying_prose(response, repaired)

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
        discarded = valid_tool_calls(response)
        _count_invalid(request, failures, discarded, attempt="first")
        repaired = await handler(_retry_request(request, failures, discarded))
        _report_repair(request, repaired)
        return _carrying_prose(response, repaired)


def _carrying_prose(discarded: Any, repaired: Any) -> Any:
    """`repaired`, with the discarded attempt's prose in front of it — the record the chemist saw.

    The one invariant this middleware owes a reader: **the assistant message the turn records is
    the text the turn streamed.** Both attempts stream, so the recorded message has to hold both
    or the persisted answer, the transcript and the graded corpus answer say something the session
    does not.

    The repaired response is edited in place rather than rebuilt, because its shape is the
    handler's choice (`_messages_of` names the three) and a rebuild would have to reproduce it —
    along with the message's id, its tool calls, its usage metadata and its response metadata,
    every one of which a later reader depends on. The message object is this call's own, freshly
    returned by the model and not yet in state, so there is nothing else holding a reference to
    the text being replaced.

    A discarded attempt with no prose — the common shape, a bare tool call — changes nothing, and
    a repaired response with no `AIMessage` to prepend to leaves the prose out rather than
    inventing a message that the graph would then have to interpret.
    """
    prose = _prose_of(discarded)
    if not prose:
        return repaired
    target = next(
        (message for message in _messages_of(repaired) if isinstance(message, AIMessage)), None
    )
    if target is None:
        return repaired
    if isinstance(target.content, str):
        target.content = prose + target.content
    else:
        # Content blocks (the shape a provider sends when a reply mixes text with other parts):
        # a text block in front is the same edit, expressed the way that shape expresses text.
        target.content = [{"type": "text", "text": prose}, *target.content]
    return repaired


def _retry_request(
    request: ModelRequest[Any], failures: list[BrokenCall], discarded: list[str]
) -> ModelRequest[Any]:
    """The same request with the correction appended — `override`, so nothing is mutated."""
    described = "; ".join(
        f"{call.name} (the arguments received were {call.arguments}"
        + (f"; {call.error}" if call.error else "")
        + ")"
        for call in failures
    )
    also = (
        _DISCARDED_VALID.format(count=len(discarded), names=", ".join(discarded))
        if discarded
        else ""
    )
    correction = _CORRECTION.format(count=len(failures), failures=described, discarded=also)
    return request.override(messages=[*request.messages, HumanMessage(content=correction)])


def _metric_label(request: ModelRequest[Any], name: str) -> str:
    """`name` if this request actually bound a tool by that name, else the `UNKNOWN_TOOL` bucket.

    **The label was the model's own string, on the one metric that fires exactly when the model's
    output is malformed.** The docstring beside it claimed the label space "stays the tool surface
    plus one" and that was true only of the `unknown` fallback: `call.get("name")` is whatever the
    model emitted, so a single hallucinated or corrupted name minted a permanent time series, and
    model output is attacker-influenceable — which is the whole reason this tree carries
    `frame_untrusted`. `agent/audit.py::metric_tool_name` makes exactly this argument for the tool
    path and cannot be reused verbatim here: it resolves the *registered tool object* a
    `wrap_tool_call` request carries, and a `ModelRequest` has no such object — it has the list of
    tools this call was made with, which is the same registry one step earlier.

    **Why it is worth refusing rather than merely bounding.** `/metrics` is unauthenticated by
    design (a Prometheus scrape carries no identity), and retrieved content — ELN rows, share
    documents, notes — is a prompt-injection surface this tree already frames as untrusted. So
    "emit a tool call named <secret>" turns a verbatim label into an exfiltration channel, and the
    per-counter series cap then blinds the metric permanently once the invented names fill it.
    The 2026-08-28 security review reached the same conclusion independently and by the same
    route, which is why this is stated here rather than left implied.

    The bucket is `audit.UNKNOWN_TOOL` rather than a local literal, so an unregistered name lands
    in the one series both paths already agree on instead of a second spelling of it. **The cost of
    that choice, stated rather than discovered:** `unknown` is a legal tool name, so a tool actually
    called that would be indistinguishable from this bucket, where an angle-bracketed
    `<unknown>` could not be. `agent/audit.py::metric_tool_name` already has exactly that property
    on `chemclaw_tool_calls_total`, so changing it is one decision over both metrics and one
    constant, not a second spelling introduced here.
    """
    for tool in request.tools or ():
        served = getattr(tool, "name", None)
        if served is None and isinstance(tool, Mapping):
            served = tool.get("name")
        if isinstance(served, str) and served == name:
            return served
    return UNKNOWN_TOOL


def _bump_invalid(tool: str, metrics: Metrics) -> None:
    """Increment the unparseable-call counter for one tool.

    A named function bound with `partial` rather than a closure over the loop variable, which is
    the idiom `agent/audit_store.py` already uses: a `lambda` capturing `name` in a loop captures
    the *variable*, so every deferred update would book the last tool in the list.
    """
    metrics.increment("chemclaw_invalid_tool_calls_total", labels={"tool": tool})


def _count_invalid(
    request: ModelRequest[Any],
    failures: list[BrokenCall],
    discarded: list[str],
    *,
    attempt: str,
) -> None:
    """Count each unparseable call under its tool, and say once what the reply cost.

    The counter takes the *clamped* name (`_metric_label`) and the log line takes the model's own
    bounded string, which is the split `audit.metric_tool_name` states: what the model asked for is
    the forensic fact and belongs in the record, and only the unbounded metric *label* is refused.

    `discarded` — the parseable calls thrown away with the reply — is named here as well as in the
    correction, because a record that shows only the failures reads as though nothing else was
    lost.
    """
    for call in failures:
        record_metric(partial(_bump_invalid, _metric_label(request, call.name)))
    log_event(
        logger,
        "model.invalid_tool_calls",
        "the model emitted %d tool call(s) with unparseable arguments (%s attempt): %s%s",
        len(failures),
        attempt,
        ", ".join(f"{call.name}: {call.error or call.arguments}" for call in failures),
        f"; {len(discarded)} valid call(s) in the same reply were discarded with it"
        if discarded
        else "",
        level=logging.WARNING,
        attempt=attempt,
        count=len(failures),
        discarded_valid=len(discarded),
        # A comma-joined string rather than a list, because a log stack indexes scalars — the same
        # rule `log_event` states for every field it takes.
        tools=", ".join(sorted({call.name for call in failures})),
    )


def _report_repair(request: ModelRequest[Any], repaired: Any) -> None:
    """Close the repair out: silence when it worked, an ERROR and a count when it did not.

    Nothing is reported as *discarded* here, and that is the difference between the two attempts:
    the second reply is the one the turn continues with, so a parseable call beside a still-broken
    one runs rather than being thrown away. What is lost at this point is named by the ERROR below
    — the calls that still cannot run — and saying "discarded" about the ones that do would be the
    inverse of the silence this middleware exists to end.
    """
    failures = invalid_tool_calls(repaired)
    if not failures:
        return
    _count_invalid(request, failures, [], attempt="second")
    # The chemist's stream, and not only the metric and the log line beside it.
    #
    # **Measured by the storm's F family:** a truncated argument document produced `HTTP 200,
    # answered=False, error=empty_answer, tools_failed=[]` — an empty answer with nothing naming a
    # cause. Both records above existed and neither is on the transcript: `chemclaw_invalid_tool_
    # calls_total` is an operator's number and this ERROR is an operator's line, while the person
    # who asked the question got a blank box. The reason the ordinary path does not cover it is
    # structural rather than an oversight — `announce_tool_failures` wraps the *tool* middleware,
    # and a call whose arguments never parsed never reaches a tool, so nothing below here can see
    # it. This is the only point that holds both the failure and a live turn.
    #
    # `reason=None` deliberately: `RefusalReason` names the five gates, and every one of them
    # refuses by *raising*. Nothing refused this call. It is a fault in the model's output, and
    # calling it a refusal would put a gate's name on a turn no gate touched.
    for call in failures:
        # Bounded by the same `FAILURE_CHARS` every other producer of this field uses, and the
        # bound is on `detail` rather than on the whole sentence so the sentence always survives.
        # `error` is preferred and is the *unbounded* half: `invalid_tool_calls` clamps `arguments`
        # with `bounded_repr` and leaves `error` as the provider gave it, which for an
        # OpenAI-compatible endpoint quotes the entire malformed argument document back.
        detail = (call.error or call.arguments)[:FAILURE_CHARS]
        record_tool_failure(
            call.name,
            f"the model's arguments for this call did not parse, twice: {detail}",
        )
    log_event(
        logger,
        "model.invalid_tool_calls_unrepaired",
        "the model emitted unparseable tool-call arguments twice; the turn continues with the "
        "second reply, in which %d call(s) still cannot run",
        len(failures),
        level=logging.ERROR,
        count=len(failures),
    )
