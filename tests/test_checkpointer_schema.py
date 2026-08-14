"""A checkpoint outlives the build that wrote it, so it says which channels wrote it.

The failure being guarded is LangGraph's, documented in `agent/checkpointer.py`: an old checkpoint
is restored into channels built from the *current* state schema, a channel the checkpoint never held
stays empty, and a node that indexes it raises a bare `KeyError` naming the field. The first two
tests here are that failure, *measured* — including the two controls that say which half of a schema
change actually causes it, because the intuitive answer (a removed field) is the wrong one and a
guard aimed at it would refuse threads that resume perfectly well.

Everything after them is the guard. The Postgres ones drive a real graph over the real
`SchemaStampedSaver`, for the reason `tests/test_plan_state.py` gives for its own real checkpointer:
the property under test is what happens when a *stored* checkpoint meets a *new* build, and a fake
saver would only prove a dict comparison. The "new build" is the one thing that cannot be staged
literally, so it is staged the way a deploy stages it — the module's declared channel set is a
different value than the one the checkpoint was written with.
"""

import asyncio
from operator import add
from typing import Annotated, Any, Generic, NotRequired, TypedDict, TypeVar, get_type_hints

import pytest
from langchain.agents.middleware.todo import PlanningState
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from chemclaw.agent import checkpointer as ckpt
from chemclaw.agent.state import ChemclawState
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


def _graph(schema: Any, node: Any, saver: Any) -> Any:
    """A one-node graph — a turn, reduced to the state it reads and writes."""
    graph = StateGraph(schema)
    graph.add_node("turn", node)
    graph.add_edge(START, "turn")
    graph.add_edge("turn", END)
    return graph.compile(checkpointer=saver)


# --- what actually fails, and what does not ------------------------------------------------------


def _suspending_graph(schema: Any, writer: Any, gate: Any, saver: Any) -> Any:
    """A two-node turn that suspends between them — `writer` runs, then `gate` interrupts.

    The shape matters: resuming this runs `gate` again and **not** `writer`, so a channel `writer`
    would have written is genuinely absent. That is the only place a resumed turn differs from a
    fresh one, and therefore the only place a moved channel can strand a thread.
    """
    graph = StateGraph(schema)
    graph.add_node("writer", writer)
    graph.add_node("gate", gate)
    graph.add_edge(START, "writer")
    graph.add_edge("writer", "gate")
    graph.add_edge("gate", END)
    return graph.compile(checkpointer=saver)


async def _old_writer(state: _OldState) -> dict[str, Any]:
    """Write the channel the later build renames."""
    return {"plan": ["screen the species"]}


async def _old_gate(state: _OldState) -> dict[str, Any]:
    """Suspend for a human, then report what the plan channel held."""
    approved = interrupt({"ask": "approve?"})
    return {"messages": [f"{approved} {len(state['plan'])}"]}


async def _new_writer(state: _NewState) -> dict[str, Any]:
    """The same write under the new name."""
    return {"todos": ["screen the species"]}


async def _new_gate(state: _NewState) -> dict[str, Any]:
    """The same read under the new name — the node a resumed turn re-enters."""
    approved = interrupt({"ask": "approve?"})
    return {"messages": [f"{approved} {len(state['todos'])}"]}


