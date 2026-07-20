"""Apply the SQL migrations in `infra/sql/` to the configured database.

Simplest thing that works (KISS): ordered `.sql` files, each idempotent
(`CREATE ... IF NOT EXISTS`), applied in filename order. No migration framework
until schema churn justifies one. Run via `make db-migrate` (and in CI before the
integration tests); also imported by the store's integration test so schema setup
lives in one place.
"""

import asyncio
from pathlib import Path

import psycopg

from chemclaw.config import settings

_SQL_DIR = Path(__file__).resolve().parent.parent / "infra" / "sql"


def _statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    psycopg's extended protocol executes one command per `execute`, so a file
    with several statements must be sent one at a time. Line comments are stripped
    first (they may contain semicolons), then the remainder is split on `;`. Our
    migration files are controlled — no `--` or `;` inside string literals — so
    this simple approach is safe.
    """
    without_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]


async def migrate(dsn: str | None = None) -> list[str]:
    """Apply every `infra/sql/*.sql` file in order; return the names applied."""
    target = dsn if dsn is not None else settings.postgres_dsn
    applied: list[str] = []
    # connect_timeout: fail fast on an unreachable database instead of hanging.
    async with await psycopg.AsyncConnection.connect(
        target, connect_timeout=settings.pg_connect_timeout_seconds
    ) as conn:
        for path in sorted(_SQL_DIR.glob("*.sql")):
            for statement in _statements(path.read_text()):
                await conn.execute(statement)
            applied.append(path.name)
        await conn.commit()
    return applied


if __name__ == "__main__":
    names = asyncio.run(migrate())
    print(f"applied migrations: {', '.join(names) or '(none)'}")
