"""A checkpoint outlives the build that wrote it, so it says which state schema wrote it.

The failure being guarded is LangGraph's, documented in `agent/checkpointer.py`: an old checkpoint
is restored into channels built from the *current* state schema, a channel the checkpoint never held
stays empty, and the first node that indexes it raises a bare `KeyError` naming a field. The first
test here is that failure, reproduced rather than described — everything after it is the guard, and
the guard is only worth its lines if the thing it catches is real.

These drive a real graph over the real `SchemaStampedSaver`, on Postgres, for the reason
`tests/test_plan_state.py` gives for its own real checkpointer: the property under test is what
happens when a *stored* checkpoint meets a *new* build, and a fake saver would only prove a dict
comparison. The "new build" is the one thing that cannot be staged literally, so it is staged the
way a deploy stages it — the module's schema fingerprint is a different value than the one the
checkpoint was written with.
"""

import asyncio
from operator import add
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from chemclaw.agent import checkpointer as ckpt
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import migrated_db_or_skip


class _OldState(TypedDict):
    """The state a build declared before a field moved."""

    messages: Annotated[list[str], add]
    plan: list[str]


class _NewState(TypedDict):
    """The same graph after the rename — `plan` is gone and `todos` took its place."""

    messages: Annotated[list[str], add]
    todos: list[str]


async def _old_node(state: _OldState) -> dict[str, Any]:
    """One turn under the old schema: it writes the plan channel that later disappears."""
    return {"plan": ["screen the species"], "messages": ["answered"]}


async def _new_node(state: _NewState) -> dict[str, Any]:
    """One turn under the new schema, reading the channel the old checkpoint never held."""
    return {"todos": [*state["todos"], "compute the barrier"]}


def _graph(schema: Any, node: Any, saver: Any) -> Any:
    """A one-node graph — a turn, reduced to the state it reads and writes."""
    graph = StateGraph(schema)
    graph.add_node("turn", node)
    graph.add_edge(START, "turn")
    graph.add_edge("turn", END)
    return graph.compile(checkpointer=saver)


def test_a_moved_state_field_fails_an_old_thread_with_a_bare_key_error() -> None:
    """The failure the guard exists for, measured — no Postgres and no guard involved.

    An `InMemorySaver` is the right saver here precisely because this test is not about durability:
    it is about what LangGraph does when `channel_values` from one schema are restored into channels
    built from another. What comes out is `KeyError: 'todos'` — raised inside the node, naming a
    field and nothing else: not the thread, not the schema change, not a remedy. That is the whole
    argument for stamping, and it is asserted here rather than believed.
    """
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "sess-renamed-field"}}

    async def _run() -> None:
        await _graph(_OldState, _old_node, saver).ainvoke({"messages": ["q1"]}, config)
        await _graph(_NewState, _new_node, saver).ainvoke({"messages": ["q2"]}, config)

    with pytest.raises(KeyError) as raised:
        asyncio.run(_run())
    assert raised.value.args == ("todos",), (
        "the mechanism this guard is built for did not reproduce; if LangGraph now restores a "
        "missing channel with a default, the stamp is no longer buying anything"
    )


def _turn(saver: Any, thread_id: str, message: str) -> Any:
    """Run one turn of the old-schema graph on `thread_id`, returning its final state."""
    return _graph(_OldState, _old_node, saver).ainvoke(
        {"messages": [message]}, {"configurable": {"thread_id": thread_id}}
    )


