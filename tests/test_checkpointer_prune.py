"""What one turn's prune of superseded checkpoints does, and what it must not do.

The claim under test is `D-2026-09-06-a-superseded-checkpoint-is-a-copy-not-a-record`: a thread's
older checkpoints are copies of state its newest one still holds in full, so deleting them disposes
of no record — and until this landed nothing bounded a thread that was still in use. `checkpoints`
grew four full copies of the whole message list per turn, so blob bytes went as the square of the
turn count: measured on this suite's own shape, 2.57 / 10.29 / 41.17 MB at 20 / 40 / 80 turns, ratio
4.00 twice.

**Every test here drives the real compiled agent against the real Postgres saver**, because all
three things that could go wrong are things a mock cannot have: a `checkpoint_ns` a `task` helper
writes on the same `thread_id`, a live turn committing on a connection the prune is not inside, and
a resume that reads back a conversation LangGraph reassembles from `checkpoint_blobs`.

`tests/test_checkpointer_schema.py` owns the stamp and the refusal; this file owns the bytes.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from chemclaw.agent import checkpointer as ckpt
from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent.state import turn_config
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.fakes_langgraph import ScriptedChatModel
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

# One turn: a tool call, then the answer. `ls` is a `FilesystemMiddleware` verb, so the script needs
# no connector and no server — what matters is that the turn takes two model calls and therefore
# writes the thirteen checkpoints a real tool-calling turn writes.
_TOOL_TURN: tuple[Any, ...] = ({"name": "ls", "args": {}}, "answered")
# The same shape through the one helper this repository compiles, which is what puts a second
# `checkpoint_ns` on the thread. The helper's own model call is scripted after the `task` call.
_TASK_TURN: tuple[Any, ...] = (
    {"name": "task", "args": {"description": "read", "subagent_type": "general-purpose"}},
    "helper report",
    "answered",
)


def _script(turn: tuple[Any, ...], turns: int) -> ScriptedChatModel:
    """A model that replays `turn` `turns` times over, with slack for the resume that follows."""
    return ScriptedChatModel(script=list(turn) * (turns + 3))


async def _drive(saver: Any, thread: str, turns: int, turn: tuple[Any, ...]) -> list[str]:
    """Take `turns` turns on one thread and return the conversation the last one saw.

    A graph per turn, because that is what the front door does — `build_langgraph_agent` binds tools
    at construction — and because a prune that only works on a graph held open across turns would
    not be a prune of anything a deployment runs.
    """
    model = _script(turn, turns)
    final: dict[str, Any] = {}
    for index in range(turns):
        agent = build_langgraph_agent(model=model, checkpointer=saver)
        final = await agent.ainvoke(
            {"messages": [HumanMessage(content=f"q{index}")]},
            config=turn_config(thread_id=thread),
        )
    return [str(message.content) for message in final["messages"]]


async def _thread_rows(thread: str) -> dict[str, int]:
    """The three tables' row counts and the blob bytes for one thread."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT (SELECT count(*) FROM checkpoints WHERE thread_id = %(t)s),"
            "       (SELECT count(*) FROM checkpoint_blobs WHERE thread_id = %(t)s),"
            "       (SELECT count(*) FROM checkpoint_writes WHERE thread_id = %(t)s),"
            "       (SELECT coalesce(sum(pg_column_size(b.*)), 0) FROM checkpoint_blobs b"
            "         WHERE b.thread_id = %(t)s)",
            {"t": thread},
        )
        row = await cur.fetchone()
    assert row is not None
    return {
        "checkpoints": int(row[0]),
        "blobs": int(row[1]),
        "writes": int(row[2]),
        "blob_bytes": int(row[3]),
    }


