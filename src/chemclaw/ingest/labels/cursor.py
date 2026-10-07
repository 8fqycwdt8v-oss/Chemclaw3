"""Persistent keyset cursor for an append-only reaction feed's drain.

A versioned corpus release keeps its position only within one run; a live feed (`append_only: true`)
stores it here, in `corpus_cursors`, so a daily fire does not re-read the whole corpus. Its own
table because `sync_cursors` holds datetime watermarks, while a keyset position is text in the
source's own domain.

The upsert is deliberately blind, not `GREATEST`: text comparison is lexicographic (`'9' > '10'`),
so a high-water upsert could skip rows on a numeric feed, while a blind one can at worst move back
and re-drain idempotently. No lag gauge, because a keyset value says nothing about how many rows lie
beyond it. A position is stored only when the drain advanced, so `updated_at` means "last moved".
"""

from chemclaw.core import db
from chemclaw.core.config import settings

_SELECT = "SELECT after FROM corpus_cursors WHERE source = %s"
_UPSERT = (
    "INSERT INTO corpus_cursors (source, after, updated_at) VALUES (%s, %s, now()) "
    "ON CONFLICT (source) DO UPDATE SET after = EXCLUDED.after, updated_at = now()"
)


async def load_corpus_cursor(source: str, dsn: str | None = None) -> str:
    """Return the stored keyset position for `source`, or `""` to start at the beginning.

    Deleting the row is the supported way to force a full re-walk.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="corpus_cursor_load") as conn:
        cursor = await conn.execute(_SELECT, (source,))
        row = await cursor.fetchone()
    return str(row[0]) if row is not None else ""


async def store_corpus_cursor(source: str, after: str, dsn: str | None = None) -> None:
    """Persist the advanced keyset position for `source` (upsert).

    An empty `after` is not stored, so it cannot overwrite a real position with a restart.
    """
    if not after:
        return
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="corpus_cursor_store") as conn:
        await conn.execute(_UPSERT, (source, after))
        await conn.commit()
