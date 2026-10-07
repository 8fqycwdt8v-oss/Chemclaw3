"""What the checkpointer does when more than one caller reaches it at once.

- `AsyncPostgresSaver._cursor` serializes every statement on `self.lock` before the pool is asked,
  so pool metrics cannot see the queue; a dedicated gauge makes it visible.
- Two pods migrating the checkpoint tables at once: `CREATE TABLE IF NOT EXISTS` races and the
  loser's error is not an `OperationalError`. Serialized by `core/migrate.py`'s advisory lock; a
  retry is insufficient because the winner is still mid-migration.
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


async def test_a_queue_on_the_savers_lock_is_visible_where_no_pool_metric_could_show_it() -> None:
    """The gauge moves while a statement is held, and the pool's own gauge does not.

    A second statement blocks on `self.lock`, a queue the pool knows nothing about.
    """
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


async def test_two_pods_migrating_the_checkpoint_tables_at_once_do_not_fail_a_turn() -> None:
    """The second migrator waits and finds the work done, instead of failing the chemist.

    Run against a schema without these tables, as on a fresh deployment. Asserts the outcome rather
    than the mechanism.
    """
    schema = "chemclaw_setup_race"

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


@pytest.mark.parametrize("error", [psycopg.errors.UniqueViolation, psycopg.errors.DuplicateTable])
async def test_a_real_setup_failure_is_still_reported_rather_than_retried_away(
    error: type[Exception],
) -> None:
    """The lock serializes migrators; it must not also swallow a failure that is not a race.

    A `setup()` that fails under the lock had nobody to race, so it is reported, not retried.
    """

    class _Saver:
        """A saver whose `setup()` records that it ran and then fails."""

        def __init__(self) -> None:
            self.calls = 0

        async def setup(self) -> None:
            """Fail the way a genuine schema problem would."""
            self.calls += 1
            raise error("duplicate key value violates unique constraint")

    await migrated_db_or_skip()
    saver = _Saver()
    with pytest.raises(error):
        await _setup_once(saver, settings.postgres_dsn)  # type: ignore[arg-type]
    assert saver.calls == 1, (
        "the lock is the whole mechanism; a retry on top of it would hide a real schema "
        "failure behind a second attempt that cannot succeed either"
    )


async def test_the_checkpointer_pool_agrees_with_every_other_pool_in_the_process() -> None:
    """The checkpointer pool agrees with every other `core/db` pool in the process.

    Compared against a real `core/db` pool rather than literals, so a setting added there cannot be
    silently missing here. `min_size` is excluded; the checkpointer's 0 is deliberate.
    """
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
