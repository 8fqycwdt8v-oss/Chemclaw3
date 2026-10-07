"""Create the store's tables under the migration credential, before the grants that name them.

`store` and `store_migrations` are upstream's schema, created by `AsyncPostgresStore.setup()`.
The grants in `infra/sql/grants/app_privileges.sql` apply only to tables that exist, so this step
runs in the `migrate` hook between the migrations and the grants; otherwise a fresh install's
runtime role would lack write privileges on `store` until the next release.

It runs as the migrator (`migration_dsn()`), because the runtime role has no `CREATE` on a fresh
schema. Idempotent (`setup()` tracks its own migrations), and skipped where the deployment keeps
no store.
"""

import asyncio
import logging

from langgraph.store.postgres.aio import AsyncPostgresStore

from chemclaw.agent.local_skills import personal_skills_available
from chemclaw.core.logging import configure_logging, log_event
from chemclaw.core.migrate import migration_dsn

logger = logging.getLogger(__name__)


async def create_store_tables(dsn: str | None = None) -> bool:
    """Run `AsyncPostgresStore.setup()` as the migrator. Returns whether it ran.

    Args:
        dsn: Override for the credential to create under. Defaults to `migration_dsn()`, the same
            resolution `core/migrate.py` and `core/grants.py` use.

    Returns:
        True when the tables were created or already existed, False when this deployment keeps no
        store and the step was skipped.
    """
    if not personal_skills_available():
        log_event(
            logger,
            "store_setup.skipped",
            "this deployment keeps no durable store, so its tables are not created",
        )
        return False
    target = dsn if dsn is not None else migration_dsn()
    # A one-shot Job: open and close its own connection under the migration credential rather than
    # joining the serving process's pool.
    async with AsyncPostgresStore.from_conn_string(target) as store:
        await store.setup()
    log_event(
        logger,
        "store_setup.ready",
        "the durable store's tables exist; the grants that name them can run",
    )
    return True


def main() -> None:
    """Entry point for `python -m chemclaw.agent.store_setup`.

    Called by `deploy/entrypoint.sh`'s `migrate` role, between the migrations and the grants.
    """
    configure_logging()
    asyncio.run(create_store_tables())


if __name__ == "__main__":
    main()
