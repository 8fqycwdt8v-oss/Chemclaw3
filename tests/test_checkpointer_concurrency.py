"""What the checkpointer does when more than one caller reaches it at once.

Two properties, both measured before they were asserted, and both invisible to every metric and
every test that existed:

- **The pod serializes on the saver's lock, and nothing could see it.** `AsyncPostgresSaver._cursor`
  opens `async with self.lock, get_connection(...)`, so every checkpointer statement in the process
  runs one at a time — *before* the pool is asked for anything. Measured on 8 concurrent turns:
  max concurrency inside the saver 1, max wait 612 ms, and during a deliberate stall
  `chemclaw_pg_pool_requests_waiting` read **0** — the exact symptom `core/db.register_pool`'s
  docstring claimed it had closed. The serialization stays (removing it is a change that wants its
  own measurement); what is fixed is that it is now visible.
- **Two pods migrating the checkpoint tables at once: one used to raise.**
  `CREATE TABLE IF NOT EXISTS` is not race-safe against itself, and the loser's error is a
  `psycopg.Error` that is not an `OperationalError`, so `_translating` never saw it and a chemist
  got a non-retryable "internal" about a state another pod had just created correctly. Serialized
  now by the advisory lock `core/migrate.py` already uses for the same problem — a retry was tried
  first and measured *still failing*, because the winner is mid-migration when the loser retries.
"""

import asyncio
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from chemclaw.agent.checkpointer import (
    SchemaStampedSaver,
    _checkpoint_pool,
    _setup_once,
    checkpointer_statements_waiting,
    close_checkpointer,
)
from chemclaw.core.config import settings
from chemclaw.core.db import _pool_for, connect
from chemclaw.core.metrics import METRICS
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


async def _pool() -> AsyncConnectionPool[Any]:
    """A pool shaped like the checkpointer's own — autocommit, opened on demand."""
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        conninfo=settings.postgres_dsn,
        kwargs={"autocommit": True},
        min_size=0,
        max_size=8,
        open=False,
    )
    await pool.open()
    return pool


def test_a_queue_on_the_savers_lock_is_visible_where_no_pool_metric_could_show_it() -> None:
    """The gauge moves while a statement is held, and the pool's own gauge does not.

    Driven by holding one checkpointer statement open and asking a second one for a cursor: the
    second is blocked on `self.lock`, which is a queue the pool has never been told about. Before
    this instrumentation an operator watching the metric they were pointed at saw a flat zero
    through a full stall.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        pool = await _pool()
        try:
            saver = SchemaStampedSaver(pool)
            assert checkpointer_statements_waiting() == 0

            holding = asyncio.Event()
            release = asyncio.Event()

            async def _hold() -> None:
                """Occupy the saver's lock the way one slow statement does."""
                async with saver._cursor():
                    holding.set()
                    await release.wait()

            async def _queued() -> None:
                """A second statement, which cannot enter until the first gives the lock back."""
                async with saver._cursor():
                    pass

            first = asyncio.create_task(_hold())
            await asyncio.wait_for(holding.wait(), 10)
            second = asyncio.create_task(_queued())
            # Let the second task reach the lock and block there.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if checkpointer_statements_waiting() >= 2:
                    break
            waiting = checkpointer_statements_waiting()
            release.set()
            await first
            await second

            assert waiting >= 2, (
                "a statement queued on the saver's lock did not show up in "
                f"chemclaw_checkpointer_statements_waiting; it read {waiting}"
            )
            assert checkpointer_statements_waiting() == 0, (
                "the gauge did not come back down, so it leaks and reads high forever"
            )
            rendered = METRICS.render()
            assert "chemclaw_checkpointer_lock_wait_seconds_count" in rendered, (
                "the wait histogram was never observed, so the cost of the queue is unmeasured"
            )
        finally:
            await pool.close()

    asyncio.run(_run())


