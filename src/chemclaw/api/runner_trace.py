"""Reading a turn's tool calls: the events a call and its result become.

Everything here is a function of what the graph stream hands over and of what the caller injected —
**no ambient state, no session, no contextvars**. The wire budgets it applies are read from
`settings` rather than written as literals, which is the repo's rule for a threshold and does not
make the functions impure: an ENV value is a constant of the process, not state a turn carries.
That is why it lives beside the runner rather than inside it: the runner's own module is a
lifecycle (contextvars, an `AsyncExitStack`, a rollback), and this is the one part of the per-turn
path that can be exercised by handing it a call and comparing the events that come back.

**`returned` is a coroutine and does one write**, which is the one thing here that is not pure and
is worth stating rather than hiding. A tool result is persisted so a surface can fetch the whole of
it (`api/tool_results.py`), and the write has to happen *before* the event naming it is yielded —
announcing a ref and then storing the bytes leaves a window in which a client that follows the ref
finds nothing. The store is reached through an injected `ResultSink`, not through the session id or
a contextvar, so the sentence above stays literally true: this module still does not know what a
session is, and a trace built with no sink (every test that does not care, the CLI paths) behaves
exactly as it did.

**There is no reassembly here any more, and this docstring used to say there was.** `feed`, the
`_names`/`_fragments` buffers it filled and the `flush` that closed out a call whose arguments
ended the stream were written against the previous engine's streamed content shape — a name on one
content, then argument fragments carrying only a `call_id` (D-138, D-159). LangGraph's `updates`
stream hands a *finished* `tool_calls` list over, and `api/graph_stream.py` deliberately does not
read the fragmented `tool_call_chunks` off the token stream — its own docstring gives the reason,
which is the two live defects that reassembly cost. So nothing in `src/` had called `feed` since
that rebuild, `flush()` could only ever return `[]` (it was iterated on every turn), and the
paragraph above named `feed` as the place the one write happens while the write was in `returned`.
Whoever adds a provider that streams argument fragments adds the reassembly back with it; nothing
else in this module changes.
"""

import logging

from chemclaw.api.events import ResultValue, ToolCallEvent, ToolResultEvent
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
    """One turn's tool calls and results, as the events a surface renders and the evidence it left.

    Two methods and no state machine: `issued` announces a call the graph has already assembled,
    `returned` reports the result that answers it. They are matched by the provider's `call_id`,
    which is why `_issued` outlives the call — the name is what the result event reports, and a
    `ToolMessage` does not carry it.

    One per turn, because every field is scoped to that turn.
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

    def issued(self, key: str, tool: str, arguments: str) -> ToolCallEvent:
        """Announce one *complete* call — the decision, with no reassembly in front of it.

        LangGraph's `updates` stream hands over a finished `tool_calls` list, so the graph driver
        (`chemclaw.api.graph_stream`) has nothing to reassemble and calls this directly. What this
        owns is everything below: the argument budget, and remembering the name so the result can
        be reported under it.

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
        """Record and describe one tool result — this module's one write.

        Ids and values come off the *full* text and the preview off the truncated one, for the
        reason `outputs` exists at all: a grounding check asking "was this in front of the model?"
        against 200 characters of a 40-chunk sweep called 39 of 40 citations fabricated in a live
        run, and the re-run with ids fixed still called six verbatim ICH limits invented because
        the figures were only in the preview.

        A result whose call was never announced is reported under its own id rather than under a
        name this trace does not have. Nothing takes that fallback today — a node's update carries
        the `tool_calls` entry before the `ToolMessage` answering it — and a `ToolResultEvent` with
        an empty `tool` would be a surface labelling a value with nothing.

        Args:
            key: The call id this answers, so the result is reported under the call's tool name.
            text: The result's full text.

        Returns:
            The event a surface renders for this result.
        """
        self.outputs.append(text)
        tool = self._issued.get(key) or key
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
