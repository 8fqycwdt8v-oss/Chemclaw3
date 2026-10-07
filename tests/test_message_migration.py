"""Stored MAF messages convert to LangChain ones, or say why not.

Rewriting `session_messages` is irreversible, so the converter is tested against the payloads MAF
actually wrote, frozen in `tests/legacy_rows.py` and verified byte-for-byte against the real
constructors when captured, so the fixture outlives the library.
"""

import asyncio
import json
from typing import Any

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from psycopg.types.json import Jsonb

from chemclaw.agent.audit import NullAuditSink
from chemclaw.agent.checkpointer import CHECKPOINT_TABLES, checkpointer, close_checkpointer
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.leaver import erase_actor
from chemclaw.agent.message_migration import (
    _MARK_CONVERTED,
    LANGCHAIN_SHAPE,
    MAF_SHAPE,
    UnconvertibleMessage,
    convert_stored_messages,
    to_langchain,
)
from chemclaw.agent.session_store import (
    SessionOwnerStore,
    is_degraded_render,
    message_from_row,
)
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.migrate import migrate
from tests.legacy_rows import (
    call_content,
    legacy_message,
    result_content,
    text_content,
)
from tests.pg import (
    TEST_SCHEMA,
    create_test_schema,
    drop_test_schema,
    migrated_db_or_skip,
    schema_dsn,
)


class _Replier(GenericFakeChatModel):
    """Answers once, and binds tools without honouring them (as `test_langgraph_agent` does)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _replies(text: str) -> Any:
    """A fake model that answers `text` and calls nothing."""
    return _Replier(messages=iter([AIMessage(content=text)]))


def test_a_plain_exchange_round_trips_to_the_right_types() -> None:
    """The three text roles map to the three LangChain text messages, content intact."""
    user = to_langchain(legacy_message("user", text_content("what is the pKa?")))
    assistant = to_langchain(legacy_message("assistant", text_content("about 15.9")))
    system = to_langchain(legacy_message("system", text_content("you are Chemclaw")))

    assert isinstance(user, HumanMessage) and user.content == "what is the pKa?"
    assert isinstance(assistant, AIMessage) and assistant.content == "about 15.9"
    assert isinstance(system, SystemMessage) and system.content == "you are Chemclaw"


def test_a_tool_call_keeps_its_name_arguments_and_id() -> None:
    """MAF's `arguments` is LangChain's `args` — a rename, not a parse.

    The id matters most: it is what pairs the call with its result, and a transcript whose pairs
    are broken is one no provider will accept as a continuation.
    """
    stored = legacy_message(
        "assistant",
        call_content("call-1", "predict_pka", {"smiles": "CCO"}),
    )

    converted = to_langchain(stored)

    assert isinstance(converted, AIMessage)
    assert converted.tool_calls == [
        {"name": "predict_pka", "args": {"smiles": "CCO"}, "id": "call-1", "type": "tool_call"}
    ]


def test_a_tool_result_answers_the_call_it_belongs_to() -> None:
    """A `ToolMessage` carries `tool_call_id`, so the pair survives the conversion."""
    stored = legacy_message("tool", result_content("call-1", "pKa 15.9"))

    converted = to_langchain(stored)

    assert isinstance(converted, ToolMessage)
    assert (converted.content, converted.tool_call_id) == ("pKa 15.9", "call-1")


def test_a_structured_result_falls_back_to_its_rendered_items() -> None:
    """A tool that returned an object is still readable, because MAF stored both forms.

    `result` is whatever the tool returned and `items` is the same value already rendered into
    content parts, so a non-string result has a text form to fall back to rather than a `repr`.
    """
    stored = legacy_message("tool", result_content("call-2", {"pka": 15.9, "units": ""}))

    converted = to_langchain(stored)

    assert isinstance(converted, ToolMessage)
    assert converted.content, "a structured result converted to empty text"


def test_an_assistant_turn_that_both_speaks_and_calls_keeps_both() -> None:
    """Text and a tool call in one message is the ordinary streaming shape, not an edge case."""
    stored = legacy_message(
        "assistant",
        text_content("let me compute that"),
        call_content("call-3", "predict_pka", {"smiles": "O"}),
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
# The tests above prove the conversion of a payload; these prove the pass over a table. Rows are
# inserted as legacy literals, since nothing writes that shape any more.


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _seeded(session_id: str) -> None:
    """A migrated database holding one realistic MAF-shaped exchange.

    Written by raw insert, because the provider now writes LangChain shape and could not produce the
    rows this migration converts.
    """
    await migrated_db_or_skip()
    legacy = [
        legacy_message("user", text_content("what is the pKa of phenol?")),
        legacy_message(
            "assistant",
            text_content("computing"),
            call_content("c1", "predict_pka", {"smiles": "Oc1ccccc1"}),
        ),
        legacy_message("tool", result_content("c1", "pKa 9.95")),
        legacy_message("assistant", text_content("about 9.95")),
    ]
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_messages WHERE session_id = %s", (session_id,))
            await cur.executemany(
                "INSERT INTO session_messages (session_id, message, message_shape, "
                "correlation_id) VALUES (%s, %s, 'maf', '')",
                [(session_id, Jsonb(message)) for message in legacy],
            )


def test_a_real_stored_conversation_converts_whole() -> None:
    """Every row a real turn wrote converts, and the exchange survives readable.

    The pairing is what is at risk: a dropped `tool_call_id` leaves a transcript no provider accepts
    as a continuation, invisible one row at a time.
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
    """A second pass converts nothing: only rows still stamped `maf` are selected.

    That is what makes an interrupted conversion safe to rerun.
    """
    _run(_seeded("sess-m6-idempotent"))
    _run(convert_stored_messages())

    assert _run(convert_stored_messages()).converted == 0