def test_a_thread_written_under_another_schema_is_refused_by_name() -> None:
    """The redeploy case: a stored checkpoint, a build whose state schema has moved.

    Asserted through a *second turn on the same thread*, which is where the damage would land in
    production — the first turn is what leaves the checkpoint behind. The exception type is the
    finding (a caller can tell this apart from an outage, which `KeyError: 'todos'` does not
    support), and the message is checked for the three facts an operator needs to act: which
    session, which schema wrote it, which schema is asking.
    """

    async def _run() -> Exception:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-schema-moved", "q1")
            written = ckpt.STATE_SCHEMA_VERSION
            patch = pytest.MonkeyPatch()
            patch.setattr(ckpt, "STATE_SCHEMA_VERSION", "0000deadbeef")
            try:
                with pytest.raises(ckpt.CheckpointSchemaMismatch) as raised:
                    await _turn(saver, "sess-schema-moved", "q2")
            finally:
                patch.undo()
            assert written in str(raised.value)
            return raised.value
        finally:
            await ckpt.close_checkpointer()

    error = asyncio.run(_run())
    message = str(error)
    assert "sess-schema-moved" in message, "the refusal does not say which session is affected"
    assert "0000deadbeef" in message, "the refusal does not say what this build declares"
    assert "Start a new session" in message, "the refusal names no remedy, so it is not actionable"


def test_a_thread_written_under_this_schema_still_resumes() -> None:
    """The counter-example: a guard that refuses everything is not a guard.

    Two turns on one thread under one build, which is every turn a real deployment takes. The
    assertion is on the accumulated `messages` channel rather than on "it did not raise", because
    that is what proves the *checkpoint was restored* — a saver that quietly returned `None` on
    every read would also not raise.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-schema-stable", "q1")
            final = await _turn(saver, "sess-schema-stable", "q2")
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    assert asyncio.run(_run()) == ["q1", "answered", "q2", "answered"]


async def _strip_stamp(thread_id: str) -> int:
    """Remove the schema stamp from a thread's checkpoints — a row a pre-guard build wrote.

    Written with SQL against the stored rows rather than by constructing a bare `AsyncPostgresSaver`
    on the side, because the condition under test is a *row shape*, and this produces exactly the
    row an older build left behind.
    """
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE checkpoints SET metadata = metadata - %s WHERE thread_id = %s",
            (ckpt.STATE_SCHEMA_KEY, thread_id),
        )
        stripped = cur.rowcount
        await conn.commit()
    return int(stripped)


def test_a_checkpoint_from_before_the_guard_resumes_rather_than_being_refused() -> None:
    """Every live session at the deploy that introduces the stamp has an unstamped checkpoint.

    Refusing those would brick every conversation in the deployment on the way *in* — the exact
    outcome the guard exists to prevent, caused by the guard. So an absent stamp is not a mismatch,
    and this is the test that keeps it that way when somebody tightens `is None or ==` into `==`.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-pre-guard", "q1")
            assert await _strip_stamp("sess-pre-guard") > 0, "nothing was stamped to strip"
            final = await _turn(saver, "sess-pre-guard", "q2")
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    assert asyncio.run(_run()) == ["q1", "answered", "q2", "answered"]


def test_the_fingerprint_moves_with_the_declared_state_and_not_otherwise() -> None:
    """The stamp is derived, so what has to hold is that it tracks the schema — both ways.

    A hand-bumped constant would need no such test and would be wrong exactly when somebody forgot
    it; a derived one needs this, because a fingerprint that never moves refuses nothing and one
    that moves on its own refuses everything. Computed over stand-in state classes rather than by
    editing `ChemclawState`, so the property is about the derivation and not about today's fields.
    """
    version = ckpt._state_schema_version

    class _Base(TypedDict):
        messages: list[str]
        todos: list[str]

    class _Renamed(TypedDict):
        messages: list[str]
        plan: list[str]

    class _Extended(_Base):
        model_calls: int

    class _Reordered(TypedDict):
        todos: list[str]
        messages: list[str]

    assert version(_Base) != version(_Renamed), "a renamed channel left the fingerprint unchanged"
    assert version(_Base) != version(_Extended), "an added channel left the fingerprint unchanged"
    assert version(_Base) == version(_Reordered), (
        "declaration order moved the fingerprint, which would refuse threads for a diff that "
        "changes no channel"
    )
