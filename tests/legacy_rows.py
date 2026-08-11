"""MAF-shaped `session_messages` payloads, frozen as literals — the rows a live database still has.

Every row written before M6's conversion pass is an `agent_framework.Message.to_dict()`, and the
pass is resumable, so a real table holds both shapes indefinitely. `durable/retention.py` and
`agent/session_store.py` both have to read them, and their tests have to produce them.

**Literals, not constructed through MAF.** The fixtures used to build a `Message` and call
`to_dict()`, which was the right call while the framework was installed: the assertion was about
what MAF *wrote*, so constructing the payload by hand would only have proven the test agreed with
itself. That argument inverts once the dependency is gone. These bytes are frozen historical data —
what some version of some library once wrote and what a production table therefore contains — and a
fixture that could only be produced by re-installing that library is a fixture that cannot outlive
it.

Captured from `agent-framework-core` 1.11.0 by round-tripping the real constructors, verbatim
including `additional_properties`. Nothing here may be "tidied": a field dropped because it looked
redundant is a field the reader under test would then never meet.
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

    A non-string result is stored **JSON-serialised**, with `items` carrying the same rendering —
    that is what MAF did, and it is the reason `to_langchain` has a fallback at all. Reproduced
    rather than simplified, because a fixture that only ever produced strings would never exercise
    the branch that exists for the other case.
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
