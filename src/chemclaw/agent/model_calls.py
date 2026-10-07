"""What happened at the model call: failure accounting and malformed tool calls.

Two `wrap_model_call` middlewares; neither is a policy, and removing them changes no answer.

**`RecordModelCalls`** counts and times every model call by outcome, using
`llm_provider.classify_model_failure` (rate limit, context length, timeout, transport, auth,
error), so outages and a too-long thread are distinguishable. The SDK's own retries
(`llm_max_retries`) happen below `ainvoke`, so one recorded call may be several wire attempts and
a `timeout`/`transport` outcome is the state after the SDK gave up. A call cancelled by turn
teardown is not counted.

**`PromoteInvalidToolCalls`** handles tool calls whose arguments do not parse. LangChain puts
them on `AIMessage.invalid_tool_calls`, which the agent never iterates, so the call would silently
vanish. They are moved onto `tool_calls` carrying the raw document under `_UNPARSED_ARGUMENTS`,
and `refuse_unparsed_arguments` raises before the body runs. The call is then an ordinary failing
tool call with an audit row, span, gates, `tool_failed` event and a `ToolMessage` the model reads
inside its own loop, counted by the loop and spend caps. Retrying inside `wrap_model_call` instead
would sit outside every bound the graph has. Calls in a reply cut off at the output limit are
demoted and refused the same way, since upstream's partial-JSON repair makes them look complete.
`chemclaw_invalid_tool_calls_total` and a WARNING record each malformed emission for operators.
"""

import json
import logging
import time
from bisect import bisect_right
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest, wrap_tool_call
from langchain_core.messages import AIMessage, BaseMessage

from chemclaw.agent.audit import UNKNOWN_TOOL, bounded_repr
from chemclaw.agent.framing import defang
from chemclaw.agent.llm_provider import classify_model_failure
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import Metrics
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# The key a promoted call carries its unparseable document under, shared by both halves of the
# mechanism. Dunder-flanked so it cannot collide with a real parameter;
# `tests/test_invalid_tool_calls.py` checks no registered tool declares it.
_UNPARSED_ARGUMENTS = "__unparsed_arguments__"

# Prefix for a promoted call's id when the provider gave none, suffixed with its index in the
# reply, because stream readers key on call id and two `""` ids would collide.
_UNPARSED_CALL_ID = "unparsed-call-"

# Set beside `_UNPARSED_ARGUMENTS` on a call that parsed only because upstream completed a document
# cut off at the output limit, so the refusal says "cut off" rather than "not valid JSON".
_CUT_OFF = "__cut_off_at_output_limit__"

# `finish_reason` values meaning the provider ran out of output budget: `length` (OpenAI-compatible)
# and `max_tokens` (Anthropic-native, possibly passed through by a gateway).
_OUTPUT_LIMIT_REASONS = frozenset({"length", "max_tokens"})

# The `error` a demoted call carries, and what the promotion recognises it by — not its id, which a
# provider may omit.
_CUT_OFF_ERROR = "the reply stopped at the output-token limit mid-call"


def model_call_middleware() -> list[Any]:
    """The two model-call observers, as the list `build_langgraph_agent` splices in.

    The promotion is outside the recorder. Spliced innermost so the recorded duration is the
    provider
    call, not first-party middleware above it.
    """
    return [PromoteInvalidToolCalls(), RecordModelCalls()]


def _observe(outcome: str, seconds: float) -> None:
    """Book one finished model call: its outcome, and how long the gateway took over it.

    No `provider` label: there is one gateway, so it would carry no information.
    """
    record_metric(
        lambda metrics: metrics.increment("chemclaw_model_calls_total", labels={"outcome": outcome})
    )
    record_metric(lambda metrics: metrics.observe("chemclaw_model_call_duration_seconds", seconds))


def _record_failure(exc: BaseException, seconds: float) -> None:
    """Classify, count and log one failed model call, then let the caller re-raise.

    The WARNING carries the exception class, never the message, which can quote the chemist's
    request.
    """
    outcome = classify_model_failure(exc)
    _observe(outcome, seconds)
    log_event(
        logger,
        "model.call_failed",
        "the model gateway failed after %.0f ms (%s: %s)",
        seconds * 1000.0,
        outcome,
        type(exc).__name__,
        level=logging.WARNING,
        outcome=outcome,
        exception=type(exc).__name__,
        duration_ms=round(seconds * 1000.0, 1),
    )
    if outcome == "auth":
        _log_credential_refusal(exc)


