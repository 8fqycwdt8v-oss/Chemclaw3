"""The ELN transcription tier: reaction records as queryable data (D-2026-08-25).

An ELN entry used to become a `created_by: agent` markdown note that a human merged through the
PR-gate. D-005's gate exists to put a human in front of *machine-generated knowledge*, and a
transcription is not that — `record_from_ord_reaction` is a pure deterministic mapping with no
model in it, so the reviewer was approving a rendering of data a chemist had already signed off on
upstream. Measured, that cost 202 ms of serialized git per entry and a corpus scan that outgrows
`eln_sync_timeout_seconds` at ~700k notes, and it bought nothing anyone could decide.

So a record lands here instead, in Postgres, exactly as migration `025` argues for observations:
with no review, Git buys a branch per entry and returns nothing. What a human *asserts* about these
runs is still a playbook or a campaign in `knowledge/`, gated as it always was, citing these
records by the same `reaction-<id>` name it always used.

**Upsert-by-id is the idempotency**, which is why nothing here asks "have I seen this?" as a
separate question of the corpus. The sync loop used to answer it by parsing every merged note on
disk — 425 µs and 2.9 kB of resident memory per note, linear — for something `ON CONFLICT` settles
in the write.

Shaped as `science.fingerprints.store` is, and for the same reason: a Protocol with an in-memory
and a Postgres implementation, so the ingest path is injectable and its tests need no database,
while the store that actually serves queries is exercised against a real one.

Four readers resolve through here, which is why the eligibility filter lives in the store rather
than in any of them: `retrieval.retrievers.FingerprintReactionRetriever` narrows a page of
structural hits, `agent.graph_tools.expand_note` serves the recipe behind one hit,
`ingest.eln.sync` skips an unchanged replay, and `kg.validate` checks that a cited record exists.
"""

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg.note import ProcessConditions, require_note_slug

logger = logging.getLogger(__name__)

# The one note type a reaction record answers to. A `type=` filter naming anything else can match
# nothing here, and that is decided without a query.
RECORD_TYPE = "reaction"


class AmbiguousReactionRecord(ChemclawError):
    """A bare reaction id that more than one ingest source has transcribed."""


def _one_of(reaction_id: str, found: Sequence[tuple[str, "ReactionRecord"]]) -> "ReactionRecord":
    """The one record a `reaction-<id>` citation names, or a refusal saying why there is no one.

    A citation carries no source (`kg.note.note_id_for_reaction` spells the bare id), so with two
    sites' transcriptions behind one id there is genuinely no right answer — and returning either
    is a coin flip that reads as a fact. That is what the bare-id primary key used to do silently,
    except worse: the later sync had already destroyed the other site's row.

    **A row with no `ingest_source` predates the key change and is superseded by one that has it.**
    Migration `056` defaults the column to `''` on rows already stored, and the first sync after the
    upgrade re-writes each of them under its real source — so during that window one id can hold a
    legacy row and its own replacement, which is not an ambiguity and must not read as one.
    """
    stated = [pair for pair in found if pair[0]]
    candidates = stated or list(found)
    if len(candidates) > 1:
        raise AmbiguousReactionRecord(
            f"reaction id {reaction_id!r} is transcribed by more than one ingest source "
            f"({', '.join(sorted(source for source, _ in candidates))}), so the citation "
            "`reaction-<id>` does not name one run. Narrow CHEMCLAW_DATA_SOURCES, or have one of "
            "the sources export a distinct entry id"
        )
    return candidates[0][1]


# The columns an ingest writes. `retracted_at` is deliberately **not** among them: see `_UPSERT`.
_COLUMNS = "reaction_id, body, compound_smiles, project, performed_at, conditions, source"
# What a read selects — the written set plus the tombstone only `retract` sets.
_READ_COLUMNS = f"{_COLUMNS}, retracted_at"

