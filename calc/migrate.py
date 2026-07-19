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


async def migrate(dsn: str | None = None) -> list[str]:
    """Apply every `infra/sql/*.sql` file in order; return the names applied."""
    target = dsn if dsn is not None else settings.postgres_dsn
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(target) as conn:
        for path in sorted(_SQL_DIR.glob("*.sql")):
            await conn.execute(path.read_text())
            applied.append(path.name)
        await conn.commit()
    return applied


if __name__ == "__main__":
    names = asyncio.run(migrate())
    print(f"applied migrations: {', '.join(names) or '(none)'}")
