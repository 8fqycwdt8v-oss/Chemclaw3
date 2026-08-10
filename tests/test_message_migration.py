"""Stored MAF messages convert to LangChain ones, or say why not (M6, D-2026-08-10).

Rewriting `session_messages` is the one irreversible step in the migration, so the function that
decides what each row *becomes* is tested against payloads produced by MAF itself rather than
against hand-written dicts. A hand-written fixture proves the converter agrees with whoever wrote
the fixture; a real `Message.to_dict()` proves it agrees with the thing that wrote the rows.
"""

import asyncio
from typing import Any

import pytest
from agent_framework import Content, Message
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from psycopg.types.json import Jsonb

from chemclaw.agent.message_migration import (
    LANGCHAIN_SHAPE,
    MAF_SHAPE,
    UnconvertibleMessage,
    convert_stored_messages,
    to_langchain,
)
from chemclaw.agent.session_store import PostgresHistoryProvider
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


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


# --- the rehearsal, against a real table ---------------------------------------------------------
#
# The plan names this as the mitigation for the migration's one irreversible step, and it is a
# different test from everything above: those prove the conversion is right about a payload, this
# proves the *pass* is right about a table. Rows are seeded through `PostgresHistoryProvider` rather
# than by INSERT, so what is converted is what the production writer actually stores — a hand-built
# row would rehearse a shape nobody writes.


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _seeded(session_id: str) -> PostgresHistoryProvider:
    """A migrated database holding one realistic exchange written by the real provider."""
    await migrated_db_or_skip()
    provider = PostgresHistoryProvider()
    await provider.save_messages(
        session_id,
        [
            Message(role="user", contents=[Content.from_text("what is the pKa of phenol?")]),
            Message(
                role="assistant",
                contents=[
                    Content.from_text("computing"),
                    Content.from_function_call(
                        call_id="c1", name="predict_pka", arguments={"smiles": "Oc1ccccc1"}
                    ),
                ],
            ),
            Message(
                role="tool",
                contents=[Content.from_function_result(call_id="c1", result="pKa 9.95")],
            ),
            Message(role="assistant", contents=[Content.from_text("about 9.95")]),
        ],
    )
    return provider


def test_a_real_stored_conversation_converts_whole() -> None:
    """Every row a real turn wrote converts, and the exchange survives readable.

    The pairing is what is actually at risk: a conversion that dropped `tool_call_id` would leave a
    transcript no provider accepts as a continuation, and nothing about a per-row conversion makes
    that visible one row at a time.
    """
    session_id = "sess-m6-rehearsal"
    _run(_seeded(session_id))

    outcome = _run(convert_stored_messages())

    assert outcome.converted >= 4

    # Asserted per session rather than through `outcome.is_complete()`: the pass converts the whole
    # table, so a refusal deliberately planted by another test in this file would make a global
    # assertion depend on test order. What this test claims is about *its* conversation.
    rows = _run(_rows_for(session_id))
    shapes = {shape for _, shape in rows}
    assert shapes == {LANGCHAIN_SHAPE}, f"unconverted rows left behind: {shapes}"

    restored = [messages_from_dict([payload])[0] for payload, _ in rows]
    assert [type(m).__name__ for m in restored] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    call_ids = {c["id"] for m in restored if isinstance(m, AIMessage) for c in m.tool_calls}
    answered = {m.tool_call_id for m in restored if isinstance(m, ToolMessage)}
    assert call_ids == answered, "the call/result pairing did not survive the conversion"


def test_a_second_pass_converts_nothing() -> None:
    """Resumability: the pass selects only rows still stamped `maf`, so re-running is a no-op.

    That is what makes an interrupted conversion safe to simply run again — and it is worth an
    assertion rather than an argument, because "idempotent" is the sort of claim that is true until
    someone adds an `OR message_shape IS NULL`.
    """
    _run(_seeded("sess-m6-idempotent"))
    _run(convert_stored_messages())

    assert _run(convert_stored_messages()).converted == 0


def test_a_row_the_converter_refuses_is_left_exactly_as_it_was() -> None:
    """A refusal must not consume the row, or the evidence is gone and the pass cannot resume.

    Aborting the whole pass instead was the alternative, and it is worse: one unreadable message
    would block every row after it. Reporting the id and moving on is what lets an operator look at
    the row while the rest of the table converts.
    """
    session_id = "sess-m6-refused"
    _run(_seeded(session_id))
    bad = _run(_insert_raw(session_id, {"type": "message", "role": "developer", "contents": []}))

    outcome = _run(convert_stored_messages())

    assert bad in outcome.refused
    shapes = dict(_run(_shape_of(bad)))
    assert shapes[bad] == MAF_SHAPE, "a refused row was stamped as converted"


async def _rows_for(session_id: str) -> list[tuple[dict[str, Any], str]]:
    """Every stored row for one session, as `(payload, shape)` in insertion order."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT message, message_shape FROM session_messages WHERE session_id = %s ORDER BY id",
            (session_id,),
        )
        return [(row[0], row[1]) for row in await cur.fetchall()]


async def _insert_raw(session_id: str, payload: dict[str, Any]) -> int:
    """Insert a row the provider would never write, to exercise the refusal path."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO session_messages (session_id, message) VALUES (%s, %s) RETURNING id",
            (session_id, Jsonb(payload)),
        )
        row = await cur.fetchone()
        await conn.commit()
        return int(row[0])  # type: ignore[index]


async def _shape_of(row_id: int) -> list[tuple[int, str]]:
    """The stamp on one row."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("SELECT id, message_shape FROM session_messages WHERE id = %s", (row_id,))
        return [(int(r[0]), r[1]) for r in await cur.fetchall()]
