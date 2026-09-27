"""Create the store's tables under the migration credential, before the grants that name them.

**The ordering defect this closes exists only once the memory store is on**, which is why it lands
with the flip rather than with the tier. `store` and `store_migrations` are created at *runtime* by
`AsyncPostgresStore.setup()` on an app pod's first turn (`agent/scratchpad.memory_store`) —
deliberately, because they are upstream's schema and transcribing it into `infra/sql/` would be a
second definition that a bump walks away from.

`infra/sql/grants/app_privileges.sql` therefore grants on them only `IF to_regclass(...) IS NOT
NULL`, and `deploy/entrypoint.sh`'s `migrate` role runs the migrations and then the grants as a
`pre-install`/`pre-upgrade` hook — before any app pod exists. So on a fresh install the tables do
not exist when the grants run, the runtime role gets no `INSERT`/`UPDATE`/`DELETE` on `store`, and
every `/memories/`, `/mine/` and `/org/` write fails until the *next* release's grants pass. The
grants file says as much in its own comment, and calls it "the one group whose grant lands on the
second run — the same run that first needs it".

That was invisible while `agent_memory_enabled` shipped off, because nothing ever wrote to `store`.
It becomes the first-boot experience the moment it does not.

**Run as the migrator, not as the runtime role, and that is the whole reason this is a step rather
than a call to `memory_store()`.** That function builds the store over the *checkpointer's* pool,
which is the runtime credential — and on a fresh install the runtime role has no `CREATE` on the
schema yet, because granting it is what the step after this one does. Using `migration_dsn()` is
what breaks the cycle: the migrator owns the schema, creates the two tables, and the grants that
follow find them and hand the runtime role its privileges on the same run.

**Idempotent, and it has to be**, because it runs on every install and upgrade beside migrations
that are tracked and applied once. `setup()` is upstream's own migration runner over
`store_migrations`, so a second run applies nothing.

**Skipped where the deployment keeps no store**, rather than creating two tables a site never asked
for. That is the same predicate the routes and the mount read, so a deployment that has not enabled
durable memory gets no schema it did not ask for and no grant it cannot use.
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
            resolution `core/migrate.py` and `core/grants.py` use, so the three steps of one hook
            Job cannot disagree about which role owns the schema.

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
    # A context-managed store rather than the process-wide one in `agent/scratchpad.py`: this is a
    # one-shot Job, so it opens its own connection under the migration credential and closes it,
    # rather than joining a pool built for a serving process that does not exist here.
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
