"""The ELN adapter contract (plan step 4.2).

Only the *contract* is fixed, never an ELN's shape: an adapter fetches raw entries newer
than a cursor and maps each into the canonical `OrdReaction`. Every ELN-specific quirk
lives behind this seam (G6), so the sync (`chemclaw.durable.eln_sync`) and everything above it are
identical no matter which ELN is wired. There is no universal ELN abstraction — one adapter
per source (docs/planning/DEFERRED.md: generalize only from a third source).
"""

from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.ord import OrdReaction

_LATE_ARRIVAL_NAMES_LOGGED = 10


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z') as a tz-aware UTC datetime.

    A naive timestamp (no UTC offset) is read as UTC: exports that omit the offset are common,
    UTC is the least-surprising reading, and a naive datetime would later raise `TypeError` when
    compared against the sync's offset-aware cursor. Raises `ValueError` on an unparseable string;
    callers wrap that in their layer-specific format error with the source path/context (DRY: both
    the free-text and ORD adapters share this exact rule).
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_late_arrival(path: Path, since: datetime) -> bool:
    """True if `path` appeared at/after the fetch floor although its payload predates it.

    Why this exists: a file-export adapter keeps entries stamped `>= since` and drops the rest,
    and the sync's overlap window (`eln_sync_overlap_seconds`) rewinds `since` only far enough to
    catch entries written slightly late. A file dropped into the export directory *after* that
    window, carrying an older payload timestamp, is therefore filtered out on this run and on
    every run after it — real data lost with no rejection and no counter. The file's modification
    time is the one available evidence that it arrived late rather than being old data already
    ingested, so it separates "genuinely stale" from "silently dropped".

    A file whose mtime cannot be read (removed mid-fetch, permission error) is *not* reported: the
    caller's own skip-and-continue path already handles unreadable files, and a false alarm here
    would train operators to ignore the warning.
    """
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    return mtime >= since


def warn_late_arrivals(logger: Logger, source: str, names: list[str]) -> None:
    """Log one aggregated WARNING naming export files that arrived too late to be ingested.

    Aggregated, not one line per file, because a permanently-late file re-qualifies on *every*
    sync run: one bounded line per fetch stays readable where an unbounded per-file storm would
    be scrolled past. Names are capped at `_LATE_ARRIVAL_NAMES_LOGGED` with the full count kept,
    so the log line cannot grow without limit. Logs nothing when nothing was late (the normal case),
    and takes the caller's `logger` so the record carries the adapter's own module name.
    """
    if not names:
        return
    shown = ", ".join(names[:_LATE_ARRIVAL_NAMES_LOGGED])
    if len(names) > _LATE_ARRIVAL_NAMES_LOGGED:
        shown += f", … (+{len(names) - _LATE_ARRIVAL_NAMES_LOGGED} more)"
    logger.warning(
        "%s: %d export file(s) arrived after the sync cursor but carry an older timestamp, so "
        "they were not ingested (%s); re-run the sync with an explicit earlier `since` to "
        "backfill them",
        source,
        len(names),
        shown,
    )


def entry_window(created_at: datetime, modified_at: datetime | None) -> datetime:
    """The timestamp an entry should be filtered on: the later of creation and amendment.

    One definition, because an adapter that filtered on `created_at` alone would silently drop
    every in-place correction its source makes — the failure this exists to close — and an adapter
    that filtered on `modified_at` alone would drop every entry that has never been amended.

    `max` rather than "modified if present, else created" only differs when a source reports an
    amendment *older* than the creation it amends, which is clock skew rather than chemistry. It is
    cheap insurance and no test can distinguish the two; said here so the choice does not read as
    load-bearing.
    """
    return max(created_at, modified_at) if modified_at is not None else created_at


class ElnMappingError(ChemclawError):
    """An adapter could not map a raw entry to a canonical reaction (G4).

    Defined at the contract level (not in a concrete adapter) so the sync's
    reject-and-continue handler catches *any* adapter's mapping failure, not just one
    adapter's error type. Concrete adapters raise this (or a subclass) for a bad entry.
    """


class RawEntry(BaseModel):
    """One raw ELN entry: its id, its creation time, and its source-shaped payload.

    `payload` is deliberately untyped (`dict[str, Any]`) — it is the ELN's own format,
    which only the adapter that produced it understands. Nothing above the adapter reads it.
    """

    entry_id: str = Field(min_length=1)
    created_at: datetime
    payload: dict[str, Any]
    # When the source last *amended* this entry, if it says. An ELN corrects an entry in place — a
    # yield revised after assay, an impurity added, a retraction — while keeping `created_at`, so
    # an entry filtered on creation time alone is never fetched again and the correction is lost
    # with no rejection and no counter. An adapter that maps a modification timestamp lets the
    # fetch window see the amendment and the sync compare content rather than skipping on id.
    #
    # Optional because a source may genuinely not record one; `None` means "not reported", not
    # "never amended", and the overlap replay remains the only thing that catches those.
    modified_at: datetime | None = None


@runtime_checkable
class ElnAdapter(Protocol):
    """Fetch new ELN entries and map them to the canonical schema. One per ELN source."""

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return entries created *or amended* at or after `since` (the sync's high-water cursor).

        Inclusive on purpose: the cursor is the newest timestamp already seen, and an
        entry stamped in that same second but exported after the run would be skipped
        forever under strictly-after semantics. Re-fetching the boundary entry is safe
        because ingestion is idempotent (id-keyed upserts + idempotent note branch).

        **Amended entries count as new.** An adapter whose source reports a modification time must
        compare the later of the two against `since` and set `RawEntry.modified_at` — otherwise a
        correction to an old entry is never fetched, and the sync cannot notice what it never
        sees. `entry_window` is that comparison, written once so two adapters cannot disagree.
        """
        ...

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map one raw entry to a canonical `OrdReaction` (the ELN-specific step)."""
        ...


@runtime_checkable
class BoundedFetch(Protocol):
    """An adapter whose fetch is bounded by a page size of its own, and says when it hit it."""

    def fetch_truncated(self) -> bool:
        """Whether the last `fetch_new_entries` stopped at its own limit with rows still waiting."""
        ...


def fetch_was_truncated(adapter: object) -> bool:
    """Whether `adapter`'s last fetch was cut short by its own page limit; `False` if it cannot say.

    **Only the side that issued the `LIMIT` knows this**, and the durable sync has to: it decides
    whether to come back for another chunk, and its wedge guard turns "more waiting, cursor did not
    move" into a loud stop. Inferring it from the batch is not possible — a fetch that returns only
    rows at or behind the cursor is an ordinary quiet day *and* the signature of a source truncating
    inside a block of tied watermarks, and treating the two alike either cries wolf on every idle
    run or misses the truncation entirely. It missed it: a source stuck on a tie reported
    `has_more=False` and read as a day with no new entries.

    Optional rather than a method on `ElnAdapter` because the file-drop adapters read a whole
    directory and have no page to be cut short by, so `False` is the true answer for them and a
    method they would all have to implement would only be a way to get it wrong.
    """
    return adapter.fetch_truncated() if isinstance(adapter, BoundedFetch) else False
