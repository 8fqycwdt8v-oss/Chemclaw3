"""A `document` artefact streamed while the model is still writing the call that creates it.

**Why this reads the fragmented tool-call chunks that `graph_stream` otherwise refuses to.** A
report drafted into `create_exhibit` is a minute of generation that arrives, through the `updates`
mode, as one whole call at the end — so the chemist watches nothing and then everything. The text
exists earlier, in the `messages` mode's `tool_call_chunks`, which `graph_stream` deliberately does
not read *calls* from: reassembling them is what cost the previous engine two live-run defects
(D-138). This module reads them for a **preview** and nothing else
(`D-2026-10-03-a-draft-is-read-off-the-arguments-the-model-is-still-writing`): every
`tool_call`, `tool_result` and `exhibit` event still comes from the completed node, a frame here is
never persisted and never decides anything, and a reassembly mistake costs a wrong preview that the
`exhibit` event then replaces — the failure mode a preview is allowed to have.

**Whole text, throttled, growth only, capped.** `markdown` is everything so far, so a dropped frame
costs nothing; frames of one call are at least `exhibit_draft_min_interval_ms` apart, and the
throttle is checked *before* the arguments are re-parsed, so the cost of parsing a growing document
is bounded by the frame rate rather than by the chunk rate; a frame is sent only when the text
grew; past `exhibit_max_spec_bytes` the call stops streaming, because the tool will refuse it.
When the model finishes the call (`close`), one last `done` frame carries whatever the throttle
held back — once per call, so it is outside the throttle by construction.

A revision by `edits` streams nothing: what it holds is replacements, not the document.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessageChunk
from langchain_core.utils.json import parse_partial_json

from chemclaw.api.events import ExhibitDraftEvent
from chemclaw.core.config import settings

#: The tools whose arguments can carry a whole document, by the operation the draft announces.
_DRAFTED: dict[str, Literal["create", "revise"]] = {
    "create_exhibit": "create",
    "revise_exhibit": "revise",
}


@dataclass
class _Call:
    """One tool call being generated: its arguments so far and what has been sent of them."""

    call_id: str
    op: Literal["create", "revise"] | None
    arguments: str = ""
    sent_chars: int = 0
    checked_at: float | None = None
    stopped: bool = False


class DraftStream:
    """The `exhibit_draft` frames one turn's model chunks produce, call by call.

    One instance per turn, fed every root-agent chunk in order (`feed`) and closed whenever the
    model node completes (`close`), which is the moment its calls are whole.
    """

    def __init__(self) -> None:
        """No call in flight."""
        self._calls: dict[tuple[str, int], _Call] = {}

    def feed(self, chunk: Any) -> list[ExhibitDraftEvent]:
        """The frames this chunk's tool-call fragments are due, in call order (usually none).

        A fragment is keyed by its message and its `index`, because only the first fragment of a
        call carries the call's id and name and every later one carries the index alone.
        """
        if not isinstance(chunk, AIMessageChunk):
            return []
        frames: list[ExhibitDraftEvent] = []
        for fragment in chunk.tool_call_chunks:
            key = (str(chunk.id or ""), int(fragment.get("index") or 0))
            call = self._calls.get(key)
            if call is None:
                name = fragment.get("name")
                if not name:
                    continue
                call = _Call(call_id=str(fragment.get("id") or ""), op=_DRAFTED.get(str(name)))
                self._calls[key] = call
            if call.op is None or call.stopped:
                continue
            call.arguments += str(fragment.get("args") or "")
            now = time.monotonic()
            interval = settings.exhibit_draft_min_interval_ms / 1000
            # Once a frame has gone, throttled on the last *parse* rather than the last frame, so
            # arguments that stopped growing the text (a title written after the spec) are not
            # re-parsed per chunk. Before it, every fragment is parsed — the arguments are still a
            # title and a kind — so the first frame goes the moment there is text to show.
            if call.sent_chars and call.checked_at is not None and now - call.checked_at < interval:
                continue
            call.checked_at = now
            if (frame := _frame(call, call.op, done=False)) is not None:
                frames.append(frame)
        return frames

    def close(self) -> list[ExhibitDraftEvent]:
        """The closing `done` frame of every call that grew since its last frame; then forget them.

        Called on each completed root node: the model node's completion is when its calls are
        whole, and any other node finds nothing open.
        """
        frames = [
            frame
            for call in self._calls.values()
            if call.op is not None
            and not call.stopped
            and (frame := _frame(call, call.op, done=True)) is not None
        ]
        self._calls.clear()
        return frames


def _frame(call: _Call, op: Literal["create", "revise"], *, done: bool) -> ExhibitDraftEvent | None:
    """The frame `call`'s arguments make now, or `None` when there is nothing new to show.

    Stops the call for good when it revises by `edits`, its spec is another kind, or its text
    passes the spec cap. A `kind` still being written (`"docu"`) is not yet another kind.
    """
    try:
        parsed = parse_partial_json(call.arguments) if call.arguments else None
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("edits") is not None:
        call.stopped = True
        return None
    spec = parsed.get("spec")
    if not isinstance(spec, dict):
        return None
    kind, markdown = spec.get("kind"), spec.get("markdown")
    if kind is not None and kind != "document":
        call.stopped = not (isinstance(kind, str) and "document".startswith(kind))
        return None
    if not isinstance(markdown, str):
        return None
    if len(markdown.encode("utf-8")) > settings.exhibit_max_spec_bytes:
        call.stopped = True
        return None
    if len(markdown) <= call.sent_chars:
        return None
    call.sent_chars = len(markdown)
    return ExhibitDraftEvent(
        call_id=call.call_id,
        op=op,
        exhibit_id=str(parsed.get("exhibit_id") or "") if op == "revise" else "",
        kind="document" if kind == "document" else "",
        title=str(parsed.get("title") or "") if op == "create" else "",
        markdown=markdown,
        done=done,
    )
