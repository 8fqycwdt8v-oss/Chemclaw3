"""A checkpoint outlives the build that wrote it, so it says which channels wrote it.

LangGraph restores an old checkpoint into channels built from the current schema; a channel the
checkpoint never held stays empty and a node that indexes it raises a bare `KeyError`. The first
tests demonstrate that failure, with controls showing it is the added channel (not a removed one)
that causes it. The rest test the guard: Postgres tests drive a real graph over the real
`SchemaStampedSaver`, staging a "new build" by changing the module's declared channel set.
"""

import asyncio
import inspect
import json
import tempfile
from operator import add, itemgetter
from pathlib import Path
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

    Resuming runs `gate` again but not `writer`, so a channel `writer` would have written is
    genuinely
    absent: the only place a moved channel can strand a thread.
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
    """A moved channel strands a turn resumed inside the graph (no Postgres, no guard).

    The result is a bare `KeyError: 'todos'` naming only a field. The control — the same new build
    on a fresh thread — answers, so the failure depends on the checkpoint.
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
    """It is the added half of a rename that raises, not the removed half.

    A removed channel is simply ignored on resume, so refusing whenever the channel set differs
    would end sessions on a deploy that only drops a field.
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
    """`NotRequired` does not make an added channel safe.

    The same optional channel raises when a resumed node indexes it and resumes when it is read with
    `.get()`. What decides a refusal is how a channel is read, which is why
    `checkpointer.channels_read_without_default` reads the source rather than the annotation.
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
    """The stamp covers this repository's channels, not the upstream base's.

    `ChemclawState` inherits from langchain's `PlanningState`; stamping the union would refuse every
    in-flight thread whenever langchain changes its own channels. Asserted: the derived set excludes
    everything the base declares and still contains today's first-party fields. The first also fails
    if the `__orig_bases__` derivation stops working.
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
    # Untracked channels are never checkpointed, so they are excluded from the stamp. This assertion
    # guards the derivation staying a partition (dropping the `if` in `_untracked_channels` reds
    # it); membership is tested by
    # `test_a_channel_declared_with_the_untracked_class_is_not_stamped`.
    assert declared.isdisjoint(ckpt.UNTRACKED_CHANNELS), (
        f"the stamp covers untracked channels {sorted(declared & set(ckpt.UNTRACKED_CHANNELS))}, "
        "which no checkpoint holds — so adding one would refuse every live thread and prevent "
        "nothing"
    )


