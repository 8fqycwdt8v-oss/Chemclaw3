"""Persistent high-water cursor for the durable ELN sync (plan step 4.5).

The scheduled sync must resume where the last run left off without the Temporal Schedule
threading state through its payload. The cursor — the newest entry timestamp already
ingested — lives in the `sync_cursors` table keyed by source (`infra/sql/007_…`): a
scheduled run loads it, syncs everything newer, and stores the advanced value, so each
firing is self-contained. Idempotent ingestion makes an occasional boundary re-fetch
harmless, so this needs no locking.
"""

from datetime import UTC, datetime

from chemclaw import db
from chemclaw.config import settings

# The cursor for a source that has never synced: the epoch, so the first run ingests the
# whole backlog (fetching is "newer than", and every real ELN entry postdates 1970).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_SELECT = "SELECT cursor FROM sync_cursors WHERE source = %s"
_UPSERT = (
    "INSERT INTO sync_cursors (source, cursor, updated_at) VALUES (%s, %s, now()) "
    "ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()"
)


async def load_cursor(source: str, dsn: str | None = None) -> datetime:
    """Return the stored high-water cursor for `source`, or the epoch if none yet."""
    target = dsn if dsn is not None else settings.postgres_dsn
    async with await db.connect(
        target, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        cursor = await conn.execute(_SELECT, (source,))
        row = await cursor.fetchone()
    return row[0] if row is not None else _EPOCH


async def store_cursor(source: str, cursor: datetime, dsn: str | None = None) -> None:
    """Persist the advanced high-water `cursor` for `source` (upsert)."""
    target = dsn if dsn is not None else settings.postgres_dsn
    async with await db.connect(
        target, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        await conn.execute(_UPSERT, (source, cursor))
        await conn.commit()