def test_a_moved_channel_strands_a_turn_resumed_inside_the_graph() -> None:
    """The failure the guard exists for, measured — no Postgres and no guard involved.

    An `InMemorySaver` is the right saver here precisely because this is not about durability: it
    is about what LangGraph does when `channel_values` from one schema are restored into channels
    built from another. What comes out is `KeyError: 'todos'` — raised inside the node, naming a
    field and nothing else: not the thread, not the schema change, not a remedy.

    The control below it is what makes this evidence rather than a demonstration: the *same* new
    build, on a thread of its own, answers. So the checkpoint is what the failure depends on, which
    is the claim the whole stamp rests on.
    """
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "sess-renamed-field"}}

    async def _resumed() -> None:
        await _suspending_graph(_OldState, _old_writer, _old_gate, saver).ainvoke(
            {"messages": ["q1"]}, config
        )
        await _suspending_graph(_NewState, _new_writer, _new_gate, saver).ainvoke(
            Command(resume="yes"), config
        )

    async def _fresh() -> list[str]:
        fresh_saver = InMemorySaver()
        fresh_config = {"configurable": {"thread_id": "sess-fresh"}}
        graph = _suspending_graph(_NewState, _new_writer, _new_gate, fresh_saver)
        await graph.ainvoke({"messages": ["q1"]}, fresh_config)
        return list((await graph.ainvoke(Command(resume="yes"), fresh_config))["messages"])

    with pytest.raises(KeyError) as raised:
        asyncio.run(_resumed())
    assert raised.value.args == ("todos",), (
        "the mechanism this guard is built for did not reproduce; if LangGraph now restores a "
        "missing channel with a default, the stamp is no longer buying anything"
    )
    assert asyncio.run(_fresh()) == ["q1", "yes 1"], (
        "the new build cannot answer on a thread of its own either, so the checkpoint is not what "
        "this failure depends on and the stamp would be aimed at the wrong thing"
    )


def test_it_is_the_added_half_of_a_rename_that_raises_and_not_the_removed_half() -> None:
    """Which direction of a schema change to refuse — measured, because it is counter-intuitive.

    A *removed* channel cannot strand anything: nothing declares it any more, so nothing indexes
    it, and the thread resumes with the removed value simply ignored. A channel this build declares
    that the checkpoint never held is the one that raises. A rename is both at once, and this says
    which half did it.

    Without this, the obvious guard — refuse whenever the channel set differs — would end in-flight
    sessions on a deploy that only *drops* a field, which is measured here to be safe.
    """

    class _Dropped(TypedDict):
        """The new build after `plan` was deleted and nothing took its place."""

        messages: Annotated[list[str], add]

    async def _dropped_writer(state: _Dropped) -> dict[str, Any]:
        return {}

    async def _dropped_gate(state: _Dropped) -> dict[str, Any]:
        approved = interrupt({"ask": "approve?"})
        return {"messages": [f"{approved} {len(state['messages'])}"]}

    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "sess-dropped-field"}}

    async def _run() -> list[str]:
        await _suspending_graph(_OldState, _old_writer, _old_gate, saver).ainvoke(
            {"messages": ["q1"]}, config
        )
        resumed = await _suspending_graph(_Dropped, _dropped_writer, _dropped_gate, saver).ainvoke(
            Command(resume="yes"), config
        )
        return list(resumed["messages"])

    assert asyncio.run(_run()) == ["q1", "yes 1"]


def test_notrequired_does_not_make_an_added_channel_safe() -> None:
    """Why the stamp cannot narrow itself to *required* channels and skip the optional ones.

    `NotRequired` says how the graph's input may be spelled; it says nothing about whether a node
    indexes the channel. Both halves are measured here: the same added optional channel raises when
    a resumed node indexes it and resumes when the node reads it with `.get()`. Since the stamp
    holds names and cannot see which of the two a node does, it covers optional channels too — and
    the module docstring says plainly that this refuses some resumes that would have worked.
    """

    class _Optional(TypedDict):
        """The new build, with one added channel that is declared optional."""

        messages: Annotated[list[str], add]
        plan: list[str]
        extra: NotRequired[list[str]]

    async def _writer(state: _Optional) -> dict[str, Any]:
        return {"plan": ["p"], "extra": ["e"]}

    async def _indexing_gate(state: _Optional) -> dict[str, Any]:
        approved = interrupt({"ask": "approve?"})
        return {"messages": [f"{approved} {len(state['extra'])}"]}

    async def _defensive_gate(state: _Optional) -> dict[str, Any]:
        approved = interrupt({"ask": "approve?"})
        return {"messages": [f"{approved} {len(state.get('extra', []))}"]}

    def _resume_under(gate: Any, thread_id: str) -> list[str]:
        saver = InMemorySaver()
        config = {"configurable": {"thread_id": thread_id}}

        async def _run() -> list[str]:
            await _suspending_graph(_OldState, _old_writer, _old_gate, saver).ainvoke(
                {"messages": ["q1"]}, config
            )
            resumed = await _suspending_graph(_Optional, _writer, gate, saver).ainvoke(
                Command(resume="yes"), config
            )
            return list(resumed["messages"])

        return asyncio.run(_run())

    with pytest.raises(KeyError) as raised:
        _resume_under(_indexing_gate, "sess-optional-indexed")
    assert raised.value.args == ("extra",)
    assert _resume_under(_defensive_gate, "sess-optional-defensive") == ["q1", "yes 0"]


