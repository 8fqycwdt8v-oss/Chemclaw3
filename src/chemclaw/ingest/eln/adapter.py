"""The ELN adapter contract (plan step 4.2).

Only the *contract* is fixed, never an ELN's shape: an adapter fetches raw entries newer
than a cursor and maps each into the canonical `OrdReaction`. Every ELN-specific quirk
lives behind this seam (G6), so the sync (`chemclaw.durable.eln_sync`) and everything above it are
identical no matter which ELN is wired. There is no universal ELN abstraction — one adapter
per source (docs/planning/DEFERRED.md: generalize only from a third source).
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


def is_late_arrival(path: Path, floor: datetime) -> bool:
    """True if `path` appeared at/after the *run's* floor although its payload predates it.

    **`floor` is the run's, never a continuation chunk's**, and the difference is not academic. A
    drain advances its cursor per chunk, so on a bulk-copy backfill — files whose mtime is the copy
    time and whose payload timestamps are old — every file the earlier chunks already ingested sits
    behind the new cursor with an mtime after it, and re-qualifies here on every later chunk.
    Measured on a 3,000-file corpus at the shipped batch size: 43,471 ledger writes across 30
    chunks, growing 99, 199, 299 … per chunk, each row telling a chemist that no scheduled run will
    fetch an entry that is already in the corpus. The question this answers — *will any scheduled
    run ever fetch this file* — is a question about the floor the run reached down to, which is why
    `fetch_new_entries` takes `report_late_arrivals` and the sync says False on every chunk whose
    floor is not that one.

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
    return mtime >= floor


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

        **`limit` bounds the read, where the read can be bounded.** The durable sync drains a
        source in chunks and truncates what it gets back to `eln_sync_batch_size`
        (`durable/eln_sync.py::_BoundedIngest`) — so without this, every chunk re-read the whole
        outstanding set to keep a hundredth of it, and a drain cost O(corpus²/batch). Measured on a
        3,000-file drop: 30 chunks, 90,000 file reads, 2.55 s against 0.31 s for a corpus a third
        the size. Passing the number down lets a source that can push the bound into its own read
        do so; the warehouse adapter turns it into its `LIMIT`, cutting a continuation
        chunk's read from the binding's page (500 rows by default) to the chunk (100).

        It is a **capability, not a requirement**, on the same terms as `fetch_was_truncated`
        below: an adapter that cannot bound its read may ignore it, because the caller truncates
        anyway. What an adapter may **never** do is return a non-prefix subset — the entries it
        withholds must all be *later*, in `entry_window` order, than every entry it returns.
        Anything else advances the cursor past an entry that was never offered, and no later fetch
        offers it again. That is why the file-drop adapters ignore this: their scan is ordered by
        filename and an entry's window is inside the payload, so a break in that scan drops
        entries the cursor then skips for good.

        `None` means unbounded, which is what a caller reading a whole corpus passes.

        **`report_late_arrivals` is the second capability, and it is a per-*run* question.** An
        adapter that reads a whole directory can see files it is *not* returning — payload behind
        the floor, mtime after it — and reports them as arrivals no scheduled run will fetch. That
        is only answerable against the floor the run reached down to: a continuation chunk's floor
        is the advancing cursor, and every file between the two was ingested by this very run, so
        judging lateness there re-refuses what the drain has just taken in. The sync passes `False`
        on exactly those chunks. An adapter that does not declare the parameter is never told, and
        keeps its previous behaviour.

        Args:
            since: The fetch floor — entries at or after it, in `entry_window` order.
            limit: At most this many entries *strictly newer* than `since`; entries at or before
                it (the sync's overlap replay) are not counted against it. `None` is unbounded.
            report_late_arrivals: Whether `since` is the run's own floor, so a file behind it that
                arrived after it may be reported as never-to-be-fetched. `False` on a continuation
                chunk, whose floor answers a different question.
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

    The one place the introspection happens, because two capabilities are now asked this way and
    the answer for a callable with no introspectable signature has to be the same for both.
    `inspect.signature` rather than a `runtime_checkable` Protocol because structural checks see
    method *names*, not their parameters — the distinction these questions are entirely about.
    """
    fetch = getattr(adapter, "fetch_new_entries", None)
    if fetch is None:
        return False
    try:
        return parameter in inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        # A builtin or a C-implemented callable has no introspectable signature. "It does not take
        # this" is the safe answer for both callers: the sync bounds the result itself, and it
        # keeps reporting late arrivals on every chunk, which is what every adapter did before
        # either capability existed.
        return False


def accepts_a_late_arrival_switch(adapter: object) -> bool:
    """Whether `adapter.fetch_new_entries` will take the `report_late_arrivals` flag.

    Asked rather than required, for the reason `accepts_a_limit` gives below: the protocol may not
    grow a parameter an out-of-tree adapter has never heard of. An adapter that does not take it is
    simply never told to stay quiet — it reports late arrivals on every chunk, which is what every
    adapter did before this existed, and which is only wrong for an adapter that reads a whole
    directory per chunk.
    """
    return _accepts(adapter, "report_late_arrivals")


def accepts_a_limit(adapter: object) -> bool:
    """Whether `adapter.fetch_new_entries` will take the optional `limit` this sync can offer.

    **A capability, asked for, rather than a parameter every adapter must grow** — the same shape
    as `fetch_was_truncated` above and for a sharper version of the same reason. Bounding the read
    rather than the result was measured worth having (a continuation chunk dropped from 500 rows
    to 100), and the first cut of it put `limit` into the `ElnAdapter` protocol. That is a
    breaking change to the one seam D-120 promises is not one: "a new source is one
    `ingest/sources/<name>/datasource.yaml` folder plus its name in `CHEMCLAW_DATA_SOURCES`, with
    **zero** core edits". An out-of-tree adapter written to the documented signature would have
    been called with two positional arguments and raised `TypeError` on its first chunk.

    So the protocol keeps the signature it published, an adapter that *can* bound its read simply
    declares the parameter, and this is how the caller finds out. `inspect.signature` rather than
    a `runtime_checkable` Protocol because structural checks see method *names*, not their
    parameters — the distinction this question is entirely about.
    """
    return _accepts(adapter, "limit")


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

    **Asked through the seam's wrappers, not only of the object handed over.** The registry always
    returns `DatedIngest(...)`, and a `runtime_checkable` Protocol is structural: a wrapper that
    does not redeclare a method simply does not have it. So this read `False` for every source in
    every deployment, including the warehouse adapter that implements `fetch_truncated` precisely
    so the workflow would come back for the truncated remainder. The capability belongs to the
    adapter, so the question has to reach it — the walk below is that rule, and a wrapper that
    exposes what it wraps through the public `inner` satisfies it by doing nothing.

    The visited set is not defensiveness about a cycle anyone would write: it is what keeps a
    mistaken `inner` returning `self` from hanging a sync run rather than failing it.
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

    **Why this is a floor and not a duplicate.** `performed_at` is what makes a series a timeline:
    `memory.progression` orders on it, and `Progression.is_timeline()` refuses to narrate a
    trajectory without it, so a corpus that loses the date loses the whole "what was tried, in what
    order" question — honestly, but completely. The warehouse adapter maps `performed_at` from its
    own bound column and has no fallback, so a site whose ELN keeps its conditions in prose — no
    experiment-date column to bind — produces an entire corpus of undated records while
    `RawEntry.created_at` sits in every one of them, **required**, because the sync watermark cannot
    advance without it.

    **Both file-drop adapters are served by this wrapper too**, where this docstring used to say
    they "already map `raw.created_at.date()` onto the record and are unaffected" — treating as
    fine the exact case the stamp below was added for. Neither shipped export carries an experiment
    date (the JSON ELN's only date field is `timestamp`, the ORD record's is
    `provenance.record_created.time`), so each was filling `performed_at` from the entry's write
    time with `date_source` left at `"stated"`: the right value under a claim nothing could
    distinguish from a chemist-entered date. They now map no date at all and this supplies both.

    So the rule belongs to the seam rather than to any adapter: an adapter *may* know better than
    the entry timestamp and its value always wins; when it does not, the entry's own time is a
    defensible ordering and nothing is a better one.

    **What the date then means, and why the note must not overclaim.** A record-creation time is
    when the entry was written, not necessarily when the run was performed — usually the same day,
    and sometimes three weeks of bench work transcribed in one afternoon. That is a weaker fact than
    a chemist-entered experiment date, so the record is stamped `date_source="entry"` and
    `ordering_caveat` says so above the table.

    An earlier draft of this asserted that `ordering_caveat` "already exists to describe" the
    weakening. It did not: it distinguished *missing* dates from present ones and knew nothing about
    where a present one came from. A filled-in date that says nothing about its own provenance turns
    `Progression.is_timeline()` true and makes the note claim "Runs in the order they were
    performed" over an afternoon of typing — the exact shape of defect this whole change is about,
    reintroduced by the fix for it. The stamp is what makes the sentence true rather than hoped for.

    It licenses no causality either: `memory.progression`'s rule that a date proves sequence and
    never response is untouched, and is if anything more load-bearing here.
    """

    def __init__(self, inner: ElnAdapter) -> None:
        """Wrap `inner`, whose mapping decisions are otherwise untouched."""
        self._inner = inner

    @property
    def inner(self) -> ElnAdapter:
        """The adapter this wraps.

        Public because the wrapper sits between the registry and every caller that asks what a
        source is: without it, "which adapter did this manifest build?" is unanswerable from outside
        and a test can only reach it through a private name. Read-only — nothing swaps an adapter
        out from under a built source.
        """
        return self._inner

    async def fetch_new_entries(
        self, since: datetime, limit: int | None = None, *, report_late_arrivals: bool = True
    ) -> list[RawEntry]:
        """Delegate unchanged — dating is purely a mapping concern, and so is bounding.

        The wrapper declares both optional parameters so the two probes answer `True` for a source
        whose adapter takes them; it forwards each only when the wrapped adapter actually does, for
        the reason those functions give. A wrapper that advertised a capability its inner adapter
        lacks would move the `TypeError` rather than prevent it.

        Only a `False` `report_late_arrivals` is forwarded, because `True` is the default every
        adapter already has and passing it would make the call fail for an adapter that predates
        the flag — the one thing the probe exists to prevent.
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
        # `model_copy` rather than a mutation: the mapped reaction is the adapter's answer, and a
        # wrapper that edits it in place makes "what did the adapter return" unanswerable in a
        # debugger and in a test. Validation is deliberately not re-run — the only field changed is
        # a date the model already accepts as optional, and re-validating would re-do the structural
        # checks `sync_entries` runs immediately afterwards anyway.
        return reaction.model_copy(
            update={"performed_at": raw.created_at.date(), "date_source": "entry"}
        )