def test_two_overlapping_passes_cannot_overwrite_the_preserved_original() -> None:
    """Two overlapping passes cannot overwrite the preserved original.

    Two passes that read before either commits both hold an id they believe unconverted, so only the
    UPDATE's `AND message_shape = 'maf'` predicate decides. Without it the loser writes converted
    bytes into `message_original`, and rollback restores a LangChain document stamped `maf`.
    Reachable because the pass takes no advisory lock and both `make db-migrate` and the
    post-upgrade Job run it.
    """
    session_id = "sess-m6-overlap"
    _run(_seeded(session_id))

    async def _collide() -> tuple[dict[str, Any], dict[str, Any]]:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT id, message FROM session_messages "
                "WHERE session_id = %s ORDER BY id LIMIT 1",
                (session_id,),
            )
            picked = await cur.fetchone()
            assert picked is not None, "the fixture seeded no convertible row"
            message_id, before = picked[0], picked[1]

            # Both passes act on the id each has already selected — the state overlapping passes
            # are in, not a second run after the first committed.
            await cur.execute(
                _MARK_CONVERTED, (json.dumps({"type": "human", "pass": 1}), message_id)
            )
            await cur.execute(
                _MARK_CONVERTED, (json.dumps({"type": "human", "pass": 2}), message_id)
            )
            await conn.commit()

            await cur.execute(
                "SELECT message_original FROM session_messages WHERE id = %s", (message_id,)
            )
            kept = await cur.fetchone()
            assert kept is not None
            return before, kept[0]

    before, preserved = _run(_collide())
    assert preserved == before, (
        "the second pass overwrote `message_original` with an already-converted payload; the "
        "`AND message_shape` predicate on `_MARK_CONVERTED` is what prevents it"
    )


