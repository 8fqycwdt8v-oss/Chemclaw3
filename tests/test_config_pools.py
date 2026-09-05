"""The measured half of the fleet Postgres budget: how many pools one front-door process opens.

`core/config/__init__.PG_POOLS_PER_FRONT_DOOR_PROCESS` is the factor the startup check multiplies a
front-door process by, and it is a property of `core/db`'s pool keying rather than of any
deployment — so it can only be stated as a literal in `core/config` (`chemclaw.core` imports no
sibling) and can only be *proven* here, against a live server.

Why this file exists at all rather than a unit test with a stubbed pool: the defect it closes was
invisible to every test the suite had, because a pool is keyed on `(loop, dsn, options)` and nothing
short of opening the real connection shapes reveals how many keys a turn-serving process produces.
The shipped chart declared 112 connections against a `maxConnections: 136` ceiling and passed, while
the fleet opened roughly 208. Adding a fourth pool anywhere on the front door's path turns this red
instead of quietly restoring that under-count.
"""

import asyncio

from chemclaw.core import db
from chemclaw.core.config import PG_POOLS_PER_FRONT_DOOR_PROCESS, settings
from tests.pg import create_checkpoint_tables, migrated_db_or_skip


def test_a_front_door_process_opens_the_pools_the_budget_charges_it_for() -> None:
    """Drive the three connection shapes a turn-serving process opens and count what it holds.

    The three are the stores' pool (any ordinary call site, at `pg_statement_timeout_seconds`),
    `/readyz`'s own probe (`api/routes/ops._database_reachable`, which passes
    `service_readiness_db_timeout_seconds` and so merges a *different* `options` string, and so keys
    a different pool), and the LangGraph checkpointer, which builds its own `AsyncConnectionPool` at
    `pg_pool_max_size` and registers it as a foreign pool.

    Asserted through `_process_max_connections()` — the same function the
    `chemclaw_pg_pool_max_size` gauge publishes — rather than by counting dictionary entries, so
    what this pins is the number the budget is denominated in and not an implementation detail of
    where a pool is held.
    """
    from chemclaw.agent.checkpointer import checkpointer, close_checkpointer

    async def _run() -> int:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        async with db.pooling():
            try:
                async with db.connection(settings.postgres_dsn, operation="pool-probe") as conn:
                    await conn.execute("SELECT 1")
                async with db.connection(
                    settings.session_store_dsn or settings.postgres_dsn,
                    statement_timeout_seconds=settings.service_readiness_db_timeout_seconds,
                ) as conn:
                    await conn.execute("SELECT 1")
                await checkpointer()
                return db._process_max_connections()
            finally:
                await close_checkpointer()

    assert asyncio.run(_run()) == PG_POOLS_PER_FRONT_DOOR_PROCESS * settings.pg_pool_max_size
