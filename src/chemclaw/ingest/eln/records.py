"""The ELN transcription tier: reaction records as queryable data.

A transcription is a deterministic mapping with no model in it, so it lands in Postgres as data
rather than as a knowledge note (D-2026-08-25-an-eln-transcription-is-data-not-a-claim). Claims
about these runs are playbooks or campaigns in `knowledge/`, citing records as `reaction-<id>`.

Upsert-by-id is the idempotency: `ON CONFLICT` settles "have I seen this?" in the write. A Protocol
with in-memory and Postgres implementations, like `science.fingerprints.store`. The eligibility
filter lives in the store because four readers resolve through it: `FingerprintReactionRetriever`,
`expand_note`, `ingest.eln.sync` and `kg.validate`.
"""

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chemclaw.core import db
from chemclaw.core.chem import canonical_smiles, standard_smiles
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.ord import RecordTier, RoleSpecies
from chemclaw.kg.note import ProcessConditions, note_id_for_reaction, require_note_slug
from chemclaw.science.fingerprints.store import CITATION_ONLY_NAMED_MAX, CitationOnlyRecords

logger = logging.getLogger(__name__)

# The one note type a reaction record answers to. A `type=` filter naming anything else can match
# nothing here, and that is decided without a query.
RECORD_TYPE = "reaction"


class UnreadableConditions(ChemclawError):
    """A `reaction_records.conditions` payload is not a JSON object, so no build can read it.

    Distinct from the version skew `_stored_conditions` tolerates: no build of this ingest writes a
    non-object.
    """


class AmbiguousReactionRecord(ChemclawError):
    """A bare reaction id that more than one ingest source has transcribed."""


def _reject_unstorable(value: str, field: str) -> str:
    r"""Refuse a string the corpus cannot hold — a NUL byte, or a lone surrogate.

    Both occur in ordinary ELN text (a NUL in prose, a lone surrogate from a truncated `\u` escape)
    and Postgres or psycopg refuses them. Refused rather than repaired, because a transcription is
    what the source said. Raising here makes it a per-entry `ValidationError` the sync files as one
    rejection; failing at the write would leave partial rows and an activity failing on every retry.
    `ingest/rejections.py::_storable` sanitises instead, because a ledger row cannot refuse.
    """
    if "\x00" in value:
        raise ValueError(
            f"{field} contains a NUL (0x00) byte at position {value.index(chr(0))}; a record is "
            "stored in Postgres text and jsonb columns, neither of which can hold one"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field} contains a character UTF-8 cannot encode (a lone surrogate at position "
            f"{exc.start}); a record is stored as UTF-8, so this value cannot be written"
        ) from exc
    return value


def _walk_storable(model: BaseModel, prefix: str) -> None:
    """Reject any unstorable string on `model`, recursing into the models and lists under it.

    Field names are joined dotted and indexed (`conditions.major_impurity`, `tags[1]`) so the
    refusal names the field to fix. Kept apart from `kg.note._walk_encodable`, which asks a
    different question of each value.
    """
    for name in type(model).model_fields:
        value = getattr(model, name)
        path = f"{prefix}{name}"
        if isinstance(value, str):
            _reject_unstorable(value, path)
        elif isinstance(value, BaseModel):
            _walk_storable(value, f"{path}.")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    _reject_unstorable(item, f"{path}[{index}]")
                elif isinstance(item, BaseModel):
                    _walk_storable(item, f"{path}[{index}].")


