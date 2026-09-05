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

**`PromoteInvalidToolCalls` — an unparseable tool call was a silent no-op.** LangChain puts a tool
call whose arguments do not parse onto `AIMessage.invalid_tool_calls` rather than `tool_calls`, and
nothing in `src/` read that field. The agent iterates `tool_calls`, so the call vanished: no
`tool_failed`, no `tool_result`, no audit row, no span. With no prose beside it the turn ended as
`empty_answer`; **with prose it proceeded as though no tool had been needed**, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` exactly.

**What reaches that field is genuinely malformed JSON, and not a truncated document.** LangChain
runs a streamed tool call's argument fragments through `parse_partial_json`, which closes an
unterminated string and an unclosed brace, so a cut stream repairs itself before anything sees it:
measured, `'{"smiles": "CC'` arrives as a valid entry on `tool_calls` with `invalid_tool_calls`
empty. The field is reached by output that is not JSON at all, or that is JSON-shaped and broken in
a way partial parsing cannot close — which a model emits rarely and does emit, and which nothing
recovered from.

**The fix is a change of address, and that is the whole of it.** The call is moved onto
`tool_calls` carrying its unparseable document under `_UNPARSED_ARGUMENTS`, and
`refuse_unparsed_arguments` — below every gate that decides — raises before the body runs. It is
then an ordinary failing tool call, and everything a failing tool call already gets, it gets: the
audit row, the span, the authorization gate, the dry-run and repeat guards, the `tool_failed` the
announcer raises carrying the model's **own call id**, and a `ToolMessage` the model reads inside
its own loop — an ordinary graph iteration the loop cap and the spend cap both count.

**Three earlier designs asked the model again from inside `wrap_model_call` instead, and this
supersedes all three** (`D-2026-08-27-a-refusal-is-not-a-crash`,
`D-2026-08-29-a-call-the-tool-chain-never-sees-is-a-call-the-tool-chain-cannot-announce`,
`D-2026-08-29-a-discarded-call-is-not-a-lost-call`,
`D-2026-08-29-a-refusal-is-not-a-failure-and-a-bound-is-not-a-truncation`). Each round of review
found defects in that machinery and each round's fix introduced the next round's, and the cause was
structural rather than careless: **a retry taken outside the graph is outside every bound the graph
has**, so each one had to be rebuilt by hand. A "never a loop" ceiling, because the loop cap could
not see the extra call. A reporting ceiling, because nothing bounded the corrective message. An
announcement rule and five prose constants, because no `tool_failed` could be raised from a call
the tool chain never saw. A `graph_stream` guard, because that announcement had no call id to pair
with. And an invariant about streamed prose, because `astream(stream_mode=["messages"])` emits per
*model call* rather than per returned message, so the discarded attempt's tokens reached the
chemist while the recorded message held only the second attempt — a divergence that had to be
closed by carrying the discarded prose forward. Promotion has none of those problems, because it
introduces no hidden call: measured with docstrings and comments stripped, **42 lines of code
replace 113** — and those 113 were the ones that kept being wrong. (This said "~20 replace ~180",
a 9x reduction against a real one of 2.7x. Neither figure had been counted;
`D-2026-08-30-a-review-of-the-review` counts them.)

The one property the old design had that this does not: it corrected the model without spending a
graph iteration. That is the trade, stated so it is not rediscovered — an iteration is what makes
the correction visible to the loop cap, the spend cap, the transcript and the audit trail, and each
of those was previously a hand-built substitute for it.

**The operator's half is unchanged and stays here.** `chemclaw_invalid_tool_calls_total` and the
WARNING answer a different question from the chemist's stream — how often the model emits malformed
output, and for which tool — and they are counted at the emission, once, because there is no second
attempt any more.
"""

import logging
import time
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

# The key a promoted call carries its unparseable document under, and the one definition of it.
# Both halves of this mechanism import it from here: `PromoteInvalidToolCalls` writes it and
# `refuse_unparsed_arguments` reads it, so they cannot disagree about the contract between them.
#
# **Dunder-flanked because it must not collide with a real parameter**, and unparseable arguments
# are exactly the case where the model's own intent for the argument names is unknowable. No tool
# in this tree declares a parameter of this shape, and `tests/test_invalid_tool_calls.py` scans the
# live registry so a future one fails the suite rather than silently swallowing a promotion — a
# tool that did declare it would have its own calls refused before the body, on every turn.
_UNPARSED_ARGUMENTS = "__unparsed_arguments__"

# The prefix a promoted call is given when the provider named no id, suffixed with its index in the
# reply. `""` was used until a reviewer measured what two of them do: `graph_stream.failed_calls`
# and `ToolCallTrace._issued` both key on a call id and both assume it identifies one call, so two
# id-less calls in one reply made a failure suppress an unrelated result and made an
# `_empty_answer_event` count more refusals than attempts. Distinct from anything a provider mints,
# and stable within the reply, which is the only scope that can collide.
_UNPARSED_CALL_ID = "unparsed-call-"


def model_call_middleware() -> list[Any]:
    """The two model-call observers, as the list `build_langgraph_agent` splices in.

    Order is nesting: the repair is outside the recorder, so a repaired turn books **two** model
    calls — which is what happened, and is the only way the cost of a malformed emission is
    visible. Spliced innermost of everything so the recorded duration is the provider call rather
    than the middleware above it: the context edits run in `wrap_model_call` too, and folding their
    token counting into `chemclaw_model_call_duration_seconds` would put first-party work into the
    histogram an operator reads as "how slow is the endpoint".
    """
    return [PromoteInvalidToolCalls(), RecordModelCalls()]


def _observe(outcome: str, seconds: float) -> None:
    """Book one finished model call: its outcome, and how long the gateway took over it.

    **No `provider` label**, and its removal is the honest half of the collapse to one gateway
    (`D-2026-09-04-a-gateway-is-the-only-provider`). It carried `settings.llm_provider`, which now
    has exactly one possible value — a label with one value is a series that costs cardinality and
    answers nothing, and keeping it as a hardcoded string would publish a distinction this
    deployment can no longer make.
    """
    record_metric(
        lambda metrics: metrics.increment("chemclaw_model_calls_total", labels={"outcome": outcome})
    )
    record_metric(lambda metrics: metrics.observe("chemclaw_model_call_duration_seconds", seconds))


def _record_failure(exc: BaseException, seconds: float) -> None:
    """Classify, count and log one failed model call, then let the caller re-raise.

    The WARNING carries the **exception class**, never the message: an endpoint's error text can
    quote the request, and the request is the chemist's question. The class and the outcome are what
    separate a rate limit from a dead endpoint from a thread that no longer fits, which is the whole
    distinction that was missing.
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


class RecordModelCalls(AgentMiddleware[Any, Any, Any]):
    """Count and time every model call, by what went wrong.

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

    LangChain lets a handler return a `ModelResponse`, a bare `AIMessage` or an
    `ExtendedModelResponse`, and both readers here — the one that counts the failures and the one
    that promotes them — must see the same thing on all three. One reading, so what is counted and
    what is moved cannot disagree about what the model said.
    """
    if isinstance(response, AIMessage):
        return [response]
    inner = getattr(response, "model_response", response)
    result = getattr(inner, "result", None) or []
    return cast(Sequence[BaseMessage], result)


def _bounded_text(value: object) -> str:
    """The tool **name** the model emitted, bounded by the audit budget and deliberately not repr'd.

    One field, and naming it is the correction: this used to serve `BrokenCall.error` as well and
    its docstring still described that field after `_bounded_reason` took it over. Bounded because
    nothing upstream limits what a model may call a tool, and the string reaches a WARNING line and
    — through the promoted call — an audit row, a metric label's comparison and the chemist's
    event stream.

    **Not** repr'd, unlike the parse error and the argument document beside it: `_metric_label`
    compares the name against the names the request actually bound, and a quoted name matches none
    of them — which would clamp every label to `UNKNOWN_TOOL` and lose the distinction the clamp
    exists to keep. Escaping happens at the sink instead (`_count_invalid`), which is what keeps
    the comparison honest and the log line unforgeable at the same time.
    """
    limit = settings.agent_audit_max_arg_chars
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _bounded_reason(value: object) -> str:
    r"""The provider's parse error, escaped and bounded from the **tail**.

    Two departures from `_bounded_text`, and each is a defect this had before it was written.

    **The tail, because the head is a copy of a field printed beside it.** LangChain builds the
    message as `Function {name} arguments:\n\n{document}\n\nare not valid JSON. Received
    JSONDecodeError {reason}` (upstream's `parse_tool_call`), so the head is the
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

    An empty error stays empty rather than becoming `"''"`, because `_count_invalid`'s log line
    tests it for truthiness to decide whether to print the reason or the document — and on the
    streamed shape it always is empty, which is when the document is the only thing there is.
    """
    text = str(value)
    if not text:
        return ""
    limit = settings.agent_audit_max_arg_chars
    quoted = repr(text)
    if len(quoted) <= limit:
        return quoted
    # Slice the *text* and quote the slice, never the other way round. Slicing `quoted` is what the
    # first version did, and it cuts an escape sequence in half: a trailing newline is `\n` in the
    # quoted form, so a cut landing between the two characters left the letter `n` in the reason a
    # chemist reads — a corruption that reads as content. Binary search because `repr` expands by
    # up to four characters per input character, so no fixed slice width is both safe and tight.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(repr(text[-mid:])) <= limit:
            lo = mid
        else:
            hi = mid - 1
    # **`text[-0:]` is the whole string, not the empty one**, so the search failing to fit even one
    # character has to be answered here rather than by the slice. It was not, and the bound was
    # then lost completely: measured at a budget of 0, 1 or 2 against a 100 kB parse error, this
    # returned **100,024** characters — the exact failure the function exists to prevent, at
    # exactly the tightening an operator would make to be safer. `agent_audit_max_arg_chars` is
    # `ge=0` with no floor, and `repr` of a single character is already three characters wide, so
    # any budget under 3 reaches this branch. The ellipsis alone is the honest answer: the budget
    # says there is no room, and something was still cut.
    return "…" + repr(text[-lo:]) if lo else "…"


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


def _count_invalid(request: ModelRequest[Any], failures: list[BrokenCall]) -> None:
    """Count each unparseable call under its tool, and say once what the model emitted.

    **The operator's record, and only the operator's.** The chemist's half used to live here too,
    hand-built; it is now the `tool_failed` that `agent/tool_authz.announce_tool_failures` raises
    for the promoted call, which is the same event every other tool failure has always produced.
    One producer, one shape, one place to change it.

    Once per emission rather than per attempt, because there is no second attempt any more: the
    model corrects inside its own loop, and that iteration is an ordinary one the loop cap counts.

    The counter takes the *clamped* name (`_metric_label`) and the log line takes the model's own
    escaped string, which is the split `audit.metric_tool_name` states: what the model asked for is
    the forensic fact and belongs in the record, and only the unbounded metric *label* is refused.

    **The name is escaped here, not merely bounded.** `_bounded_text` deliberately does not quote —
    `_metric_label` compares the name against the bound tools and a quoted name matches none of
    them — but this line is `%s`-formatted into a log whose default formatter is single-line and
    unescaped (`log_json` ships `False`), so a newline in a model-authored name forged a second
    record reading `… ERROR chemclaw.audit: actor=admin action=approve_plan result=granted`. The
    parse error beside it was escaped and the name was not, which is half a fix. `repr` at the sink
    rather than at the source, so the comparison `_metric_label` makes is untouched.
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

    **The whole mechanism is a change of address.** `ToolNode` iterates `tool_calls`; LangChain puts
    a call whose arguments did not parse on `invalid_tool_calls`; so the call is invisible to every
    control this system has, and that invisibility — not the malformed JSON — is the defect. Moving
    it one field over makes it an ordinary failing tool call, and everything downstream then works
    because it already worked: `agent/tool_authz.announce_tool_failures` raises the `tool_failed`
    the chemist reads, the audit middleware writes the row, the span opens, the authorization gate
    and the dry-run and repeat guards all run, and `surface_domain_errors` hands the model a
    `ToolMessage` it can act on inside its own loop.

    **What this replaces, and why the replacement is smaller.** The first three versions of this
    module retried the model from inside `wrap_model_call`, discarded the reply, and hand-rolled a
    report to three audiences (`D-2026-08-29-a-call-the-tool-chain-never-sees…`,
    `…-a-discarded-call-is-not-a-lost-call`, `…-a-refusal-is-not-a-failure…`). Each round's fix
    introduced the next round's defect, and the reason was structural: a retry taken outside the
    graph is outside every bound the graph has, so each one had to be rebuilt by hand — a
    "never a loop" ceiling because the loop cap could not see it, a reporting ceiling because
    nothing bounded the correction, an announcement rule because no `tool_failed` could be raised,
    and a `graph_stream` guard because the announcement had no call id. Promoting the call costs
    one graph iteration instead of one hidden model call, and every one of those bounds already
    applies to it.

    **The document travels under `_UNPARSED_ARGUMENTS` rather than as `{}`.** An empty argument
    dict is not a safe stand-in: **11 of the 54 in-process tools take no required argument**, so a
    promoted call with empty args would satisfy the schema and *run* — the tool would execute on a
    request the model never successfully expressed. The sentinel makes the promotion refusable
    before the body, and carries the raw document to the one place that can use it: back to the
    model, which is the only party that can re-emit it.

    Both hooks declared, for `RecordModelCalls`'s reason.
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

    The counter and the WARNING are the operator's record and stay exactly where they were. What
    is gone is the chemist-facing half: it is no longer this module's job, because the promoted
    call raises a `tool_failed` through the announcer every other failure already uses.

    Edited in place rather than rebuilt: the response's shape is the handler's choice, so a rebuild
    would have to reproduce the message's id, its usage metadata and its response metadata — three
    things nothing here has an opinion about. The message is this call's own, freshly returned and
    not yet in state.
    """
    failures = invalid_tool_calls(response)
    if not failures:
        return response
    _count_invalid(request, failures)
    for message in _messages_of(response):
        if not isinstance(message, AIMessage) or not message.invalid_tool_calls:
            continue
        promoted: list[Any] = list(message.tool_calls or [])
        # Bounded, because one reply may carry any number of these and each becomes an audit row,
        # a stream event and a `ToolMessage` in the model's own context — see
        # `agent_max_promoted_invalid_calls` for the measurement and for why nothing is lost past
        # the bound. `_count_invalid` above has already counted and named every one of them.
        ceiling = settings.agent_max_promoted_invalid_calls
        entries = message.invalid_tool_calls[:ceiling] if ceiling else message.invalid_tool_calls
        for index, call in enumerate(entries):
            promoted.append(
                {
                    # **Bounded, because this one reaches further than the operator's WARNING.**
                    # It becomes `request.tool_call["name"]`, and from there `audit_events.tool`,
                    # the `chemclaw.tool` span attribute and `ToolFailedEvent.tool` on the
                    # chemist's stream — none of which bounds it, and `agent/audit.py::_recording`
                    # `%s`-formats it into a log line the default formatter does not escape. The
                    # design this replaced bounded the name for exactly this reason and the
                    # promotion dropped the bound; `_bounded_text`'s own docstring already claimed
                    # this was happening. Escaping stays at the sinks, so `_metric_label`'s
                    # comparison against the bound tools is untouched.
                    "name": _bounded_text(call.get("name") or UNKNOWN_TOOL),
                    # The model's own id, kept: it is what pairs the `tool_failed` the announcer
                    # raises with the `tool_call` event the stream already emitted, and dropping it
                    # is what forced the previous design's `graph_stream` guard.
                    #
                    # **A missing id becomes a distinct synthetic one rather than `""`.** A
                    # provider may omit it — measured, both `_convert_dict_to_message` and the
                    # streamed chunk merge yield `id=None` when it does — and two such calls in one
                    # reply then shared the empty string, which two independent readers key on:
                    # `graph_stream.failed_calls` suppressed an unrelated call's `tool_result`, and
                    # `ToolCallTrace._issued` collapsed both into one, so `_empty_answer_event`
                    # printed "1 tool call(s) attempted, 2 refused by a gate" — an impossible count.
                    # The index makes it unique within the reply, which is the scope that collides.
                    "id": str(call.get("id") or "") or f"{_UNPARSED_CALL_ID}{index}",
                    "args": {_UNPARSED_ARGUMENTS: bounded_repr(call.get("args"))},
                    "type": "tool_call",
                }
            )
        message.tool_calls = promoted
        message.invalid_tool_calls = []
    return response


class UnparsedArguments(ChemclawError):
    """The model asked for a tool with arguments that were not valid JSON.

    A `ChemclawError` so the two mechanisms that already exist do the work, which is
    `RepeatedCallRefusal`'s argument exactly: the audit middleware records it as an `error`
    outcome, and `surface_domain_errors` hands the sentence to the model verbatim rather than an
    opaque "Function failed."

    **Deliberately not in `agent/audit.refusal_reason`'s table.** That table names the five *gates*
    — decisions this system made on purpose. A document that will not parse is a fault, so
    `refusal_reason` returns `None` and every surface renders it as one: `Chemclaw3_ui` in the
    failure red, `evals/live` in `tools_failed`, `_TurnLedger` in `tool_failures`. That is the
    correct classification and it is reached by adding nothing.
    """


@wrap_tool_call
async def refuse_unparsed_arguments(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Refuse a promoted call before its body runs — below every gate that decides.

    **Below the gates, so all of them see the call**: the announcer raises `tool_failed`, the
    audit trail records the row, and the authorization, dry-run and repeat guards all run on a call
    the model really did make. That is the whole gain over the design this replaces, where none of
    them could see it.

    **Not innermost, though this said so.** `enforce_plan_approval` and `stamp_plan_link` are
    appended below it whenever a profile enables the harness, so under `harness_enabled` they nest
    inside — measured against a gated profile, not read off the list. Since this raises before
    calling its handler, **the plan gate never sees a promoted call**. That is the right outcome
    and is now written down rather than inherited: arguments that did not parse are not a
    well-formed request for a gate to decide about, so the turn reports a fault (`reason=None`)
    rather than a refusal nothing actually made.

    **Before the body, because an empty argument dict is not safe.** Measured against the live
    registry, **11 of 54** in-process tools have no required argument, so a promotion that dropped
    the malformed document instead of carrying it under `_UNPARSED_ARGUMENTS` would satisfy those
    schemas and execute the tool. This gate runs inside `awrap_tool_call`, which wraps
    `_execute_tool_async` — so it raises before LangGraph validates the arguments and before the
    body is entered, and neither the schema's opinion nor the tool's own defaults are consulted.

    The sentence names the document because the model is the only party that can fix it, and it is
    the only thing either record holds that says what broke.
    """
    arguments = request.tool_call.get("args") or {}
    document = arguments.get(_UNPARSED_ARGUMENTS) if isinstance(arguments, dict) else None
    if document is None:
        return await handler(request)
    raise UnparsedArguments(
        f"The arguments for this call were not valid JSON, so it did not run. What was received "
        f"was {defang(str(document))}. Re-issue the call with complete, valid JSON arguments; if "
        f"you cannot, say what you were unable to do rather than answering as though the tool had "
        f"returned."
    )