# --- what the stamp covers -----------------------------------------------------------------------


def test_the_stamp_covers_this_repository_s_channels_and_not_the_upstream_base_s() -> None:
    """A dependency bump must not be able to move the stamp — the guard's own worst failure mode.

    `ChemclawState` inherits four of its six channels from langchain's `PlanningState`, and a
    `TypedDict` merges those into `__annotations__`, so the naive reading reports all six. A stamp
    over all six would refuse **every in-flight thread in the fleet** the next time langchain adds
    or renames one of its own channels — the guard bricking the sessions it exists to protect, on a
    change nobody associated with turn state.

    Both halves are asserted: that the derived set excludes everything the upstream base declares
    (which survives a first-party field being added, so it does not need editing for one), and that
    it still contains the fields this repository declares today. The first also fails loudly if the
    derivation itself ever stops working — `__orig_bases__` is only populated while the base is
    generic — which turns that into a red build instead of a fleet-wide refusal.
    """
    upstream = set(get_type_hints(PlanningState, include_extras=True))
    declared = set(ckpt.FIRST_PARTY_CHANNELS)

    assert declared.isdisjoint(upstream), (
        f"the stamp covers upstream channels {sorted(declared & upstream)}, so a langchain bump "
        "that touches one of them would refuse every live thread"
    )
    assert declared >= {"model_calls", "loop_capped"}, (
        "the stamp covers none of the channels this repository declares, so it refuses nothing"
    )
    assert declared < set(get_type_hints(ChemclawState, include_extras=True))


def test_a_channel_added_to_the_upstream_base_does_not_move_the_stamp() -> None:
    """The same property end to end, staged as the dependency bump it is meant to survive.

    Stand-in classes rather than a real langchain upgrade: the two bases differ by exactly the
    change a minor bump makes, and the state built on each is otherwise identical.
    """
    response = TypeVar("response")

    class _UpstreamNow(TypedDict, Generic[response]):
        messages: list[str]

    class _UpstreamNext(TypedDict, Generic[response]):
        messages: list[str]
        jump_to: str

    class _OursNow(_UpstreamNow[int]):
        model_calls: int

    class _OursNext(_UpstreamNext[int]):
        model_calls: int

    assert ckpt._first_party_channels(_OursNow) == ("model_calls",)
    assert ckpt._first_party_channels(_OursNext) == ("model_calls",)


def test_the_stamp_moves_when_this_repository_s_own_channels_do() -> None:
    """The counter-property: a stamp that never moves refuses nothing.

    Computed over a stand-in extending the real state rather than by editing `ChemclawState`, so
    what is pinned is the derivation and not today's fields. Declaration order must not move it,
    or a diff that changes no channel would refuse threads.
    """

    class _Added(ChemclawState):
        retrieved_notes: NotRequired[str]

    class _Renamed(TypedDict):
        beta: int
        alpha: int

    class _Reordered(TypedDict):
        alpha: int
        beta: int

    assert ckpt._first_party_channels(_Added) == ("retrieved_notes",)
    assert ckpt._first_party_channels(_Renamed) == ckpt._first_party_channels(_Reordered)