def _log_credential_refusal(exc: BaseException) -> None:
    """Name the gateway and the status that refused this deployment's credential, at ERROR.

    The remedy is an operator's (a rotated, revoked or unset `CHEMCLAW_LLM_API_KEY`), so the line
    carries the request host and HTTP status from the SDK exception. Never the credential or the
    response body.
    """
    request = getattr(exc, "request", None)
    host = getattr(getattr(request, "url", None), "host", None) or "an unknown host"
    status = getattr(exc, "status_code", None)
    log_event(
        logger,
        "model.gateway_refused_credential",
        "the model gateway at %s refused this deployment's credential (HTTP %s); check "
        "CHEMCLAW_LLM_API_KEY",
        host,
        status,
        level=logging.ERROR,
        gateway_host=host,
        status=status,
    )


class RecordModelCalls(AgentMiddleware[Any, Any, Any]):
    """Count and time every model call, by what went wrong.

    Both sync and async hooks, because `create_agent` puts the middleware in both chains.
    Observation only: it re-raises whatever the call raised, unchanged.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        """Record the call, then return what it produced (sync path)."""
        start = time.perf_counter()
        try:
            response = handler(request)
        except Exception as exc:
            _record_failure(exc, time.perf_counter() - start)
            raise
        _observe("ok", time.perf_counter() - start)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """Record the call — the path a turn actually takes."""
        start = time.perf_counter()
        try:
            response = await handler(request)
        except Exception as exc:
            _record_failure(exc, time.perf_counter() - start)
            raise
        _observe("ok", time.perf_counter() - start)
        return response


def _messages_of(response: Any) -> Sequence[BaseMessage]:
    """The messages in whatever shape a `wrap_model_call` handler answered with.

    `ModelResponse`, bare `AIMessage` or `ExtendedModelResponse`; one reading so counting and
    promotion see the same messages.
    """
    if isinstance(response, AIMessage):
        return [response]
    inner = getattr(response, "model_response", response)
    result = getattr(inner, "result", None) or []
    return cast(Sequence[BaseMessage], result)


def _bounded_text(value: object) -> str:
    """The tool **name** the model emitted, bounded by the audit budget and deliberately not repr'd.

    Bounded because nothing upstream limits a tool name and it reaches logs, audit rows and the
    event
    stream. Not repr'd because `_metric_label` compares it against the bound tool names; escaping
    happens at the sinks instead.
    """
    limit = settings.agent_audit_max_arg_chars
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _bounded_reason(value: object) -> str:
    r"""The provider's parse error, escaped and bounded from the **tail**.

    The tail, because upstream's message starts with the tool name and a copy of the argument
    document; the parse reason is at the end. Escaped (unlike `_bounded_text`), because the text
    embeds the model's document and reaches a single-line log unquoted, where a newline could forge
    a
    record. An empty error stays empty, since `_count_invalid` tests it for truthiness.
    """
    text = str(value)
    if not text:
        return ""
    limit = settings.agent_audit_max_arg_chars
    quoted = repr(text)
    if len(quoted) <= limit:
        return quoted
    # Slice the text and quote the slice, never the reverse, so an escape sequence is never cut in
    # half.
    # A search because `repr` expands each character by up to four. The range starts at 1 because
    # `text[-0:]` is the whole string; an index of 0 means nothing fits and is handled below. The
    # keys
    # are non-decreasing (prepending a character never shortens the quoted form), as `bisect_right`
    # requires.
    longest = bisect_right(range(1, len(text) + 1), limit, key=lambda n: len(repr(text[-n:])))
    # The ellipsis alone is the honest answer when nothing fits: the budget says there is no room,
    # and something was still cut.
    return "…" + repr(text[-longest:]) if longest else "…"


@dataclass(frozen=True, slots=True)
class BrokenCall:
    """One tool call the model emitted whose arguments could not be parsed.

    `arguments` (what the model sent) is kept apart from `error` (the parse error), since only the
    former survives the streaming path.
    """

    name: str
    """The tool the model named, bounded — it is the model's own string and may be anything."""

    error: str
    """The SDK's reason, escaped and tail-bounded — empty on the streamed shape, 100 kB off it."""

    arguments: str
    """The malformed argument document, bounded. The one field the streamed shape populates."""


