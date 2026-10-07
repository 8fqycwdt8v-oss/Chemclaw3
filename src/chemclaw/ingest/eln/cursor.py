"""Persistent high-water cursor for the durable ELN sync.

The newest entry timestamp already ingested lives in `sync_cursors`, keyed by source, so each
scheduled run loads it, syncs everything newer and stores the advanced value without the Schedule
threading state through its payload.

No locking: nothing advances one source's cursor concurrently (one Schedule under
`ScheduleOverlapPolicy.SKIP`; the backfill CLI passes an explicit `since`). The write is nonetheless
`GREATEST(stored, new)`, so a lagging writer can never move the mark backwards whatever the ingest's
idempotence. Consequently `store_cursor` cannot rewind: to force a re-drain, delete the row.

Not applicable to `ingest/labels/cursor.py`, whose cursor is `TEXT`: `GREATEST` would compare it
lexicographically (`'9' > '10'`) and skip rows.
"""

from datetime import UTC, datetime

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

# The cursor for a source that has never synced: the epoch, so the first run ingests the
# whole backlog (fetching is "newer than", and every real ELN entry postdates 1970).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_SELECT = "SELECT cursor FROM sync_cursors WHERE source = %s"
_UPSERT = (
    "INSERT INTO sync_cursors (source, cursor, updated_at) VALUES (%s, %s, now()) "
    "ON CONFLICT (source) DO UPDATE SET "
    "cursor = GREATEST(sync_cursors.cursor, EXCLUDED.cursor), updated_at = now() "
    # Return the stored mark, which may differ from the argument under `GREATEST`, so the lag gauge
    # never moves backwards when the table did not.
    "RETURNING cursor"
)


# The last cursor each source was seen holding in this process, so the gauge is per pod and alerts
# must aggregate (`min by (source)`). The cursor rather than the lag is stored so the gauge computes
# `now() - cursor` at scrape time: a sync that stopped running keeps climbing rather than freezing.
_OBSERVED: dict[str, datetime] = {}


def _cursor_lags() -> dict[str, float]:
    """How far behind now each observed cursor is, in seconds — the gauge family's source.

    A never-synced source holds the epoch and honestly reports decades. Floored at zero for clock
    differences between machines.
    """
    now = datetime.now(UTC)
    # No per-source guard needed: `observe_cursor` is the only writer and normalizes every value to
    # aware UTC.
    return {
        source: max(0.0, (now - cursor).total_seconds()) for source, cursor in _OBSERVED.items()
    }


def observe_cursor(source: str, cursor: datetime) -> None:
    """Record where `source`'s cursor stands, for `chemclaw_ingest_cursor_lag_seconds`.

    Normalized to UTC here (a naive value is read as UTC), because one naive datetime would make
    `_cursor_lags` raise and the registry drop the whole gauge family. Telemetry, so it normalizes
    rather than rejects.

    Called on load as well as on store: a wedged sync stores nothing, and observing what each run
    loaded is what makes its lag climb visibly.
    """
    _OBSERVED[source] = cursor if cursor.tzinfo is not None else cursor.replace(tzinfo=UTC)


async def load_cursor(source: str, dsn: str | None = None) -> datetime:
    """Return the stored high-water cursor for `source`, or the epoch if none yet."""
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="sync_cursor_load") as conn:
        cursor = await conn.execute(_SELECT, (source,))
        row = await cursor.fetchone()
    stored: datetime = row[0] if row is not None else _EPOCH
    observe_cursor(source, stored)
    return stored


async def store_cursor(source: str, cursor: datetime, dsn: str | None = None) -> None:
    """Advance `source`'s high-water cursor to `cursor` (a store behind the mark is a no-op).

    The gauge observes the mark the statement returns, not the argument.
    """
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="sync_cursor_store") as conn:
        result = await conn.execute(_UPSERT, (source, cursor))
        row = await result.fetchone()
        await conn.commit()
    # `DO UPDATE` is unconditional, so the statement always writes a row and always returns one;
    # the fallback exists for the type checker, not for a state this can reach.
    observe_cursor(source, row[0] if row is not None else cursor)


# Bound at import: any process that can move a cursor should report the lag, with nothing else to
# remember.
record_metric(lambda m: m.bind_gauge_family("chemclaw_ingest_cursor_lag_seconds", _cursor_lags))