def test_a_row_the_converter_refuses_is_left_exactly_as_it_was() -> None:
    """A row the converter refuses is left exactly as it was.

    Consuming it would destroy the evidence and break resumption; aborting the pass would block
    every later row. The id is reported and the rest of the table converts.
    """
    session_id = "sess-m6-refused"
    _run(_seeded(session_id))
    bad = _run(_insert_raw(session_id, {"type": "message", "role": "developer", "contents": []}))

    outcome = _run(convert_stored_messages())

    assert bad in outcome.refused
    shapes = dict(_run(_shape_of(bad)))
    assert shapes[bad] == MAF_SHAPE, "a refused row was stamped as converted"


def test_the_conversion_preserves_the_original_and_the_rollback_is_one_statement() -> None:
    """Conversion preserves the original, and rollback is one statement.

    The shape stamp says what a row holds now; reversibility needs the original bytes. Asserted that
    the previous release, reading `message_original`, gets back exactly the messages it wrote (the
    `ToolMessage` with its id, the `AIMessage` with its call) and that the documented recovery
    restores the row byte-for-byte.
    """
    session_id = "sess-m6-preserved"
    _run(_seeded(session_id))
    # A row the converter refuses, to pin the other half: nothing is written for a row nothing
    # rewrote, so a NULL here means "never converted" rather than "converted and not preserved".
    refused_id = _run(
        _insert_raw(
            session_id,
            legacy_message("tool", result_content("c2", "logP 1.5"), result_content("c3", "mp 41")),
        )
    )
    before = {row_id: payload for row_id, payload, _, _ in _run(_full_rows(session_id))}

    outcome = _run(convert_stored_messages())
    assert refused_id in outcome.refused

    after = _run(_full_rows(session_id))
    for row_id, message, original, shape in after:
        if row_id == refused_id:
            assert (shape, original) == (MAF_SHAPE, None), "a row nothing rewrote grew an original"
            # And it is still readable in the shape it is stored in, unconverted and unharmed.
            assert message == before[row_id]
            continue
        assert shape == LANGCHAIN_SHAPE
        assert original == before[row_id], "the preserved original is not what the row held"

    # What the previous release actually gets back. Not "the bytes match" — the messages do, with
    # the pairing intact and nothing degraded, which is the property the conversion destroyed.
    restored = [
        message_from_row(original, MAF_SHAPE)
        for row_id, _, original, _ in after
        if original is not None
    ]
    assert [type(m).__name__ for m in restored] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert not any(is_degraded_render(m) for m in restored), "the previous release reads a guess"
    calls = {c["id"] for m in restored if isinstance(m, AIMessage) for c in m.tool_calls}
    assert calls == {m.tool_call_id for m in restored if isinstance(m, ToolMessage)} == {"c1"}

    # The recovery, exactly as `067_session_message_original.sql` and the module docstring write it.
    _run(_roll_back(session_id))
    rolled = _run(_full_rows(session_id))
    assert {row_id: message for row_id, message, _, _ in rolled} == before
    assert {shape for _, _, _, shape in rolled} == {MAF_SHAPE}
    assert all(original is None for _, _, original, _ in rolled)


def test_an_unconverted_row_still_reads_through_the_reader_the_previous_release_had() -> None:
    """An unconverted row still reads through the previous release's strict reader.

    The pass runs post-upgrade, so a failed rollout converts nothing and both images must serve an
    all-`maf` table; `to_langchain` converts such rows cleanly.
    """
    session_id = "sess-m6-unconverted"
    _run(_seeded(session_id))

    for payload, shape in _run(_rows_for(session_id)):
        assert shape == MAF_SHAPE
        assert not is_degraded_render(message_from_row(payload, shape))
        to_langchain(payload)


async def _full_rows(session_id: str) -> list[tuple[int, Any, Any, str]]:
    """Every stored row for one session as `(id, message, message_original, shape)`."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, message, message_original, message_shape FROM session_messages "
            "WHERE session_id = %s ORDER BY id",
            (session_id,),
        )
        return [(int(r[0]), r[1], r[2], r[3]) for r in await cur.fetchall()]


async def _roll_back(session_id: str) -> None:
    """The recovery statement the SQL comment and the module docstring both publish, run verbatim.

    Written here rather than paraphrased: a documented procedure nobody executes is a procedure
    that is wrong the first time somebody needs it.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE session_messages "
                "   SET message = message_original, message_shape = 'maf', message_original = NULL "
                " WHERE session_id = %s AND message_original IS NOT NULL",
                (session_id,),
            )
        await conn.commit()


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


