"""The two deleters of the checkpoint tables, driven against a turn landing in the middle of them.

`durable/retention.py` was fixed under `D-2026-09-06-a-sweep-and-a-live-turn-are-two-writers` and
the same defect stayed in the other two sites for a week: `agent/leaver.py`'s erasure and
`agent/session_store.py`'s single-session delete each built their own checkpoints-first order by
iterating `CHECKPOINT_TABLES`, with no re-ask, so a checkpoint committed by a live turn *after*
`DELETE FROM checkpoints` survived while the later `DELETE FROM checkpoint_blobs` took its payload.

**The interleaving is stepped by hand, and it has to be.** The window is a millisecond wide and
cannot be hit by timing: the deleter's statements are executed one at a time on one connection
while a second connection plays the checkpointer's committed write between them — which is exactly
what the checkpointer's `autocommit=True` pool does to a sweep that thinks one transaction protects
it. That is the same instrumentation the wave-6 review used to find this, kept here because a test
that cannot produce the interleaving cannot tell the two orders apart: both are green without it.
"""

import asyncio

import pytest

from chemclaw.agent.checkpointer import (
    CHECKPOINT_TABLES,
    checkpoint_thread_delete_statements,
)
from chemclaw.agent.session_store import _session_delete_statements
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

# A thread id per test. They share one isolation schema, and the interleaved insert is an
# `ON CONFLICT DO NOTHING` against a row the deleter's *open* transaction may have just deleted —
# which blocks until that transaction ends, so a checkpoint id left behind by an earlier test
# deadlocks the next one against its own deleter rather than failing it.
_SESSION_THREAD = "sess-delete-order-session"
_ERASE_THREAD = "sess-delete-order-erase"


async def _seed(thread_id: str, checkpoint_id: str) -> None:
    """One thread with a checkpoint and the blob its channel values live in."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO checkpoint_blobs "
                "(thread_id, checkpoint_ns, channel, version, type, blob) "
                "VALUES (%s, '', 'messages', %s, 'msgpack', %s) ON CONFLICT DO NOTHING",
                (thread_id, checkpoint_id, b"payload"),
            )
            await cur.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                "VALUES (%s, '', %s, '{}', '{}') ON CONFLICT DO NOTHING",
                (thread_id, checkpoint_id),
            )
        await conn.commit()


async def _count(table: str, thread_id: str) -> int:
    """Rows of `table` for one thread."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s", (thread_id,))
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _delete_with_a_turn_landing_midway(
    statements: tuple[tuple[str, str], ...], params: dict[str, object], thread_id: str
) -> None:
    """Run the deleter's statements, committing a live turn's checkpoint after the first one."""
    async with await connect(settings.postgres_dsn) as deleter:
        async with deleter.cursor() as cur:
            for index, (_table, statement) in enumerate(statements):
                await cur.execute(statement, params)
                if index == 0:
                    # The checkpointer's own pool is `autocommit=True`, so a turn's write is
                    # committed and visible to every *later* statement's snapshot — which is the
                    # whole mechanism, and why one transaction around the deleter proves nothing.
                    await _seed(thread_id, "c2")
        await deleter.commit()


def test_a_session_delete_leaves_no_checkpoint_whose_payload_it_took() -> None:
    """A turn landing mid-delete keeps its checkpoint *and* the blob that checkpoint needs.

    Before the re-ask this left `checkpoints=1, blobs=0` — a thread that reads back as a bricked
    conversation (`CheckpointValuesMissing`) produced by an operation that reported success.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed(_SESSION_THREAD, "c1")
        await _delete_with_a_turn_landing_midway(
            tuple(
                (table, statement)
                for table, statement in _session_delete_statements()
                if table in CHECKPOINT_TABLES
            ),
            {"session_id": _SESSION_THREAD},
            _SESSION_THREAD,
        )
        surviving = await _count("checkpoints", _SESSION_THREAD)
        blobs = await _count("checkpoint_blobs", _SESSION_THREAD)
        assert surviving == 1, "the turn's own checkpoint should survive a delete it raced"
        assert blobs >= 1, (
            "a surviving checkpoint whose blobs were taken is a thread nobody can resume; "
            f"checkpoints={surviving} blobs={blobs}"
        )

    asyncio.run(_run())


def test_an_erasure_leaves_no_checkpoint_whose_payload_it_took() -> None:
    """The same statements the erasure sweep runs, against the same interleaving.

    `leaver` reaches its threads through a `session_owners` subselect rather than by id, so the
    predicate differs and the rule does not: the two dependent statements must re-ask whether the
    thread still has a `checkpoints` row.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        from chemclaw.agent.leaver import _CHECKPOINT_ERASE

        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET owner = EXCLUDED.owner",
                    (_ERASE_THREAD, "oid-delete-order"),
                )
            await conn.commit()
        await _seed(_ERASE_THREAD, "c1")
        await _delete_with_a_turn_landing_midway(
            _CHECKPOINT_ERASE, {"actors": ["oid-delete-order"]}, _ERASE_THREAD
        )
        surviving = await _count("checkpoints", _ERASE_THREAD)
        blobs = await _count("checkpoint_blobs", _ERASE_THREAD)
        assert surviving == 1
        assert blobs >= 1, f"checkpoints={surviving} blobs={blobs}"

    asyncio.run(_run())


@pytest.mark.parametrize("table", [t for t in CHECKPOINT_TABLES if t != "checkpoints"])
def test_every_dependent_statement_re_asks_whether_the_thread_still_has_a_checkpoint(
    table: str,
) -> None:
    """The rule itself, so a fourth checkpointer table cannot be added without it.

    Asserted on the shape rather than only on the behaviour above, because the behavioural test
    needs a database and this one names what a reader has to preserve.
    """
    statements = dict(checkpoint_thread_delete_statements("thread_id = %(session_id)s"))
    assert "NOT EXISTS (SELECT 1 FROM checkpoints c" in statements[table], statements[table]
    assert statements["checkpoints"].count("NOT EXISTS") == 0, (
        "the table that dates the thread is the one the others re-ask about; it cannot re-ask "
        "about itself"
    )