def _one_of(reaction_id: str, found: Sequence[tuple[str, "ReactionRecord"]]) -> "ReactionRecord":
    """The one record a `reaction-<id>` citation names, or a refusal saying why there is no one.

    A bare citation carries no source, so with two sources' transcriptions behind one id there is no
    right answer and picking one would read as fact. A row with an empty `ingest_source` predates
    migration `056` and is superseded by one that has a source, which is not an ambiguity.
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


# The columns an ingest writes, which is also everything a read selects.
_COLUMNS = (
    "reaction_id, body, compound_smiles, project, performed_at, conditions, source, retracted_at, "
    "tier, species"
)

_UPSERT = f"""
INSERT INTO reaction_records (ingest_source, {_COLUMNS})
VALUES (%(ingest_source)s, %(reaction_id)s, %(body)s, %(compound_smiles)s, %(project)s,
        %(performed_at)s, %(conditions)s, %(source)s, %(retracted_at)s, %(tier)s, %(species)s)
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
    -- A withdrawal is refreshed like everything else, and so is its *reversal*: a source that
    -- re-publishes a withdrawn entry sends it without a tombstone, and the row must go back to
    -- answering as current. Writing `COALESCE(reaction_records.retracted_at, EXCLUDED.…)` here
    -- would make a retraction permanent on a tier whose whole rule is that the row is what the
    -- source last said.
    retracted_at = EXCLUDED.retracted_at,
    -- The tier follows the body: an amendment that adds or removes a structure moves the row.
    tier = EXCLUDED.tier,
    species = EXCLUDED.species,
    last_seen = now()
"""

# Every row that answers to the bare id, with the source that keys it — `_one_of` decides which is
# the citation's, and refuses when nothing here can.
_SELECT_ONE = f"SELECT ingest_source, {_COLUMNS} FROM reaction_records WHERE reaction_id = %s"

# The qualified read: one source's row, which the primary key makes unique.
_SELECT_ONE_FOR_SOURCE = (
    f"SELECT ingest_source, {_COLUMNS} FROM reaction_records "
    "WHERE reaction_id = %s AND ingest_source = %s"
)

_SELECT_KNOWN = "SELECT reaction_id FROM reaction_records WHERE reaction_id = ANY(%s)"

# Which of a page of candidate ids the source has withdrawn; asked in this direction because `066`'s
# partial index answers it and the complement is far larger.
_SELECT_RETRACTED = (
    "SELECT ingest_source, reaction_id FROM reaction_records "
    "WHERE reaction_id = ANY(%s) AND retracted_at IS NOT NULL"
)

# Which of a page of candidate ids no structure search may serve: withdrawn, or citation-only
# (`110`). A citation-only row has a fingerprint only if amended from structured, and the app role
# cannot delete that stale fingerprint row.
_SELECT_WITHHELD = (
    "SELECT ingest_source, reaction_id FROM reaction_records "
    "WHERE reaction_id = ANY(%s) AND (retracted_at IS NOT NULL OR tier <> 'structured')"
)

_SELECT_BODIES = (
    "SELECT reaction_id, body FROM reaction_records "
    "WHERE ingest_source = %s AND reaction_id = ANY(%s)"
)

# The records no structure index holds, counted, and which list one of `patterns` as a drawn species
# (`ReactionRecordStore.citation_only`); `114`'s partial index matches this `WHERE`. `strpos` rather
# than `LIKE`, since `%` occurs in SMILES ring bonds. An empty `patterns` makes `hit` false without
# reading a body.
_SELECT_CITATION_ONLY = """
SELECT count(*),
       count(*) FILTER (WHERE hit),
       (array_agg(ingest_source ORDER BY ingest_source, reaction_id) FILTER (WHERE hit))[1:%(cap)s],
       (array_agg(reaction_id ORDER BY ingest_source, reaction_id) FILTER (WHERE hit))[1:%(cap)s]