_UPSERT = f"""
INSERT INTO reaction_records (ingest_source, {_COLUMNS})
VALUES (%(ingest_source)s, %(reaction_id)s, %(body)s, %(compound_smiles)s, %(project)s,
        %(performed_at)s, %(conditions)s, %(source)s)
ON CONFLICT (ingest_source, reaction_id) DO UPDATE SET
    -- Every field is refreshed, because an ELN amends an entry *in place*: a yield corrected after
    -- assay, an impurity added, a retraction. The old note path compared bodies to notice that and
    -- needed a full corpus parse to do it; here the newer rendering simply wins.
    body = EXCLUDED.body,
    compound_smiles = EXCLUDED.compound_smiles,
    project = EXCLUDED.project,
    performed_at = EXCLUDED.performed_at,
    conditions = EXCLUDED.conditions,
    source = EXCLUDED.source,
    -- **`retracted_at` is absent from both halves of this statement, and that is the design.**
    -- A source that soft-deletes keeps exporting a withdrawn entry, so the overlap window
    -- re-fetches it every run; refreshing the tombstone from an ingest would clear it on the
    -- first replay and the run would answer as current again. Nothing here can honestly clear
    -- one either: a delta fetch cannot distinguish "reinstated" from "still exported, still
    -- withdrawn". A retraction is set by `retract` and by nothing else.
    last_seen = now()
"""

# Every row that answers to the bare id, with the source that keys it — `_one_of` decides which is
# the citation's, and refuses when nothing here can.
_SELECT_ONE = f"SELECT ingest_source, {_READ_COLUMNS} FROM reaction_records WHERE reaction_id = %s"

_SELECT_KNOWN = "SELECT reaction_id FROM reaction_records WHERE reaction_id = ANY(%s)"

# One statement for a whole reported batch, carrying each entry's own retraction time — a report
# names several withdrawals and they did not happen at one instant. `retracted_at IS NULL` makes it
# idempotent and makes the *earliest* report win: the sweep re-reads its window every run (a
# retraction never advances the sync cursor), so the same withdrawal arrives many times and must
# not creep forward. `rowcount` is therefore the number of rows this sweep actually retired, which
# is the number it reports — not the number it was told about.
_RETRACT = """
UPDATE reaction_records AS r SET retracted_at = v.at
FROM (
    SELECT unnest(%(ids)s::text[]) AS id, unnest(%(ats)s::timestamptz[]) AS at
) AS v
WHERE r.ingest_source = %(source)s AND r.reaction_id = v.id AND r.retracted_at IS NULL
"""

_SELECT_BODIES = (
    "SELECT reaction_id, body FROM reaction_records "
    "WHERE ingest_source = %s AND reaction_id = ANY(%s)"
)


class ReactionRecord(BaseModel):
    """One transcribed ELN entry: the rendered body plus what narrows a search to it.

    Frozen, because a record is what the source said. Amending one means re-rendering it from the
    amended entry, never editing the rendering — which is also what keeps the upsert idempotent.
    """

    model_config = ConfigDict(frozen=True)

    reaction_id: str = Field(min_length=1)
    body: str
    compound_smiles: str | None = None
    project: str | None = None
    performed_at: date | None = None
    # The numbers a chemist compares, kept as numbers beside the prose that renders them
    # (`kg.note.ProcessConditions`). `None` means the entry recorded none of them, which is not the
    # same claim as an empty block.
    conditions: ProcessConditions | None = None
    source: str = Field(min_length=1)
    # When the *source* reported this entry withdrawn; `None` is not retracted.
    #
    # **Not `valid_to`, and the distinction is the whole decision**
    # (D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry). `is_current` below says why a
    # record has no validity window: a result does not expire on its own, it is *superseded*, which
    # is a claim a human makes in a note. A withdrawal is neither — it is the originating system
    # saying the entry should not have been published, a fact as deterministic as the entry itself
    # and gated no more than it is.
    #
    # Set only from a retraction a source **reports**, never from an entry's absence from an
    # export: `ingest.eln.sync` is a delta, so "not seen this run" is the permanent normal state of
    # every entry ever ingested, and sweeping on it would retire the corpus on the first run.
    retracted_at: datetime | None = None

    @field_validator("reaction_id")
    @classmethod
    def _slug_only(cls, value: str) -> str:
        """An entry id must stay a safe slug even though it is no longer a filename.

        It becomes the `reaction-<id>` citation that campaign and playbook notes carry into git, so
        external JSON reaches a committed note body through here. One rule, `kg.note`'s, because
        two spellings of "safe id" is how one of them drifts.
        """
        return require_note_slug(value)

    def is_current(self, as_of: date) -> bool:
        """Whether this is servable as *current* evidence on `as_of`.

        Two ways to fail, and they are different facts about the run.

        **Not yet valid.** The `Note.is_current` lower bound, and it is reachable:
        `eln_sync_future_tolerance_seconds` deliberately admits an entry stamped slightly ahead of
        the wall clock rather than rejecting a real experiment over a clock skew.

        **Retracted.** A run still has no `valid_to` — a result does not expire on its own, it is
        superseded, which is a separate claim a human makes in a note — but a source *withdrawing*
        an entry is not that, and this is the bound that carries it. Compared by date because the
        caller's question is asked as one; a retraction is the moment the record stopped being
        current, so the day it was withdrawn is already not current (`valid_to`'s bound is
        inclusive because it names the last day something held, which is the opposite convention
        for the opposite fact).

        A `retracted_at` in the future reads as "not yet retracted" and needs no guard: unlike a
        cursor, nothing here is poisoned by it, and it corrects itself when that date arrives.

        The row is never deleted for either reason — `read` keeps serving it, which is what makes a
        withdrawn run still answerable as of an earlier date.
        """
        if self.retracted_at is not None and as_of >= self.retracted_at.date():
            return False
        return self.performed_at is None or as_of >= self.performed_at

    def passes(self, filters: dict[str, Any], as_of: date) -> bool:
        """Whether this record satisfies `filters` and is current — the eligibility rule itself.

        The one definition both backends answer with, so the in-memory store the ingest tests use
        cannot drift from the SQL the deployment runs. Two rules are inherited from
        `retrieval.retrievers._eligible_notes` deliberately, both because a filter must never widen
        what it was asked:

        - **A record with no `performed_at` fails a windowed query** rather than passing it. It
          cannot be *shown* to fall in the window, and a caller asking what happened in a period is
          not asking for runs of unknown date.
        - **A not-yet-current record is dropped**, matching `Note.is_current` — which since
          D-2026-08-27 also drops a *retracted* one, so a withdrawn run leaves every
          current-evidence sweep the moment its source reports it, with no reader learning a
          new rule.
        """
        if (want_type := filters.get("type")) is not None and want_type != RECORD_TYPE:
            return False
        if (want_tag := filters.get("tag")) is not None and want_tag != self.project:
            return False
        if not self.is_current(as_of):
            return False
        since, until = filters.get("since"), filters.get("until")
        if since is None and until is None:
            return True
        if self.performed_at is None:
            return False
        if since is not None and self.performed_at < since:
            return False
        return not (until is not None and self.performed_at > until)


