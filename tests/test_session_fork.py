"""A fork carries the whole thread, leaves the parent alone, and is a session in its own right.

**Three properties, and the third is the one a row count cannot see.** That a fork copied the right
number of rows says nothing about whether the copy *works* — a thread whose blobs were copied at the
wrong versions has exactly the right row count and resumes with holes. So the last test here
resumes the fork on a real checkpointer and reads the history back through the graph, which is the
only assertion that could have caught the failure `agent/session_fork.py`'s docstring is about.

Every test needs a real Postgres and says so by skipping without one (`tests/pg.py`), and every one
that touches a checkpoint table creates it first: `AsyncPostgresSaver.setup()` makes those tables,
not a migration, so a database that has never run the agent does not have them — and on a dev
database an unqualified name resolves through `public` and passes locally while failing in CI.
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from psycopg.types.json import Jsonb

from chemclaw.agent.langgraph_agent import build_langgraph_agent
from chemclaw.agent import session_fork
from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
from chemclaw.agent.session_fork import SessionForkError, fork_session
from chemclaw.agent.session_store import SessionOwnerStore
from chemclaw.agent.state import turn_config, turn_input
from chemclaw.core import db
from chemclaw.core.config import settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


class _Model(GenericFakeChatModel):
    """A scripted model that can be bound, because `create_agent` binds tools on every request."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding and keep replaying the script."""
        return self


async def _seed(thread_id: str, *, versions: tuple[str, ...] = ("1", "2")) -> None:
    """A thread with two checkpoints and one blob per version — the shape a fork must preserve.

    **Two versions is the point, not padding.** `checkpoint_blobs` rows are shared across a
    thread's checkpoints, so a channel written at version 1 and unchanged since is referenced by
    the newest checkpoint without belonging to it. A fork that copied "the tip" would take the
    version-2 row and leave version 1 behind, which no assertion about the newest checkpoint could
    detect.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for index, version in enumerate(versions, start=1):
                await cur.execute(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                    "VALUES (%s, '', %s, %s, '{}'::jsonb)",
                    (
                        thread_id,
                        f"ckpt-{index}",
                        Jsonb({"v": 1, "id": f"ckpt-{index}", "ts": "2026-08-29T00:00:00+00:00"}),
                    ),
                )
                await cur.execute(
                    "INSERT INTO checkpoint_blobs "
                    "(thread_id, checkpoint_ns, channel, version, type, blob) "
                    "VALUES (%s, '', 'messages', %s, 'msgpack', %s)",
                    (thread_id, version, f"payload-{version}".encode()),
                )
                await cur.execute(
                    "INSERT INTO checkpoint_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
                    "VALUES (%s, '', %s, 'task-1', 0, 'messages', 'msgpack', %s)",
                    (thread_id, f"ckpt-{index}", b"payload"),
                )
            await cur.execute(
                "INSERT INTO session_messages (session_id, message, message_shape) "
                "VALUES (%s, %s, 'langchain')",
                (thread_id, Jsonb({"type": "human", "content": "the parent's question"})),
            )
        await conn.commit()


async def _counts(thread_id: str) -> dict[str, int]:
    """How many rows each of the four tables holds for one thread."""
    counts: dict[str, int] = {}
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        for table, column in (
            ("checkpoints", "thread_id"),
            ("checkpoint_blobs", "thread_id"),
            ("checkpoint_writes", "thread_id"),
            ("session_messages", "session_id"),
        ):
            await cur.execute(
                f"SELECT count(*) FROM {table} WHERE {column} = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def test_a_fork_copies_every_row_of_the_thread_and_leaves_the_parent_alone() -> None:
    """The child holds the parent's whole thread; the parent holds exactly what it held.

    Both halves, because a copy that also mutated the source would satisfy the first on its own —
    and the parent being untouched is the property the whole feature rests on.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-parent")
        before = await _counts("fork-parent")

        child = await fork_session("fork-parent", "owner-1", None)

        return before, await _counts("fork-parent"), await _counts(child)

    before, after, child = asyncio.run(_run())

    assert child == before, f"the fork did not carry the whole thread: {child} vs {before}"
    assert after == before, f"forking mutated the parent: {after} vs {before}"


def test_a_fork_carries_blobs_from_every_version_not_only_the_newest() -> None:
    """The failure a row count would hide: the tip's blob copied and its ancestors' left behind.

    Asserted on the payloads rather than the count, because "two blob rows" is true of both the
    correct copy and one that duplicated the newest version twice.
    """

    async def _run() -> set[bytes]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-versions")

        child = await fork_session("fork-versions", "owner-1", None)

        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id = %s", (child,))
            return {bytes(row[0]) for row in await cur.fetchall()}

    assert asyncio.run(_run()) == {b"payload-1", b"payload-2"}


