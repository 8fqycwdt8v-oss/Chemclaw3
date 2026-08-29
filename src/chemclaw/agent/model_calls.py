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

**It stayed silent to the one reader who matters, and that half was fixed two days later**
(`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce`). The
counter and the WARNING above are an operator's record; a chemist reads the turn's event stream,
and `tool_failed` is put on it by `agent/tool_authz.announce_tool_failures` — a `wrap_tool_call`
middleware. A call whose arguments never parsed never reaches the tool chain, because `ToolNode`
iterates `tool_calls`, so the one failure class that cannot reach the announcer is the one class
this middleware exists for. Measured against the live stack on the mock model, on two behaviours
that differ only in whether the argument document is *parseable*:

    '{"query": "benzene"}'  ->  tool_call, tool_failed, error/empty_answer
    '{"text": }'            ->  error/empty_answer, "after 0 tool call(s)"

Same silence, one wire shape apart — and the second sentence is worse than nothing, because it
tells a chemist their question was too broad about a turn in which the model asked for exactly the
right tool twice. So every call this middleware knows will not run is now announced on the turn's
own side-channel (`core/turn_signals.record_tool_failure`), which is what `graph_stream` turns into
a `ToolFailedEvent`. The **audit row and the span are still absent, deliberately**: both record a
tool *invocation*, and there was none — synthesising one would put a call that never ran into the
trail that says what ran. The announcement carries no `call_id`, and the reason is that there is
nothing to match it to rather than nothing to carry: the entries do hold an id, and `BrokenCall`
drops it, because a discarded call raises no `tool_call` event for a consumer to pair it with.

**The set announced is "what the turn will not run", and getting that wrong the first time cost
three readers.** The original version announced each attempt as it was discarded. A discarded call
is not a lost call when the repair works — the model re-issues it and it runs — so a turn that
answered, with both its tools succeeding, told `evals/live` `failed_loudly=True`, booked two
`tool_failures` on a ledger that also recorded two successful calls, and put two rows in
`Chemclaw3_ui`'s failure red above the answer. `_announce_unrun` asks the question once instead,
after the repair, against the reply the turn continues with. The counter and the WARNING stay
per-attempt (`_count_invalid`) because an operator is asking a different question — how often the
model emits malformed output, and what it cost — and the two answers differing is the correct
outcome rather than a discrepancy.

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
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from chemclaw.agent.audit import UNKNOWN_TOOL, bounded_repr
from chemclaw.agent.llm_provider import classify_model_failure
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


# What the *chemist* is told about the same two events, on the turn's event stream. Separate text
# from `_CORRECTION` above rather than the same string reused, because the two are addressed to
# different readers and only one of them can act: the model is asked to re-emit the call, and the
# person is told which step of their answer is missing and why. `ToolFailedEvent.message` is what
# `Chemclaw3_ui` renders beside the tool's name, so each of these is a sentence rather than a code.
#
# The argument document is quoted for the reason the correction quotes it — on the streaming path
# `error` is `None`, so the document is the only thing either record holds that says what broke —
# and it arrives already bounded by `bounded_repr`.
_UNPARSEABLE_FAILURE = (
    "The model asked for this tool with arguments that are not valid JSON, so the call did not "
    "run. The arguments received were {arguments}{error}."
)
# Said once when a reply held more unrunnable calls than `agent_max_reported_lost_calls`. A count
# rather than silence: a bound that drops the remainder without saying so is the truncation this
# module exists to end, one level up. It rides the `unknown` bucket rather than inventing a tool
# name, because it is about the reply rather than about any one call.
_OVER_THE_LINE = (
    "{count} further tool call(s) in the same reply also could not run and are not listed "
    "individually."
)