@runtime_checkable
class ReactionRecordStore(Protocol):
    """Persistence + lookup contract for transcribed ELN entries. Backends implement this."""

    async def record(self, records: Sequence[ReactionRecord], source: str) -> int:
        """Insert or replace `source`'s records by reaction id; return how many were written.

        **`source` is the registry source name and it is half of the row's identity**, not a label
        on it. Two ELNs may legitimately use one entry id — `ingest_reaction` said so and answered
        it with a `source` *column* beside a bare-id key, which only ever recorded which one won:
        the upsert refreshed every field, so the later sync replaced the earlier site's
        transcription and every citation to it then resolved to a different run at a different
        site, with `kg-validate` still passing. The label index put the pair in its key from the
        start; this is the same rule here.

        Not a field on `ReactionRecord`, because that model is what `record_from_ord_reaction`
        renders out of one entry and the registry name is not in the entry. The store is where the
        two meet.
        """
        ...

    async def read(self, reaction_id: str) -> ReactionRecord | None:
        """One record by its bare ELN id, or `None` when the corpus does not hold it.

        Never the `reaction-` note id: that prefix is a citation spelling
        (`kg.note.note_id_for_reaction`), and accepting both is how a store ends up holding two
        names for one row.

        Raises `AmbiguousReactionRecord` when two ingest sources have transcribed the id — see
        `_one_of` for why that is a refusal rather than a pick.
        """
        ...

    async def bodies(self, reaction_ids: Sequence[str], source: str) -> dict[str, str]:
        """The stored body of each of `reaction_ids` the corpus holds — the unchanged check.

        **Keyed on the ids the caller is about to write, never on the corpus.** The sync's overlap
        window deliberately re-fetches entries it has seen, and answering "is this one unchanged?"
        used to mean parsing the whole corpus, which is what made the sync outgrow its own activity
        timeout. The question is about a bounded page of candidates, so the lookup is too.

        The body, not merely the id: an ELN amends an entry *in place* — a yield corrected after
        assay, an impurity added, a retraction — while keeping its `created_at`, so treating "seen
        before" as "unchanged" drops every correction silently.

        Scoped to `source` for the reason `record` is: comparing a page of one ELN's entries
        against another ELN's rows of the same ids answers a question nobody asked, and it answers
        it wrong in both directions — a false "unchanged" skips a real entry, and a false "changed"
        re-ingests one forever.
        """
        ...

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current (`ReactionRecord.passes`)."""
        ...

    async def retract(self, retractions: Mapping[str, datetime], source: str) -> int:
        """Mark each of `source`'s entries withdrawn at the given moment; return how many changed.

        **The count is rows this call actually retired**, not entries it was told about: a row
        already retracted is left alone, so the earliest report wins and a re-report is a no-op.
        That matters because the sweep re-reads its window every run — a retraction never advances
        the sync cursor — so the same withdrawal arrives many times and the number an operator sees
        must still mean "this many runs left current evidence today".

        An id the corpus does not hold is silently no rows: a source may withdraw an entry this
        deployment never ingested (it predates the cursor, or it was rejected as bad data), and
        that is not an error to report.

        Keyed per entry rather than one timestamp for the batch because a report names several
        withdrawals that did not happen at one instant, and `retracted_at` is a fact about each.

        Scoped to `source` for the reason `record` and `bodies` are: two ELNs may use one entry id,
        and one site withdrawing its entry says nothing about the other site's run.

        **Nothing reverses this.** A reinstatement would have to be reported as its own fact, and
        no delta fetch can imply one — an entry reappearing in an export is indistinguishable from
        an entry that never left it. Of the two possible mistakes only re-ingesting is recoverable.
        """
        ...

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` the corpus holds at all — the citation-existence check.

        Existence regardless of currency or filter, because a citation to a run performed tomorrow
        is a real citation to a real record; `kg.validate` is asking whether the link resolves, not
        whether the record is servable as current evidence.
        """
        ...