# --- the checkpointer, and what erasure must reach (M6) -------------------------------------------


def test_turn_state_survives_a_new_process_over_the_same_database() -> None:
    """A checkpointed thread outlives the graph that wrote it.

    Two separately built agents over one `thread_id`, with the saver dropped in between, approximate
    a pod restart; the second agent must see the first turn's messages. One `asyncio.run` for the
    whole test, because the checkpointer pool is bound to the loop it was opened in.
    """

    async def _scenario() -> list[str]:
        await migrated_db_or_skip()
        thread = {"configurable": {"thread_id": "sess-m6-durable"}}
        try:
            first = build_langgraph_agent(
                model=_replies("remembered"),
                audit_sink=NullAuditSink(),
                checkpointer=await checkpointer(),
            )
            await first.ainvoke({"messages": [("user", "first question")]}, config=thread)

            # A brand-new agent over the same thread, as a restarted pod would build.
            second = build_langgraph_agent(
                model=_replies("second"),
                audit_sink=NullAuditSink(),
                checkpointer=await checkpointer(),
            )
            snapshot = await second.aget_state(thread)
            return [str(m.content) for m in snapshot.values["messages"]]
        finally:
            await close_checkpointer()

    restored = _run(_scenario())

    assert "first question" in restored
    assert "remembered" in restored


def test_erasure_reaches_turn_state_not_just_the_transcript() -> None:
    """A departing person's checkpointed conversation is erased with their transcript.

    `checkpoints`, `checkpoint_blobs` and `checkpoint_writes` hold the same conversation as graph
    state. Asserted end to end, since the failure is an erasure that looks like it worked.
    """
    actor, session_id = "leaver@example.com", "sess-m6-erasure"

    async def _scenario() -> tuple[int, int]:
        await migrated_db_or_skip()
        try:
            await SessionOwnerStore().record(session_id, actor, None)
            graph = build_langgraph_agent(
                model=_replies("state to erase"),
                audit_sink=NullAuditSink(),
                checkpointer=await checkpointer(),
            )
            await graph.ainvoke(
                {"messages": [("user", "remember me")]},
                config={"configurable": {"thread_id": session_id}},
            )
            before = await _checkpoint_rows(session_id)
            # `apply=True` because the default is a dry run that counts and rolls back — which is
            # the right default for an unrecoverable operation, and would make this test assert
            # nothing while passing.
            report = await erase_actor(actor, apply=True)
            assert report.applied, "the erasure did not commit"
            assert sum(report.erased[t] for t in CHECKPOINT_TABLES) == before
            return before, await _checkpoint_rows(session_id)
        finally:
            await close_checkpointer()

    before, after = _run(_scenario())

    assert before > 0, "the fixture stored no checkpoint to erase"
    assert after == 0, "turn state survived the erasure"


async def _checkpoint_rows(thread_id: str) -> int:
    """How many rows the checkpointer holds for one thread, across all three of its tables."""
    total = 0
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        for table in CHECKPOINT_TABLES:
            await cur.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            total += int(row[0])  # type: ignore[index]
    return total