async def _namespaces(thread: str) -> dict[str, int]:
    """Checkpoints per `checkpoint_ns` on one thread — the partition the prune must respect."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT checkpoint_ns, count(*) FROM checkpoints WHERE thread_id = %s GROUP BY 1",
            (thread,),
        )
        return {str(name): int(count) for name, count in await cur.fetchall()}


async def _ready(monkeypatch: pytest.MonkeyPatch, keep: int) -> Any:
    """A migrated schema with the checkpoint tables, a fresh saver, and the retention set."""
    await migrated_db_or_skip()
    await create_checkpoint_tables()
    monkeypatch.setattr(settings, "checkpoint_retain_per_thread", keep)
    await ckpt.close_checkpointer()
    return await ckpt.checkpointer()


def test_a_thread_stops_growing_with_the_square_of_its_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The A/B the fix rests on: same turns, same script, prune off then prune on.

    Both arms in one test on purpose. A threshold on the pruned arm alone would be a number to
    re-tune every time the fixture's message size moved; the *ratio* between two arms of the same
    fixture is the claim, and it is what says the growth changed shape rather than got smaller.

    The row assertion is the sharper of the two. Twelve turns write 156 `checkpoints` rows
    unpruned; pruned, the thread holds the retained checkpoints plus one turn's writes and nothing
    else, which is a bound that does not move with turn count at all.
    """

    async def _run(keep: int, thread: str) -> tuple[dict[str, int], list[str]]:
        saver = await _ready(monkeypatch, keep)
        try:
            conversation = await _drive(saver, thread, 12, _TOOL_TURN)
            return await _thread_rows(thread), conversation
        finally:
            await ckpt.close_checkpointer()

    unpruned, unpruned_conversation = asyncio.run(_run(0, "prune-off"))
    pruned, pruned_conversation = asyncio.run(_run(3, "prune-on"))

    assert unpruned["checkpoints"] > 100, (
        "the unpruned arm did not grow, so this fixture is not measuring what it claims to"
    )
    assert pruned["checkpoints"] <= 20, (
        f"a pruned thread holds {pruned['checkpoints']} checkpoints after 12 turns; the bound is "
        "the retained ones plus one turn's writes, and it must not grow with turn count"
    )
    assert pruned["blob_bytes"] * 3 < unpruned["blob_bytes"], (
        f"pruning saved too little to be the fix: {pruned['blob_bytes']} against "
        f"{unpruned['blob_bytes']} bytes of `checkpoint_blobs`"
    )
    # The whole point of keeping the tip: what a chemist and the model can read is untouched.
    assert pruned_conversation == unpruned_conversation, (
        "the pruned thread reads back a different conversation, which is the one thing this "
        "prune may never do"
    )
    assert len(pruned_conversation) > 12


def test_every_namespace_of_a_thread_is_bounded_and_not_only_the_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `task` helper writes its own `checkpoint_ns` on the caller's `thread_id`, and it counts.

    Measured on a real helper: one `tools:<uuid>` namespace per `task` call, seven `checkpoints` and
    three `checkpoint_blobs` each, all under the caller's thread — and a *new* namespace every call,
    so namespace count grows with helper use for the life of the session.

    **The direction the review expected is not the direction this statement fails in, and the
    measurement is why the assertion below is the shape it is.** The caveat was written as
    over-pruning: take the newest K checkpoints across namespaces and a live helper's namespace goes
    whole. That is the failure of a *thread-wide* floor, and this statement does not have one — its
    `oldest_kept` groups by `checkpoint_ns`, so a namespace with no row in the global top-K gets no
    floor and is never touched. Measured with the `PARTITION BY` removed and everything else
    identical, on two threads driven the same way: the root namespace went 52 → 3 in both arms,
    while every helper namespace went 7 → 3 partitioned and stayed at **7** unpartitioned. So the
    real failure is a leak that grows with `task` calls rather than a loss, and over-pruning stays
    possible only in the window where a helper's own checkpoints are the newest on the thread.
    One `PARTITION BY` closes both, and this test fails if it is dropped.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], list[str]]:
        saver = await _ready(monkeypatch, 3)
        try:
            conversation = await _drive(saver, "prune-helper", 4, _TASK_TURN)
            partitioned = await _namespaces("prune-helper")
            # A second thread driven identically, pruned by hand with the partition removed. Two
            # threads rather than one, because the first has already been pruned correctly and
            # could not show what the broken form would have left.
            await _drive(saver, "prune-helper-flat", 4, _TASK_TURN)
            flat = ckpt._PRUNE_SUPERSEDED.replace("PARTITION BY checkpoint_ns ", "")
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(flat, {"thread": "prune-helper-flat", "keep": 3})
                await conn.commit()
            return partitioned, await _namespaces("prune-helper-flat"), conversation
        finally:
            await ckpt.close_checkpointer()

    partitioned, unpartitioned, conversation = asyncio.run(_run())

    helpers = [name for name in partitioned if name]
    assert helpers, (
        "no subgraph namespace was written, so this fixture never exercised a helper and the "
        "partition is untested — check that the `task` call in `_TASK_TURN` still runs"
    )
    # All but one: the prune runs once a turn, on the root namespace's input checkpoint, so the
    # helper the *last* turn spawned has not been reached yet and still holds its full seven. That
    # is the residual the implementation states — a thread is bounded at the retained checkpoints
    # plus one turn's writes, not at the retained checkpoints.
    settled = sorted(partitioned[name] for name in helpers)[:-1]
    assert settled and all(count == 3 for count in settled), (
        f"a settled helper namespace is not bounded at the retained count: {partitioned}"
    )
    assert max(partitioned[name] for name in helpers) <= 7, (
        f"a helper namespace grew past one turn's own writes: {partitioned}"
    )
    assert partitioned[""] > 0, "the root namespace was emptied"
    assert any(count > 3 for name, count in unpartitioned.items() if name), (
        "the unpartitioned form bounded the helper namespaces too, so this test cannot tell the "
        f"two statements apart and proves nothing about the partition: {unpartitioned}"
    )
    assert len(conversation) > 4