# --- the guard, over a real saver -----------------------------------------------------------------


def _turn(saver: Any, thread_id: str, message: str) -> Any:
    """Run one turn of the old-schema graph on `thread_id`, returning its final state."""
    return _graph(_OldState, _old_node, saver).ainvoke(
        {"messages": [message]}, {"configurable": {"thread_id": thread_id}}
    )


def test_a_thread_that_never_held_a_channel_this_build_declares_is_refused_by_name() -> None:
    """The redeploy case: a stored checkpoint, a build that has since declared a new channel.

    Asserted through a *second turn on the same thread*, which is where the damage would land in
    production — the first turn is what leaves the checkpoint behind. The exception type is the
    finding (a caller can tell this apart from an outage, which `KeyError: 'todos'` does not
    support), and the message is checked for the three facts an operator needs to act: which
    session, which channel it is missing, and what to do about it.
    """

    async def _run() -> Exception:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-channel-added", "q1")
            patch = pytest.MonkeyPatch()
            patch.setattr(
                ckpt, "FIRST_PARTY_CHANNELS", (*ckpt.FIRST_PARTY_CHANNELS, "retrieved_notes")
            )
            try:
                with pytest.raises(ckpt.CheckpointSchemaMismatch) as raised:
                    await _turn(saver, "sess-channel-added", "q2")
            finally:
                patch.undo()
            return raised.value
        finally:
            await ckpt.close_checkpointer()

    message = str(asyncio.run(_run()))
    assert "sess-channel-added" in message, "the refusal does not say which session is affected"
    assert "retrieved_notes" in message, "the refusal does not name the channel that is missing"
    assert "model_calls" in message, "the refusal does not say what the thread does hold"
    assert "Start a new session" in message, "the refusal names no remedy, so it is not actionable"


def test_a_channel_this_build_no_longer_declares_does_not_refuse_the_thread() -> None:
    """A dropped field is measured harmless above, so the guard must not end sessions over one.

    Staged as the deploy stages it: the thread was stamped with today's channels, and the build
    reading it declares one fewer. The assertion is on the accumulated `messages` channel, because
    that is what proves the checkpoint was *restored* rather than quietly skipped.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-channel-dropped", "q1")
            patch = pytest.MonkeyPatch()
            patch.setattr(ckpt, "FIRST_PARTY_CHANNELS", ckpt.FIRST_PARTY_CHANNELS[:-1])
            try:
                final = await _turn(saver, "sess-channel-dropped", "q2")
            finally:
                patch.undo()
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    assert asyncio.run(_run()) == ["q1", "answered", "q2", "answered"]


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


async def _rewrite_stamp(thread_id: str, stamp: str | None) -> int:
    """Replace a thread's stamp with `stamp`, or remove it — the row an older build left behind.

    Written with SQL against the stored rows rather than by constructing a bare
    `AsyncPostgresSaver` on the side, because the condition under test is a *row shape*.
    """
    if stamp is None:
        statement = "UPDATE checkpoints SET metadata = metadata - %s WHERE thread_id = %s"
        params: tuple[Any, ...] = (ckpt.STATE_CHANNELS_KEY, thread_id)
    else:
        statement = (
            "UPDATE checkpoints SET metadata = jsonb_set(metadata, %s, to_jsonb(%s::text)) "
            "WHERE thread_id = %s"
        )
        params = ([ckpt.STATE_CHANNELS_KEY], stamp, thread_id)
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(statement, params)
        rewritten = cur.rowcount
        await conn.commit()
    return int(rewritten)


def _resume_with_stamp(thread_id: str, stamp: str | None) -> list[str]:
    """Take a turn, force the thread's stamp to `stamp`, then take another under a wider build.

    The widened `FIRST_PARTY_CHANNELS` is what makes the result decisive: a stamp this build could
    read would be missing that channel and refuse, so resuming proves the stamp was treated as
    absent rather than as a match.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, thread_id, "q1")
            assert await _rewrite_stamp(thread_id, stamp) > 0, "no checkpoint row was rewritten"
            patch = pytest.MonkeyPatch()
            patch.setattr(
                ckpt, "FIRST_PARTY_CHANNELS", (*ckpt.FIRST_PARTY_CHANNELS, "retrieved_notes")
            )
            try:
                final = await _turn(saver, thread_id, "q2")
            finally:
                patch.undo()
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    return asyncio.run(_run())