def test_the_fork_is_owned_by_the_caller_and_keeps_the_parent_s_profile() -> None:
    """A fork is findable and no wider than what it came from.

    The profile matters more than it looks: a profile only ever *narrows*, so a fork that dropped
    it would hand the caller an agent that can do more than the session they forked.
    """

    async def _run() -> tuple[bool, str | None, str | None]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-owned")

        child = await fork_session("fork-owned", "owner-42", "safety")

        return await SessionOwnerStore().lookup(child)

    found, owner, profile = asyncio.run(_run())

    assert found
    assert owner == "owner-42"
    assert profile == "safety"


def test_forking_a_session_that_has_taken_no_turn_is_refused() -> None:
    """Nothing to branch from is an error, not an empty session that looks like a fork."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await fork_session("fork-nonexistent", "owner-1", None)

    with pytest.raises(SessionForkError, match="no saved state"):
        asyncio.run(_run())


def test_the_fork_resumes_with_the_parent_s_history_and_then_diverges() -> None:
    """The assertion no row count can make: the copy actually *works* as a thread.

    A real checkpointer, a real compiled graph, and three turns — one on the parent, then a fork,
    then one on each. What is proven is that the fork's second turn sees the parent's first (so the
    thread was carried, not merely counted) and that the two threads then move independently (so
    the fork is a branch rather than an alias).
    """

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=settings.postgres_dsn,
            kwargs={"autocommit": True},
            min_size=0,
            max_size=4,
            open=False,
        )
        await pool.open()
        try:
            saver = AsyncPostgresSaver(cast(Any, pool))
            parent = "fork-live-parent"

            def graph(answer: str) -> Any:
                return build_langgraph_agent(
                    model=_Model(messages=iter([AIMessage(content=answer)])),
                    checkpointer=saver,
                )

            await graph("the parent's answer").ainvoke(
                turn_input("first question"), config=turn_config(parent)
            )
            child = await fork_session(parent, "owner-1", None)

            # A turn on each side, after the fork. Divergence is what makes them two threads.
            await graph("the child's answer").ainvoke(
                turn_input("child question"), config=turn_config(child)
            )
            await graph("the parent's second answer").ainvoke(
                turn_input("parent question"), config=turn_config(parent)
            )

            async def texts(thread: str) -> list[str]:
                state = await saver.aget_tuple(cast(Any, turn_config(thread)))
                assert state is not None
                return [str(m.content) for m in state.checkpoint["channel_values"]["messages"]]

            return await texts(parent), await texts(child)
        finally:
            await pool.close()

    parent_texts, child_texts = asyncio.run(_run())

    # The fork inherited the parent's first exchange...
    assert "first question" in parent_texts
    assert "first question" in child_texts, "the fork did not carry the parent's history"
    # ...and then the two went their own ways.
    assert "child question" in child_texts
    assert "child question" not in parent_texts, "the fork wrote into its parent's thread"
    assert "parent question" in parent_texts
    assert "parent question" not in child_texts, "the parent wrote into the fork's thread"


def test_a_copy_that_fails_partway_leaves_no_half_session_behind() -> None:
    """The atomicity claim, asserted rather than trusted to the connection's defaults.

    `fork_session` copies four tables and says it does so in one transaction. That is only true if
    the connection is *not* autocommit — it is not (`db.connection` yields `autocommit=False`), but
    "not true today" and "cannot become true" are different, and a pool option flipped somewhere
    else would turn a failed fork into a session that lists in `GET /sessions` and cannot load.

    Failure is injected by pointing the copy at a fourth table that does not exist, so three
    tables' rows are already written when it raises — the exact shape a partial fork would take.
    """

    async def _run() -> dict[str, int]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed("fork-atomic")
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            session_fork, "CHECKPOINT_TABLES", (*CHECKPOINT_TABLES, "no_such_table")
        )
        try:
            with pytest.raises(Exception):
                await fork_session("fork-atomic", "owner-1", None)
        finally:
            monkeypatch.undo()
        # Nothing may survive under *any* child id: the fork mints its own, so the assertion is
        # over the whole table rather than over one id this test does not know.
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM checkpoints WHERE thread_id <> %s", ("fork-atomic",)
            )
            row = await cur.fetchone()
            return {"orphans": int(row[0]) if row else 0}

    assert asyncio.run(_run()) == {"orphans": 0}, "a failed fork left checkpoint rows behind"


def test_a_memory_deployment_says_it_cannot_fork_rather_than_failing_oddly() -> None:
    """The route is registered and reachable, and answers honestly with no durable store.

    An API-level case beside the store-level ones above, because the two can fail independently: a
    correct `fork_session` behind an unregistered route is a 404, and a registered route that
    assumed a store would be a 500 on the shipped `session_store=memory` default. 501 says the
    deployment does not have the feature, which is the true answer — a fork copies a *thread*, and
    a memory deployment has none to copy.
    """
    from fastapi.testclient import TestClient

    from tests.test_service import _app

    client = TestClient(_app())
    session_id = client.post("/sessions").json()["session_id"]

    response = client.post(f"/sessions/{session_id}/fork")

    assert response.status_code == 501
    assert "durable session store" in response.json()["detail"]
