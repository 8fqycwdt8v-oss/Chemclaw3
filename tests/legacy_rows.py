"""MAF-shaped `session_messages` payloads, frozen as literals — the rows a live database still has.

Rows written before the conversion pass are `agent_framework.Message.to_dict()` output, and the
pass is resumable, so a table holds both shapes. `durable/retention.py` and
`agent/session_store.py` must read them. Captured verbatim from `agent-framework-core` 1.11.0; do
not tidy them, since a dropped field is one the reader under test would never meet.
"""

import json
from typing import Any


def legacy_message(role: str, *contents: dict[str, Any]) -> dict[str, Any]:
    """One stored row of any shape, from the content parts it carried."""
    return {
        "type": "message",
        "role": role,
        "contents": list(contents),
        "additional_properties": {},
    }


def text_content(text: str) -> dict[str, Any]:
    """A prose content part, as MAF stored it."""
    return {"type": "text", "text": text, "additional_properties": {}}


def call_content(
    call_id: str, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A tool-call content part, as MAF stored it."""
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments if arguments is not None else {},
        "additional_properties": {},
    }


def result_content(call_id: str, result: Any = "ok") -> dict[str, Any]:
    """A tool-result content part, as MAF stored it.

    A non-string result is stored JSON-serialised, with `items` carrying the same rendering — the
    case `to_langchain`'s fallback exists for.
    """
    rendered = result if isinstance(result, str) else json.dumps(result)
    return {
        "type": "function_result",
        "call_id": call_id,
        "result": rendered,
        "items": [text_content(rendered)],
        "additional_properties": {},
    }


def legacy_text(role: str, text: str) -> dict[str, Any]:
    """One prose message, as MAF stored it."""
    return {
        "type": "message",
        "role": role,
        "contents": [{"type": "text", "text": text, "additional_properties": {}}],
        "additional_properties": {},
    }


def legacy_call(
    call_id: str, name: str = "screen_hazards", text: str = "checking"
) -> dict[str, Any]:
    """An assistant message carrying prose and one tool call, as MAF stored it."""
    return {
        "type": "message",
        "role": "assistant",
        "contents": [
            {"type": "text", "text": text, "additional_properties": {}},
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": {},
                "additional_properties": {},
            },
        ],
        "additional_properties": {},
    }


def legacy_result(call_id: str, result: str = "ok") -> dict[str, Any]:
    """The tool message answering `call_id`, as MAF stored it."""
    return {
        "type": "message",
        "role": "tool",
        "contents": [
            {
                "type": "function_result",
                "call_id": call_id,
                "result": result,
                "items": [{"type": "text", "text": result, "additional_properties": {}}],
                "additional_properties": {},
            }
        ],
        "additional_properties": {},
    }