def invalid_tool_calls(response: Any) -> list[BrokenCall]:
    """Every tool call in `response` whose arguments did not parse, as bounded strings.

    A missing name falls back to `UNKNOWN_TOOL` so such entries are still counted. Every field is
    model output and is bounded by `settings.agent_audit_max_arg_chars`.
    """
    return [
        BrokenCall(
            name=_bounded_text(call.get("name") or UNKNOWN_TOOL),
            error=_bounded_reason(call.get("error") or ""),
            arguments=bounded_repr(call.get("args")),
        )
        for message in _messages_of(response)
        if isinstance(message, AIMessage)
        for call in (message.invalid_tool_calls or [])
    ]


def _metric_label(request: ModelRequest[Any], name: str) -> str:
    """`name` if this request actually bound a tool by that name, else the `UNKNOWN_TOOL` bucket.

    The emitted name is model output, which retrieved content can steer; as a label on the
    unauthenticated `/metrics` it would mint unbounded series and be an exfiltration channel. The
    same clamp as `audit.metric_tool_name`, but against the tools this `ModelRequest` was made with.
    The bucket is `audit.UNKNOWN_TOOL`, shared with `chemclaw_tool_calls_total`.
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

    Bound with `partial` rather than a loop-variable closure, which would book the last tool for
    all.
    """
    metrics.increment("chemclaw_invalid_tool_calls_total", labels={"tool": tool})


def _count_invalid(request: ModelRequest[Any], failures: list[BrokenCall]) -> None:
    """Count each unparseable call under its tool, and say once what the model emitted.

    The operator's record only; the chemist sees the `tool_failed` the announcer raises for the
    promoted call. The counter takes the clamped name (`_metric_label`); the log line takes the
    model's own name, repr-escaped at this sink so a newline cannot forge a log record.
    """
    for call in failures:
        record_metric(partial(_bump_invalid, _metric_label(request, call.name)))
    log_event(
        logger,
        "model.invalid_tool_calls",
        "the model emitted %d tool call(s) with unparseable arguments, now promoted so the tool "
        "chain refuses them: %s",
        len(failures),
        ", ".join(f"{call.name!r}: {call.error or call.arguments}" for call in failures),
        level=logging.WARNING,
        count=len(failures),
        # A comma-joined string rather than a list, because a log stack indexes scalars — the same
        # rule `log_event` states for every field it takes. Quoted for the reason above.
        tools=", ".join(sorted({repr(call.name) for call in failures})),
    )


class PromoteInvalidToolCalls(AgentMiddleware[Any, Any, Any]):
    """Move a call the model mis-serialised onto `tool_calls`, so the tool chain can refuse it.

    `ToolNode` iterates only `tool_calls`, so a call left on `invalid_tool_calls` bypasses every
    control. Moved one field over, it becomes an ordinary failing tool call that the announcer,
    audit,
    span, gates and `surface_domain_errors` all handle, at the cost of one graph iteration.

    The document travels under `_UNPARSED_ARGUMENTS` rather than as `{}`, because several tools take
    no required argument and would run on empty arguments; the sentinel makes the call refusable
    before the body. Both sync and async hooks, for `RecordModelCalls`'s reason.
    """

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> Any:
        """Promote, then return (sync path — declared for `RecordModelCalls`'s reason)."""
        return _promote(request, handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
    ) -> Any:
        """Promote — the path a turn actually takes."""
        return _promote(request, await handler(request))


