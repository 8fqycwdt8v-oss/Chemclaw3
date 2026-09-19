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
from langgraph.channels.last_value import LastValue
from langgraph.channels.untracked_value import UntrackedValue
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from chemclaw.agent import checkpointer as ckpt
from chemclaw.agent.state import ChemclawState, TurnFlag, TurnTotal
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

    `ChemclawState` inherits part of its state from langchain's `PlanningState`, and a `TypedDict`
    merges those into `__annotations__`, so the naive reading reports both sets as one. A stamp over
    that union would refuse **every in-flight thread in the fleet** the next time langchain adds or
    renames one of its own channels — the guard bricking the sessions it exists to protect, on a
    change nobody associated with turn state. No count is written here or in the module: two
    sentences in `agent/checkpointer.py` said "six" over a state that had grown to eight, and the
    test below is what makes the set checkable instead.

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
    assert declared >= {"active_agent"}, (
        "the stamp covers none of the restorable channels this repository declares, so it refuses "
        "nothing"
    )
    assert declared < set(get_type_hints(ChemclawState, include_extras=True))
    # **The named channel is a restorable one, and it has to be.** This asserted `loop_capped`
    # while the stamp covered every first-party name, including five `UntrackedValue` channels no
    # checkpoint can hold — so the guard refused the next ordinary turn of every live session each
    # time a per-turn counter was added, and pre-empted nothing, because a channel absent from
    # every build's checkpoint cannot be missing from one build's relative to another's.
    # `_first_party_channels` now excludes them; the exclusion itself is asserted here.
    #
    # **And this assertion cannot fail for `ChemclawState` by construction, which is worth saying
    # rather than leaving to read as stronger than it is.** Both tuples come from one walk of
    # `_own_channels` partitioned by one predicate, so for the shipped state class they are disjoint
    # whatever `_is_untracked` answers. What it does catch is a mutation of `_untracked_channels`
    # itself — dropping the `if` there reds it — so it is a guard on the *derivation* staying a
    # partition, not on the membership being right. The membership is
    # `test_a_channel_declared_with_the_untracked_class_is_not_stamped` below, which drives a state
    # class this file declares and therefore has two independent answers to compare.
    assert declared.isdisjoint(ckpt.UNTRACKED_CHANNELS), (
        f"the stamp covers untracked channels {sorted(declared & set(ckpt.UNTRACKED_CHANNELS))}, "
        "which no checkpoint holds — so adding one would refuse every live thread and prevent "
        "nothing"
    )


def test_the_declared_channels_partition_the_state() -> None:
    """What this repository declares and what the base declares are exactly the state, and disjoint.

    The assertion a prose count was standing in for. `agent/checkpointer.py` twice said the state
    holds six channels, and by the time a reviewer counted them it held eight — both sentences
    written by sessions that were not editing the file that had grown. A number in prose is a claim
    about a commit (`D-2026-09-03`); a partition is a claim a test can hold, so the numbers are
    deleted and this stands in their place.

    It fails in both directions that matter. A first-party channel the derivation drops leaves a
    name in neither half, which is a channel `FIRST_PARTY_CHANNELS` will not stamp and a resume will
    not refuse; a base channel it picks up leaves a name in both, which is the fleet-wide refusal on
    a dependency bump that `..._and_not_the_upstream_base_s` guards from the other side.
    """
    upstream = set(get_type_hints(PlanningState, include_extras=True))
    declared = set(ckpt.FIRST_PARTY_CHANNELS)
    untracked = set(ckpt.UNTRACKED_CHANNELS)
    whole = set(get_type_hints(ChemclawState, include_extras=True))
    # **Three parts rather than two, because one first-party part is deliberately unstamped.**
    # `UntrackedValue` channels are never written to a checkpoint, so stamping them refused every
    # live session whenever a per-turn counter was added and could pre-empt nothing. They are still
    # asserted to be *somewhere*: a channel the derivation drops by accident lands in none of the
    # three and fails here exactly as it did when there were two.
    first_party = declared | untracked

    assert first_party | upstream == whole, (
        "the parts do not add up to the state: "
        f"in none {sorted(whole - (first_party | upstream))}, "
        f"in none's state {sorted((first_party | upstream) - whole)}"
    )
    assert not (first_party & upstream), (
        f"{sorted(first_party & upstream)} is claimed by both halves"
    )
    # Same narrowness as the sibling assertion above and for the same reason: one walk, one
    # predicate, so this holds for `ChemclawState` however `_is_untracked` answers. It reds on a
    # mutation of `_untracked_channels`, which is what it is for.
    assert not (declared & untracked), (
        f"{sorted(declared & untracked)} is both stamped and untracked, so the partition of the "
        "first-party half is not one"
    )


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


