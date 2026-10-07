"""Reconcile the runtime principal's database privileges (`make db-grants`).

Applies `infra/sql/grants/*.sql` as the migrator on every deploy, after the migrations. Not a
numbered migration (D-2026-08-05-append-only-by-grant-not-by-contract): a grant reconciles a
growing schema with a role that may be created at any time, so it must re-run, whereas migrations
apply once per file. The migration runner globs `infra/sql/*.sql` non-recursively, so these files
are invisible to it.

No advisory lock or `lock_timeout`: `GRANT`/`REVOKE` take only brief catalog row locks, and
concurrent deploys applying the same idempotent statements converge.
"""

import asyncio
from pathlib import Path

import psycopg

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.migrate import migration_dsn

# Beside the migrations rather than under a settings key of its own: the two are one directory's
# worth of SQL, and a second configurable path is a second thing to get wrong in a container image.
GRANTS_SUBDIR = "grants"


def grant_files() -> list[Path]:
    """Every `infra/sql/grants/*.sql` file, in filename order.

    Ordered because the reconciliation revokes before it grants.
    """
    return sorted((Path(settings.sql_migrations_dir) / GRANTS_SUBDIR).glob("*.sql"))


def _report(diagnostic: psycopg.errors.Diagnostic) -> None:
    """Print what the reconciliation reported, so a deploy log carries it.

    `app_privileges.sql` ends in a drift audit (role membership, writes held by `PUBLIC`, default
    privileges, app-owned tables left unwritable) emitted as notices. Reported rather than raised,
    because a raise would fail the `pre-upgrade` hook over a hand-grant the deploy cannot undo, and
    printed because nothing else in the process reads notices.
    """
    print(f"{diagnostic.severity}: {diagnostic.message_primary}")


async def apply_grants(dsn: str | None = None) -> list[str]:
    """Apply every grant file; return the names applied.

    Idempotent: each file revokes what it is about to grant. Nothing is tracked in
    `schema_migrations`, since tracking would make it run-once. An empty directory is an error,
    because a deploy that reconciled nothing must not report success.

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
        conn.add_notice_handler(_report)
        for _name, text in sources:
            await conn.execute(text)
        await conn.commit()
    return [name for name, _ in sources]


if __name__ == "__main__":
    print(f"applied grants: {', '.join(asyncio.run(apply_grants()))}")