class InMemoryReactionRecordStore:
    """Process-local `ReactionRecordStore` for tests and single-run use.

    Keyed by `(source, reaction_id)`, so re-recording one source's id replaces it and two sources
    sharing an id keep both rows — the same identity the durable store's primary key gives, which
    is what lets an ingest test assert replay behaviour without a database.
    """

    def __init__(self) -> None:
        """Start with an empty corpus."""
        self._records: dict[tuple[str, str], ReactionRecord] = {}

    async def record(self, records: Sequence[ReactionRecord], source: str) -> int:
        """Insert or replace each of `source`'s records by reaction id; return how many."""
        for item in records:
            self._records[(source, item.reaction_id)] = item
        return len(records)

    async def read(self, reaction_id: str) -> ReactionRecord | None:
        """One record by its bare ELN id, or `None`; refuses an id two sources both hold."""
        found = [
            (source, record)
            for (source, stored_id), record in sorted(self._records.items())
            if stored_id == reaction_id
        ]
        return _one_of(reaction_id, found) if found else None

    async def bodies(self, reaction_ids: Sequence[str], source: str) -> dict[str, str]:
        """The stored body of each of `source`'s `reaction_ids` this store holds."""
        return {
            reaction_id: self._records[(source, reaction_id)].body
            for reaction_id in reaction_ids
            if (source, reaction_id) in self._records
        }

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current."""
        today = date.today()
        return {
            reaction_id
            for reaction_id in reaction_ids
            if any(
                record.passes(filters, today)
                for (_, stored_id), record in self._records.items()
                if stored_id == reaction_id
            )
        }

    async def retract(self, retractions: Mapping[str, datetime], source: str) -> int:
        """Mark `source`'s entries withdrawn; return how many rows this call changed."""
        retired = 0
        for reaction_id, at in retractions.items():
            record = self._records.get((source, reaction_id))
            if record is None or record.retracted_at is not None:
                continue
            self._records[(source, reaction_id)] = record.model_copy(update={"retracted_at": at})
            retired += 1
        return retired

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` this store holds at all, under any source."""
        held = {stored_id for _, stored_id in self._records}
        return {reaction_id for reaction_id in reaction_ids if reaction_id in held}

    async def all_records(self) -> list[ReactionRecord]:
        """Everything stored, in `(source, id)` order — a test affordance, not the Protocol."""
        return [self._records[key] for key in sorted(self._records)]


class PostgresReactionRecordStore:
    """The durable `ReactionRecordStore` — `reaction_records`, one row per ELN entry."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout."""
        async with db.connection(settings.postgres_dsn) as conn:
            yield conn

    async def record(self, records: Sequence[ReactionRecord], source: str) -> int:
        """Upsert `source`'s transcribed reactions; return how many were written.

        One round trip for the batch rather than one per record: the sync loop hands over a whole
        chunk, and the per-entry cost is the thing this tier exists to remove.
        """
        if not records:
            return 0
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    _UPSERT,
                    [
                        {
                            "ingest_source": source,
                            "reaction_id": item.reaction_id,
                            "body": item.body,
                            "compound_smiles": item.compound_smiles,
                            "project": item.project,
                            "performed_at": item.performed_at,
                            "conditions": Jsonb(item.conditions.model_dump(exclude_none=True))
                            if item.conditions
                            else None,
                            "source": item.source,
                        }
                        for item in records
                    ],
                )
            await conn.commit()
        return len(records)

    async def read(self, reaction_id: str) -> ReactionRecord | None:
        """One record by its bare ELN id, or `None` when the corpus does not hold it.

        Every row answering to the id comes back, not the first one the plan happened to return:
        `_one_of` is what decides between them, and it refuses rather than picking when two ingest
        sources have both transcribed the id.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_ONE, (reaction_id,))
                rows = await cur.fetchall()
        if not rows:
            return None
        # Sorted by the source alone: it is unique per id (the primary key says so), and sorting
        # whole rows would compare a `date` against a `None` on any tie that cannot happen.
        ordered = sorted(rows, key=lambda row: str(row[0]))
        return _one_of(reaction_id, [(row[0], _record(row[1:])) for row in ordered])

    async def bodies(self, reaction_ids: Sequence[str], source: str) -> dict[str, str]:
        """The stored body of each of `source`'s `reaction_ids` the corpus holds."""
        if not reaction_ids:
            return {}
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_BODIES, (source, list(reaction_ids)))
                rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current, narrowed in SQL.

        `ReactionRecord.passes` expressed against the columns. The candidate set is a page of
        structural hits, so the ids go down as a parameter and the narrowing comes back — the
        alternative, fetching the page's bodies to filter them in Python, moves the corpus's
        largest column across the wire to answer a question about its smallest ones.
        """
        if not reaction_ids:
            return set()
        want_type = filters.get("type")
        if want_type is not None and want_type != RECORD_TYPE:
            # Nothing in this table can be any other type, so the query is skipped rather than run.
            return set()
        clauses = [
            "reaction_id = ANY(%(ids)s)",
            "(performed_at IS NULL OR performed_at <= %(today)s)",
            # `ReactionRecord.is_current`'s retraction bound. The row stays — `read` still serves
            # it — but a withdrawn run is not current evidence, so it leaves every retrieval sweep
            # that resolves through here.
            "(retracted_at IS NULL OR retracted_at > %(today)s)",
        ]
        params: dict[str, Any] = {"ids": list(reaction_ids), "today": date.today()}
        if (want_tag := filters.get("tag")) is not None:
            clauses.append("project = %(tag)s")
            params["tag"] = want_tag
        if (since := filters.get("since")) is not None:
            clauses.append("performed_at >= %(since)s")
            params["since"] = since
        if (until := filters.get("until")) is not None:
            clauses.append("performed_at <= %(until)s")
            params["until"] = until
        statement = f"SELECT reaction_id FROM reaction_records WHERE {' AND '.join(clauses)}"
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(statement, params)
                rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def retract(self, retractions: Mapping[str, datetime], source: str) -> int:
        """Mark `source`'s entries withdrawn in one statement; return how many rows changed.

        One `UPDATE ... FROM unnest(...)` rather than a statement per id, for `record`'s reason —
        the sweep hands over a whole reported batch — and `rowcount` is then the answer directly,
        which an `executemany` could not give.
        """
        if not retractions:
            return 0
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _RETRACT,
                    {
                        "ids": list(retractions),
                        "ats": list(retractions.values()),
                        "source": source,
                    },
                )
                retired = cur.rowcount
            await conn.commit()
        return retired

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` the corpus holds at all."""
        if not reaction_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_KNOWN, (list(reaction_ids),))
                rows = await cur.fetchall()
        return {row[0] for row in rows}


def _record(row: tuple[Any, ...]) -> ReactionRecord:
    """Build a `ReactionRecord` from a `_READ_COLUMNS` row, validated through the model."""
    return ReactionRecord(
        reaction_id=row[0],
        body=row[1],
        compound_smiles=row[2],
        project=row[3],
        performed_at=row[4],
        conditions=ProcessConditions(**row[5]) if row[5] else None,
        source=row[6],
        retracted_at=row[7],
    )


def default_record_store() -> PostgresReactionRecordStore:
    """The production reaction-record store — the one every reader resolves through."""
    return PostgresReactionRecordStore()