def test_erasure_still_works_where_the_checkpointer_has_never_run() -> None:
    """Erasure still works where the checkpointer has never run and its tables do not exist.

    Postgres resolves `DELETE FROM checkpoints` at parse time, so the existence check must be a
    separate query, not a `WHERE to_regclass(...)` inside the statement. Driven in a schema of its
    own so the absence is real.
    """

    async def _scenario() -> dict[str, int]:
        await migrated_db_or_skip()
        schema = f"{TEST_SCHEMA}_no_checkpointer"
        base = settings.postgres_dsn
        await create_test_schema(base, schema)
        try:
            with_schema = schema_dsn(base, schema)
            original, settings.postgres_dsn = settings.postgres_dsn, with_schema
            try:
                await migrate()
                return (await erase_actor("nobody@example.com")).erased
            finally:
                settings.postgres_dsn = original
        finally:
            await drop_test_schema(base, schema)

    erased = _run(_scenario())

    assert {erased[table] for table in CHECKPOINT_TABLES} == {0}
    assert erased["session_messages"] == 0, "the rest of the sweep must still have run"


def test_a_row_answering_parallel_calls_is_refused_rather_than_truncated() -> None:
    """A row answering parallel calls is refused rather than truncated.

    A `ToolMessage` answers one call, so converting a multi-result row would discard results and
    leave unanswered `tool_use` blocks a provider rejects. The refused row keeps its `maf` stamp and
    still renders through `session_store.message_from_row`.
    """
    row = legacy_message(
        "tool",
        result_content("c1", "pKa 9.95"),
        result_content("c2", "logP 1.46"),
        result_content("c3", "mp 41 C"),
    )

    with pytest.raises(UnconvertibleMessage, match="answers 3 calls"):
        to_langchain(row)

    # And it is still readable in the shape it is stored in, which is what makes refusing safe.
    assert message_from_row(row, MAF_SHAPE).content


def test_a_malformed_langchain_row_degrades_instead_of_failing_the_transcript() -> None:
    """A malformed LangChain row degrades instead of failing the whole transcript.

    Almost every row is stamped `langchain`, `messages_from_dict` refuses unknown types (asserted
    below), and `GET /sessions/{id}/messages` has no handler, so the LangChain branch must be
    guarded too or one bad row is a 500 for the whole transcript.
    """
    row = {"type": "not-a-message-type", "data": {"content": "the pKa of phenol is 9.95"}}

    with pytest.raises(ValueError, match="unexpected message type"):
        messages_from_dict([row])

    assert message_from_row(row, LANGCHAIN_SHAPE).content == "the pKa of phenol is 9.95"


def test_a_refused_row_is_attributed_to_whoever_spoke_it() -> None:
    """A refused row is attributed to whoever spoke it.

    Both stored vocabularies are read (MAF's `role`, LangChain's `type`), so a chemist's question is
    never rendered as agent speech; only a payload naming neither falls back to the model's voice.
    """
    asked = legacy_message(
        "user",
        text_content("what is the pKa of phenol?"),
        # An unknown content type is what makes the row refusable at all; the question is still in
        # it, and the speaker still stated.
        {"type": "image", "uri": "s3://bucket/spectrum.png"},
    )
    restored = message_from_row(asked, MAF_SHAPE)
    assert isinstance(restored, HumanMessage), "a chemist's question rendered as agent speech"
    assert restored.content == "what is the pKa of phenol?"

    # The same rule through the other vocabulary: `data` is unusable, `type` is not.
    langchain_row: dict[str, Any] = {"type": "human", "data": None}
    assert isinstance(message_from_row(langchain_row, LANGCHAIN_SHAPE), HumanMessage)

    # And the default stays the model's voice for a payload that names no speaker at all.
    assert isinstance(message_from_row({"contents": ["oops"]}, MAF_SHAPE), AIMessage)


def test_a_contents_list_holding_a_non_dict_degrades_rather_than_raising() -> None:
    """A `contents` list holding a non-dict degrades rather than raising.

    `_reject_unknown_content` skips non-dict parts, so the text join raises `AttributeError`, not
    `UnconvertibleMessage`. Asserted on the converter first, so the payload is proven to raise.
    """
    row: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "contents": ["oops"],
        "additional_properties": {},
    }

    with pytest.raises(AttributeError):
        to_langchain(row)

    restored = message_from_row(row, MAF_SHAPE)
    assert isinstance(restored, AIMessage)
    assert restored.content == "", "a row with no readable prose renders empty, not raising"