def test_two_pods_migrating_the_checkpoint_tables_at_once_do_not_fail_a_turn() -> None:
    """The second migrator waits and finds the work done, instead of failing the chemist.

    Run against a schema that has never seen these tables, which is what a fresh deployment is and
    what CI's throwaway container is. Measured unguarded: `pod-A` raised `UniqueViolation` on
    `checkpoint_migrations_pkey` (and, on another run, on `pg_type_typname_nsp_index`) while
    `pod-B` succeeded and every index ended up present and valid — so the failure was reported
    about a state that was already correct.

    **A single retry was measured here first and is not sufficient**, which is why this asserts the
    outcome rather than the mechanism: with the retry in place the loser still failed, because the
    winner's migration was in flight when it re-read the version ledger and it collided on the same
    row again.
    """
    schema = "chemclaw_setup_race"

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.commit()
        separator = "&" if "?" in settings.postgres_dsn else "?"
        dsn = f"{settings.postgres_dsn}{separator}options=-c%20search_path%3D{schema}"
        # `dict_row`, because `SchemaStampedSaver` wraps upstream's saver, which reads its rows
        # by column name. The default tuple factory type-checks as a different pool entirely.
        pools = [
            AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
                conninfo=dsn,
                kwargs={"autocommit": True, "row_factory": dict_row},
                min_size=0,
                max_size=4,
                open=False,
            )
            for _ in range(2)
        ]
        try:
            for pool in pools:
                await pool.open()
            savers = [SchemaStampedSaver(pool) for pool in pools]
            results = await asyncio.gather(
                *(_setup_once(saver, dsn) for saver in savers), return_exceptions=True
            )
            failures = [r for r in results if isinstance(r, BaseException)]
            assert not failures, (
                "a concurrent migrator's error reached the caller as a non-retryable failure "
                f"about a schema the other pod had just created: {failures}"
            )
            # And the schema really is complete afterwards, so nothing is hiding a half-run.
            async with await connect(dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT count(*) FROM pg_tables WHERE schemaname = %s "
                        "AND tablename IN ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes')",
                        (schema,),
                    )
                    row = await cur.fetchone()
            assert row is not None and int(row[0]) == 3, row
        finally:
            for pool in pools:
                await pool.close()
            async with await connect(settings.postgres_dsn) as conn:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                await conn.commit()

    asyncio.run(_run())


@pytest.mark.parametrize("error", [psycopg.errors.UniqueViolation, psycopg.errors.DuplicateTable])
def test_a_real_setup_failure_is_still_reported_rather_than_retried_away(
    error: type[Exception],
) -> None:
    """The lock serializes migrators; it must not also swallow a failure that is not a race.

    A single retry was the first fix and is measurably not enough — the winner's migration is
    still in flight when the loser retries, so it collides on the same ledger row again. Having
    replaced it with the lock, this pins that no retry crept back in beside it: a `setup()` that
    fails under the lock has nobody to have raced.
    """

    class _Saver:
        """A saver whose `setup()` records that it ran and then fails."""

        def __init__(self) -> None:
            self.calls = 0

        async def setup(self) -> None:
            """Fail the way a genuine schema problem would."""
            self.calls += 1
            raise error("duplicate key value violates unique constraint")

    async def _run() -> None:
        await migrated_db_or_skip()
        saver = _Saver()
        with pytest.raises(error):
            await _setup_once(saver, settings.postgres_dsn)  # type: ignore[arg-type]
        assert saver.calls == 1, (
            "the lock is the whole mechanism; a retry on top of it would hide a real schema "
            "failure behind a second attempt that cannot succeed either"
        )

    asyncio.run(_run())


def test_the_checkpointer_pool_agrees_with_every_other_pool_in_the_process() -> None:
    """The one pool every turn's state write goes through, held to `core/db`'s own settings.

    It used to name none of them, so it ran on psycopg_pool's defaults while every `core/db` pool
    in the same process ran on the configured ones — measured live: `timeout=30.0` against
    `pg_pool_timeout_seconds=10.0`, `max_idle=600` against `pg_pool_max_idle_seconds=300`, and no
    `check` at all. A saturated waiter was therefore refused at 30.02 s rather than 10.01 s,
    holding an admission permit for six times the admission timeout, and a backend killed from
    outside the pool reached a turn as `AdminShutdown` where `core/db`'s pool would have swapped it.

    **Compared against a `core/db` pool rather than against literals**, which is the whole point: a
    setting added there must not be silently declined here, and a test naming the numbers would
    agree with itself while the two pools drifted apart. `min_size` is excluded and only
    `min_size` — the checkpointer's 0 is deliberate and says why beside itself.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        reference = _pool_for(settings.postgres_dsn, None, settings.pg_pool_max_size)
        try:
            mine = await _checkpoint_pool()
            for setting in ("timeout", "max_idle", "max_size"):
                assert getattr(mine, setting) == getattr(reference, setting), (
                    f"the checkpointer pool's {setting} is {getattr(mine, setting)} where every "
                    f"other pool in this process uses {getattr(reference, setting)}"
                )
            assert mine._check is reference._check is not None, (
                "the checkpointer pool has no connection check, so a backend killed outside the "
                "pool reaches a turn instead of being swapped"
            )
        finally:
            await close_checkpointer()

    asyncio.run(_run())
