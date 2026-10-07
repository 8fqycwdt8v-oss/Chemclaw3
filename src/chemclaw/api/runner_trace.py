"""Reading a turn's tool calls: the events a call and its result become.

A function of what the graph stream hands over and what the caller injected — no session, no
contextvars — so it can be tested by handing it a call and comparing events. The one write is in
`returned`: a result's full text is persisted through an injected `ResultSink` before the event
naming its ref is yielded, so a client following the ref always finds it. No sink stores nothing.
Calls arrive fully assembled from LangGraph's `updates` stream, so there is no argument reassembly
here.
"""

import logging

from chemclaw.api.events import ResultValue, ToolCallEvent, ToolResultEvent
from chemclaw.api.tool_results import ResultSink, stored_within_cap
from chemclaw.core.config import settings
from chemclaw.core.quantities import labelled_values, returned_values
from chemclaw.kg.note import mentioned_ids

logger = logging.getLogger(__name__)

# How many characters of a tool call's arguments the trace event carries: the same setting the audit
# trail truncates to (`agent/audit.py`).


class ToolCallTrace:
    """One turn's tool calls and results, as the events a surface renders and the evidence it left.

    `issued` announces an assembled call and `returned` reports its result, matched by `call_id`;
    `_issued` keeps the name because a `ToolMessage` does not carry it. One per turn.
    """

    def __init__(self, sink: ResultSink | None = None) -> None:
        """Start an empty trace; one per turn, since every field is scoped to that turn.

        `sink` stores a result's full text so a surface can fetch it (`api/tool_results.py`); `None`
        stores nothing and every `result_ref` stays empty.
        """
        self._sink = sink
        # Every announced call's name, so its result is reported under it; bounded by the loop cap.
        self._issued: dict[str, str] = {}
        # What this turn's tools returned, in full and in order — the evidence the verifier and
        # shape gate check the answer against. Not the emitted events, which carry only a short
        # preview that would make most citations look fabricated. Bounded by the calls in one turn;
        # never leaves the process.
        self.outputs: list[str] = []

    @property
    def called_tools(self) -> list[str]:
        """Every tool this turn issued a call for, in the order the calls were announced.

        A view of `_issued`, so it cannot disagree with the `tool_call` events. Includes calls that
        failed: the question is whether the turn reached for a tool.
        """
        return list(self._issued.values())

    def issued(self, key: str, tool: str, arguments: str) -> ToolCallEvent:
        """Announce one complete call, applying the argument budget and remembering the name.

        Args:
            key: The provider's call id, which is what a later result names.
            tool: The tool's advertised name.
            arguments: The call's arguments, already rendered as text.

        Returns:
            The event a surface renders for this call.
        """
        self._issued[key] = tool
        return ToolCallEvent(tool=tool, arguments=arguments[: settings.agent_audit_max_arg_chars])

    async def returned(
        self, key: str, text: str, *, cut: bool = False, full_ref: str = "", stored_ref: str = ""
    ) -> ToolResultEvent:
        """Record and describe one tool result — this module's one write.

        Ids, numbers and values come off `text`, the full text the model read, since grounding asks
        what was in front of the model. Only the ref varies: `full_ref` (the full output a cut
        kept), else `stored_ref` (already stored by the middleware, so not stored again), else
        `text` is stored now. `text` arrives without the handle line. `key` names the call; `cut`
        says the model received a cut (`tool_result_size.was_cut`). A result whose call was never
        announced is reported under its own id.
        """
        self.outputs.append(text)
        tool = self._issued.get(key) or key
        return ToolResultEvent(
            tool=tool,
            preview=text[: settings.agent_audit_max_arg_chars],
            note_ids=mentioned_ids(text),
            numbers=_capped_numbers(tool, text),
            values=_capped_values(tool, text),
            # Awaited here so the bytes are durable before the ref naming them leaves the process.
            result_ref=full_ref or stored_ref or await stored_within_cap(self._sink, tool, text),
            result_inline=_inline(text),
            result_cut=cut,
        )


def _capped_numbers(tool: str, text: str) -> list[float]:
    """The distinct values a result returned, bounded for the wire, saying so when it bounds them.

    The cap (`stream_max_result_numbers`) rarely fires, so it logs when it does: a silent truncation
    reads as completeness.
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

    Capped independently of `numbers`, so neither list's contents depend on the other's. Empty for a
    non-JSON result: `labelled_values` does not guess names out of prose.
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

    Measured in bytes, since the cap protects a wire. Not logged when empty: nothing is lost, as the
    result stays reachable through its ref.
    """
    if settings.stream_inline_result_bytes <= 0:
        return ""
    return text if len(text.encode("utf-8")) <= settings.stream_inline_result_bytes else ""
