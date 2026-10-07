"""The ELN adapter contract.

An adapter fetches raw entries newer than a cursor and maps each into the canonical `OrdReaction`.
Every ELN-specific quirk lives behind this seam, so the sync (`chemclaw.durable.eln_sync`) and
everything above it are identical whichever ELN is wired. One adapter per source; no universal ELN
abstraction.
"""

import inspect
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.ord import OrdReaction

_LATE_ARRIVAL_NAMES_LOGGED = 10

# How many colliding file names one refusal message spells out; the reason column is capped
# (`ingest.rejections._MAX_REASON_CHARS`), so the count comes first and the list is bounded.
_COLLIDING_NAMES_NAMED = 5


def entry_id_or_stem(stated: object, path: Path, field: str) -> str:
    """The id one export file claims, falling back to its file name when it claims none.

    Absent and blank differ: an omitted or `null` field falls back to the file stem; a stated id is
    transcribed as given (`0` included); a stated field that names nothing is refused, since the
    file name is not what the source said.

    Args:
        stated: whatever the payload carried in its id field — `None` when it carried none.
        path: the export file, whose stem is the fallback.
        field: the field's name in this format, for the refusal message.

    Raises:
        ElnMappingError: the field is present and names nothing. Both adapters' scan handlers catch
        this, so the file costs only itself.
    """
    if stated is None:
        return path.stem
    # A JSON boolean is not an id (and `bool` is an `int`), so it is treated as naming nothing
    # rather than filed under `"False"`.
    text = "" if isinstance(stated, bool) else str(stated).strip()
    if not text:
        raise ElnMappingError(
            f"{path.name} states {field!r} as {stated!r}, which names no entry. An export that "
            f"carries {field!r} is claiming an id; leave the field out to be identified by file "
            "name instead"
        )
    return text


def refuse_colliding_ids(
    logger: Logger, source: str, files_by_id: dict[str, list[str]]
) -> dict[str, str]:
    """Refuse every entry id two or more export files claim, and say which files claimed it.

    `reaction_records` is keyed `(ingest_source, reaction_id)`, so two files with one id would be
    one row written twice, silently losing one experiment. Both are refused rather than one kept, as
    `records._one_of` does one layer up: keeping either is a coin flip that reads as fact, while a
    refusal names both files in the ledger. Collisions are checked across the whole directory, not
    the fetch window.

    Args:
        logger: the calling adapter's own, so the record carries that module's name.
        source: the data source's name, for the log line.
        files_by_id: every parsed entry id, mapped to the file names that claimed it.

    Returns:
        The refusals to file, keyed by entry id — empty in the ordinary case.
    """
    refused: dict[str, str] = {}
    for entry_id, names in sorted(files_by_id.items()):
        if len(names) < 2:
            continue
        shown = ", ".join(sorted(names)[:_COLLIDING_NAMES_NAMED])
        if len(names) > _COLLIDING_NAMES_NAMED:
            shown += f", … (+{len(names) - _COLLIDING_NAMES_NAMED} more)"
        reason = (
            f"{len(names)} export files carry the entry id {entry_id!r} ({shown}), so the id does "
            "not name one run and every one of them is refused: the record store keys a row by "
            "(source, entry id) and would have kept whichever was written last. Give each export "
            "a distinct id, or split the directory by source"
        )
        refused[entry_id] = reason
        logger.warning("%s: %s", source, reason)
    return refused


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z') as a tz-aware UTC datetime.

    A naive timestamp is read as UTC, since exports often omit the offset and a naive datetime
    cannot be compared with the sync's aware cursor. Raises `ValueError` on an unparseable string;
    callers wrap it with their own context.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_late_arrival(path: Path, floor: datetime) -> bool:
    """True if `path` appeared at/after the *run's* floor although its payload predates it.

    A file dropped into the export directory after the sync's overlap window, carrying an older
    payload timestamp, would be filtered out on every run; its mtime is the only evidence it arrived
    late. `floor` must be the run's own floor, never a continuation chunk's advancing cursor, or
    every file the run already ingested would re-qualify. A file whose mtime cannot be read is not
    reported; the caller's skip path handles it.
    """
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    return mtime >= floor


