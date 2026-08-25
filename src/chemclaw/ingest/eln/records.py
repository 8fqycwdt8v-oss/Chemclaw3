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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.kg.note import ProcessConditions, require_note_slug

logger = logging.getLogger(__name__)

# The one note type a reaction record answers to. A `type=` filter naming anything else can match
# nothing here, and that is decided without a query.
RECORD_TYPE = "reaction"

_COLUMNS = "reaction_id, body, compound_smiles, project, performed_at, conditions, source"

_UPSERT = f"""
INSERT INTO reaction_records ({_COLUMNS})
VALUES (%(reaction_id)s, %(body)s, %(compound_smiles)s, %(project)s, %(performed_at)s,
        %(conditions)s, %(source)s)
ON CONFLICT (reaction_id) DO UPDATE SET
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

_SELECT_ONE = f"SELECT {_COLUMNS} FROM reaction_records WHERE reaction_id = %s"

_SELECT_KNOWN = "SELECT reaction_id FROM reaction_records WHERE reaction_id = ANY(%s)"

_SELECT_BODIES = "SELECT reaction_id, body FROM reaction_records WHERE reaction_id = ANY(%s)"


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

    def is_current(self, as_of: date) -> bool:
        """Whether this is servable as *current* evidence on `as_of`.

        The `Note.is_current` rule with the half a record cannot have removed: a run has no
        `valid_to` — a result does not expire on its own, it is superseded, which is a separate
        claim a human makes in a note. So only the not-yet-valid case remains, and it is reachable:
        `eln_sync_future_tolerance_seconds` deliberately admits an entry stamped slightly ahead of
        the wall clock rather than rejecting a real experiment over a clock skew.
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

    async def record(self, records: Sequence[ReactionRecord]) -> int:
        """Insert or replace records by reaction id; return how many were written."""
        ...

    async def read(self, reaction_id: str) -> ReactionRecord | None:
        """One record by its bare ELN id, or `None` when the corpus does not hold it.

        Never the `reaction-` note id: that prefix is a citation spelling
        (`kg.note.note_id_for_reaction`), and accepting both is how a store ends up holding two
        names for one row.
        """
        ...

    async def bodies(self, reaction_ids: Sequence[str]) -> dict[str, str]:
        """The stored body of each of `reaction_ids` the corpus holds — the unchanged check.

        **Keyed on the ids the caller is about to write, never on the corpus.** The sync's overlap
        window deliberately re-fetches entries it has seen, and answering "is this one unchanged?"
        used to mean parsing the whole corpus, which is what made the sync outgrow its own activity
        timeout. The question is about a bounded page of candidates, so the lookup is too.

        The body, not merely the id: an ELN amends an entry *in place* — a yield corrected after
        assay, an impurity added, a retraction — while keeping its `created_at`, so treating "seen
        before" as "unchanged" drops every correction silently.
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

    Keyed by reaction id, so re-recording an id replaces it — the same idempotency the `ON CONFLICT`
    clause gives the durable store, which is what lets an ingest test assert replay behaviour
    without a database.
    """

    def __init__(self) -> None:
        """Start with an empty corpus."""
        self._records: dict[str, ReactionRecord] = {}

    async def record(self, records: Sequence[ReactionRecord]) -> int:
        """Insert or replace each record by reaction id; return how many were written."""
        for item in records:
            self._records[item.reaction_id] = item
        return len(records)

    async def read(self, reaction_id: str) -> ReactionRecord | None:
        """One record by its bare ELN id, or `None`."""
        return self._records.get(reaction_id)

    async def bodies(self, reaction_ids: Sequence[str]) -> dict[str, str]:
        """The stored body of each of `reaction_ids` this store holds."""
        return {
            reaction_id: self._records[reaction_id].body
            for reaction_id in reaction_ids
            if reaction_id in self._records
        }

    async def eligible(self, reaction_ids: Sequence[str], filters: dict[str, Any]) -> set[str]:
        """Which of `reaction_ids` pass `filters` and are current."""
        today = date.today()
        return {
            reaction_id
            for reaction_id in reaction_ids
            if (item := self._records.get(reaction_id)) is not None and item.passes(filters, today)
        }

    async def known(self, reaction_ids: Sequence[str]) -> set[str]:
        """Which of `reaction_ids` this store holds at all."""
        return {reaction_id for reaction_id in reaction_ids if reaction_id in self._records}

    async def all_records(self) -> list[ReactionRecord]:
        """Everything stored, in id order — a test affordance, not part of the Protocol."""
        return [self._records[key] for key in sorted(self._records)]


class PostgresReactionRecordStore:
    """The durable `ReactionRecordStore` — `reaction_records`, one row per ELN entry."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout."""
        async with db.connection(settings.postgres_dsn) as conn:
            yield conn

    async def record(self, records: Sequence[ReactionRecord]) -> int:
        """Upsert transcribed reactions; return how many were written.

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
        """One record by its bare ELN id, or `None` when the corpus does not hold it."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_ONE, (reaction_id,))
                row = await cur.fetchone()
        return _record(row) if row is not None else None

    async def bodies(self, reaction_ids: Sequence[str]) -> dict[str, str]:
        """The stored body of each of `reaction_ids` the corpus holds."""
        if not reaction_ids:
            return {}
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_BODIES, (list(reaction_ids),))
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