def test_a_checkpoint_from_before_the_guard_resumes_rather_than_being_refused() -> None:
    """Every live session at the deploy that introduces the stamp has an unstamped checkpoint.

    Refusing those would brick every conversation in the deployment on the way *in* — the exact
    outcome the guard exists to prevent, caused by the guard. So an absent stamp is not a mismatch,
    and this is the test that keeps it that way.
    """
    assert _resume_with_stamp("sess-pre-guard", None) == ["q1", "answered", "q2", "answered"]


def test_a_stamp_this_build_cannot_read_is_treated_as_absent() -> None:
    """The first version of this guard stamped a twelve-character schema hash, not a channel list.

    A rolling deploy runs both builds at once, so both directions matter and both are handled the
    same way — by treating anything that is not a list of names as no stamp at all. The value below
    is the real fingerprint that build wrote for today's `ChemclawState`.
    """
    assert _resume_with_stamp("sess-legacy-stamp", "bf5b523b8e62") == [
        "q1",
        "answered",
        "q2",
        "answered",
    ]


def test_concurrent_first_turns_get_one_migrated_saver() -> None:
    """A cold start with traffic must not hand a turn a saver whose migrations have not run.

    `checkpointer()` published `_saver` *before* awaiting `setup()`, and `_checkpoint_pool()`
    published `_pool` before awaiting `open()`. Both are check-then-await-then-act, so a second turn
    arriving inside either await saw a non-`None` global and got an unusable object — `relation
    "checkpoints" does not exist`. That is not a rare interleaving: `api/runner._turn_checkpointer`
    is awaited once per turn and the shipped chart runs two replicas, so every deploy under load is
    the window.

    **`setup()` is slowed here, and that is what gives the test power rather than luck.** Two
    earlier versions — gather ten `checkpointer()` calls, then gather ten first turns — both passed
    against the unfixed code, because at real speed the migrations happen to finish inside the first
    task's slice. What the defect needs is a *second caller inside the first one's await*, so the
    await is made wide enough to observe instead of being raced for. Measured against the unfixed
    body, three of four tasks received the saver with `setup()` still unfinished; with the lock, all
    four wait for it.

    The flag is the assertion because it is the property that matters: what a turn gets back is a
    checkpointer whose tables exist. Saver identity is checked too — two savers would mean two
    `setup()` runs and two pools for one process.
    """
    migrated: dict[str, bool] = {"done": False}
    original = ckpt.SchemaStampedSaver.setup

    async def _slow_setup(self: Any) -> None:
        """Stand in for the ten real migrations, widened so the window is observable."""
        await asyncio.sleep(0.05)
        await original(self)
        migrated["done"] = True

    async def _run() -> list[tuple[bool, int]]:
        await migrated_db_or_skip()
        await ckpt.close_checkpointer()

        async def _take(_index: int) -> tuple[bool, int]:
            saver = await ckpt.checkpointer()
            return migrated["done"], id(saver)

        try:
            return list(await asyncio.gather(*(_take(index) for index in range(4))))
        finally:
            await ckpt.close_checkpointer()

    patch = pytest.MonkeyPatch()
    patch.setattr(ckpt.SchemaStampedSaver, "setup", _slow_setup)
    try:
        taken = asyncio.run(_run())
    finally:
        patch.undo()

    assert all(ready for ready, _ in taken), "a turn got a checkpointer that was not migrated"
    assert len({saver for _, saver in taken}) == 1, "one process, one checkpointer"