def test_adding_a_per_turn_counter_does_not_move_the_stamp() -> None:
    """A channel no checkpoint can hold is not a channel a resume can be missing.

    **This is the defect, staged as the change that caused it.** The stamp is the set of names the
    *writing* build declared, and the load refuses when any name the *current* build declares is
    absent from it. While the derivation covered every first-party name, five of the six it returned
    were `UntrackedValue` subclasses — `TurnTotal`, `TurnFlag` — whose declarations in
    `agent/state.py` each say in so many words that the channel is never written to a checkpoint. So
    adding a per-turn counter, which this repository does routinely and which cannot affect a
    restore, refused the **next ordinary turn** of every live Postgres-backed session; and it
    pre-empted nothing, because a channel absent from every build's checkpoints cannot be missing
    from one build's relative to another's. At the build before `active_agent` existed, *all four*
    stamped names were of that kind, so the stamp could not have pre-empted anything at all.

    Three mutations, not one, because a guard whose only failing mutation is the one it was written
    for is a regression test for a fixed bug (`tasks/lessons.md`, 2026-09-18). The first is the
    defect; the second is a *reword* of it — a different untracked shape, so a guard keyed on the
    class name rather than on the base would pass it; the third is the case that must still move the
    stamp, which is what stops the fix from being "never refuse anything".
    """
    response = TypeVar("response")

    class _Upstream(TypedDict, Generic[response]):
        messages: list[str]

    class _Base(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]

    class _PlusTurnTotal(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        handoffs: NotRequired[Annotated[int, TurnTotal(int)]]

    class _PlusTurnFlag(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        spend_capped: NotRequired[Annotated[bool, TurnFlag(bool)]]

    class _PlusRestorable(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        retrieved_notes: NotRequired[Annotated[list[str], LastValue(list)]]

    assert ckpt._first_party_channels(_Base) == ("active_agent",)
    assert ckpt._first_party_channels(_PlusTurnTotal) == ("active_agent",), (
        "adding a TurnTotal moved the stamp, so every live session's next turn is refused for a "
        "channel no checkpoint holds"
    )
    assert ckpt._first_party_channels(_PlusTurnFlag) == ("active_agent",), (
        "adding a TurnFlag moved the stamp — the same defect in the other untracked shape, which a "
        "guard keyed on one class name would miss"
    )
    assert ckpt._first_party_channels(_PlusRestorable) == ("active_agent", "retrieved_notes"), (
        "adding a restorable channel did not move the stamp, so the guard now refuses nothing and "
        "the KeyError it exists to pre-empt is live again"
    )
    # Both halves are derived from one walk, so the complement has to agree.
    assert ckpt._untracked_channels(_PlusTurnTotal) == ("handoffs",)
    assert ckpt._untracked_channels(_PlusRestorable) == ()


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
    assert "active_agent" in message, "the refusal does not say what the thread does hold"
    assert "Start a new session" in message, "the refusal names no remedy, so it is not actionable"


def test_a_channel_this_build_no_longer_declares_does_not_refuse_the_thread() -> None:
    """A dropped field is measured harmless above, so the guard must not end sessions over one.

    Staged as the deploy stages it: the thread was stamped with channels the *writing* build
    declared, and the build reading it declares one fewer. The assertion is on the accumulated
    `messages` channel, because that is what proves the checkpoint was *restored* rather than
    quietly skipped.

    **The staging is written both ways round, because slicing the live tuple degenerated.** This
    patched `FIRST_PARTY_CHANNELS[:-1]`, and the fix that excluded every untracked channel left the
    stamp as the 1-tuple `('active_agent',)` — so `[:-1]` is `()`, and the scenario silently stopped
    being "declares one fewer" and became "declares none at all", which is the trivial case where
    `missing` is empty for any stamp whatsoever. It still caught a symmetric-comparison defect, so
    it was not vacuous, but nothing in it said the drop it staged had disappeared.

    So the *writing* build is the one given the extra channel now, which is the direction a deploy
    actually moves: the thread is stamped with `(…, 'retired_channel')` and the reading build
    declares only what ships today. That is a genuine one-fewer regardless of how many channels the
    stamp holds, and it stays real if `FIRST_PARTY_CHANNELS` ever shrinks to nothing at all.
    """
    retired = (*ckpt.FIRST_PARTY_CHANNELS, "retired_channel")

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            # Turn one runs under the *old* build, which declared one channel this build does not.
            patch = pytest.MonkeyPatch()
            patch.setattr(ckpt, "FIRST_PARTY_CHANNELS", retired)
            try:
                await _turn(saver, "sess-channel-dropped", "q1")
            finally:
                patch.undo()
            # Turn two runs under today's, which declares one fewer than the stamp records.
            final = await _turn(saver, "sess-channel-dropped", "q2")
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    assert len(retired) == len(ckpt.FIRST_PARTY_CHANNELS) + 1, (
        "the staged scenario is not 'declares one fewer' any more, so this test is about the "
        "degenerate case rather than about a dropped channel"
    )
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


# --- the value stamp: a thread that lost half its rows must not read back as an empty one -------


async def _delete_blobs(thread_id: str) -> int:
    """Delete a thread's `checkpoint_blobs`, leaving its `checkpoints` rows standing.

    The state `durable/retention.py` produced when a live turn landed between two of its DELETEs,
    reached here by the shortest route rather than by re-staging the race — the race is measured in
    `tests/test_retention.py`, and what this file is about is what the *reader* does with the row
    it leaves behind, whichever route made it.
    """
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
        deleted = cur.rowcount
        await conn.commit()
    return int(deleted)


def test_a_thread_that_has_lost_its_blobs_is_refused_rather_than_read_as_empty() -> None:
    """The measured failure: `checkpoints` present, `checkpoint_blobs` gone, and no complaint.

    Before the value stamp this resumed silently — `aget_state` returned an empty log, no
    exception, no log line, and the next turn answered as a brand-new conversation on a thread the
    chemist believed they were continuing. That is verbatim what `agent/checkpointer.py`'s module
    docstring calls worse than no answer.

    Asserted through a *second turn on the same thread*, because that is where the damage lands: the
    first turn is what leaves the rows behind. The message is checked for the two facts an operator
    needs — which session, and which channel is gone.
    """

    async def _run() -> Exception:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-blobs-deleted", "q1")
            assert await _delete_blobs("sess-blobs-deleted") > 0, "the thread had no blobs to lose"
            with pytest.raises(ckpt.CheckpointValuesMissing) as raised:
                await _turn(saver, "sess-blobs-deleted", "q2")
            return raised.value
        finally:
            await ckpt.close_checkpointer()

    message = str(asyncio.run(_run()))
    assert "sess-blobs-deleted" in message, "the refusal does not say which session is affected"
    assert "messages" in message, "the refusal does not name the channel whose value is gone"
    assert "Start a new session" in message, "the refusal names no remedy, so it is not actionable"


def test_the_value_stamp_does_not_refuse_a_healthy_thread() -> None:
    """The control that decided the *shape* of the guard, not merely that it has a counter-example.

    The obvious signal is `channel_versions` minus what loaded, and it is wrong: a channel is
    consumed by the step that reads it, which bumps its version and writes no blob, so a healthy
    checkpoint routinely names channels holding no value. Measured on a healthy three-turn thread,
    that comparison flagged **every** checkpoint — a guard built on it would refuse every thread in
    the fleet on the deploy that introduced it.

    So this asserts both halves: that a healthy thread resumes, *and* that it is a thread the naive
    signal would have refused. Without the second half the test passes against a guard that is
    wrong for the reason this one was written to avoid.
    """

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-values-healthy", "q1")
            final = await _turn(saver, "sess-values-healthy", "q2")
            tip = await saver.aget_tuple({"configurable": {"thread_id": "sess-values-healthy"}})
            assert tip is not None, "the thread has no checkpoint to inspect"
            loaded = tip.checkpoint["channel_values"]
            unbacked = [name for name in tip.checkpoint["channel_versions"] if name not in loaded]
            return list(final["messages"]), unbacked
        finally:
            await ckpt.close_checkpointer()

    messages, unbacked = asyncio.run(_run())
    assert messages == ["q1", "answered", "q2", "answered"], (
        "a healthy thread did not resume through the value stamp"
    )
    assert unbacked, (
        "this thread has a value for every channel it versions, so it does not exercise the "
        "false positive the guard was shaped around — pick a graph with a consumed channel"
    )


def test_a_checkpoint_written_before_the_value_stamp_resumes() -> None:
    """An unstamped checkpoint passes, for the reason the channel stamp's unstamped case does.

    Every live session at the deploy that introduces this stamp has checkpoints without it, and a
    rolling deploy keeps writing them from the older pod for the length of the rollout. Staged by
    removing the key from the stored row and then removing the *blobs* too: a thread that has lost
    everything the guard looks for still resumes, which is the only way to prove the guard read the
    absent stamp rather than getting lucky.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-values-unstamped", "q1")
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(
                    "UPDATE checkpoints SET metadata = metadata - %s WHERE thread_id = %s",
                    (ckpt.CHECKPOINT_VALUES_KEY, "sess-values-unstamped"),
                )
                rewritten = cur.rowcount
                await conn.commit()
            assert rewritten > 0, "no checkpoint row was rewritten"
            await _delete_blobs("sess-values-unstamped")
            final = await _turn(saver, "sess-values-unstamped", "q2")
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    assert asyncio.run(_run()) == ["q2", "answered"], (
        "an unstamped checkpoint was refused, which would brick every live session on the deploy "
        "that introduces the stamp"
    )


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


def test_strict_serde_blocks_import_by_name_deserialization() -> None:
    """The checkpoint serializer refuses to run a `module:callable` named in a stored blob.

    `AsyncPostgresSaver` with no `serde=` builds a permissive `JsonPlusSerializer` whose msgpack
    ext hook runs `getattr(import_module(mod), attr)(*args)` on values taken straight from the
    stored bytes — arbitrary code execution on the resume of a poisoned `checkpoint_blobs` row,
    reachable from the app credential's own INSERT+DELETE grant. `_strict_serde` pins the hook to
    `SAFE_MSGPACK_TYPES`; a poisoned type is blocked (returns a degraded value, never executes)
    while every legitimate channel still round-trips.
    """
    import ormsgpack
    from langchain_core.messages import AIMessage, HumanMessage

    serde = ckpt._strict_serde()
    # legitimate turn state round-trips
    payload: dict[str, Any] = {
        "messages": [HumanMessage(content="hi"), AIMessage(content="ok")],
        "model_calls": 3,
    }
    restored = serde.loads_typed(serde.dumps_typed(payload))
    assert len(restored["messages"]) == 2
    assert restored["model_calls"] == 3

    # an os.system ext payload does not execute under the strict serializer
    marker = "/tmp/strict-serde-should-not-exist"
    evil = ormsgpack.packb(
        ormsgpack.Ext(1, ormsgpack.packb(("os", "system", (f"touch {marker}",))))
    )
    serde.loads_typed(("msgpack", evil))  # must not raise, must not execute
    import os.path

    assert not os.path.exists(marker), "strict serde executed a blocked callable"


def test_upstream_default_serde_is_still_permissive() -> None:
    """Pin the upstream default this workaround exists for, so a fix upstream turns this red.

    If langgraph-checkpoint ever makes the msgpack ext hook strict by default, `_strict_serde`
    becomes redundant and this assertion fails, prompting its removal — the `test_upstream_surface`
    pattern for a shape upstream never promised.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    assert JsonPlusSerializer()._allowed_msgpack_modules is True


def test_a_channel_declared_with_the_untracked_class_is_not_stamped() -> None:
    """The one untracked spelling `_is_untracked` missed, which is the one its origin uses.

    **`isinstance(bound, UntrackedValue)` is a test on a channel *instance*, and upstream declares
    its own untracked channel with the *class*.** `ModelCallLimitMiddleware` writes
    `run_model_call_count: NotRequired[Annotated[int, UntrackedValue, PrivateStateAttr]]`, and
    `agent/state.py` quotes that line verbatim as where this repository's shape comes from —
    LangGraph resolves a bare channel class in an annotation by constructing it, so the two
    spellings mean the same thing.

    Driven before the fix, with one channel added in exactly that spelling: it landed in
    `FIRST_PARTY_CHANNELS` rather than in `UNTRACKED_CHANNELS`, and the next ordinary turn of a
    session written by the previous build was refused against a real Postgres —
    `refusing turn state for session sess-a5-probe: it never held state channel(s) probe_counter`,
    `CheckpointSchemaMismatch: … Start a new session`. The fleet-wide refusal the whole derivation
    exists to close, live again, with all 19 tests in this file green, through the one shape
    `_is_untracked`'s own docstring promised was covered ("read off the annotation rather than off a
    list of class names, so a sixth untracked channel shape is covered the day it is written").

    Both directions, and the second is the reason this is not just a repeat of the sibling test
    above: a predicate widened to "any class in the metadata" would also swallow a *restorable*
    channel declared by class, so `LastValue` in the same position must still be stamped.

    Four class-form spellings rather than one, because three one-token narrowings of the predicate
    were watched passing against fewer:

    - the **bare class** (`UntrackedValue`), which is upstream's own;
    - a **subclass as a class** (`TurnTotal`, not `TurnTotal(int)`), which `bound is UntrackedValue`
      admits and which is how this repository's own channels would read if anybody dropped the call;
    - the marker **beside another** and **after** it, because upstream's declaration carries
      `PrivateStateAttr` in the same `Annotated` and the order of two markers is arbitrary — a
      predicate reading `__metadata__[0]` passes every spelling that happens to put it first.
    """
    response = TypeVar("response")

    class _Upstream(TypedDict, Generic[response]):
        messages: list[str]

    class _ByClass(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        probe_counter: NotRequired[Annotated[int, UntrackedValue]]

    class _SubclassByClass(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        probe_counter: NotRequired[Annotated[int, TurnTotal]]

    class _ByClassBesideAnotherMarker(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        probe_counter: NotRequired[Annotated[int, UntrackedValue, "a second marker"]]

    class _ByClassAfterAnotherMarker(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        probe_counter: NotRequired[Annotated[int, "a first marker", UntrackedValue]]

    class _RestorableByClass(_Upstream[int]):
        active_agent: NotRequired[Annotated[str, LastValue(str)]]
        retrieved_notes: NotRequired[Annotated[list[str], LastValue]]

    assert ckpt._first_party_channels(_ByClass) == ("active_agent",), (
        "a channel declared with the `UntrackedValue` class is in the stamp, so adding one refuses "
        "the next ordinary turn of every live session for a channel no checkpoint holds — the "
        "exact spelling `agent/state.py` cites as this shape's origin"
    )
    assert ckpt._untracked_channels(_ByClass) == ("probe_counter",), (
        "the class-declared channel is in neither half, so nothing names the exclusion and the "
        "partition test cannot see it"
    )
    assert ckpt._first_party_channels(_SubclassByClass) == ("active_agent",), (
        "a `TurnTotal` written as a class rather than as `TurnTotal(int)` is in the stamp, so the "
        "class arm recognises `UntrackedValue` itself and not what inherits from it"
    )
    assert ckpt._first_party_channels(_ByClassBesideAnotherMarker) == ("active_agent",), (
        "the class form is not recognised beside a second marker; upstream's own declaration "
        "carries `PrivateStateAttr` in the same `Annotated`"
    )
    assert ckpt._first_party_channels(_ByClassAfterAnotherMarker) == ("active_agent",), (
        "the class form is only recognised as the *first* marker, and the order of two markers in "
        "one `Annotated` is arbitrary — upstream could reorder its own declaration tomorrow"
    )
    assert ckpt._first_party_channels(_RestorableByClass) == ("active_agent", "retrieved_notes"), (
        "a restorable channel declared by class is now excluded from the stamp, so the predicate "
        "has become 'any class in the metadata' and the guard refuses nothing"
    )