def test_an_unknown_content_type_is_refused_as_both_the_docstring_and_the_ddl_promise() -> None:
    """The claim was in two places and true in neither: unknown parts were dropped to empty text.

    Nothing matched them, so they vanished and the row was stamped converted — a silent drop in an
    irreversible pass, invisible because what came out still looked like a message.
    """
    row = legacy_message("assistant", text_content("here it is"))
    row["contents"].append({"type": "image", "uri": "s3://bucket/spectrum.png"})

    with pytest.raises(UnconvertibleMessage, match="image"):
        to_langchain(row)


def test_streamed_call_arguments_are_parsed_rather_than_discarded() -> None:
    """Streamed call arguments, stored as a JSON string, are parsed rather than discarded."""
    streamed = legacy_message(
        "assistant",
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "predict_pka",
            "arguments": '{"smiles": "CCO"}',
            "additional_properties": {},
        },
    )

    converted = to_langchain(streamed)

    assert isinstance(converted, AIMessage)
    assert converted.tool_calls[0]["args"] == {"smiles": "CCO"}

    # A half-streamed fragment still degrades rather than failing the row: the blob is already
    # unreconstructable, and the call stays visible with its name and id.
    broken = legacy_message(
        "assistant",
        {
            "type": "function_call",
            "call_id": "c2",
            "name": "predict_pka",
            "arguments": '{"smiles": "CC',
            "additional_properties": {},
        },
    )
    degraded = to_langchain(broken)
    assert isinstance(degraded, AIMessage)
    assert degraded.tool_calls[0]["args"] == {}


def test_the_erased_table_list_is_derived_from_upstream_not_asserted_against_itself() -> None:
    """`CHECKPOINT_TABLES` is complete, derived from upstream rather than asserted against itself.

    The erasure test counts against the same constant, so a missing table would pass it.
    `AsyncPostgresSaver.setup()` runs `base.MIGRATIONS`, so the tables it creates are where thread
    state can live; `checkpoint_migrations` is excluded by name because it holds no conversation.
    """
    import re

    from langgraph.checkpoint.postgres import base

    created = {
        match.group(1)
        for statement in base.MIGRATIONS
        if (match := re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement))
    }
    assert created, "no CREATE TABLE found in the checkpointer's migrations — the parse is broken"

    assert set(CHECKPOINT_TABLES) == created - {"checkpoint_migrations"}, (
        "the checkpointer creates a table the erasure sweep does not clear (or clears one it does "
        "not create): " + str(sorted(created ^ set(CHECKPOINT_TABLES)))
    )


def test_a_pass_reports_the_rows_it_converted_not_the_rows_it_attempted() -> None:
    """A pass reports rows it converted, not rows it attempted.

    A row a peer converted first matches nothing under the UPDATE predicate, so counting attempts
    would over-report under overlapping passes and disagree with
    `SELECT count(*) ... WHERE message_shape`, the operator's only check.
    """
    _run(_seeded("sess-m6-double-count"))

    async def _still_maf() -> int:
        """How many rows in the whole table are still MAF-shaped."""
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM session_messages WHERE message_shape = %s", (MAF_SHAPE,)
            )
            row = await cur.fetchone()
            assert row is not None
            return int(row[0])

    async def _both() -> tuple[int, int]:
        # Counted over the whole table because the pass converts the whole table; a refused row
        # keeps its `maf` stamp, so the drop in MAF-shaped rows is exactly what the two passes
        # converted.
        before = await _still_maf()
        first, second = await asyncio.gather(convert_stored_messages(), convert_stored_messages())
        return first.converted + second.converted, before - await _still_maf()

    reported, actually_converted = _run(_both())
    assert reported == actually_converted, (
        f"two passes reported {reported} conversions over {actually_converted} rows — the count "
        "is attempts, not rows the UPDATE matched"
    )
