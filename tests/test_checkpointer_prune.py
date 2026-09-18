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


async def _write_namespace(saver: Any, thread: str, namespace: str, count: int) -> None:
    """Put `count` checkpoints on one thread under `namespace`, through the saver's own API.

    A second namespace on a thread used to arrive for free, because a `task` helper inherited its
    caller's saver and checkpointed under `tools:<uuid>`.
    `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer` closed that — it was 98% of
    what a spawn cost — so the partition below has to be driven rather than observed as a side
    effect. Written through `aput` rather than as raw `INSERT`s so the rows carry the real
    `channel_versions` the prune's floor is computed from.
    """
    from langgraph.checkpoint.base import empty_checkpoint

    config = {"configurable": {"thread_id": thread, "checkpoint_ns": namespace}}
    checkpoint = empty_checkpoint()
    for index in range(count):
        versions: dict[str, str | int | float] = {"messages": f"{index + 1:032d}.0"}
        checkpoint = {
            **checkpoint,
            "id": f"{index:032d}-0000-0000-0000",
            "channel_versions": versions,
        }
        config = await saver.aput(config, checkpoint, {"source": "loop"}, versions)


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
    """A thread can carry more than one `checkpoint_ns`, and the prune must bound each of them.

    **This test used to get its second namespace for free, and that is the thing that changed.**
    A `task` helper inherited its caller's saver and checkpointed under `tools:<uuid>` on the
    caller's own `thread_id` — one new namespace per call, seven `checkpoints` each.
    `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer` closed that, because it was
    98% of what a spawn cost in checkpoint rows, and the assertion that no such namespace appears
    now lives in `tests/test_subagents.py` where the helper is built.

    So the namespace is written deliberately here. The guard is worth keeping without a shipped
    subgraph behind it: the statement is generic over namespaces, LangGraph writes one for *any*
    subgraph that inherits a saver, and the failure it prevents is silent.

    **The direction the review expected is not the direction this statement fails in.** The caveat
    was written as over-pruning: take the newest K checkpoints across namespaces and a live
    namespace goes whole. That is the failure of a *thread-wide* floor, and this statement does not
    have one — its `oldest_kept` groups by `checkpoint_ns`, so a namespace with no row in the
    global top-K gets no floor and is never touched. The real failure is a leak that grows with
    namespaces rather than a loss, and this test fails if the `PARTITION BY` is dropped.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], list[str]]:
        saver = await _ready(monkeypatch, 3)
        try:
            conversation = await _drive(saver, "prune-ns", 4, _TOOL_TURN)
            await _write_namespace(saver, "prune-ns", "tools:probe", 7)
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(ckpt._PRUNE_SUPERSEDED, {"thread": "prune-ns", "keep": 3})
                await conn.commit()
            partitioned = await _namespaces("prune-ns")
            # A second thread driven identically, pruned by hand with the partition removed. Two
            # threads rather than one, because the first has already been pruned correctly and
            # could not show what the broken form would have left.
            await _drive(saver, "prune-ns-flat", 4, _TOOL_TURN)
            await _write_namespace(saver, "prune-ns-flat", "tools:probe", 7)
            flat = ckpt._PRUNE_SUPERSEDED.replace("PARTITION BY checkpoint_ns ", "")
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(flat, {"thread": "prune-ns-flat", "keep": 3})
                await conn.commit()
            return partitioned, await _namespaces("prune-ns-flat"), conversation
        finally:
            await ckpt.close_checkpointer()

    partitioned, unpartitioned, conversation = asyncio.run(_run())

    extra = [name for name in partitioned if name]
    assert extra, (
        "no second namespace was written, so the partition is untested — check that "
        "`_write_namespace` still reaches the saver"
    )
    assert all(partitioned[name] == 3 for name in extra), (
        f"a non-root namespace is not bounded at the retained count: {partitioned}"
    )
    assert partitioned[""] > 0, "the root namespace was emptied"
    assert any(count > 3 for name, count in unpartitioned.items() if name), (
        "the unpartitioned form bounded the second namespace too, so this test cannot tell the "
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