FROM (
    SELECT ingest_source, reaction_id,
           EXISTS (SELECT 1 FROM unnest(%(patterns)s::text[]) AS p WHERE strpos(body, p) > 0) AS hit
    FROM reaction_records
    WHERE tier = 'citation-only' AND retracted_at IS NULL
) AS outside
"""


def drawn_species_patterns(query: str | None) -> list[str]:
    """The body text a citation-only record carries for a drawn species spelled like `query`.

    `record._species_line` renders each drawn species as ``- `<smiles>` (<role>)``, so the check is
    textual over the spellings tried: as given, RDKit-canonical and standardized. A reaction query
    is split into its molecules. An unparseable string is tried as given only. `None` or blank asks
    for no check.
    """
    if not query or not query.strip():
        return []
    spellings: list[str] = []
    for side in query.strip().split(">"):
        for piece in side.split("."):
            piece = piece.strip()
            if not piece:
                continue
            for spelling in (piece, canonical_smiles(piece), standard_smiles(piece)):
                if spelling not in spellings:
                    spellings.append(spelling)
    return [f"- `{spelling}` (" for spelling in spellings]


def _citation_only(
    outside: int, found: Sequence[tuple[str, str]], total: int, checked: bool
) -> CitationOnlyRecords:
    """The disclosure from the counts and the first `found` `(source, id)` pairs, ids cited."""
    return CitationOnlyRecords(
        outside_index=outside,
        query_checked=checked,
        giving_query=total,
        examples=[
            note_id_for_reaction(reaction_id, source)
            for source, reaction_id in found[:CITATION_ONLY_NAMED_MAX]
        ],
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
    # The numbers a chemist compares, kept as numbers (`kg.note.ProcessConditions`). `None` means
    # none were recorded, which differs from an empty block.
    conditions: ProcessConditions | None = None
    source: str = Field(min_length=1)
    # When the source reported this entry withdrawn; `None` is "not retracted". Set from
    # `RawEntry.retracted_at`, never inferred from absence, since a fetch is a delta
    # (D-2026-09-13-a-withdrawal-is-a-fact-a-source-reports).
    retracted_at: datetime | None = None
    # `CITATION_ONLY` when the source named a species without its structure: citable, never served
    # by structure search. Defaults to `STRUCTURED`, as the migration says of existing rows.
    tier: RecordTier = RecordTier.STRUCTURED
    # Each compared role's canonical structures (`ingest.eln.ord.RoleSpecies`), so the turn-time
    # comparison can diff species without parsing `body`. `None` means no projection stored (an
    # older row, or citation-only) and is skipped by every comparison, never read as four empty
    # roles.
    species: RoleSpecies | None = None

    @field_validator("reaction_id")
    @classmethod
    def _slug_only(cls, value: str) -> str:
        """An entry id must stay a safe slug even though it is no longer a filename.

        It becomes the `reaction-<id>` citation committed into note bodies; one rule, `kg.note`'s.
        """
        return require_note_slug(value)

    @model_validator(mode="after")
    def _text_is_storable(self) -> "ReactionRecord":
        """Refuse a record carrying text no column of this tier can hold (`_reject_unstorable`).

        Walked over the whole model so a new field cannot forget it; covers nested models and lists,
        including `conditions`, its own `jsonb` column.
        """
        _walk_storable(self, "")
        return self

    def is_current(self, as_of: date) -> bool:
        """Whether this is servable as *current* evidence on `as_of`.

        False when not yet valid (`eln_sync_future_tolerance_seconds` admits slightly future-stamped
        entries) or when the source withdrew it. A result does not expire; it is superseded by a
        human claim. `read()` still serves a retracted row so a citation to it resolves and says so.
        """
        if self.retracted_at is not None:
            return False
        return self.performed_at is None or as_of >= self.performed_at

    def passes(self, filters: dict[str, Any], as_of: date) -> bool:
        """Whether this record satisfies `filters` and is current — the eligibility rule itself.

        The single definition both backends answer with. A filter must never widen what was asked:

        - A record with no `performed_at` fails a windowed query, since it cannot be shown to fall
          inside.
        - A not-yet-current record is dropped, matching `Note.is_current`.
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

        `source` is the registry source name and half of the row's identity, since two ELNs may use
        one entry id. Not a field on `ReactionRecord`, which is rendered from one entry that does
        not know its registry name.
        """
        ...

    async def read(self, reaction_id: str, source: str = "") -> ReactionRecord | None:
        """One record by its bare ELN id, or `None` when the corpus does not hold it.

        Never the `reaction-` note id, which is a citation spelling. With `source`, the primary key
        answers exactly; without it, the read spans sources and raises `AmbiguousReactionRecord`
        when two have the id (see `_one_of`).
        """
        ...

    async def bodies(self, reaction_ids: Sequence[str], source: str) -> dict[str, str]:
        """The stored body of each of `reaction_ids` the corpus holds — the unchanged check.

        Keyed on the bounded page the caller is about to write, never the corpus. The body rather
        than the id, because ELNs amend entries in place. Scoped to `source`, since another source's
        row of the same id says nothing about this entry.
        """
        ...

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current (`ReactionRecord.passes`)."""
        ...

    async def retracted(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` — `(ingest_source, reaction_id)` — the source has reported withdrawn.

        Separate from `eligible`, which drops hits with no stored record; an unfiltered sweep must
        keep those, so it asks only for the positive set of withdrawals. Per source, since
        `reaction_fingerprints` keys by source and one site's withdrawal must not drop another's
        run. An empty source matches any.
        """
        ...

    async def structurally_withheld(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` no structure search may serve: withdrawn, or citation-only.

        `retracted` plus one clause, covering an entry amended from structured to citation-only
        whose old fingerprint row cannot be deleted. `retracted` stays separate because the sync
        asks only about withdrawal. An empty source matches any.
        """
        ...

    async def citation_only(self, query: str | None = None) -> CitationOnlyRecords:
        """The records no structure index holds, and which of them list `query` as drawn.

        Lets every structural tool state its denominator
        (D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable). Withdrawn records
        are not counted. `query` is matched as text (`drawn_species_patterns`), never through an
        index; `None` asks for the count alone.
        """
        ...

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` the corpus holds at all — the citation-existence check.

        Regardless of currency or filter: `kg.validate` asks whether a link resolves, not whether
        the record is current.
        """
        ...


class InMemoryReactionRecordStore:
    """Process-local `ReactionRecordStore` — the reference the SQL one is written to match.

    A differential test oracle, not a deployment backend: no configuration returns it
    (D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend). Keyed by `(source,
    reaction_id)`, the durable store's primary key.
    """

    def __init__(self) -> None:
        """Start with an empty corpus."""
        self._records: dict[tuple[str, str], ReactionRecord] = {}

    async def record(self, records: Sequence[ReactionRecord], source: str) -> int:
        """Insert or replace each of `source`'s records by reaction id; return how many."""
        for item in records:
            self._records[(source, item.reaction_id)] = item
        return len(records)

    async def read(self, reaction_id: str, source: str = "") -> ReactionRecord | None:
        """One record by its bare ELN id, or `None`; refuses an unqualified id two sources hold."""
        found = [
            (stored_source, record)
            for (stored_source, stored_id), record in sorted(self._records.items())
            if stored_id == reaction_id and (not source or stored_source == source)
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

    async def retracted(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` this store holds a withdrawal for; an empty source matches any."""
        withdrawn = {
            key for key, record in self._records.items() if record.retracted_at is not None
        }
        return _pair_off(refs, withdrawn)

    async def structurally_withheld(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` are withdrawn or citation-only; an empty source matches any."""
        withheld = {
            key
            for key, record in self._records.items()
            if record.retracted_at is not None or record.tier is not RecordTier.STRUCTURED
        }
        return _pair_off(refs, withheld)

    async def citation_only(self, query: str | None = None) -> CitationOnlyRecords:
        """The non-withdrawn citation-only records, and which list a spelling of `query`."""
        patterns = drawn_species_patterns(query)
        outside = sorted(
            key
            for key, record in self._records.items()
            if record.tier is RecordTier.CITATION_ONLY and record.retracted_at is None
        )
        found = [
            key
            for key in outside
            if any(pattern in self._records[key].body for pattern in patterns)
        ]
        return _citation_only(len(outside), found, len(found), bool(patterns))

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

        One round trip for the whole batch.
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
                            "retracted_at": item.retracted_at,
                            "tier": item.tier.value,
                            "species": Jsonb(item.species.model_dump())
                            if item.species is not None
                            else None,
                        }
                        for item in records
                    ],
                )
            await conn.commit()
        return len(records)

    async def read(self, reaction_id: str, source: str = "") -> ReactionRecord | None:
        """One record by its bare ELN id, or `None` when the corpus does not hold it.

        Every row for the id comes back so `_one_of` can refuse an ambiguous bare citation; a
        qualified one uses a narrowed statement on the primary key.
        """
        statement, params = (
            (_SELECT_ONE_FOR_SOURCE, (reaction_id, source))
            if source
            else (_SELECT_ONE, (reaction_id,))
        )
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(statement, params)
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

        `ReactionRecord.passes` against the columns; the page's ids go down as a parameter rather
        than fetching bodies to filter in Python.
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
            # `ReactionRecord.is_current`'s second bound, expressed against the column.
            "retracted_at IS NULL",
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

    async def retracted(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` the source has reported withdrawn; an empty source matches any.

        One statement over the ids (`066`'s partial index), paired with sources in Python, since
        withdrawals are rare.
        """
        if not refs:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_RETRACTED, ([reaction_id for _, reaction_id in refs],))
                rows = await cur.fetchall()
        return _pair_off(refs, {(row[0], row[1]) for row in rows})

    async def structurally_withheld(self, refs: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of `refs` are withdrawn or citation-only; an empty source matches any.

        One statement over the page, paired off in Python as in `retracted`; both conditions are
        rare.
        """
        if not refs:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_WITHHELD, ([reaction_id for _, reaction_id in refs],))
                rows = await cur.fetchall()
        return _pair_off(refs, {(row[0], row[1]) for row in rows})

    async def citation_only(self, query: str | None = None) -> CitationOnlyRecords:
        """One statement over `114`'s partial index: the count, the matches, the first few ids."""
        patterns = drawn_species_patterns(query)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _SELECT_CITATION_ONLY, {"patterns": patterns, "cap": CITATION_ONLY_NAMED_MAX}
                )
                row = await cur.fetchone()
        # An aggregate without GROUP BY returns exactly one row; the fallback is for the type.
        outside, total, sources, ids = row if row is not None else (0, 0, None, None)
        found = list(zip(sources or [], ids or [], strict=True))
        return _citation_only(outside, found, total, bool(patterns))

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` the corpus holds at all."""
        if not reaction_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_KNOWN, (list(reaction_ids),))
                rows = await cur.fetchall()
        return {row[0] for row in rows}


def _stored_conditions(reaction_id: str, stored: Any) -> ProcessConditions | None:
    """One row's `conditions` payload as a model, ignoring fields this build does not know.

    The read tolerates what the write forbids because a rolling upgrade is not atomic: a newer build
    may have written a field this one does not know, and failing would surface on a chemist's
    structure query. Unknown keys are dropped (the row keeps them); a known field with an unreadable
    value still raises. `{}` (recorded, all unknown) is kept distinct from NULL.

    Args:
        reaction_id: The record the payload belongs to, for the log line.
        stored: The `conditions` column as psycopg returns it — a parsed object, or SQL `NULL`.

    Returns:
        The conditions, or `None` when the column is NULL.
    """
    if stored is None:
        return None
    if not isinstance(stored, Mapping):
        # A bare `jsonb` column can hold an array or scalar; refuse it with a message naming the
        # row.
        raise UnreadableConditions(
            f"reaction_records row {reaction_id!r} holds {type(stored).__name__} in `conditions` "
            "where a JSON object is required; the row was written by something other than this "
            "ingest."
        )
    known = {key: value for key, value in stored.items() if key in ProcessConditions.model_fields}
    if len(known) != len(stored):
        logger.info(
            "reaction %s carries condition field(s) this build does not know (%s); ignoring them",
            reaction_id,
            ", ".join(sorted(set(stored) - set(known))),
        )
    return ProcessConditions(**known)


def _pair_off(refs: Sequence[tuple[str, str]], found: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The `refs` that `found` holds, where a ref with an empty source matches any source.

    One rule so the in-memory oracle and the SQL store agree on what a bare citation matches.
    """
    return {
        (source, reaction_id)
        for source, reaction_id in refs
        if (source, reaction_id) in found
        or (not source and any(stored == reaction_id for _, stored in found))
    }


def _record(row: tuple[Any, ...]) -> ReactionRecord:
    """Build a `ReactionRecord` from a `_COLUMNS` row, validated through the model."""
    return ReactionRecord(
        reaction_id=row[0],
        body=row[1],
        compound_smiles=row[2],
        project=row[3],
        performed_at=row[4],
        conditions=_stored_conditions(row[0], row[5]),
        source=row[6],
        retracted_at=row[7],
        tier=RecordTier(row[8]),
        # Validated, with unknown keys dropped, for the rolling-upgrade reason `_stored_conditions`
        # gives.
        species=RoleSpecies.model_validate(row[9]) if row[9] is not None else None,
    )


def default_record_store() -> PostgresReactionRecordStore:
    """The production reaction-record store — the one every reader resolves through."""
    return PostgresReactionRecordStore()
