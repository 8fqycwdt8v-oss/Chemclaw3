"""A `document` artefact streamed while the model is still writing the call that creates it.

The `updates` mode delivers a `create_exhibit` call whole at the end; the text exists earlier in the
`messages` mode's `tool_call_chunks`. This reads them for a preview only
(`D-2026-10-03-a-draft-is-read-off-the-arguments-the-model-is-still-writing`): every `tool_call`,
`tool_result` and `exhibit` event still comes from the completed node, a draft frame is never
persisted, and a reassembly mistake costs only a preview the `exhibit` event replaces.

Each frame carries the whole text so far (a dropped frame costs nothing). Frames are throttled by
`_interval_seconds`, so draft bytes are linear in document size, and the throttle is checked before
re-parsing. Before the first frame, arguments are re-parsed only when they have doubled
(`_Call.parse_at`). A call stops streaming when its spec is not an object, its arguments exceed
`_argument_bound`, or its text passes `exhibit_max_spec_bytes`. `close` sends one final `done`
frame per call. A revision by `edits` streams nothing.
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
    sent_bytes: int = 0
    checked_at: float | None = None
    parse_at: int = 0
    stopped: bool = False


class DraftStream:
    """The `exhibit_draft` frames one turn's model chunks produce, call by call.

    One instance per turn, fed every root-agent chunk in order (`feed`) and closed whenever the
    model node completes (`close`), when its calls are whole.
    """

    def __init__(self) -> None:
        """No call in flight."""
        self._calls: dict[tuple[str, int], _Call] = {}

    def feed(self, chunk: Any) -> list[ExhibitDraftEvent]:
        """The frames this chunk's tool-call fragments are due, in call order (usually none).

        Keyed by message and `index`, because only a call's first fragment carries its id and name.
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
            if len(call.arguments) > _argument_bound():
                call.stopped = True
                continue
            now = time.monotonic()
            interval = _interval_seconds(call.sent_bytes)
            # After the first frame, throttle on the last parse, so arguments that stopped growing
            # the text are not re-parsed per chunk; before it, on the arguments' growth.
            if call.sent_chars:
                if call.checked_at is not None and now - call.checked_at < interval:
                    continue
            elif len(call.arguments) < call.parse_at:
                continue
            call.checked_at = now
            call.parse_at = 2 * len(call.arguments)
            if (frame := _frame(call, call.op, done=False)) is not None:
                frames.append(frame)
        return frames

    def close(self) -> list[ExhibitDraftEvent]:
        """The closing `done` frame of every call that grew since its last frame; then forget them.

        Called on each completed root node; only the model node's completion finds calls open.
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


def _interval_seconds(sent_bytes: int) -> float:
    """How long a call waits after a frame of `sent_bytes` before its next one is considered.

    The floor `exhibit_draft_min_interval_ms`, stretched to `sent_bytes /
    exhibit_draft_bytes_per_ms` for long documents: each frame is the whole text, so a fixed
    interval would make draft bytes quadratic in size. It also bounds the parse cost.
    """
    floor = settings.exhibit_draft_min_interval_ms
    return max(floor, sent_bytes / settings.exhibit_draft_bytes_per_ms) / 1000


def _argument_bound() -> int:
    """The longest a call's arguments may be and still hold a spec under `exhibit_max_spec_bytes`.

    The spec cap, plus title and note at their caps fully escaped (`_WORST_ESCAPE`), plus
    `exhibit_draft_argument_slack_chars` for keys and whitespace. Counting characters against a byte
    cap is the safe direction, so a call over this would be refused by the tool.
    """
    escaped = _WORST_ESCAPE * (settings.exhibit_max_title_chars + settings.exhibit_max_note_chars)
    return settings.exhibit_max_spec_bytes + escaped + settings.exhibit_draft_argument_slack_chars


#: The most characters JSON spends on one character of a string: a `\uXXXX` escape is six. A
#: property of the format rather than a threshold anybody tunes, so a constant, not a setting.
_WORST_ESCAPE = 6


def _frame(call: _Call, op: Literal["create", "revise"], *, done: bool) -> ExhibitDraftEvent | None:
    """The frame `call`'s arguments make now, or `None` when there is nothing new to show.

    Stops the call for good when it revises by `edits`, its spec is not an object or is another
    kind, or its text passes the spec cap. A `kind` still being written (`"docu"`) is not yet
    another kind.
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
        # Absent or not yet begun is waiting; anything else — a spec written as a JSON string, a
        # list — is not a document this preview can read however long it grows.
        call.stopped = spec is not None
        return None
    kind, markdown = spec.get("kind"), spec.get("markdown")
    if kind is not None and kind != "document":
        call.stopped = not (isinstance(kind, str) and "document".startswith(kind))
        return None
    if not isinstance(markdown, str):
        return None
    size = len(markdown.encode("utf-8"))
    if size > settings.exhibit_max_spec_bytes:
        call.stopped = True
        return None
    if len(markdown) <= call.sent_chars:
        return None
    call.sent_chars = len(markdown)
    call.sent_bytes = size
    return ExhibitDraftEvent(
        call_id=call.call_id,
        op=op,
        exhibit_id=str(parsed.get("exhibit_id") or "") if op == "revise" else "",
        kind="document" if kind == "document" else "",
        title=str(parsed.get("title") or "") if op == "create" else "",
        markdown=markdown,
        done=done,
    )
