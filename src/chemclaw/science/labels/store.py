"""The reaction-label index: a derived, versioned, rebuildable view of every reaction corpus.

**Not the record of truth.** For an ELN reaction that is the PR-gated note in git; for a patent
corpus it is the source table. Both tables here can be dropped and refilled from those, and the
only thing lost is the time it takes. That is what lets the schema change without a migration
argument, and why nothing reads a label as evidence without also reading its citation.

Two write paths, and keeping them apart is the invariant this module exists to hold:

* `record()` writes the record phase and **only** the record phase. Re-ingesting an amended ELN
  entry must not silently discard the labels a two-day backfill derived, so the upsert names its
  columns rather than replacing the row.
* `store_labels()` writes the derived phase and **only** the derived phase, stamping
  `labeller_version`. It never touches `record_smiles`, the conditions or the recorded roles.

`stale()` is the query that makes the background service possible: a row whose `labeller_version`
is NULL (never derived) or different from the current one (derived by a superseded labeller) is
work to do. Nothing has to remember to mark anything.
"""

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field, computed_field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.vocabulary import SpeciesRole

log = logging.getLogger(__name__)


class LabelIndexError(ChemclawError):
    """A label row could not be written or read back."""


class CorpusCoverage(BaseModel):
    """How much of the row set an answer was drawn from is actually labelled — and the sentence.

    Scoped to the **facet's** rows, never to the whole corpus, because the two are different claims
    and only one of them is useful: "3% of the patent corpus is labelled" read as "3% of the
    Buchwalds are labelled" is a different lie, and the labelling drain does not proceed uniformly
    across reaction types.

    `verdict` is a `computed_field` and not a bare property for the reason
    `FingerprintSearch.verdict` spells out at length: a plain property is not serialized, so the
    one sentence that explains what the numbers mean never leaves this process. That lesson was
    learned on a hazard screen that told a chemist "no hazards detected" six times.
    """

    labelled: int = Field(ge=0, description="Rows in scope carrying the current labeller version.")
    total: int = Field(ge=0, description="Rows in scope at all, labelled or not.")
    sources: list[str] = Field(default_factory=list, description="Which sources the scope spans.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What the reader must know about this answer's denominator before quoting it."""
        if self.total == 0:
            return (
                "NO ROWS IN SCOPE: nothing in the reaction-label index matched this facet at all, "
                "so the question was not answered. This is NOT evidence that no such reaction "
                "exists — report that the corpus may not be indexed and say which sources are "
                "configured."
            )
        if self.labelled == 0:
            return (
                f"NOT ANSWERABLE YET: {self.total} reaction(s) match this facet and NONE of them "
                "have been labelled at the current labeller version, so no role, name or "
                "structure feature could be read. Report that the labelling backfill has not "
                "reached these rows. Do not present this as a finding about the chemistry."
            )
        if self.labelled < self.total:
            share = 100 * self.labelled / self.total
            return (
                f"PARTIAL: this answer is drawn from {self.labelled} of {self.total} matching "
                f"reaction(s) ({share:.0f}%) — the rest are not yet labelled at the current "
                "version. Treat counts as a lower bound and say so; a reagent absent here may "
                "simply live in an unlabelled row."
            )
        return (
            f"COMPLETE: all {self.total} matching reaction(s) are labelled at the current version, "
            "so counts over this facet are totals rather than lower bounds."
        )


class LabelIndex:
    """The read/write contract both backends implement.

    A plain base class rather than a `Protocol`, unlike `FingerprintStore`: every method here is
    abstract and there is no third implementation in prospect, so a Protocol would buy structural
    typing nobody uses and cost a second declaration to keep in step.
    """

    async def record(self, label: ReactionLabel) -> None:
        """Insert or update the record phase of one reaction, leaving any derived phase intact."""
        raise NotImplementedError

    async def stale(
        self, version: str, limit: int, sources: Sequence[str] | None = None
    ) -> list[ReactionLabel]:
        """Rows never derived, or derived under a version other than `version`, oldest key first.

        Deterministic order, so a drain that dies mid-batch resumes on the same rows — and so a
        row the labeller cannot process is the *same* row on every attempt, which is exactly why
        the drain retries the batch item by item before giving up on it.
        """
        raise NotImplementedError

    async def store_labels(self, label: ReactionLabel, version: str) -> None:
        """Write the derived phase of one reaction and stamp it `version`."""
        raise NotImplementedError

    async def coverage(
        self, version: str, reaction_keys: Iterable[tuple[str, str]] | None = None
    ) -> CorpusCoverage:
        """Labelled-vs-total over a facet's rows, or over the whole index when `None`."""
        raise NotImplementedError

    async def count(self) -> int:
        """How many reactions the index holds — the operator's number, not a hot path."""
        raise NotImplementedError


class InMemoryLabelIndex(LabelIndex):
    """Process-local backend for tests and single-run use; the reference the SQL one matches."""

    def __init__(self) -> None:
        """Start empty."""
        self._rows: dict[tuple[str, str], ReactionLabel] = {}

    async def record(self, label: ReactionLabel) -> None:
        """Upsert the record phase, carrying an already-derived phase across where it still holds.

        "Still holds" is the whole subtlety. An amended entry whose `record_smiles` changed is a
        different reaction, so its name, class and atom map are about something else and are
        dropped — which also clears `labeller_version`, putting the row back in `stale()` where it
        belongs. An entry whose structures are unchanged keeps everything, because re-ingesting a
        note edit must not silently discard a backfill that took days.
        """
        key = (label.source, label.reaction_id)
        existing = self._rows.get(key)
        if existing is None or existing.record_smiles != label.record_smiles:
            self._rows[key] = label
            return
        carried = existing.model_dump(include=_DERIVED_FIELDS | _STAMP_FIELDS)
        species = [_carry_species(new, existing.species) for new in label.species]
        self._rows[key] = label.model_copy(update={**carried, "species": species})

    async def stale(
        self, version: str, limit: int, sources: Sequence[str] | None = None
    ) -> list[ReactionLabel]:
        """Rows whose stamp differs from `version`, in key order, capped at `limit`."""
        allowed = frozenset(sources) if sources is not None else None
        rows = [
            row
            for row in self._rows.values()
            if row.labeller_version != version and (allowed is None or row.source in allowed)
        ]
        rows.sort(key=lambda r: (r.source, r.reaction_id))
        return rows[:limit]

    async def store_labels(self, label: ReactionLabel, version: str) -> None:
        """Write the derived phase over the stored record phase, stamped `version`."""
        key = (label.source, label.reaction_id)
        existing = self._rows.get(key)
        if existing is None:
            raise LabelIndexError(f"no record-phase row for {key!r}; label the corpus first")
        derived = label.model_dump(include=_DERIVED_FIELDS)
        species = [
            stored.model_copy(update=_derived_species(new))
            for stored, new in zip(existing.species, label.species, strict=False)
        ]
        self._rows[key] = existing.model_copy(
            update={
                **derived,
                "labeller_version": version,
                "labelled_at": datetime.now(UTC),
                "species": species,
            }
        )

    async def coverage(
        self, version: str, reaction_keys: Iterable[tuple[str, str]] | None = None
    ) -> CorpusCoverage:
        """Labelled-vs-total over the given keys, or over everything."""
        if reaction_keys is None:
            rows = list(self._rows.values())
        else:
            rows = [self._rows[k] for k in reaction_keys if k in self._rows]
        return CorpusCoverage(
            labelled=sum(1 for r in rows if r.labeller_version == version),
            total=len(rows),
            sources=sorted({r.source for r in rows}),
        )

    async def count(self) -> int:
        """How many reactions are held."""
        return len(self._rows)


# The reaction-row columns `store_labels` may write and `record` must never touch. One tuple, read
# by both, so the two halves of the split cannot drift into overlapping.
_DERIVED_FIELDS = {
    "mapped_smiles",
    "named_reaction",
    "reaction_class",
    "rxno_id",
    "confidence",
    "method",
}

# The stamp itself. Separate from `_DERIVED_FIELDS` because `store_labels` sets it explicitly from
# its `version` argument rather than from the payload, while `record` carries it across verbatim.
_STAMP_FIELDS = {"labeller_version", "labelled_at"}


def _derived_species(species: SpeciesLabel) -> dict[str, Any]:
    """The species columns the derived phase owns."""
    return {
        "derived_role": species.derived_role,
        "scaffold": species.scaffold,
        "functional_groups": list(species.functional_groups),
    }


def _carry_species(new: SpeciesLabel, stored: Sequence[SpeciesLabel]) -> SpeciesLabel:
    """Re-apply an already-derived species phase to a freshly recorded species of the same ordinal.

    Matched on `ordinal` *and* `smiles`: an amended ELN entry may have re-ordered or replaced a
    charge, and inheriting a ligand classification onto a different structure would be worse than
    re-deriving it.
    """
    for old in stored:
        if old.ordinal == new.ordinal and old.smiles == new.smiles:
            return new.model_copy(update=_derived_species(old))
    return new


class PostgresLabelIndex(LabelIndex):
    """Durable backend over `reaction_labels` + `reaction_species`.

    A short-lived (pooled) connection per call, the choice both the calculation store and the
    fingerprint store made — KISS, and the pool means it costs no handshake in a worker.

    Every statement here names its columns. That is not style: the record phase and the derived
    phase share a row, and `SELECT *`/`DO UPDATE SET (...) = ROW(EXCLUDED.*)` would let one write
    path silently clobber the other's columns the first time somebody adds a field.
    """

    # Record phase. `ON CONFLICT` names only the record columns, so a re-ingest cannot discard a
    # derived phase — except deliberately: when `record_smiles` changed the reaction is a different
    # one, so its derived phase is cleared and `labeller_version` goes back to NULL, which puts the
    # row into `stale()` on the next drain. Exactly the in-memory backend's rule, in SQL.
    _RECORD = """
        INSERT INTO reaction_labels (
            source, reaction_id, record_smiles, citation, performed_on,
            temperature_c, time_h, yield_percent, workup_text
        ) VALUES (
            %(source)s, %(reaction_id)s, %(record_smiles)s, %(citation)s, %(performed_on)s,
            %(temperature_c)s, %(time_h)s, %(yield_percent)s, %(workup_text)s
        )
        ON CONFLICT (source, reaction_id) DO UPDATE SET
            record_smiles = EXCLUDED.record_smiles,
            citation = EXCLUDED.citation,
            performed_on = EXCLUDED.performed_on,
            temperature_c = EXCLUDED.temperature_c,
            time_h = EXCLUDED.time_h,
            yield_percent = EXCLUDED.yield_percent,
            workup_text = EXCLUDED.workup_text,
            mapped_smiles = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.mapped_smiles END,
            named_reaction = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.named_reaction END,
            reaction_class = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.reaction_class END,
            rxno_id = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.rxno_id END,
            confidence = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.confidence END,
            method = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.method END,
            labeller_version = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.labeller_version END,
            labelled_at = CASE WHEN reaction_labels.record_smiles = EXCLUDED.record_smiles
                THEN reaction_labels.labelled_at END
    """

    # Same shape one level down: a species whose structure at this ordinal is unchanged keeps its
    # derived role and features; a replaced structure loses them, because inheriting a "ligand"
    # verdict onto a different molecule is worse than re-deriving it.
    _RECORD_SPECIES = """
        INSERT INTO reaction_species (source, reaction_id, ordinal, smiles, role)
        VALUES (%(source)s, %(reaction_id)s, %(ordinal)s, %(smiles)s, %(role)s)
        ON CONFLICT (source, reaction_id, ordinal) DO UPDATE SET
            smiles = EXCLUDED.smiles,
            role = EXCLUDED.role,
            derived_role = CASE WHEN reaction_species.smiles = EXCLUDED.smiles
                THEN reaction_species.derived_role END,
            scaffold = CASE WHEN reaction_species.smiles = EXCLUDED.smiles
                THEN reaction_species.scaffold END,
            functional_groups = CASE WHEN reaction_species.smiles = EXCLUDED.smiles
                THEN reaction_species.functional_groups END
    """

    # An amendment that removed a charge leaves a higher ordinal behind; without this the index
    # would answer "this reaction used TEA" from a species the current record no longer has.
    _TRIM_SPECIES = """
        DELETE FROM reaction_species
        WHERE source = %(source)s AND reaction_id = %(reaction_id)s AND ordinal >= %(kept)s
    """

    # `IS DISTINCT FROM`, not `<>`: NULL means never derived and is the commonest stale row on a
    # fresh corpus, and `<>` would exclude precisely those.
    _STALE = """
        SELECT source, reaction_id, record_smiles, citation, performed_on, temperature_c,
               time_h, yield_percent, workup_text, mapped_smiles, named_reaction, reaction_class,
               rxno_id, confidence, method, labeller_version, labelled_at
        FROM reaction_labels
        WHERE labeller_version IS DISTINCT FROM %(version)s
          AND (%(sources)s::text[] IS NULL OR source = ANY(%(sources)s::text[]))
        ORDER BY source, reaction_id
        LIMIT %(limit)s
    """

    _SPECIES_FOR = """
        SELECT source, reaction_id, ordinal, smiles, role, derived_role, scaffold, functional_groups
        FROM reaction_species
        JOIN unnest(%(sources)s::text[], %(ids)s::text[]) AS k(s, i)
          ON source = k.s AND reaction_id = k.i
        ORDER BY source, reaction_id, ordinal
    """

    _STORE_LABELS = """
        UPDATE reaction_labels SET
            mapped_smiles = %(mapped_smiles)s,
            named_reaction = %(named_reaction)s,
            reaction_class = %(reaction_class)s,
            rxno_id = %(rxno_id)s,
            confidence = %(confidence)s,
            method = %(method)s,
            labeller_version = %(version)s,
            labelled_at = now()
        WHERE source = %(source)s AND reaction_id = %(reaction_id)s
    """

    _STORE_SPECIES = """
        UPDATE reaction_species SET
            derived_role = %(derived_role)s,
            scaffold = %(scaffold)s,
            functional_groups = %(functional_groups)s
        WHERE source = %(source)s AND reaction_id = %(reaction_id)s AND ordinal = %(ordinal)s
    """

    _COVERAGE_ALL = """
        SELECT count(*) FILTER (WHERE labeller_version = %(version)s), count(*),
               array_agg(DISTINCT source)
        FROM reaction_labels
    """

    _COVERAGE_KEYS = """
        SELECT count(*) FILTER (WHERE labeller_version = %(version)s), count(*),
               array_agg(DISTINCT source)
        FROM reaction_labels
        JOIN unnest(%(sources)s::text[], %(ids)s::text[]) AS k(s, i)
          ON source = k.s AND reaction_id = k.i
    """

    _COUNT = "SELECT count(*) FROM reaction_labels"

    def __init__(self, dsn: str | None = None) -> None:
        """Bind to the configured DSN (or an explicit one, for tests against a scratch database)."""
        self._dsn = dsn if dsn is not None else settings.postgres_dsn

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a bounded connection from the shared pool."""
        async with db.connection(self._dsn) as conn:
            yield conn

    async def record(self, label: ReactionLabel) -> None:
        """Write the record phase of one reaction and its species, in one transaction.

        One transaction because a reaction whose species were replaced but whose row was not — or
        the reverse — is a row that answers questions about a flask that never existed.
        """
        async with self._connection() as conn:
            await conn.execute(self._RECORD, _record_params(label))
            for species in label.species:
                await conn.execute(
                    self._RECORD_SPECIES,
                    {
                        "source": label.source,
                        "reaction_id": label.reaction_id,
                        "ordinal": species.ordinal,
                        "smiles": species.smiles,
                        "role": species.role,
                    },
                )
            await conn.execute(
                self._TRIM_SPECIES,
                {
                    "source": label.source,
                    "reaction_id": label.reaction_id,
                    "kept": len(label.species),
                },
            )
            await conn.commit()

    async def stale(
        self, version: str, limit: int, sources: Sequence[str] | None = None
    ) -> list[ReactionLabel]:
        """Rows never derived or derived under another version, with their species attached."""
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(
                self._STALE,
                {
                    "version": version,
                    "limit": limit,
                    "sources": list(sources) if sources is not None else None,
                },
            )
            rows = await cur.fetchall()
            if not rows:
                return []
            keys = [(str(r[0]), str(r[1])) for r in rows]
            await cur.execute(
                self._SPECIES_FOR,
                {"sources": [k[0] for k in keys], "ids": [k[1] for k in keys]},
            )
            species_rows = await cur.fetchall()
        by_key: dict[tuple[str, str], list[SpeciesLabel]] = {k: [] for k in keys}
        for row in species_rows:
            by_key[(str(row[0]), str(row[1]))].append(_species_from_row(row))
        return [_label_from_row(r, by_key[(str(r[0]), str(r[1]))]) for r in rows]

    async def store_labels(self, label: ReactionLabel, version: str) -> None:
        """Write the derived phase of one reaction and its species, stamped `version`."""
        async with self._connection() as conn:
            params = label.model_dump(include=_DERIVED_FIELDS)
            params.update(
                {"source": label.source, "reaction_id": label.reaction_id, "version": version}
            )
            cur = await conn.execute(self._STORE_LABELS, params)
            if cur.rowcount == 0:
                raise LabelIndexError(
                    f"no record-phase row for ({label.source!r}, {label.reaction_id!r}); "
                    "the corpus must be recorded before it can be labelled"
                )
            for species in label.species:
                await conn.execute(
                    self._STORE_SPECIES,
                    {
                        "source": label.source,
                        "reaction_id": label.reaction_id,
                        "ordinal": species.ordinal,
                        "derived_role": species.derived_role,
                        "scaffold": species.scaffold,
                        "functional_groups": list(species.functional_groups),
                    },
                )
            await conn.commit()

    async def coverage(
        self, version: str, reaction_keys: Iterable[tuple[str, str]] | None = None
    ) -> CorpusCoverage:
        """Labelled-vs-total over a facet's keys, or over the whole index."""
        params: dict[str, Any] = {"version": version}
        if reaction_keys is None:
            sql = self._COVERAGE_ALL
        else:
            keys = list(reaction_keys)
            if not keys:
                return CorpusCoverage(labelled=0, total=0, sources=[])
            sql = self._COVERAGE_KEYS
            params["sources"] = [k[0] for k in keys]
            params["ids"] = [k[1] for k in keys]
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
        if row is None:
            return CorpusCoverage(labelled=0, total=0, sources=[])
        return CorpusCoverage(labelled=int(row[0]), total=int(row[1]), sources=sorted(row[2] or []))

    async def count(self) -> int:
        """How many reactions the index holds."""
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(self._COUNT)
            row = await cur.fetchone()
        return int(row[0]) if row else 0


def default_label_index() -> PostgresLabelIndex:
    """The durable index on the configured DSN — the one every caller in this tree should use."""
    return PostgresLabelIndex()


def _record_params(label: ReactionLabel) -> dict[str, Any]:
    """The record-phase columns of one reaction, as query parameters."""
    return {
        "source": label.source,
        "reaction_id": label.reaction_id,
        "record_smiles": label.record_smiles,
        "citation": label.citation,
        "performed_on": label.performed_on,
        "temperature_c": label.temperature_c,
        "time_h": label.time_h,
        "yield_percent": label.yield_percent,
        "workup_text": label.workup_text,
    }


def _label_from_row(row: Sequence[Any], species: list[SpeciesLabel]) -> ReactionLabel:
    """Rebuild a `ReactionLabel` from one `_STALE` row plus its species."""
    return ReactionLabel(
        source=str(row[0]),
        reaction_id=str(row[1]),
        record_smiles=str(row[2]),
        citation=str(row[3]),
        performed_on=row[4],
        temperature_c=row[5],
        time_h=row[6],
        yield_percent=row[7],
        workup_text=row[8],
        species=species,
        mapped_smiles=row[9],
        named_reaction=row[10],
        reaction_class=row[11],
        rxno_id=row[12],
        confidence=row[13],
        method=row[14],
        labeller_version=row[15],
        labelled_at=row[16],
    )


def _species_from_row(row: Sequence[Any]) -> SpeciesLabel:
    """Rebuild a `SpeciesLabel` from one `_SPECIES_FOR` row."""
    derived = row[5]
    return SpeciesLabel(
        ordinal=int(row[2]),
        smiles=str(row[3]),
        role=str(row[4]),
        derived_role=SpeciesRole(derived) if derived is not None else None,
        scaffold=row[6],
        functional_groups=list(row[7] or []),
    )
