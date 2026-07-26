"""Shared bootstrap for Postgres-backed integration tests.

CI provides a real (pgvector-enabled) database; the offline sandbox has none, so
`migrated_db_or_skip` turns an unreachable server into a skip. Kept in one place
so every Postgres-backed test uses the same connect-check + migration (DRY); each
test file only constructs its own store on top of the migrated database.
"""

import psycopg
import pytest

from calc.migrate import migrate
from chemclaw.config import settings


async def migrated_db_or_skip() -> None:
    """Ensure a reachable, migrated Postgres database, or skip if none is available."""
    try:
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        await conn.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (offline sandbox): {exc}")
    await migrate()