def test_a_prune_beside_live_turns_leaves_the_thread_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the version floor is chosen for, driven rather than argued.

    The prune runs on its own connection, so it is not in the turn's transaction and the turn is not
    in its own — the same two-writer shape `D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers`
    found for the retention sweep, where the fix was a `NOT EXISTS` that left one window open. Here
    the predicate is a version floor over monotone counters, so a row written after the statement's
    snapshot sorts above every floor it computed.

    Hammered every 5 ms for the whole of eight turns, with `keep=1` — the most aggressive setting
    the config allows, chosen because a margin would hide a fault this test exists to find.
    """

    async def _run() -> tuple[list[str], int]:
        saver = await _ready(monkeypatch, 0)  # the turns' own prune off; this one is the racer
        stop = asyncio.Event()
        passes = 0

        async def _hammer() -> None:
            nonlocal passes
            async with db.connection(settings.postgres_dsn) as conn:
                while not stop.is_set():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            ckpt._PRUNE_SUPERSEDED, {"thread": "prune-race", "keep": 1}
                        )
                        await cur.fetchone()
                    await conn.commit()
                    passes += 1
                    await asyncio.sleep(0.005)

        racer = asyncio.create_task(_hammer())
        try:
            await _drive(saver, "prune-race", 8, _TOOL_TURN)
        finally:
            stop.set()
            await racer
        # Resume on a fresh saver: the assertion is that the thread reads back, not that the
        # in-process graph still had it.
        await ckpt.close_checkpointer()
        resumed = await ckpt.checkpointer()
        try:
            agent = build_langgraph_agent(model=_script(_TOOL_TURN, 2), checkpointer=resumed)
            final = await agent.ainvoke(
                {"messages": [HumanMessage(content="q-final")]},
                config=turn_config(thread_id="prune-race"),
            )
            return [str(message.content) for message in final["messages"]], passes
        finally:
            await ckpt.close_checkpointer()

    conversation, passes = asyncio.run(_run())

    assert passes > 20, f"only {passes} prune passes landed, so nothing was really raced"
    assert conversation.count("answered") == 9, (
        f"the raced thread lost turns: {conversation.count('answered')} answers of 9 in "
        f"{len(conversation)} messages"
    )


def test_the_prune_is_off_when_a_deployment_asks_for_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 is the escape hatch for a deployment that wants the whole history, and it must be real.

    Asserted against the row count rather than by counting statements: what a deployment is buying
    with 0 is the rows, and a prune that ran but deleted nothing would be indistinguishable from
    one that did not run — until the day it deleted something.
    """

    async def _run() -> dict[str, int]:
        saver = await _ready(monkeypatch, 0)
        try:
            await _drive(saver, "prune-zero", 5, _TOOL_TURN)
            return await _thread_rows("prune-zero")
        finally:
            await ckpt.close_checkpointer()

    rows = asyncio.run(_run())
    assert rows["checkpoints"] >= 5 * 13, (
        f"{rows['checkpoints']} checkpoints after five turns: something pruned a thread whose "
        "deployment asked for none of it"
    )


def test_a_prune_that_cannot_run_does_not_take_the_turn_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Housekeeping may not cost a chemist an answer that is already written and committed.

    The failure is injected at the statement rather than by patching `_prune_superseded` itself,
    so what is under test is the `except` clause and not a stub of it.
    """

    async def _run() -> list[str]:
        saver = await _ready(monkeypatch, 3)
        monkeypatch.setattr(ckpt, "_PRUNE_SUPERSEDED", "SELECT no_such_function()")
        try:
            return await _drive(saver, "prune-broken", 2, _TOOL_TURN)
        finally:
            await ckpt.close_checkpointer()

    conversation = asyncio.run(_run())
    assert conversation.count("answered") == 2, (
        f"a failing prune took the turn with it: {conversation}"
    )