_DISCARDED_FAILURE = (
    "This call's own arguments were valid, but another tool call in the same reply had arguments "
    "that could not be parsed, so the whole reply was discarded and this call did not run either."
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


def _bounded_text(value: object) -> str:
    """One field of the model's own output, bounded by the audit budget and deliberately not repr'd.

    Bounded because nothing upstream limits either field this serves, and both reach a log line, a
    sentence sent back to the model, and — since
    `D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce` — the
    chemist's event stream.

    **The parse error is the one that actually gets big, and it was the one left unbounded.**
    `invalid_tool_calls`' docstring has always claimed every field here is bounded on the way out;
    `error` was not, and it is not merely unbounded but reliably *large*: LangChain's
    `parse_tool_call` folds the entire raw argument document into the exception message, which
    `langchain_openai` stores verbatim. Measured on a 100 kB document with the budget at 200
    chars — `arguments` 201, `error` **100,260**, and that error reached a 100 kB
    `ToolFailedEvent.message`, a 100 kB corrective `HumanMessage`, and a 100 kB WARNING. The
    corrective message is the worst of the three: `_retry_request` appends it from the *innermost*
    middleware, below `context_compaction_middleware`, so the budget has already been computed and
    nothing reduces it — the failure `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget`
    exists to prevent. It reads empty on the streamed shape, which is why it went unnoticed.

    **Not** repr'd, unlike the argument document beside it: `_metric_label` compares the *name*
    against the names the request actually bound, and a quoted name matches none of them — which
    would clamp every label to `UNKNOWN_TOOL` and lose the distinction the clamp exists to keep.
    """
    limit = settings.agent_audit_max_arg_chars
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _bounded_reason(value: object) -> str:
    r"""The provider's parse error, escaped and bounded from the **tail**.

    Two departures from `_bounded_text`, and each is a defect this had before it was written.

    **The tail, because the head is a copy of a field printed beside it.** LangChain builds the
    message as `Function {name} arguments:\n\n{document}\n\nare not valid JSON. Received
    JSONDecodeError {reason}` (`langchain_core/output_parsers/openai_tools.py`), so the head is the
    tool name and a verbatim second copy of `BrokenCall.arguments` — and the only thing in the
    field that is *not* already in the record beside it is the reason, at the very end. Measured
    against the 200-char budget: the reason survived a 102-char argument document and was gone from
    122 upward, so a chemist read the same document twice and never learned why it would not parse.
    `agent/tool_result_size.py` states the general rule this broke — "head and tail, never head
    alone… a head-truncated result reads as complete and silently drops the outcome"; here the head
    is pure duplication, so the tail alone is what carries information. Upstream's "For
    troubleshooting, visit: …" boilerplate sits between the two and spends about half the budget,
    which is worth knowing when reading a truncated one.

    **Escaped, because this string reaches a log line unquoted.** `_bounded_text` deliberately does
    not `repr`, for a reason about the *name* — `_metric_label` compares it against the bound tools
    and a quoted name matches none of them. That reason does not extend to this field, and
    extending it silently was the defect: the provider's text embeds the model's own document,
    which is attacker-influenceable through every retrieved corpus this tree frames as untrusted,
    and `log_json` defaults to **false**, so an embedded newline forged a second log line
    (`… ERROR chemclaw.audit: actor=admin action=approve_plan result=granted`). `bounded_repr`
    already escapes `arguments` for the same reason; this closes the half beside it.

    An empty error stays empty rather than becoming `"''"`, because `_because` tests it for
    truthiness to decide whether the clause exists at all — and on the streamed shape it always is.
    """
    text = str(value)
    if not text:
        return ""
    quoted = repr(text)
    limit = settings.agent_audit_max_arg_chars
    return quoted if len(quoted) <= limit else "…" + quoted[-limit:]


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
    """The SDK's reason, escaped and tail-bounded — empty on the streamed shape, 100 kB off it."""

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
            name=_bounded_text(call.get("name") or UNKNOWN_TOOL),
            error=_bounded_reason(call.get("error") or ""),
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
        _bounded_text(call.get("name") or UNKNOWN_TOOL)
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

    **Its failures reach them only when they are still failures**, which is the correction
    `D-2026-08-29-a-discarded-call-is-not-a-lost-call` made to the paragraph that stood here. This
    used to say a discarded call is a call that did not run and the chemist is told so; that is
    false whenever the repair works, and it made three readers report failures on turns where
    nothing failed. `_announce_unrun` asks after the repair instead, so a turn that recovers is
    silent to the chemist and fully recorded for the operator.

    That paragraph outlived its own change by a day, naming a function this module no longer has —
    the prose failure this repository's own rule exists to catch, committed in the file the
    correction touched.
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
        try:
            repaired = handler(_retry_request(request, failures, discarded))
        except Exception:
            # The retry is the only window in which the operator's record exists and the chemist's
            # does not: `_count_invalid` has already fired and `_announce_unrun` has not. A 429 or
            # a context-length refusal on the second call would otherwise book the counter and say
            # nothing to the person who asked. `Exception`, not `BaseException`: a cancelled turn
            # is not a lost call and has its own outcome.
            _announce_unrun(failures, discarded, None)
            raise
        _report_repair(request, repaired)
        _announce_unrun(failures, discarded, repaired)
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
        try:
            repaired = await handler(_retry_request(request, failures, discarded))
        except Exception:
            # The retry is the only window in which the operator's record exists and the chemist's
            # does not: `_count_invalid` has already fired and `_announce_unrun` has not. A 429 or
            # a context-length refusal on the second call would otherwise book the counter and say
            # nothing to the person who asked. `Exception`, not `BaseException`: a cancelled turn
            # is not a lost call and has its own outcome.
            _announce_unrun(failures, discarded, None)
            raise
        _report_repair(request, repaired)
        _announce_unrun(failures, discarded, repaired)
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
    named, over = _reportable(failures)
    described = "; ".join(
        f"{call.name} (the arguments received were {call.arguments}{_because(call)})"
        for call in named
    ) + (f"; and {over} more not listed here" if over else "")
    shown, also_over = _reportable(discarded)
    also = (
        _DISCARDED_VALID.format(
            count=len(discarded),
            names=", ".join(shown) + (f", and {also_over} more" if also_over else ""),
        )
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

    **The operator's two records, and deliberately not the chemist's.** This fires per *attempt*,
    because what an operator is alerting on is how often the model emits malformed output and what
    that costs — a repaired turn emitted twice and paid for two model calls, and a counter that
    hid the recovered one would understate the rate. What the chemist is told is a different
    question with a different answer (`_announce_unrun`), asked once, after the repair, about the
    work that did not happen. The two disagreeing is correct and was measured: an operator sees
    2 on `chemclaw_invalid_tool_calls_total` for the turn that shows a chemist one lost call.

    The counter takes the *clamped* name (`_metric_label`) and the log line takes the model's own
    bounded string, which is the split `audit.metric_tool_name` states: what the model asked for is
    the forensic fact and belongs in the record, and only the unbounded metric *label* is refused.

    `discarded` — the parseable calls thrown away with the reply — is counted in the log line here
    as well as named in the correction, because a record that shows only the failures reads as
    though nothing else was lost.
    """
    for call in failures:
        record_metric(partial(_bump_invalid, _metric_label(request, call.name)))
    # The counter takes every call; the *line* takes a bounded prefix, for `_reportable`'s reason.
    # An operator alerting on the rate reads the counter, and a WARNING that can reach 400 kB is
    # not a better record of the same event.
    listed, over = _reportable(failures)
    log_event(
        logger,
        "model.invalid_tool_calls",
        "the model emitted %d tool call(s) with unparseable arguments (%s attempt): %s%s",
        len(failures),
        attempt,
        ", ".join(f"{call.name}: {call.error or call.arguments}" for call in listed)
        + (f", and {over} more" if over else ""),
        f"; {len(discarded)} valid call(s) in the same reply were discarded with it"
        if discarded
        else "",
        level=logging.WARNING,
        attempt=attempt,
        count=len(failures),
        discarded_valid=len(discarded),
        # A comma-joined string rather than a list, because a log stack indexes scalars — the same
        # rule `log_event` states for every field it takes.
        tools=", ".join(sorted({call.name for call in listed})),
    )


def _announce_unrun(failures: list[BrokenCall], discarded: list[str], repaired: Any) -> None:
    """Tell the chemist about every call this turn will not run — once each, after the repair.

    **Announcing at the moment of discard was wrong, and three readers proved it.** The first
    version reported each attempt as it was discarded, on the argument that a discarded call is a
    call that did not run. It is not, when the repair works: the model re-issues it and it runs.
    Measured on a compiled graph — a broken `predict_pka` beside a valid `find_notes`, repaired,
    both then running and the turn answering — `evals/live` recorded
    `tools_failed=['predict_pka', 'find_notes']` and `failed_loudly=True` about a turn in which
    nothing failed and `find_notes` was never even invoked, `_TurnLedger` booked two
    `tool_failures` against calls that succeeded, and `Chemclaw3_ui` renders a `reason`-less
    failure in the danger red with a `failed` badge, so the chemist saw two red rows above their
    answer. The ADR that introduced it claimed `reason` distinguished the two cases; it does not —
    `RefusalReason` names the five *gates*, and both of these carry `None`.

    So the question is asked once, at the end, about the state the turn actually continues in: a
    call is announced if the repaired reply either cannot run it or never asks for it. That is the
    same invariant stated from the other side — *what the chemist did not get* — and it is the one
    a `tool_failed` means.

    **Counted by name rather than matched as a set**, because under-reporting is the failure this
    whole middleware exists to end: a model that emitted two calls to one tool and re-issued one
    of them has lost the other, and a set difference would call that even. Names are all there is
    to count with — `BrokenCall` carries no id, and an id would have nothing to match against,
    since a discarded call raises no `tool_call` event.

    Args:
        failures: The first reply's unparseable calls.
        discarded: The first reply's parseable calls, thrown away with it.
        repaired: The response the turn continues with.
    """
    lost = [
        (call.name, _UNPARSEABLE_FAILURE.format(arguments=call.arguments, error=_because(call)))
        for call in invalid_tool_calls(repaired)
    ]
    # What the repaired reply asks for, by name. Its *broken* calls count here too: they are
    # already in `lost` above, so counting them is what stops one call the model got wrong twice
    # from being announced twice.
    asked_again = Counter(valid_tool_calls(repaired))
    asked_again.update(name for name, _ in lost)
    first_reply = [
        (call.name, _UNPARSEABLE_FAILURE.format(arguments=call.arguments, error=_because(call)))
        for call in failures
    ] + [(name, _DISCARDED_FAILURE) for name in discarded]
    for name, message in first_reply:
        if asked_again[name]:
            asked_again[name] -= 1
        else:
            lost.append((name, message))
    reported, over = _reportable(lost)
    for name, message in reported:
        record_tool_failure(name, message)
    if over:
        record_tool_failure(UNKNOWN_TOOL, _OVER_THE_LINE.format(count=over))


def _reportable(entries: list[Any]) -> tuple[list[Any], int]:
    """The prefix of `entries` this reply may report in full, and how many are over the line.

    **Nothing bounded how many unrunnable calls one reply could hold.**
    `agent_max_parallel_tool_calls` is a concurrency ceiling on calls that *run*;
    `len(AIMessage.invalid_tool_calls)` had no bound at all, and every entry is quoted back to the
    model by `_retry_request` in a `HumanMessage` appended from the innermost middleware — below
    `context_compaction_middleware`, so the budget is already computed and nothing reduces it.
    Measured with every field at its own 200-char ceiling: 8 malformed calls cost a 7.2 kB
    correction, 1000 cost **841 kB** and 2000 stream events.
    `D-2026-08-28-a-budget-in-the-wrong-unit-is-not-a-budget` is the decision this reaches through
    the one message it could not see.

    The remainder is **counted, never dropped in silence** — that is the difference between a bound
    and a truncation, and it is the rule this module enforces about tool calls in the first place.
    """
    limit = settings.agent_max_reported_lost_calls
    return entries[:limit], max(0, len(entries) - limit)


def _because(call: BrokenCall) -> str:
    """The parse error as a trailing clause, or nothing — it is empty on the streamed shape."""
    return f"; {call.error}" if call.error else ""


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
    log_event(
        logger,
        "model.invalid_tool_calls_unrepaired",
        "the model emitted unparseable tool-call arguments twice; the turn continues with the "
        "second reply, in which %d call(s) still cannot run",
        len(failures),
        level=logging.ERROR,
        count=len(failures),
    )
