"""Reading a turn's streamed updates: tool-call reassembly and the approval prompt.

Everything here is a function of the provider-shaped objects `chemclaw.api.runner` receives from
`agent.run` and of what the caller injected — **no ambient state, no session, no contextvars**. The
wire budgets it applies are read from `settings` rather than written as literals, which is the
repo's rule for a threshold and does not make the functions impure: an ENV value is a constant of
the process, not state a turn carries. That is why it lives beside the runner
rather than inside it: the runner's own module is a lifecycle (contextvars, an `AsyncExitStack`, a
rollback), and this is the one part of the per-turn path that can be exercised by handing it a
content object and comparing the events that come back.

**`feed` is a coroutine and does one write**, which is the one thing here that is not pure and is
worth stating rather than hiding. A tool result is now persisted so a surface can fetch the whole
of it (`api/tool_results.py`), and the write has to happen *before* the event naming it is yielded
— announcing a ref and then storing the bytes leaves a window in which a client that follows the
ref finds nothing. The store is reached through an injected `ResultSink`, not through the session
id or a contextvar, so the sentence above stays literally true: this module still does not know
what a session is, and a trace built with no sink (every test that does not care, the CLI paths)
behaves exactly as it did.

Duck-typed throughout, deliberately. A provider's function-call and function-result content
classes are rarely stable top-level exports and their shape varies by version — the previous
engine's were not, which is what this was written against — so these match on structure (a
`call_id`/`arguments` pair, a result-bearing attribute) rather than importing a concrete type.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any

from chemclaw.api.events import Event, ResultValue, ToolCallEvent, ToolResultEvent
from chemclaw.api.tool_results import ResultSink
from chemclaw.core.config import settings
from chemclaw.core.quantities import labelled_values, returned_values
from chemclaw.kg.note import mentioned_ids

logger = logging.getLogger(__name__)

# How many characters of a tool call's arguments the trace event carries — enough to see *what*
# was called without streaming a whole evidence payload to the UI. This is the *same* budget the
# audit trail applies (`agent/audit.py`), which is why it now reads the same setting rather than
# repeating its default: the comment here used to say "mirrors the audit trail truncation" beside a
# literal 200, so raising the audit budget for a fuller trail moved one of the two and the claim
# quietly stopped being true (2026-08-05 review).


class ToolCallTrace:
    """Reassemble a streamed function call, so `tool_call` can carry the arguments it promises.

    A streamed call does not arrive as one object. The provider sends the *name* first, on a
    content whose `arguments` is still empty, and then streams the argument JSON as fragments on
    further contents that carry only the `call_id` — no name. Reading name-and-arguments off a
    single content, as this did, therefore matched exactly the one content that never has any
    arguments, and skipped every fragment for want of a name: `ToolCallEvent.arguments` was empty
    on every call ever emitted, and could not have been anything else (D-138). The field is
    documented as "a short argument preview" and read by the UI trace, so this was a promise the
    stream never kept.

    Fragments for one call arrive contiguously, so a call is complete once an update goes by
    without adding to it — that is the flush condition, and it needs no knowledge of which
    content type terminates a call. The event therefore lands slightly later than before: after
    the arguments rather than after the name. That is the more truthful order anyway, because a
    tool cannot run before its arguments are complete.

    **That flush condition was still one step too late** (D-159). For a streamed call the next
    update to arrive is the one carrying the call's *result* — the provider finishes the argument
    stream, the framework runs the tool, and only then does anything else come down the wire. So
    "an update went by without adding to it" fired after execution, and the trace announced
    `predict_pka(...)` once the twenty seconds were already spent. From the chemist's side a
    working calculation and a hung server were the same thing.

    So a call now completes the moment its accumulated arguments **parse as JSON**, which is
    exactly when the provider has finished sending them and before the tool is invoked. No new
    provider signal is needed and D-138's promise is kept: the event still carries the whole
    argument preview, it just no longer waits for the result to prove the arguments ended. The
    update-went-by rule stays as the fallback for arguments that never parse (a provider that
    streams something other than JSON) so nothing can be stranded.

    Results are matched back by `call_id` and emitted as `ToolResultEvent`, which is why
    `_names` outlives the flush: the name is what the result event reports, and it is not on the
    result content.

    Still duck-typed: a function-call content class is rarely a stable top-level export and its
    shape varies by version, so this matches on structure (a `call_id`/`arguments` pair) rather
    than importing a concrete type.
    """

    def __init__(self, sink: ResultSink | None = None) -> None:
        """Start an empty trace; one per turn, since every field is scoped to that turn.

        `sink` is where a result's full text is stored so a surface can fetch it back
        (`api/tool_results.py`); `None` stores nothing and every `result_ref` stays empty, which is
        the honest state and the one every consumer already has to handle. Injected rather than
        resolved from the session id here because this class deliberately knows nothing about
        sessions — see the module docstring.
        """
        self._sink = sink
        self._names: dict[str, str] = {}
        self._fragments: dict[str, list[str]] = {}
        # The name of every call already announced, kept so its result can be reported under the
        # same name. Bounded by the calls in one turn, which the loop cap already bounds.
        self._issued: dict[str, str] = {}
        # What this turn's tools returned, in full, in the order they came back — the evidence the
        # answer verifier and the parameter-shape gate check the answer against. Kept here rather
        # than read back off the emitted `ToolResultEvent`s because those carry a 200-character
        # *preview*: a `gather_evidence` result is ~20,000 characters over 40 chunks, so scoring
        # against the preview would call 39 of its 40 citations fabricated. The budget is right for
        # the UI and wrong for a grounding check, so the two read different things from one place.
        # Bounded by the calls in one turn, like `_issued`, and never leaves the process.
        self.outputs: list[str] = []

    @property
    def called_tools(self) -> list[str]:
        """Every tool this turn issued a call for, in the order the calls were announced.

        Read off `_issued`, which already exists so a result can be reported under its call's name
        — so this is a view of state the trace keeps, not a second ledger that could disagree with
        the `tool_call` events the surface saw. Includes calls that went on to fail: the answer
        gate's question is whether the turn *reached* for a tool, and a failed call did.
        """
        return list(self._issued.values())

    async def feed(self, update: Any) -> list[Event]:
        """Take one streamed update; return the calls it issued and the results it returned.

        A coroutine because storing a result is a database write and it has to complete *before*
        the event naming it is handed back — see the module docstring. With no sink there is
        nothing to await and this is a synchronous function wearing `async`.
        """
        growing: set[str] = set()
        done: set[str] = set()
        results: list[Event] = []
        for content in getattr(update, "contents", None) or []:
            if not (hasattr(content, "arguments") or hasattr(content, "call_id")):
                continue
            name = str(getattr(content, "name", "") or "")
            key = str(getattr(content, "call_id", "") or "") or name
            if not key:
                continue
            if name:
                self._names.setdefault(key, name)
            if key not in self._names and key not in self._issued:
                continue  # a fragment for a call whose opening content we never saw
            arguments = getattr(content, "arguments", None)
            if arguments is None:
                # The call id with no arguments field at all: this is the call's *result* coming
                # back, so it must not count as the call still growing. Note the test is `is
                # None` and not falsiness — an empty string is a real fragment of the argument
                # stream, and treating it as the end flushed the call before its arguments had
                # arrived, which is how this reached a second live run still empty.
                text = _result_text(content)
                if text is not None:
                    results.append(await self.returned(key, text))
                continue
            if isinstance(arguments, Mapping) and arguments:
                # A structured argument object: the call arrived whole rather than streamed, so it
                # is finished now, and waiting would only delay it behind the next update's text.
                #
                # **The test is the argument's type, not the presence of a name**, and that
                # distinction was measured rather than reasoned. This branch used to read
                # `if name and arguments:` on the stated assumption that "the streamed shape never
                # looks like this — its named content carries empty arguments and its fragments
                # carry no name". True of Anthropic. False of the OpenAI Responses API, which puts
                # the name on *every* `response.function_call_arguments.delta` — so each fragment
                # matched, overwrote the ones before it, and flushed. One eight-fragment call
                # announced **ten `tool_call` events against one `tool_result`**, the first
                # carrying `{"t` as if it were the whole argument document
                # (`docs/archive/storm-2026-08-04.md`).
                #
                # A name says nothing about completeness; only the arguments do. A string is
                # therefore always accumulated below and finished by `_arguments_complete`, which
                # closes a single complete fragment on the same update anyway — so the whole-call
                # case that genuinely sends a string loses nothing.
                self._fragments[key] = [json.dumps(arguments)]
                done.add(key)
            else:
                # Only the streamed shape reaches here: the whole-object case returned above, so a
                # non-empty `Mapping` is impossible in this branch. It used to be tested for again
                # anyway, and the 2026-08-05 review enumerated the input space to show that no value
                # takes it.
                fragments = self._fragments.setdefault(key, [])
                if isinstance(arguments, str) and arguments:
                    fragments.append(arguments)
            growing.add(key)
            if _arguments_complete(self._fragments.get(key)):
                # The provider has finished sending this call's arguments, so the tool is about
                # to run. Announcing now is the whole point of D-159: waiting for the next update
                # means waiting for the result, and the wait between them is the part worth
                # showing.
                done.add(key)
        return [*self._take((set(self._fragments) - growing) | done), *results]

    def flush(self) -> list[Event]:
        """Emit whatever is still open — the stream ended before an untouched update arrived."""
        return self._take(set(self._fragments))

    def issued(self, key: str, tool: str, arguments: str) -> ToolCallEvent:
        """Announce one *complete* call — the decision, with no reassembly in front of it.

        `feed` reaches this through `_take` after buffering fragments, because a token stream
        delivers a call's arguments in pieces. LangGraph's `updates` stream hands over a finished
        `tool_calls` list,
        so the graph driver has nothing to reassemble and calls this directly
        (`chemclaw.api.graph_stream`). What must not differ between the two is everything below:
        the argument budget, and remembering the name so the result can be reported under it.

        Args:
            key: The provider's call id, which is what a later result names.
            tool: The tool's advertised name.
            arguments: The call's arguments, already rendered as text.

        Returns:
            The event a surface renders for this call.
        """
        self._issued[key] = tool
        return ToolCallEvent(tool=tool, arguments=arguments[: settings.agent_audit_max_arg_chars])

    async def returned(self, key: str, text: str) -> ToolResultEvent:
        """Record and describe one tool result — the decision, shared by both engines.

        Ids and values come off the *full* text and the preview off the truncated one, for the
        reason `outputs` exists at all: a grounding check asking "was this in front of the model?"
        against 200 characters of a 40-chunk sweep called 39 of 40 citations fabricated in a live
        run, and the re-run with ids fixed still called six verbatim ICH limits invented because
        the figures were only in the preview.

        Args:
            key: The call id this answers, so the result is reported under the call's tool name.
            text: The result's full text.

        Returns:
            The event a surface renders for this result.
        """
        self.outputs.append(text)
        tool = self.tool_of(key)
        return ToolResultEvent(
            tool=tool,
            preview=text[: settings.agent_audit_max_arg_chars],
            note_ids=mentioned_ids(text),
            numbers=_capped_numbers(tool, text),
            values=_capped_values(tool, text),
            # Awaited here rather than by the caller so the bytes are durable before the ref
            # naming them leaves the process.
            result_ref=await _stored_ref(self._sink, tool, text),
            result_inline=_inline(text),
        )

    def tool_of(self, key: str) -> str:
        """The tool name one call id was issued under, falling back to the id itself.

        Public because a *failed* call has to be reported under the same name as a successful one:
        `api/graph_stream` names an unsignalled failure with this, and a second copy of the lookup
        would let one surface label a call `predict_pka` and the other label it `call_017`. The
        fallbacks are ordered because both halves can be missing — `_issued` is written when the
        call is announced and `_names` when a streamed fragment first carries a name.
        """
        return self._issued.get(key) or self._names.get(key, key)

    def _take(self, keys: set[str]) -> list[Event]:
        events: list[Event] = []
        for key in [k for k in self._fragments if k in keys]:
            arguments = "".join(self._fragments.pop(key))
            # Remembered rather than discarded: the result content carries no name, so `issued` is
            # what lets `ToolResultEvent` report which tool answered.
            events.append(self.issued(key, self._names.pop(key, key), arguments))
        return events


def _capped_numbers(tool: str, text: str) -> list[float]:
    """The distinct values a result returned, bounded for the wire, saying so when it bounds them.

    The cap is unreachable in normal traffic (`stream_max_result_numbers`), which is exactly
    why the log line matters: the one time it fires, a consumer told to trust this list would be
    trusting an incomplete one, and nothing else in the event would say so. This repository's rule
    is that a silent truncation reads as completeness — it is the whole reason the preview needed a
    companion field in the first place.
    """
    values = returned_values(text)
    if len(values) <= settings.stream_max_result_numbers:
        return values
    logger.warning(
        "tool %s returned %d distinct numeric values; the trace event carries the first %d",
        tool,
        len(values),
        settings.stream_max_result_numbers,
    )
    return values[: settings.stream_max_result_numbers]


def _capped_values(tool: str, text: str) -> list[ResultValue]:
    """The named values a JSON result returned, under the same cap the bare numbers take.

    Same bound and the same reason: this list goes to a browser, so it must be bounded, and the
    bound is the operator's rather than a literal. Capped independently of `numbers` because they
    are different lists over the same result — a payload can carry fifty distinct values under
    forty labels — and sharing one budget between them would make either one's contents depend on
    the other's.

    Silent on a non-JSON result, which is not a failure: `labelled_values` refuses to guess a name
    out of prose, and the figures are on the wire regardless.
    """
    quantities = labelled_values(text)
    if len(quantities) > settings.stream_max_result_numbers:
        logger.warning(
            "tool %s returned %d labelled values; the trace event carries the first %d",
            tool,
            len(quantities),
            settings.stream_max_result_numbers,
        )
        quantities = quantities[: settings.stream_max_result_numbers]
    return [ResultValue(label=q.label, value=q.value, unit=q.unit) for q in quantities]


def _inline(text: str) -> str:
    """The result itself when it is small enough to ride along, or `""` when it is not.

    Measured in bytes for the same reason `_stored_ref` measures in bytes: the cap is protecting a
    wire, and a result full of multi-byte characters is up to four times its length in what is
    actually sent.

    No log line on the empty case, and that is the difference from every other cap in this file.
    Those are *truncations*, where silence reads as completeness; this is a shortcut declining to
    apply, and the result stays reachable through its ref exactly as it always was. Nothing is
    lost, so there is nothing to report.
    """
    if settings.stream_inline_result_bytes <= 0:
        return ""
    return text if len(text.encode("utf-8")) <= settings.stream_inline_result_bytes else ""


async def _stored_ref(sink: ResultSink | None, tool: str, text: str) -> str:
    """Store `text` and return the ref a surface fetches it by, or `""` when it was not stored.

    Deliberately the same shape as `_capped_numbers` above, because it is the same rule one step
    further on: the bound comes from `settings` rather than a literal, an over-cap result is
    *refused rather than trimmed*, and the refusal is logged. Trimming would be the worse failure
    here — a truncated `ScreenResult` is still valid JSON and would render as a complete hazard
    screen with flags missing, which is precisely the "silent truncation reads as completeness"
    problem the numbers cap exists to avoid, made worse by the payload looking whole.

    Measured in bytes, not characters, because the cap is protecting a `BYTEA` column: a result
    full of multi-byte characters is up to four times its length in what is actually written.

    `""` covers every way a result can fail to be stored — no sink, over the cap, or a write that
    raised (swallowed one layer down in `session_sink`). One value, one meaning, and none of them
    fails the turn.
    """
    if sink is None or settings.stream_max_result_bytes <= 0:
        return ""
    size = len(text.encode("utf-8"))
    if size > settings.stream_max_result_bytes:
        logger.warning(
            "tool %s returned %d bytes, over the %d-byte store cap; its trace event carries no "
            "result_ref and the full result is not fetchable",
            tool,
            size,
            settings.stream_max_result_bytes,
        )
        return ""
    return await sink(tool, text)


def _arguments_complete(fragments: list[str] | None) -> bool:
    """Whether the accumulated argument fragments are a finished JSON value.

    This is the signal that replaces "wait for the next update" as the moment a call is announced
    (D-159). It works because the provider streams a tool call's arguments as one JSON document
    and does not invoke the tool until that document is closed — so a successful parse is exactly
    the boundary between "still arriving" and "about to run", and it is knowable from the bytes
    already in hand rather than from something that follows.

    False for anything that does not parse, which keeps the old update-went-by rule as the
    fallback: a provider streaming a non-JSON argument format still gets its call announced, just
    at the previous, later moment. Nothing is stranded, and nothing regresses.
    """
    if not fragments:
        return False
    try:
        json.loads("".join(fragments))
    except (ValueError, TypeError):
        return False
    return True


def _result_text(content: Any, /) -> str | None:
    """What a function-result content returned, in full, or None when it carries nothing.

    Duck-typed over the attribute the framework version happens to use, for the same reason the
    call side is: the concrete content class is not a stable export. A result that is empty or
    unreadable yields None rather than a value the trace does not have — the trace should not claim
    one, and the verifier must not treat "nothing came back" as evidence.

    Untruncated on purpose. The caller truncates for the wire (`agent_audit_max_arg_chars`, the UI's
    budget) and keeps the whole text for grounding, which are different jobs with different right
    answers; returning the preview here would silently make the second one impossible.

    Failures are deliberately not reported here. A raised call already surfaces as
    `ToolFailedEvent` through the tool middleware, which has the exception and its message; a
    second event for the same outcome would leave a consumer choosing which to believe.
    """
    for attribute in ("result", "output", "value", "text"):
        value = getattr(content, attribute, None)
        if value is None or value == "":
            continue
        return value if isinstance(value, str) else str(value)
    return None
