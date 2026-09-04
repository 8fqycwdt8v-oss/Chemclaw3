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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


def _reject_unstorable(value: str, field: str) -> str:
    r"""Refuse a string the corpus cannot hold — a NUL byte, or a lone surrogate.

    Both are ordinary ELN free text rather than adversarial input. A NUL reaches a record through
    any prose field an export carries (a procedure, a hypothesis, an impurity name, an unmapped
    attribute) and Postgres refuses one in a `text` or `jsonb` value outright; a lone surrogate
    reaches it from a JSON export with a truncated `\u` escape — `json.loads('"\ud800"')` returns
    one happily — and psycopg refuses that a step earlier, when it encodes the parameter.

    **Why the record refuses rather than repairs.** A transcription is what the source said, and
    `record._without_wikilinks` states the rule this follows: "deleting a chemist's characters to
    make them safe is the same mistake as trusting them". Refusing here also puts the failure where
    the sync can act on it — `ValidationError` is one of the two types `sync_entries` treats as
    per-entry bad data, so the entry becomes one rejection with a reason in the ledger and the rest
    of the batch ingests. Left to the write, it was a `psycopg.DataError` at the *last* of
    `ingest_reaction`'s five writes: fingerprint and label rows committed, no record, no ledger row,
    and an activity that failed the same way on every retry until the source stopped advancing.

    The mirror of this decision is `ingest/rejections.py::_storable`, which sanitises the same two
    values instead of refusing them, because a ledger row has nowhere left to refuse to.
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
    """Reject any unstorable string on `model`, recursing into the models nested under it.

    Field names are joined dotted (`conditions.major_impurity`) so the refusal reason — which is
    what the ingest ledger stores and a chemist eventually reads — names the field a fix has to
    touch rather than the record it sits in. `kg.note._walk_encodable` is the same shape over the
    same problem for notes; it is not shared, because that one asks a different question of each
    value and the two answers must be able to diverge (see `_reject_unstorable`).
    """
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, str):
            _reject_unstorable(value, f"{prefix}{name}")
        elif isinstance(value, BaseModel):
            _walk_storable(value, f"{prefix}{name}.")


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


# The columns an ingest writes, which is also everything a read selects.
_COLUMNS = "reaction_id, body, compound_smiles, project, performed_at, conditions, source"

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
    last_seen = now()
"""

# Every row that answers to the bare id, with the source that keys it — `_one_of` decides which is
# the citation's, and refuses when nothing here can.
_SELECT_ONE = f"SELECT ingest_source, {_COLUMNS} FROM reaction_records WHERE reaction_id = %s"

_SELECT_KNOWN = "SELECT reaction_id FROM reaction_records WHERE reaction_id = ANY(%s)"

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

    @field_validator("reaction_id")
    @classmethod
    def _slug_only(cls, value: str) -> str:
        """An entry id must stay a safe slug even though it is no longer a filename.

        It becomes the `reaction-<id>` citation that campaign and playbook notes carry into git, so
        external JSON reaches a committed note body through here. One rule, `kg.note`'s, because
        two spellings of "safe id" is how one of them drifts.
        """
        return require_note_slug(value)

    @model_validator(mode="after")
    def _text_is_storable(self) -> "ReactionRecord":
        """Refuse a record carrying text no column of this tier can hold (`_reject_unstorable`).

        Walked over the model rather than written at each field, the same argument
        `record._without_wikilinks` makes about applying its substitution once to the assembled
        body: the next field added to this record cannot forget it. The walk covers strings and
        nested models, which is every field this model has — `conditions` is the nested one, and it
        matters, because it is a `jsonb` column of its own that an impurity name reaches without
        passing through `body` at all.
        """
        _walk_storable(self, "")
        return self

    def is_current(self, as_of: date) -> bool:
        """Whether this is servable as *current* evidence on `as_of`.

        One way to fail: **not yet valid**. That is the `Note.is_current` lower bound, and it is
        reachable — `eln_sync_future_tolerance_seconds` deliberately admits an entry stamped
        slightly ahead of the wall clock rather than rejecting a real experiment over a clock skew.

        There is no upper bound and no tombstone. A *result* does not expire on its own, it is
        superseded, which is a claim a human makes in a note. A source **withdrawing** an entry is
        a different fact and would deserve its own bound — one was built here and removed, because
        nothing could set it and three of the four readers of this tier ignored it; see
        `D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry` for what a working one costs.
        """
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
        - **A not-yet-current record is dropped**, matching `Note.is_current`.
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
    """Build a `ReactionRecord` from a `_COLUMNS` row, validated through the model."""
    return ReactionRecord(
        reaction_id=row[0],
        body=row[1],
        compound_smiles=row[2],
        project=row[3],
        performed_at=row[4],
        conditions=ProcessConditions(**row[5]) if row[5] else None,
        source=row[6],
    )


def default_record_store() -> PostgresReactionRecordStore:
    """The production reaction-record store — the one every reader resolves through."""
    return PostgresReactionRecordStore()