def warn_late_arrivals(logger: Logger, source: str, names: list[str]) -> None:
    """Log one aggregated WARNING naming export files that arrived too late to be ingested.

    Aggregated because a late file re-qualifies on every run; names are capped at
    `_LATE_ARRIVAL_NAMES_LOGGED` with the full count kept. Logs nothing when nothing was late, and
    uses the caller's `logger`.
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


def entry_window(
    created_at: datetime, modified_at: datetime | None, retracted_at: datetime | None = None
) -> datetime:
    """The timestamp an entry should be filtered on: the latest thing the source did to it.

    The max of creation, amendment and withdrawal, so an in-place correction or a retraction moves
    the entry past the cursor; filtering on creation alone would never fetch them, and on amendment
    alone would drop never-amended entries.
    """
    return max(stamp for stamp in (created_at, modified_at, retracted_at) if stamp is not None)


class ElnMappingError(ChemclawError):
    """An adapter could not map a raw entry to a canonical reaction.

    Defined at the contract level so the sync's reject-and-continue handler catches any adapter's
    mapping failure.
    """


class RawEntry(BaseModel):
    """One raw ELN entry: its id, its creation time, and its source-shaped payload.

    `payload` is deliberately untyped (`dict[str, Any]`) — it is the ELN's own format,
    which only the adapter that produced it understands. Nothing above the adapter reads it.
    """

    entry_id: str = Field(min_length=1)
    created_at: datetime
    payload: dict[str, Any]
    # When the source last amended this entry, if it says. ELNs correct entries in place while
    # keeping `created_at`; mapping this lets the fetch window see the amendment and the sync
    # compare content. `None` means "not reported", not "never amended".
    modified_at: datetime | None = None
    # When the source reported this entry withdrawn, if it reports withdrawals at all.
    #
    # An explicit field, never absence: a fetch is a delta, so "not seen this run" is normal for
    # every ingested entry. A withdrawn entry rides the same re-export channel as a correction.
    # Re-publishing without it un-retracts, because the row is what the source last said.
    retracted_at: datetime | None = None


@runtime_checkable
class ElnAdapter(Protocol):
    """Fetch new ELN entries and map them to the canonical schema. One per ELN source."""

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Return entries created *or amended* at or after `since` (the sync's high-water cursor).

        Inclusive, so an entry stamped in the cursor's own second is not skipped forever;
        re-fetching it is safe because ingestion is idempotent. Amended entries count as new,
        compared via `entry_window`.

        `limit` and `report_late_arrivals` are optional capabilities an adapter may declare (probed
        by `accepts_a_limit` / `accepts_a_late_arrival_switch`), not protocol requirements. An
        adapter that bounds its read must return a prefix in `entry_window` order — everything
        withheld must be later than everything returned — or the cursor skips entries for good; the
        file-drop adapters cannot guarantee that and ignore `limit`.

        Args:
            since: The fetch floor — entries at or after it, in `entry_window` order.
            limit: At most this many entries strictly newer than `since`; the overlap replay is not
            counted. `None` is unbounded.
            report_late_arrivals: Whether `since` is the run's own floor, so a late-arriving file
            behind it may be reported. `False` on a continuation chunk.
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


def _accepts(adapter: object, parameter: str) -> bool:
    """Whether `adapter.fetch_new_entries` declares `parameter`, so it may be offered.

    `inspect.signature` rather than a `runtime_checkable` Protocol, which sees method names but not
    parameters.
    """
    fetch = getattr(adapter, "fetch_new_entries", None)
    if fetch is None:
        return False
    try:
        return parameter in inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        # No introspectable signature (a builtin): "does not take it" is safe — the sync bounds the
        # result itself and late arrivals keep being reported.
        return False


def accepts_a_late_arrival_switch(adapter: object) -> bool:
    """Whether `adapter.fetch_new_entries` will take the `report_late_arrivals` flag.

    Asked rather than required so out-of-tree adapters written to the published signature keep
    working; one that lacks it reports late arrivals on every chunk.
    """
    return _accepts(adapter, "report_late_arrivals")


def accepts_a_limit(adapter: object) -> bool:
    """Whether `adapter.fetch_new_entries` will take the optional `limit` this sync can offer.

    A capability rather than a protocol parameter: adding a parameter to `ElnAdapter` would break
    out-of-tree adapters written to the published signature. An adapter that can bound its read
    declares it; the caller truncates the result either way.
    """
    return _accepts(adapter, "limit")


def fetch_was_truncated(adapter: object) -> bool:
    """Whether `adapter`'s last fetch was cut short by its own page limit; `False` if it cannot say.

    Only the side that issued the `LIMIT` knows, and the durable sync needs it to come back for more
    and to detect a wedge; a batch of rows at the cursor looks the same as a quiet day. Optional,
    since file-drop adapters have no page. The walk follows each wrapper's public `inner`, because
    the registry always wraps the adapter (`DatedIngest`) and a structural check does not see
    through a wrapper; the visited set stops a self-referencing `inner` from hanging.
    """
    seen: set[int] = set()
    candidate: object | None = adapter
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        if isinstance(candidate, BoundedFetch):
            return candidate.fetch_truncated()
        candidate = getattr(candidate, "inner", None)
    return False


class DatedIngest:
    """An `ElnAdapter` that carries the entry's own timestamp onto a record with no date.

    `performed_at` is what makes a series a timeline (`memory.progression`), so a record the adapter
    left undated gets `RawEntry.created_at`'s date. An adapter's own date always wins. Neither
    file-drop export carries an experiment date, so all of them rely on this.

    An entry's write time is weaker than a chemist-entered experiment date, so the record is stamped
    `date_source="entry"` and `ordering_caveat` says so above the table. It licenses ordering, never
    causality.
    """

    def __init__(self, inner: ElnAdapter) -> None:
        """Wrap `inner`, whose mapping decisions are otherwise untouched."""
        self._inner = inner

    @property
    def inner(self) -> ElnAdapter:
        """The adapter this wraps.

        Public so callers and `fetch_was_truncated` can reach the built adapter. Read-only.
        """
        return self._inner

    async def fetch_new_entries(
        self, since: datetime, limit: int | None = None, *, report_late_arrivals: bool = True
    ) -> list[RawEntry]:
        """Delegate unchanged — dating is purely a mapping concern, and so is bounding.

        Declares both optional parameters so the probes see them, but forwards each only when the
        inner adapter accepts it. Only a `False` `report_late_arrivals` is forwarded; `True` is
        every adapter's default.
        """
        extra: dict[str, Any] = {}
        if limit is not None and accepts_a_limit(self._inner):
            extra["limit"] = limit
        if not report_late_arrivals and accepts_a_late_arrival_switch(self._inner):
            extra["report_late_arrivals"] = False
        return await self._inner.fetch_new_entries(since, **extra)

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Map through the wrapped adapter, then date the record if it came back undated."""
        reaction = self._inner.map_to_ord(raw)
        if reaction.performed_at is not None:
            return reaction
        # `model_copy` rather than mutation, so the adapter's own answer stays inspectable. Not
        # re-validated: the only change is an optional date, and `sync_entries` validates next
        # anyway.
        return reaction.model_copy(
            update={"performed_at": raw.created_at.date(), "date_source": "entry"}
        )