def test_the_declared_channels_partition_the_state() -> None:
    """The declared channels partition the state, disjointly.

    A first-party channel the derivation drops lands in neither half (unstamped, never refused); a
    base channel it picks up lands in both (a fleet-wide refusal on a dependency bump).
    """
    upstream = set(get_type_hints(PlanningState, include_extras=True))
    declared = set(ckpt.FIRST_PARTY_CHANNELS)
    untracked = set(ckpt.UNTRACKED_CHANNELS)
    whole = set(get_type_hints(ChemclawState, include_extras=True))
    # Three parts, because untracked first-party channels are deliberately unstamped; a channel
    # dropped by accident still lands in none of them and fails here.
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
    """Adding a per-turn counter does not move the stamp.

    `UntrackedValue` channels (`TurnTotal`, `TurnFlag`) are never checkpointed, so they cannot be
    missing on resume; stamping them would refuse every live session's next turn whenever a counter
    is added. Three mutations: the original case, a different untracked shape (so a guard keyed on
    the class name fails), and a tracked channel that must still move the stamp.
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

    Computed over a stand-in extending the real state, so the derivation is pinned rather than
    today's fields. Declaration order must not move it.
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


def _reader_tree(source: str) -> Path:
    """A one-module source tree for `checkpointer.channels_read_without_default` to read.

    A staged "new build" patches both `FIRST_PARTY_CHANNELS` (what is declared) and `SOURCE_ROOT`
    (what reads it).
    """
    root = Path(tempfile.mkdtemp(prefix="chemclaw-reader-"))
    (root / "reader.py").write_text(source, encoding="utf-8")
    return root


#: A build whose one reader of the added channel indexes it — the case the refusal exists for.
_INDEXES_IT = 'def node(state):\n    return state["retrieved_notes"]\n'
#: The same build, reading it with a default — the case that must no longer drain a session.
_DEFAULTS_IT = 'def node(state):\n    return state.get("retrieved_notes", [])\n'


def _reading_build(patch: pytest.MonkeyPatch, reader: str) -> None:
    """Stage the build that declares `retrieved_notes` and reads it as `reader` does."""
    patch.setattr(ckpt, "FIRST_PARTY_CHANNELS", (*ckpt.FIRST_PARTY_CHANNELS, "retrieved_notes"))
    patch.setattr(ckpt, "SOURCE_ROOT", _reader_tree(reader))


def _turn(saver: Any, thread_id: str, message: str) -> Any:
    """Run one turn of the old-schema graph on `thread_id`, returning its final state."""
    return _graph(_OldState, _old_node, saver).ainvoke(
        {"messages": [message]}, {"configurable": {"thread_id": thread_id}}
    )


def test_a_thread_that_never_held_a_channel_this_build_declares_is_refused_by_name() -> None:
    """A thread that never held a channel this build declares is refused by name.

    Asserted on a second turn of the same thread. The exception type lets a caller tell it from an
    outage, and the message names the session, the missing channel and the remedy.
    """

    async def _run() -> Exception:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, "sess-channel-added", "q1")
            patch = pytest.MonkeyPatch()
            _reading_build(patch, _INDEXES_IT)
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
    """A channel this build no longer declares does not refuse the thread.

    The writing build is given an extra `retired_channel`, so the reading build genuinely declares
    one fewer however many channels ship. Asserted on the accumulated `messages`, proving the
    checkpoint was restored rather than skipped.
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
    """A thread written under this schema still resumes.

    Asserted on the accumulated `messages`, because a saver returning `None` would also not raise.
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

    The widened build would refuse a readable stamp, so resuming proves the stamp was treated as
    absent.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, thread_id, "q1")
            assert await _rewrite_stamp(thread_id, stamp) > 0, "no checkpoint row was rewritten"
            patch = pytest.MonkeyPatch()
            _reading_build(patch, _INDEXES_IT)
            try:
                final = await _turn(saver, thread_id, "q2")
            finally:
                patch.undo()
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    return asyncio.run(_run())


def test_a_checkpoint_from_before_the_guard_resumes_rather_than_being_refused() -> None:
    """A checkpoint from before the guard resumes rather than being refused.

    Refusing unstamped checkpoints would end every live conversation at the deploy that introduced
    the stamp.
    """
    assert _resume_with_stamp("sess-pre-guard", None) == ["q1", "answered", "q2", "answered"]


def test_a_stamp_this_build_cannot_read_is_treated_as_absent() -> None:
    """A stamp this build cannot read (an older schema-hash format) is treated as absent.

    A rolling deploy runs both builds at once. The value is a real fingerprint of that older format.
    """
    assert _resume_with_stamp("sess-legacy-stamp", "bf5b523b8e62") == [
        "q1",
        "answered",
        "q2",
        "answered",
    ]


def test_each_read_is_classified_and_anything_unrecognised_counts_as_an_index() -> None:
    """Each read is classified, and anything unrecognised counts as an index.

    Each safe shape is paired with its unsafe neighbour, so a classifier answering "safe" for
    everything fails. A name in a tuple, passed to `itemgetter` or bound to a variable used as a key
    reads the channel as surely as an index, so unknown shapes fail closed.
    """
    safe = {
        "get": 'state.get("c")',
        "get with a default": 'state.get("c", [])',
        "setdefault": 'state.setdefault("c", [])',
        "a write": 'state["c"] = 1',
        "a delete": 'del state["c"]',
        "a returned update": 'return {"c": 1}',
        "a membership test": 'if "c" in state: pass',
    }
    unsafe = {
        "an index": 'return state["c"]',
        "a one-argument pop": 'state.pop("c")',
        "itemgetter": 'return operator.itemgetter("c")(state)',
        "a name held in a tuple": 'names = ("c",)',
        "a name bound to a variable": 'key = "c"',
        "a comparison": 'if name == "c": pass',
        "an augmented assignment": 'state["c"] += 1',
    }
    for label, line in safe.items():
        tree = _reader_tree(f"def node(state, name=None):\n    {line}\n")
        assert ckpt.channels_read_without_default({"c"}, tree) == frozenset(), (
            f"{label} cannot raise for an absent channel, but it was classified as an index, so a "
            "deploy adding a channel read that way drains every live session again"
        )
    for label, line in unsafe.items():
        tree = _reader_tree(f"import operator\ndef node(state, name=None):\n    {line}\n")
        assert ckpt.channels_read_without_default({"c"}, tree) == frozenset({"c"}), (
            f"{label} was classified as safe, so a session missing that channel resumes into a "
            "node that raises a bare KeyError mid-turn"
        )
    assert ckpt.channels_read_without_default({"c", "d"}, _reader_tree('state.get("c")\n')) == (
        frozenset()
    ), "a channel no module names at all has no reader to raise, so it must not be refused"


def test_a_tree_the_derivation_cannot_read_refuses_rather_than_resumes() -> None:
    """The other two fail-closed arms: a module that does not parse, and a tree with no source."""
    broken = _reader_tree('def node(state):\n    return state.get("c"\n')
    assert ckpt.channels_read_without_default({"c"}, broken) == frozenset({"c"}), (
        "an unparseable reader was skipped, so whatever it does with the channel was assumed safe"
    )
    empty = Path(tempfile.mkdtemp(prefix="chemclaw-no-source-"))
    assert ckpt.channels_read_without_default({"c"}, empty) == frozenset({"c"}), (
        "a bytecode-only install read as 'nothing indexes anything', which resumes every session "
        "into whatever its nodes do"
    )


def test_the_shipped_tree_reads_every_restorable_channel_with_a_default() -> None:
    """`active_agent` is derived as tolerant-read, not declared so.

    If a reader starts indexing it, this fails; that is correct behaviour to review, not an
    assertion to edit.
    """
    assert ckpt.FIRST_PARTY_CHANNELS == ("active_agent",)
    assert ckpt.channels_read_without_default(ckpt.FIRST_PARTY_CHANNELS) == frozenset()


@pytest.mark.parametrize(
    ("reader", "refused"),
    [(_DEFAULTS_IT, False), (_INDEXES_IT, True)],
    ids=["read-with-a-default-resumes", "indexed-is-refused"],
)
def test_adding_a_channel_drains_live_sessions_only_when_something_indexes_it(
    reader: str, refused: bool
) -> None:
    """Adding a channel drains live sessions only when something indexes it.

    Turn two runs under a build declaring `retrieved_notes`: read with a default it resumes
    (asserted on accumulated `messages`); indexed, it is refused by name.
    """
    thread_id = f"sess-added-{'indexed' if refused else 'defaulted'}"

    async def _run() -> list[str] | Exception:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, thread_id, "q1")
            patch = pytest.MonkeyPatch()
            _reading_build(patch, reader)
            try:
                final = await _turn(saver, thread_id, "q2")
            except ckpt.CheckpointSchemaMismatch as refusal:
                return refusal
            finally:
                patch.undo()
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    outcome = asyncio.run(_run())
    if refused:
        assert isinstance(outcome, ckpt.CheckpointSchemaMismatch), outcome
        assert "retrieved_notes" in str(outcome)
    else:
        assert outcome == ["q1", "answered", "q2", "answered"], (
            "a channel every reader takes with a default still ended the live session"
        )


class _Widened(TypedDict):
    """The build after `retrieved_notes` was added, for the mid-turn resume below."""

    messages: Annotated[list[str], add]
    plan: list[str]
    retrieved_notes: NotRequired[list[str]]


async def _widened_writer(state: _Widened) -> dict[str, Any]:
    """The writer a resumed turn does not re-run — so the channel it would write stays absent."""
    return {"plan": ["p"], "retrieved_notes": ["n"]}


async def _gate_that_reads_by_itemgetter(state: _Widened) -> dict[str, Any]:
    """The node a resumed turn re-enters, reading the new channel in a shape no grep would find."""
    approved = interrupt({"ask": "approve?"})
    return {"messages": [f"{approved} {len(itemgetter('retrieved_notes')(state))}"]}


def test_a_read_the_classifier_does_not_know_is_refused_rather_than_a_key_error() -> None:
    """A read the classifier does not know is refused at load, not raised as a `KeyError`.

    The re-entered node reads the new channel through `itemgetter`. The control points the
    derivation at a defaulting reader and gets the bare `KeyError`, proving the resume reaches the
    indexing node.
    """
    node_source = inspect.getsource(_gate_that_reads_by_itemgetter)

    def _resume(reader: str, thread_id: str) -> None:
        async def _run() -> None:
            await migrated_db_or_skip()
            saver = await ckpt.checkpointer()
            config = {"configurable": {"thread_id": thread_id}}
            try:
                await _suspending_graph(_OldState, _old_writer, _old_gate, saver).ainvoke(
                    {"messages": ["q1"]}, config
                )
                patch = pytest.MonkeyPatch()
                _reading_build(patch, reader)
                try:
                    graph = _suspending_graph(
                        _Widened, _widened_writer, _gate_that_reads_by_itemgetter, saver
                    )
                    await graph.ainvoke(Command(resume="yes"), config)
                finally:
                    patch.undo()
            finally:
                await ckpt.close_checkpointer()

        asyncio.run(_run())

    with pytest.raises(ckpt.CheckpointSchemaMismatch) as refused:
        _resume("from operator import itemgetter\n" + node_source, "sess-itemgetter-derived")
    assert "retrieved_notes" in str(refused.value)

    with pytest.raises(KeyError) as raised:
        _resume(_DEFAULTS_IT, "sess-itemgetter-misderived")
    assert raised.value.args == ("retrieved_notes",), (
        "the control did not reach a node indexing the absent channel, so the refusal above is not "
        "evidence that the derivation is what stopped a KeyError"
    )


def test_a_session_stamped_before_active_agent_existed_resumes() -> None:
    """A session stamped before `active_agent` existed resumes.

    Its one reader uses `.get()` with a fallback to the root, so the derivation treats it as
    tolerant and the load resumes, peer mesh off or on.
    """
    thread_id = "sess-pre-active-agent"
    stamp = ["billed_tokens", "loop_capped", "model_calls", "spend_capped"]

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        saver = await ckpt.checkpointer()
        try:
            await _turn(saver, thread_id, "q1")
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(
                    "UPDATE checkpoints SET metadata = jsonb_set(metadata, %s, %s::jsonb) "
                    "WHERE thread_id = %s",
                    ([ckpt.STATE_CHANNELS_KEY], json.dumps(stamp), thread_id),
                )
                assert cur.rowcount > 0, "no checkpoint row was rewritten"
                await conn.commit()
            final = await _turn(saver, thread_id, "q2")
            return list(final["messages"])
        finally:
            await ckpt.close_checkpointer()

    assert asyncio.run(_run()) == ["q1", "answered", "q2", "answered"]


# --- the value stamp: a thread that lost half its rows must not read back as an empty one -------


async def _delete_blobs(thread_id: str) -> int:
    """Delete a thread's `checkpoint_blobs`, leaving its `checkpoints` rows standing.

    The state a retention race can leave (that race is tested in `tests/test_retention.py`); this
    file tests what the reader does with it.
    """
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
        deleted = cur.rowcount
        await conn.commit()
    return int(deleted)


def test_a_thread_that_has_lost_its_blobs_is_refused_rather_than_read_as_empty() -> None:
    """A thread that has lost its blobs is refused rather than read as empty.

    Otherwise the next turn silently answers as a brand-new conversation. Asserted on a second turn;
    the message names the session and the missing channel.
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
    """The value stamp does not refuse a healthy thread.

    `channel_versions` minus what loaded is the wrong signal: a consumed channel bumps its version
    and writes no blob, so healthy checkpoints routinely name empty channels. Asserted: a healthy
    thread resumes and is one the naive signal would have refused.
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
    """A checkpoint written before the value stamp resumes.

    Rolling deploys keep writing unstamped checkpoints. The key and the blobs are both removed, so
    resuming proves the guard read the absent stamp rather than getting lucky.
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
    """Concurrent first turns get one migrated saver.

    `_saver` and `_pool` must not be published before `setup()`/`open()` complete, or a second
    caller inside the await gets a saver without tables. `setup()` is slowed so a second caller
    lands inside the first one's await; at real speed the race is rarely hit. Saver identity is
    checked too (one `setup()`, one pool).
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

    The default `JsonPlusSerializer` msgpack hook imports and calls names from stored bytes, so a
    poisoned `checkpoint_blobs` row is code execution on resume. `_strict_serde` pins the hook to
    `SAFE_MSGPACK_TYPES`: a poisoned type is blocked while every legitimate channel round-trips.
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

    If the msgpack hook becomes strict by default, `_strict_serde` is redundant and can be removed.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    assert JsonPlusSerializer()._allowed_msgpack_modules is True


def test_a_channel_declared_with_the_untracked_class_is_not_stamped() -> None:
    """A channel declared with the untracked class, rather than an instance, is not stamped.

    Upstream declares its own untracked channel as `Annotated[int, UntrackedValue, ...]` and
    LangGraph constructs a bare class, so both spellings mean the same. Both directions: a
    restorable `LastValue` declared by class must still be stamped. Four class-form spellings,
    because narrower predicates pass fewer: the bare class, a subclass as a class, and the marker
    beside and after another marker (order in `Annotated` is arbitrary).
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
