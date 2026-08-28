"""Persistent high-water cursor for the durable ELN sync (plan step 4.5).

The scheduled sync must resume where the last run left off without the Temporal Schedule
threading state through its payload. The cursor — the newest entry timestamp already
ingested — lives in the `sync_cursors` table keyed by source (`infra/sql/007_…`): a
scheduled run loads it, syncs everything newer, and stores the advanced value, so each
firing is self-contained. Idempotent ingestion makes an occasional boundary re-fetch
harmless.

**This needs no locking, and the reason is that nothing advances one source's cursor
concurrently** — not that the write would be safe if something did. The scheduled sync is one
Temporal Schedule under `ScheduleOverlapPolicy.SKIP` firing one workflow whose activities are
strictly sequential, and the only other starter (`cli.live_data.backfill`) passes an explicit
`since`, so it never reads or writes this table. The write itself is last-writer-wins: two drains
that both loaded one mark leave the *lagging* one's value, measured in `tests/test_cursor.py`
against overlapping transactions on real Postgres. What makes that acceptable rather than merely
unreached is its direction — every stored value is a mark somebody had already ingested through,
so a lost update moves the cursor *back* and costs a re-ingest; it can never move it past an entry
nobody read. The tests pin both halves, so a later high-water spelling of the upsert has to be a
decision rather than a drift. See
`docs/decisions/D-2026-08-27-what-a-second-background-worker-would-race-on.md`.
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
    "ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()"
)


# The last cursor each source was seen holding, in this process — so the gauge is per *pod*, and an
# alert on it has to aggregate (`min by (source)`) rather than read one series. At
# `workers.background.replicas: 1` there is one series and the distinction is invisible; with two,
# the pod that did not run the last sync reports a mark one scheduling interval older, or no series
# at all until it runs its first. Not the *lag* — the cursor — so
# the gauge below is `now() - cursor` computed at scrape time rather than at sync time: an operator
# reading a frozen "3600 s behind" cannot tell a source that is an hour behind from a sync that
# stopped running an hour ago, and those are the two states this metric exists to separate. The
# reading costs one subtraction and no query, which is what makes refreshing it on the sync pass
# (rather than on every scrape) the right trade in the first place.
_OBSERVED: dict[str, datetime] = {}


def _cursor_lags() -> dict[str, float]:
    """How far behind now each observed cursor is, in seconds — the gauge family's source.

    A source that has never synced holds the epoch and therefore reports decades, which is the
    honest reading: it is not "up to date", it has ingested nothing. Floored at zero because a
    cursor may legitimately sit a fraction ahead of local wall clock across two machines.
    """
    now = datetime.now(UTC)
    # No per-source guard, and it does not need one: `observe_cursor` is the only writer of
    # `_OBSERVED` and it normalizes, so every value here is aware. Guarding *here* instead would
    # have been the wrong place — one bad source would drop only itself, but the reading would be
    # silently missing rather than corrected.
    return {
        source: max(0.0, (now - cursor).total_seconds()) for source, cursor in _OBSERVED.items()
    }


def observe_cursor(source: str, cursor: datetime) -> None:
    """Record where `source`'s cursor stands, for `chemclaw_ingest_cursor_lag_seconds`.

    **Normalized to UTC here, because one naive datetime removed the whole family.** `_cursor_lags`
    subtracts inside a dict comprehension with no per-source guard, so a single tz-naive value
    raises `TypeError: can't subtract offset-naive and offset-aware datetimes` for *every* source —
    and a gauge source that raises is dropped whole by the registry's guard, so
    `ChemclawIngestCursorStalled` loses the data it fires on, permanently and silently. Measured
    with `observe_cursor("naive-source", datetime(2026, 1, 1))`: the family vanished from the
    scrape, leaving `chemclaw_gauge_read_failures_total` +1 and one line saying the gauge "could
    not be read" — neither of which names a cursor, a source, or a lag.

    The load path cannot produce one (`sync_cursors.cursor` is `TIMESTAMPTZ`, so psycopg hands back
    an aware value), but `store_cursor` persists whatever its caller computed and nothing enforces
    tz-awareness on the way in — `durable/eln_sync.py` hands it a value derived from an ELN's own
    timestamps. Normalizing at the one door both paths go through is the fix that cannot be
    forgotten by a third; a naive value is *read as UTC* rather than rejected, because this is
    telemetry and refusing to observe a cursor would lose the reading the caller came to give.

    **Called on load as well as on store, and the load is the half that matters.** The wedge
    `ingest/eln/sync.py` documents at length — a fetch that keeps returning the same amended page,
    so the cursor never moves past it — advances nothing and stores nothing, while the run's own
    log reads `ingested=N rejected=0`. Observing what each run *loaded* is what makes that visible:
    the cursor stands still and the lag climbs by one scheduling interval per run, which is a shape
    no alert on ingest counts can see, because a source that is genuinely quiet produces the same
    counts and a lag that does not climb past its own cadence.
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
    """Persist the advanced high-water `cursor` for `source` (upsert)."""
    target = dsn if dsn is not None else settings.postgres_dsn
    async with db.connection(target, operation="sync_cursor_store") as conn:
        await conn.execute(_UPSERT, (source, cursor))
        await conn.commit()
    observe_cursor(source, cursor)


# Bound at import rather than from a startup hook, for the reason `db.bind_pool_metrics` is bound
# from `pooling()`: the reading lives in this module, so a process that can move a cursor is
# exactly a process that should report the lag, and there is no second place to remember it in.
record_metric(lambda m: m.bind_gauge_family("chemclaw_ingest_cursor_lag_seconds", _cursor_lags))
