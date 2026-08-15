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

    **An empty directory is an error, not a successful no-op.** This step runs on every deploy and
    its whole purpose is the reconciliation the module docstring describes; finding no files means
    the directory is missing or `sql_migrations_dir` points somewhere else, and the deploy then
    continued with a role holding whatever privileges it happened to have — the exact failure this
    exists to prevent, reported as success and exiting 0. `chemclaw.core.migrate` cannot no-op
    silently because it tracks by checksum; this one has nothing to compare against, so the count
    is the only signal there is.

    Raises:
        RuntimeError: When no grant file was found, naming the directory that was searched.
    """
    target = dsn if dsn is not None else migration_dsn()
    paths = grant_files()
    if not paths:
        raise RuntimeError(
            f"no grant files in {Path(settings.sql_migrations_dir) / GRANTS_SUBDIR} — the runtime "
            "role's privileges were not reconciled. Check sql_migrations_dir and that the grants "
            "directory ships in this image."
        )
    sources = await asyncio.to_thread(lambda: [(p.name, p.read_text()) for p in paths])
    async with await connect(target) as conn:
        for _name, text in sources:
            await conn.execute(text)
        await conn.commit()
    return [name for name, _ in sources]


if __name__ == "__main__":
    # No `or '(none)'` fallback any more: `apply_grants` raises rather than returning an empty list,
    # so this line only ever prints files that were actually applied. The friendly parenthetical it
    # used to print was the deploy's only sign that nothing had been reconciled, and it exited 0.
    print(f"applied grants: {', '.join(asyncio.run(apply_grants()))}")
