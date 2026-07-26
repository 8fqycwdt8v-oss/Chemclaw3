"""Apply the SQL migrations in `infra/sql/` to the configured database.

Ordered `.sql` files applied in filename order, each tracked in a `schema_migrations`
ledger so a file runs exactly once and an already-applied file that later changes is
flagged as drift rather than silently re-run. Run via `make db-migrate` (and in CI
before the integration tests); also imported by the store's integration test so
schema setup lives in one place.

Each file is sent whole (psycopg's simple-query protocol executes all of a file's
semicolon-separated statements in one round trip when there are no placeholders), so
a statement containing a `;` inside a string literal or a `DO $$ … $$` block applies
intact — no fragile client-side splitting.
"""

import asyncio
import hashlib
from pathlib import Path

from chemclaw.config import settings
from chemclaw.db import connect

_SQL_DIR = Path(__file__).resolve().parent.parent / "infra" / "sql"
# The ledger's own DDL. Applied first and not itself tracked — it is the tracker.
_LEDGER_FILE = "000_schema_migrations.sql"


class MigrationError(RuntimeError):
    """A migration cannot be applied safely (e.g. an applied file was edited)."""


def _checksum(text: str) -> str:
    """SHA-256 of a migration file's text, to detect edits after it was applied.

    File integrity, deliberately not `chemclaw.ids.stable_hash` (which is for
    content-addressed *identity* keys over JSON) — here the raw bytes are what matter.
    """
    return hashlib.sha256(text.encode()).hexdigest()


async def migrate(dsn: str | None = None) -> list[str]:
    """Apply every not-yet-applied `infra/sql/*.sql` file in order; return the names applied.

    Idempotent: files recorded in `schema_migrations` are skipped, so re-running applies
    nothing and returns `[]`. Raises `MigrationError` if a previously applied file's
    checksum no longer matches — an edited migration must become a new file, never a
    silent in-place change.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    applied: list[str] = []
    async with await connect(target) as conn:
        # Bootstrap the ledger before anything can be tracked against it.
        await conn.execute((_SQL_DIR / _LEDGER_FILE).read_text())
        for path in sorted(_SQL_DIR.glob("*.sql")):
            if path.name == _LEDGER_FILE:
                continue
            text = path.read_text()
            checksum = _checksum(text)
            cursor = await conn.execute(
                "SELECT checksum FROM schema_migrations WHERE filename = %s", (path.name,)
            )
            row = await cursor.fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise MigrationError(
                        f"migration {path.name} was edited after being applied "
                        f"(recorded checksum differs); add a new migration file instead"
                    )
                continue
            await conn.execute(text)
            await conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
            applied.append(path.name)
        await conn.commit()
    return applied


if __name__ == "__main__":
    names = asyncio.run(migrate())
    print(f"applied migrations: {', '.join(names) or '(none — already up to date)'}")
