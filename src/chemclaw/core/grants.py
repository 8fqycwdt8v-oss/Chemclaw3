"""Reconcile the runtime principal's database privileges (`make db-grants`).

Applies `infra/sql/grants/*.sql` as the migrator, on every deploy, after the migrations. That
cadence is the whole reason this is not a numbered migration
(D-2026-08-05-append-only-by-grant-not-by-contract):

- `infra/sql/*.sql` is applied **exactly once per file** and tracked by checksum, which is right
  for a schema change. A grant is not a schema change; it is a *reconciliation* between a schema
  that keeps growing and a role that may be created at any point in a deployment's life.
- As a one-shot migration it would be wrong twice over. A deployment that creates its runtime role
  after the first `db-migrate` would never have the grants applied at all — and every table added
  by a later migration would ship with no grant, so the application would break on first use of it
  while the ledger reported everything applied.

The runner globs `infra/sql/*.sql` non-recursively, so these files are invisible to it by
construction rather than by an exclusion list that could be forgotten.

No advisory lock and no `lock_timeout`, unlike `chemclaw.core.migrate`: `GRANT` and `REVOKE` take
brief row locks on `pg_class`, not `ACCESS EXCLUSIVE` on a table, so there is no lock queue to get
stuck in front of live traffic. Two concurrent deploys applying the same idempotent statements
converge.
"""

import asyncio
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.migrate import migration_dsn

# Beside the migrations rather than under a settings key of its own: the two are one directory's
# worth of SQL, and a second configurable path is a second thing to get wrong in a container image.
GRANTS_SUBDIR = "grants"


def grant_files() -> list[Path]:
    """Every `infra/sql/grants/*.sql` file, in filename order.

    Ordered for the same reason the migrations are: the reconciliation revokes before it grants, so
    two files run in a defined sequence rather than whatever the filesystem returns.
    """
    return sorted((Path(settings.sql_migrations_dir) / GRANTS_SUBDIR).glob("*.sql"))


async def apply_grants(dsn: str | None = None) -> list[str]:
    """Apply every grant file; return the names applied.

    Idempotent by construction — each file revokes what it is about to grant, so re-running
    converges rather than accumulating. Nothing is tracked in `schema_migrations`, deliberately:
    tracking would reintroduce exactly the run-once semantics this step exists to avoid.
    """
    target = dsn if dsn is not None else migration_dsn()
    paths = grant_files()
    sources = await asyncio.to_thread(lambda: [(p.name, p.read_text()) for p in paths])
    async with await connect(target) as conn:
        for _name, text in sources:
            await conn.execute(text)
        await conn.commit()
    return [name for name, _ in sources]


if __name__ == "__main__":
    names = asyncio.run(apply_grants())
    print(f"applied grants: {', '.join(names) or '(none — no grant files)'}")
