"""Persistent keyset cursor for an append-only reaction feed's drain.

`corpus_sync.py` carries its keyset position *inside one run* and stores nothing, because a corpus
release is a versioned load: re-walking an unchanged one is a no-op, and a new one has to be walked
from the top anyway. A live feed is the other case — every daily fire would read the whole corpus to
find the rows added since yesterday — so a source whose binding says `append_only: true` keeps its
position here, in `corpus_cursors` (`infra/sql/063_…`).

**Its own table rather than a row in `sync_cursors`.** That column is `TIMESTAMPTZ` and its contract
is a datetime watermark; a keyset position is a `TEXT` key in the source's own domain, which may be
a bigint, a ULID or a padded string.

**No lag gauge, unlike `ingest/eln/cursor.py`, and the absence is deliberate.** That module can say
how far behind a cursor is because a datetime subtracts from `now()`. A keyset value is opaque —
nothing here can tell how many rows sit beyond it — so a gauge would have to invent a number.
`ReactionCorpusWorkflow` already reports `read`/`recorded` per pass, and a feed that has stopped
advancing shows up there as a run that records nothing while `has_more` stays false.
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

    `""` is what a source that has never drained holds, and it is also what an operator leaves
    behind by deleting the row — the supported way to force a full re-walk. Both mean the same
    thing to `drain_corpus`, which is why neither is distinguished here.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="corpus_cursor_load") as conn:
        cursor = await conn.execute(_SELECT, (source,))
        row = await cursor.fetchone()
    return str(row[0]) if row is not None else ""


async def store_corpus_cursor(source: str, after: str, dsn: str | None = None) -> None:
    """Persist the advanced keyset position for `source` (upsert).

    An empty `after` is not stored: a pass that advanced past nothing has nothing to resume after,
    and writing it would overwrite a real position with a restart.
    """
    if not after:
        return
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="corpus_cursor_store") as conn:
        await conn.execute(_UPSERT, (source, after))
        await conn.commit()
