"""Stored MAF messages convert to LangChain ones, or say why not (M6, D-2026-08-10).

Rewriting `session_messages` is the one irreversible step in the migration, so the function that
decides what each row *becomes* is tested against the payloads MAF actually wrote — frozen in
`tests/legacy_rows.py` and verified byte-for-byte against its real constructors when they were
captured. What the converter must agree with is the *table*, and a table full of historical bytes
outlives the library that produced them; a fixture that could only be rebuilt by re-installing that
library could not.
"""

import asyncio
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
    LANGCHAIN_SHAPE,
    MAF_SHAPE,
    UnconvertibleMessage,
    convert_stored_messages,
    to_langchain,
)
from chemclaw.agent.session_store import SessionOwnerStore, message_from_row
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
# The plan names this as the mitigation for the migration's one irreversible step, and it is a
# different test from everything above: those prove the conversion is right about a payload, this
# proves the *pass* is right about a table. The rows are the legacy literals inserted directly,
# because nothing writes that shape any more — see `_seeded`.


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _seeded(session_id: str) -> None:
    """A migrated database holding one realistic MAF-shaped exchange.

    Written by raw insert, not through the provider, and that is not a shortcut: the provider
    writes *LangChain* shape now, so a fixture that went through it could not produce the rows this
    migration exists to convert. Legacy rows are precisely the rows nothing writes any more.
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


# --- the checkpointer, and what erasure must reach (M6) -------------------------------------------


def test_turn_state_survives_a_new_process_over_the_same_database() -> None:
    """The durable half of the rebuild: a checkpointed thread outlives the graph that wrote it.

    Two separately-built agents over one `thread_id`, with the process's saver dropped in between —
    the closest thing to a pod restart a test can stage. What is asserted is that the second agent
    sees the first turn's messages, which is the whole reason D-2026-08-10 moves turn state here.

    One `asyncio.run` for the whole test, not one per step: the checkpointer pool is bound to the
    loop it was opened in, so closing it from a second loop raises. Production has one loop per
    process, so a test that spans several is testing a shape nothing runs.
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
    """A departing person's checkpointed conversation goes with their transcript.

    The gap this closes: `_ERASE` deleted `session_messages` and left `checkpoints`,
    `checkpoint_blobs` and `checkpoint_writes` holding the same conversation as graph state — so the
    sweep would report success while the turn state stayed readable. Asserted end to end, because
    the failure mode is precisely an erasure that *looks* like it worked.
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
    """The state every current deployment is in: the checkpointer's tables do not exist.

    Erasure must not become the one operation such a deployment cannot perform. The first attempt
    at this guard put `WHERE to_regclass('checkpoints') IS NOT NULL` inside the statement, which
    does nothing — Postgres resolves `DELETE FROM checkpoints` at parse time, so the whole erasure
    failed with `relation "checkpoints" does not exist`. That is why the check is a separate query,
    and why this test exists rather than a comment saying the guard works.

    Driven through a schema of its own so the absence is real rather than arranged.
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
    """The destructive case: one stored `tool` row holds one result per parallel call.

    A `ToolMessage` answers exactly one call, so converting such a row can only keep one — and
    keeping the first silently destroyed the rest, in the pass this module's own docstring calls
    the irreversible step. The second-order damage is worse than the loss: the assistant message
    still carries all three calls, so the converted thread acquires unanswered `tool_use` blocks
    that a provider rejects outright — the poison pill `agent/message_pairing.py` exists to keep
    out of a conversation.

    Refusing costs nothing visible. The row keeps its `maf` stamp and `session_store
    .message_from_row` still reads it, so the conversation renders exactly as it did.
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
    """The guarded branch was the rare one: only the MAF conversion sat inside the `try`.

    Since M6 every row this system writes is stamped `langchain`, so the unguarded branch is the
    one nearly every read takes. `messages_from_dict` refuses a type it does not know — asserted
    below rather than assumed, because the whole defect is what that refusal does next — and the
    one caller of `get_messages` is `GET /sessions/{id}/messages`, which has no handler. One bad
    row therefore answered the *entire* transcript with a 500, which is the outcome this
    function's own docstring has always promised it would not produce.
    """
    row = {"type": "not-a-message-type", "data": {"content": "the pKa of phenol is 9.95"}}

    with pytest.raises(ValueError, match="unexpected message type"):
        messages_from_dict([row])

    assert message_from_row(row, LANGCHAIN_SHAPE).content == "the pKa of phenol is 9.95"


def test_a_refused_row_is_attributed_to_whoever_spoke_it() -> None:
    """The fallback said `AIMessage` for every shape, so a chemist's question became agent speech.

    Worse than a blank bubble, because nothing about it looks wrong: the transcript shows the
    system stating what it was asked. The row names its speaker even when its contents cannot be
    converted, so both stored vocabularies are read — MAF's `role`, LangChain's `type` — and only
    a payload that names neither falls back to the model's own voice.
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
    """The refusal handler named one exception type and this row raises a different one.

    `_reject_unknown_content` skips non-dict parts, so a `contents` element that is not a mapping
    reaches the text join and raises `AttributeError` from inside the converter — past a handler
    watching for `UnconvertibleMessage`, and out through the transcript route. Asserted on the
    converter first, so the test proves the payload really is one that raises rather than trusting
    that it is.
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
    """Both forms are in the table, and only the decoded one was read.

    A call assembled from streamed fragments stores its `arguments` as a JSON *string*, so every
    streamed call in the archive converted to `args: {}` — losing exactly what a reviewer asks
    a tool call about, permanently.
    """
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
    """`CHECKPOINT_TABLES` says its test "has to prove the list is complete". This is that proof.

    The erasure test next door sums `report.erased[t] for t in CHECKPOINT_TABLES` against a baseline
    counted over **the same constant** — so both sides move together and a fourth
    conversation-bearing table would be missed by the sweep with the test still green. A departing
    person's turn state surviving an erasure that reports success is the one outcome a right-to-be-
    forgotten path must never produce, and it would have been invisible.

    The truth is derivable: `AsyncPostgresSaver.setup()` runs `base.MIGRATIONS`, so the tables it
    creates are what a thread's state can live in. `checkpoint_migrations` is excluded by name and
    with a reason — it records which of those statements have run and holds no conversation — so a
    *new* table joining the set fails here instead of silently outliving a data-subject request.
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
