"""Stored MAF messages convert to LangChain ones, or say why not (M6, D-2026-08-10).

Rewriting `session_messages` is the one irreversible step in the migration, so the function that
decides what each row *becomes* is tested against payloads produced by MAF itself rather than
against hand-written dicts. A hand-written fixture proves the converter agrees with whoever wrote
the fixture; a real `Message.to_dict()` proves it agrees with the thing that wrote the rows.
"""

from typing import Any

import pytest
from agent_framework import Content, Message
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chemclaw.agent.message_migration import UnconvertibleMessage, to_langchain


def _stored(role: str, *contents: Content) -> dict[str, Any]:
    """One `session_messages.message` payload, produced the way the store produces them."""
    return Message(role=role, contents=list(contents)).to_dict()


def test_a_plain_exchange_round_trips_to_the_right_types() -> None:
    """The three text roles map to the three LangChain text messages, content intact."""
    user = to_langchain(_stored("user", Content.from_text("what is the pKa?")))
    assistant = to_langchain(_stored("assistant", Content.from_text("about 15.9")))
    system = to_langchain(_stored("system", Content.from_text("you are Chemclaw")))

    assert isinstance(user, HumanMessage) and user.content == "what is the pKa?"
    assert isinstance(assistant, AIMessage) and assistant.content == "about 15.9"
    assert isinstance(system, SystemMessage) and system.content == "you are Chemclaw"


def test_a_tool_call_keeps_its_name_arguments_and_id() -> None:
    """MAF's `arguments` is LangChain's `args` — a rename, not a parse.

    The id matters most: it is what pairs the call with its result, and a transcript whose pairs
    are broken is one no provider will accept as a continuation.
    """
    stored = _stored(
        "assistant",
        Content.from_function_call(
            call_id="call-1", name="predict_pka", arguments={"smiles": "CCO"}
        ),
    )

    converted = to_langchain(stored)

    assert isinstance(converted, AIMessage)
    assert converted.tool_calls == [
        {"name": "predict_pka", "args": {"smiles": "CCO"}, "id": "call-1", "type": "tool_call"}
    ]


def test_a_tool_result_answers_the_call_it_belongs_to() -> None:
    """A `ToolMessage` carries `tool_call_id`, so the pair survives the conversion."""
    stored = _stored("tool", Content.from_function_result(call_id="call-1", result="pKa 15.9"))

    converted = to_langchain(stored)

    assert isinstance(converted, ToolMessage)
    assert (converted.content, converted.tool_call_id) == ("pKa 15.9", "call-1")


def test_a_structured_result_falls_back_to_its_rendered_items() -> None:
    """A tool that returned an object is still readable, because MAF stored both forms.

    `result` is whatever the tool returned and `items` is the same value already rendered into
    content parts, so a non-string result has a text form to fall back to rather than a `repr`.
    """
    stored = _stored(
        "tool", Content.from_function_result(call_id="call-2", result={"pka": 15.9, "units": ""})
    )

    converted = to_langchain(stored)

    assert isinstance(converted, ToolMessage)
    assert converted.content, "a structured result converted to empty text"


def test_an_assistant_turn_that_both_speaks_and_calls_keeps_both() -> None:
    """Text and a tool call in one message is the ordinary streaming shape, not an edge case."""
    stored = _stored(
        "assistant",
        Content.from_text("let me compute that"),
        Content.from_function_call(call_id="call-3", name="predict_pka", arguments={"smiles": "O"}),
    )

    converted = to_langchain(stored)

    assert isinstance(converted, AIMessage)
    assert converted.content == "let me compute that"
    assert [call["name"] for call in converted.tool_calls] == ["predict_pka"]


def test_an_unknown_role_is_refused_rather_than_guessed_at() -> None:
    """Stopping beats coercing: a message that reaches the model subtly wrong is worse.

    The rows being converted are a real conversation history, and there is no example to check a
    guess against — so the migration names the row it cannot read and stops.
    """
    with pytest.raises(UnconvertibleMessage, match="unknown role"):
        to_langchain({"type": "message", "role": "developer", "contents": []})


def test_a_tool_result_with_no_call_id_is_refused() -> None:
    """A result answering nothing is a malformed exchange every provider rejects."""
    with pytest.raises(UnconvertibleMessage, match="call_id"):
        to_langchain(
            {
                "type": "message",
                "role": "tool",
                "contents": [{"type": "function_result", "result": "orphaned"}],
            }
        )


def test_a_tool_message_holding_no_result_is_refused() -> None:
    """The `tool` role with no `function_result` is not something to invent a body for."""
    with pytest.raises(UnconvertibleMessage, match="function_result"):
        to_langchain({"type": "message", "role": "tool", "contents": []})