def _promote(request: ModelRequest[Any], response: Any) -> Any:
    """Record the malformed emission for the operator, and move its calls where they can be seen.

    Edited in place so the message keeps its id and metadata; it is this call's own, not yet in
    state.
    """
    _demote_cut_off_calls(response)
    failures = invalid_tool_calls(response)
    if not failures:
        return response
    _count_invalid(request, failures)
    for message in _messages_of(response):
        if not isinstance(message, AIMessage) or not message.invalid_tool_calls:
            continue
        promoted: list[Any] = list(message.tool_calls or [])
        # Bounded, because each promoted call becomes an audit row, a stream event and a
        # `ToolMessage`;
        # `_count_invalid` has already counted every one (see `agent_max_promoted_invalid_calls`).
        ceiling = settings.agent_max_promoted_invalid_calls
        entries = message.invalid_tool_calls[:ceiling] if ceiling else message.invalid_tool_calls
        for index, call in enumerate(entries):
            promoted.append(
                {
                    # Bounded because the name becomes `request.tool_call["name"]` and reaches the
                    # audit row, span and
                    # event stream unbounded. Escaping stays at the sinks so `_metric_label` is
                    # unaffected.
                    "name": _bounded_text(call.get("name") or UNKNOWN_TOOL),
                    # The model's own id pairs the `tool_failed` event with the `tool_call` the
                    # stream already emitted.
                    # A missing id becomes a synthetic one unique within the reply, since stream
                    # readers key on it.
                    "id": str(call.get("id") or "") or f"{_UNPARSED_CALL_ID}{index}",
                    "args": {
                        _UNPARSED_ARGUMENTS: bounded_repr(call.get("args")),
                        **({_CUT_OFF: True} if call.get("error") == _CUT_OFF_ERROR else {}),
                    },
                    "type": "tool_call",
                }
            )
        message.tool_calls = promoted
        message.invalid_tool_calls = []
    return response


def _finish_reason(message: AIMessage) -> str:
    """Why the provider stopped emitting this message, as `response_metadata` names it.

    Compare by containment: streamed chunks concatenate repeated strings (`"lengthlength"`).
    """
    metadata = message.response_metadata or {}
    return str(metadata.get("finish_reason") or metadata.get("stop_reason") or "")


def _demote_cut_off_calls(response: Any) -> None:
    """Move every tool call of a reply cut off at the output limit onto `invalid_tool_calls`.

    Upstream's `parse_partial_json` closes a truncated document, so a cut-off call looks valid; the
    reply's `finish_reason` is what reveals it. Every call in the reply is demoted, since the merged
    message does not record which call was being written, and the model re-issues them together.
    Demoted rather than refused here so `_promote` and `refuse_unparsed_arguments` handle it
    (D-2026-09-25-a-call-cut-off-at-the-output-limit-does-not-run). Marked with `_CUT_OFF_ERROR`.
    """
    for message in _messages_of(response):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        reason = _finish_reason(message)
        if not any(limit in reason for limit in _OUTPUT_LIMIT_REASONS):
            continue
        for call in message.tool_calls:
            message.invalid_tool_calls.append(
                {
                    "name": call.get("name"),
                    "args": json.dumps(call.get("args"), default=str),
                    "id": call.get("id"),
                    "error": _CUT_OFF_ERROR,
                    "type": "invalid_tool_call",
                }
            )
        message.tool_calls = []


class UnparsedArguments(ChemclawError):
    """The model asked for a tool with arguments that were not valid JSON.

    A `ChemclawError` so audit records an `error` outcome and `surface_domain_errors` hands the
    sentence to the model. Not in `agent/audit.refusal_reason`'s table: it is a fault, not a gate's
    decision, and every surface renders it as one.
    """


@wrap_tool_call
async def refuse_unparsed_arguments(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse a promoted call before its body runs — below the audit, authorization and guard gates.

    Those gates all see the call. Under the harness, `enforce_plan_approval` and `stamp_plan_link`
    nest inside this, so the plan gate never sees a promoted call; the turn reports a fault, not a
    refusal. Raising inside `awrap_tool_call` happens before argument validation and the body, so an
    empty or default argument set can never run. The message names the document, since only the
    model can fix it.
    """
    arguments = request.tool_call.get("args") or {}
    document = arguments.get(_UNPARSED_ARGUMENTS) if isinstance(arguments, dict) else None
    if document is None:
        return await handler(request)
    if arguments.get(_CUT_OFF):
        raise UnparsedArguments(
            f"Your reply stopped at the output-token limit while this call was being written, so "
            f"its arguments may be incomplete and it did not run. Completed by the client from the "
            f"cut-off document, they read {defang(str(document))} — which may not be what you "
            f"meant. Re-issue the call with its complete arguments — fewer or "
            f"shorter calls in one reply if the limit is what cut it — rather than answering as "
            f"though the tool had returned."
        )
    raise UnparsedArguments(
        f"The arguments for this call were not valid JSON, so it did not run. What was received "
        f"was {defang(str(document))}. Re-issue the call with complete, valid JSON arguments; if "
        f"you cannot, say what you were unable to do rather than answering as though the tool had "
        f"returned."
    )
