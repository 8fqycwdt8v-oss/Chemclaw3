"""The reaction-label index: a derived, versioned, rebuildable view of every reaction corpus.

Not the record of truth: both tables can be dropped and refilled from `reaction_records` or the
source table, and nothing reads a label as evidence without its citation.

Two write paths, kept apart:

* `record()` writes only the record phase; the upsert names its columns so re-ingesting an amended
  entry does not discard derived labels.
* `store_labels()` writes only the derived phase and stamps `labeller_version`.

`stale()` returns rows whose `labeller_version` is NULL or differs from the current one, so nothing
has to remember to mark work. A row the labelling server could not answer for is stamped with
`underived_stamp`, which `stale()` treats as done while `coverage`, `select` and `current_version`
do not treat it as labelled.
"""

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.science.labels.facets import AgentCount, Facet, FacetSelection, FrequencyReport
from chemclaw.science.labels.records import CorpusCoverage, ReactionLabel, SpeciesLabel
from chemclaw.science.labels.vocabulary import SpeciesRole

log = logging.getLogger(__name__)


class LabelIndexError(ChemclawError):
    """A label row could not be written or read back."""


# Marks a stamp for a row the drain passed over without deriving anything. The drain must advance
# past it, but the row keeps the previous labeller's content, so it must not count as labelled at
# this version. A suffix on the one column rather than a second column, so staleness and currency
# cannot drift; only this module composes or reads the tag.
_UNDERIVED_SUFFIX = "+underived"


def underived_stamp(version: str) -> str:
    """The stamp for a row the drain reached at `version` and could derive nothing for."""
    return f"{version}{_UNDERIVED_SUFFIX}"


def is_underived_stamp(labeller_version: str | None) -> bool:
    """Whether a stored stamp says the drain passed over this row without deriving anything."""
    return labeller_version is not None and labeller_version.endswith(_UNDERIVED_SUFFIX)


def _stamp(version: str, derived: bool) -> str:
    """The value `labeller_version` takes for one `store_labels` call.

    One place, so both backends agree. Refuses a `version` already carrying the marker, since the
    version string comes from a remote server and would otherwise make a derived row
    indistinguishable from an underived one.
    """
    if _UNDERIVED_SUFFIX in version:
        raise LabelIndexError(
            f"labeller version {version!r} contains {_UNDERIVED_SUFFIX!r}, which this index uses "
            "to mark a row the labeller could not derive anything for; a derived row stamped with "
            "it would be indistinguishable from an un-derived one"
        )
    return version if derived else underived_stamp(version)


class LabelIndex:
    """The read/write contract both backends implement.

    A plain base class rather than a `Protocol`: every method is abstract and there is no third
    implementation, so structural typing would buy nothing.
    """

    async def record(self, label: ReactionLabel) -> None:
        """Insert or update the record phase of one reaction, leaving any derived phase intact."""
        raise NotImplementedError

    async def stale(
        self, version: str, limit: int, sources: Sequence[str] | None = None
    ) -> list[ReactionLabel]:
        """Rows never derived, or derived under a version other than `version`, oldest key first.

        Deterministic order, so a drain that dies mid-batch resumes on the same rows and an
        unprocessable row is the same row on every attempt.

        `sources` is a scoping affordance for tests only; the drain must read every source, because
        a source without a `labels:` block still needs labelling (`tests/test_label_enrichment.py`
        pins it).
        """
        raise NotImplementedError

    async def store_labels(
        self, label: ReactionLabel, version: str, *, derived: bool = True
    ) -> None:
        """Write the derived phase of one reaction and stamp it for `version`.

        `derived` says whether the labeller actually answered for this row. A row the server could
        not answer for is passed `derived=False`, so it leaves `stale()` without being counted as
        labelled.
        """
        raise NotImplementedError

    async def coverage(
        self, version: str, reaction_keys: Iterable[tuple[str, str]] | None = None
    ) -> CorpusCoverage:
        """Labelled-vs-total over a facet's rows, or over the whole index when `None`."""
        raise NotImplementedError

    async def count(self) -> int:
        """How many reactions the index holds — the operator's number, not a hot path."""
        raise NotImplementedError

    async def current_version(self) -> str | None:
        """The labeller version the index is *currently* labelled at, or `None` if nothing is.

        The version of the most recently labelled row: mid-upgrade this names the new labeller, so
        searches answer from its rows and `CorpusCoverage` reports the rest as unlabelled. Read from
        the index, not the labelling server, so searches do not depend on a background service being
        up.
        """
        raise NotImplementedError

    async def select(self, facet: Facet, version: str, limit: int) -> FacetSelection:
        """The labelled reactions this facet selects, with the coverage of its own row set.

        Only rows labelled at `version` are returned: an unlabelled row cannot satisfy any facet,
        and showing it beside labelled rows would imply it was checked. It counts towards `coverage`
        instead.
        """
        raise NotImplementedError

    async def agent_counts(
        self, facet: Facet, version: str, roles: frozenset[SpeciesRole], limit: int
    ) -> FrequencyReport:
        """How often each species appears in each of `roles` across the facet's reactions."""
        raise NotImplementedError


class InMemoryLabelIndex(LabelIndex):
    """Process-local reference ranking; the SQL one is written to match it.

    A differential oracle, not a deployment backend: no configuration returns it
    (`tests/test_reference_stores.py` holds that).
    """

    def __init__(self) -> None:
        """Start empty."""
        self._rows: dict[tuple[str, str], ReactionLabel] = {}

    async def record(self, label: ReactionLabel) -> None:
        """Upsert the record phase, carrying an already-derived phase across where it still holds.

        If `record_smiles` changed, the entry is a different reaction: its derived phase is dropped
        and `labeller_version` cleared, putting it back in `stale()`. Otherwise everything is kept,
        so a note edit does not discard a backfill.
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
        """Rows whose stamp differs from `version`, in key order, capped at `limit`.

        An underived stamp at this version also counts as done, or an unlabellable batch would be
        re-read on every pass.
        """
        allowed = frozenset(sources) if sources is not None else None
        current = {version, underived_stamp(version)}
        rows = [
            row
            for row in self._rows.values()
            if row.labeller_version not in current and (allowed is None or row.source in allowed)
        ]
        rows.sort(key=lambda r: (r.source, r.reaction_id))
        return rows[:limit]

    async def store_labels(
        self, label: ReactionLabel, version: str, *, derived: bool = True
    ) -> None:
        """Write the derived phase over the stored record phase, stamped for `version`.

        `derived=False` puts the marker on the stamp and leaves `labelled_at` unchanged, so the row
        leaves the stale set but is not reported as labelled.

        Species are paired by `ordinal`, the key the Postgres backend's `UPDATE ... WHERE ordinal =
        %s` matches on, so both backends agree regardless of answer order; an ordinal the stored row
        lacks is ignored.
        """
        key = (label.source, label.reaction_id)
        existing = self._rows.get(key)
        if existing is None:
            raise LabelIndexError(f"no record-phase row for {key!r}; label the corpus first")
        written = label.model_dump(include=_DERIVED_FIELDS)
        by_ordinal = {new.ordinal: _derived_species(new) for new in label.species}
        species = [
            stored.model_copy(update=by_ordinal[stored.ordinal])
            if stored.ordinal in by_ordinal
            else stored
            for stored in existing.species
        ]
        self._rows[key] = existing.model_copy(
            update={
                **written,
                "labeller_version": _stamp(version, derived),
                "labelled_at": datetime.now(UTC) if derived else existing.labelled_at,
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

    async def current_version(self) -> str | None:
        """The version of the most recently *derived* row.

        Underived stamps are skipped, so a corpus whose whole re-label found the server down does
        not report a version no row's content was derived under.
        """
        labelled = [
            r
            for r in self._rows.values()
            if r.labelled_at is not None and not is_underived_stamp(r.labeller_version)
        ]
        if not labelled:
            return None
        newest = max(labelled, key=_labelled_key)
        return newest.labeller_version

    async def select(self, facet: Facet, version: str, limit: int) -> FacetSelection:
        """The reference implementation of the facet query — plain Python over the held rows."""
        scoped = [r for r in self._rows.values() if _in_scope(r, facet)]
        labelled = [r for r in scoped if r.labeller_version == version and _matches(r, facet)]
        labelled.sort(key=lambda r: (r.source, r.reaction_id))
        return FacetSelection(
            rows=labelled[:limit],
            truncated=len(labelled) > limit,
            coverage=CorpusCoverage(
                labelled=sum(1 for r in scoped if r.labeller_version == version),
                total=len(scoped),
                sources=sorted({r.source for r in scoped}),
            ),
        )

    async def agent_counts(
        self, facet: Facet, version: str, roles: frozenset[SpeciesRole], limit: int
    ) -> FrequencyReport:
        """Roll the selection's species up by role, most common first."""
        selection = await self.select(facet, version, limit)
        return _roll_up(selection, roles)


# The reaction-row columns `store_labels` may write and `record` must never touch; shared by both so
# the split cannot overlap.
_DERIVED_FIELDS = {
    "mapped_smiles",
    "named_reaction",
    "reaction_class",
    "rxno_id",
    "confidence",
    "method",
}

# The stamp: `store_labels` sets it from its `version` argument, while `record` carries it across
# verbatim.
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

    Matched on `ordinal` and `smiles`, so a classification is never inherited onto a different
    structure.
    """
    for old in stored:
        if old.ordinal == new.ordinal and old.smiles == new.smiles:
            return new.model_copy(update=_derived_species(old))
    return new


class PostgresLabelIndex(LabelIndex):
    """Durable backend over `reaction_labels` + `reaction_species`.

    A pooled connection per call. Every statement names its columns: the record and derived phases
    share a row, and `SELECT *` or a whole-row update would let one write path clobber the other's
    columns.
    """

    # Record phase. `ON CONFLICT` names only the record columns, so a re-ingest keeps the derived
    # phase, unless `record_smiles` changed: then the derived phase is cleared and
    # `labeller_version` reset to NULL, returning the row to `stale()`.
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

    # Same rule per species: an unchanged structure at this ordinal keeps its derived role and
    # features; a replaced one loses them.
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

    # Removes species past the current record's last ordinal, so the index does not answer from a
    # species the amended record no longer has.
    _TRIM_SPECIES = """
        DELETE FROM reaction_species
        WHERE source = %(source)s AND reaction_id = %(reaction_id)s AND ordinal >= %(kept)s
    """

    # `IS DISTINCT FROM`, not `<>`, so NULL (never derived) rows are stale. The underived stamp for
    # this version is also not stale, or the row would be re-read every pass.
    _STALE = """
        SELECT source, reaction_id, record_smiles, citation, performed_on, temperature_c,
               time_h, yield_percent, workup_text, mapped_smiles, named_reaction, reaction_class,
               rxno_id, confidence, method, labeller_version, labelled_at
        FROM reaction_labels
        WHERE labeller_version IS DISTINCT FROM %(version)s
          AND labeller_version IS DISTINCT FROM %(underived)s
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
            labeller_version = %(stamp)s,
            labelled_at = CASE WHEN %(derived)s THEN now() ELSE labelled_at END
        WHERE source = %(source)s AND reaction_id = %(reaction_id)s
    """

    _STORE_SPECIES = """
        UPDATE reaction_species SET
            derived_role = %(derived_role)s,
            scaffold = %(scaffold)s,
            functional_groups = %(functional_groups)s
        WHERE source = %(source)s AND reaction_id = %(reaction_id)s AND ordinal = %(ordinal)s
    """

    # The one coverage projection (labelled at this version, total, sources present); each coverage
    # read appends its own scope.
    _COVERAGE_COLUMNS = """
        SELECT count(*) FILTER (WHERE labeller_version = %(version)s), count(*),
               array_agg(DISTINCT source)
        FROM reaction_labels
    """

    _COVERAGE_ALL = _COVERAGE_COLUMNS

    _COVERAGE_KEYS = (
        _COVERAGE_COLUMNS
        + """
        JOIN unnest(%(sources)s::text[], %(ids)s::text[]) AS k(s, i)
          ON source = k.s AND reaction_id = k.i
    """
    )

    _COUNT = "SELECT count(*) FROM reaction_labels"

    # The newest labelling's version. Its `WHERE` and `ORDER BY` match
    # `reaction_labels_current_version_idx` (migration 086) exactly; changing either falls back to a
    # sequential scan on every rxnfp tool call. The `NOT LIKE` skips underived stamps, as `_STALE`
    # does, and filters the same index scan rather than defeating it.
    _CURRENT_VERSION = (
        "SELECT labeller_version FROM reaction_labels WHERE labelled_at IS NOT NULL "
        f"AND labeller_version NOT LIKE '%{_UNDERIVED_SUFFIX}' "
        "ORDER BY labelled_at DESC, source, reaction_id LIMIT 1"
    )

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

        One transaction so the row and its species never describe different reactions.
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
        """Rows never derived or derived under another version, with their species attached.

        A row already passed over at `version` without deriving anything is excluded (see `_STALE`).
        """
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(
                self._STALE,
                {
                    "version": version,
                    "underived": underived_stamp(version),
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

    async def store_labels(
        self, label: ReactionLabel, version: str, *, derived: bool = True
    ) -> None:
        """Write the derived phase of one reaction and its species, stamped for `version`.

        `derived=False` marks the stamp and holds `labelled_at`. The species half is still written,
        since roles can fall back to the source's coarse map.
        """
        async with self._connection() as conn:
            params = label.model_dump(include=_DERIVED_FIELDS)
            params.update(
                {
                    "source": label.source,
                    "reaction_id": label.reaction_id,
                    "stamp": _stamp(version, derived),
                    "derived": derived,
                }
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

    async def current_version(self) -> str | None:
        """The version of the most recently *derived* row — see the in-memory twin for why.

        Served by `reaction_labels_current_version_idx`, since every rxnfp tool calls it. Executed
        with no parameters, so the literal `%` in `_CURRENT_VERSION`'s LIKE pattern must not be
        doubled.
        """
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(self._CURRENT_VERSION)
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] is not None else None

    async def select(self, facet: Facet, version: str, limit: int) -> FacetSelection:
        """The facet's labelled reactions, plus the coverage of the row set it drew them from.

        Two statements because the denominators differ: the selection is over labelled rows matching
        every narrowing, coverage over every row in scope. One row over `limit` is fetched and
        dropped, so a page that exactly fills the cap is distinguishable from a truncated one.
        """
        where, params = _facet_sql(facet)
        params["version"] = version
        params["limit"] = limit + 1
        sql = (
            "SELECT source, reaction_id, record_smiles, citation, performed_on, temperature_c, "
            "time_h, yield_percent, workup_text, mapped_smiles, named_reaction, reaction_class, "
            "rxno_id, confidence, method, labeller_version, labelled_at "
            "FROM reaction_labels r "
            f"WHERE labeller_version = %(version)s{where} "
            "ORDER BY source, reaction_id LIMIT %(limit)s"
        )
        async with self._connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            species = await self._species_for(cur, [(str(r[0]), str(r[1])) for r in rows])
            coverage = await self._scope_coverage(cur, facet, version)
        return FacetSelection(
            rows=[_label_from_row(r, species[(str(r[0]), str(r[1]))]) for r in rows],
            truncated=truncated,
            coverage=coverage,
        )

    async def agent_counts(
        self, facet: Facet, version: str, roles: frozenset[SpeciesRole], limit: int
    ) -> FrequencyReport:
        """Roll the facet's species up by role.

        In Python over a bounded selection, sharing `_roll_up` with the in-memory backend, because
        the counting rules (one count per reaction, per-role denominator, median over recorded
        yields) are not a plain `GROUP BY`. The cap is reported via `truncated`.
        """
        selection = await self.select(facet, version, limit)
        return _roll_up(selection, roles)

    async def _species_for(
        self, cur: Any, keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], list[SpeciesLabel]]:
        """Every species of the given reactions, grouped by key — one query, not one per row."""
        grouped: dict[tuple[str, str], list[SpeciesLabel]] = {k: [] for k in keys}
        if not keys:
            return grouped
        await cur.execute(
            self._SPECIES_FOR,
            {"sources": [k[0] for k in keys], "ids": [k[1] for k in keys]},
        )
        for row in await cur.fetchall():
            grouped[(str(row[0]), str(row[1]))].append(_species_from_row(row))
        return grouped

    async def _scope_coverage(self, cur: Any, facet: Facet, version: str) -> CorpusCoverage:
        """Labelled-vs-total over the facet's *scope* — see `_in_scope` for why that is narrower.

        Every narrowing `_in_scope` applies must be restated here, or the denominator disagrees
        between backends.
        """
        params: dict[str, Any] = {"version": version}
        clauses: list[str] = []
        if facet.sources:
            params["sources"] = sorted(facet.sources)
            clauses.append("source = ANY(%(sources)s::text[])")
        if facet.reaction_keys:
            pairs = sorted(facet.reaction_keys)
            params["rk_sources"] = [s for s, _ in pairs]
            params["rk_ids"] = [i for _, i in pairs]
            clauses.append(
                "EXISTS (SELECT 1 FROM unnest(%(rk_sources)s::text[], %(rk_ids)s::text[]) "
                "AS k(s, i) WHERE k.s = source AND k.i = reaction_id)"
            )
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        await cur.execute(self._COVERAGE_COLUMNS + where, params)
        row = await cur.fetchone()
        if row is None:
            return CorpusCoverage(labelled=0, total=0, sources=[])
        return CorpusCoverage(labelled=int(row[0]), total=int(row[1]), sources=sorted(row[2] or []))


def _labelled_key(row: ReactionLabel) -> tuple[datetime, str, str]:
    """Sort key for "most recently labelled", with the row key as a deterministic tie-break."""
    assert row.labelled_at is not None  # the caller filtered on it
    return (row.labelled_at, row.source, row.reaction_id)


def _facet_sql(facet: Facet) -> tuple[str, dict[str, Any]]:
    """The facet as an SQL fragment ANDed onto the version filter, plus its bound parameters.

    Species narrowings are `EXISTS` subqueries, not joins, so a reaction with several matching
    species counts once. No value reaches the statement text; the facet's fields are model-supplied.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if facet.sources:
        clauses.append("r.source = ANY(%(sources)s::text[])")
        params["sources"] = sorted(facet.sources)
    if facet.reaction_keys:
        # Two parallel arrays zipped by `unnest`, matched against the table's own key columns rather
        # than a concatenated string. The set is bounded by `fingerprint_max_top_k`, so no index is
        # needed.
        pairs = sorted(facet.reaction_keys)
        clauses.append(
            "EXISTS (SELECT 1 FROM unnest(%(rk_sources)s::text[], %(rk_ids)s::text[]) AS k(s, i) "
            "WHERE k.s = r.source AND k.i = r.reaction_id)"
        )
        params["rk_sources"] = [source for source, _ in pairs]
        params["rk_ids"] = [reaction_id for _, reaction_id in pairs]
    if facet.named_reaction:
        clauses.append("lower(r.named_reaction) = lower(%(named)s)")
        params["named"] = facet.named_reaction
    if facet.rxno_id:
        clauses.append("r.rxno_id = %(rxno)s")
        params["rxno"] = facet.rxno_id
    if facet.species_smiles is not None:
        role_filter = ""
        if facet.species_roles:
            role_filter = " AND s.derived_role = ANY(%(species_roles)s::text[])"
            params["species_roles"] = sorted(r.value for r in facet.species_roles)
        clauses.append(
            "EXISTS (SELECT 1 FROM reaction_species s WHERE s.source = r.source "
            "AND s.reaction_id = r.reaction_id AND s.smiles = %(species)s" + role_filter + ")"
        )
        params["species"] = facet.species_smiles
    if facet.product_smiles:
        clauses.append(
            "EXISTS (SELECT 1 FROM reaction_species s WHERE s.source = r.source "
            "AND s.reaction_id = r.reaction_id AND s.derived_role = 'product' "
            "AND s.smiles = ANY(%(products)s::text[]))"
        )
        params["products"] = sorted(facet.product_smiles)
    if facet.product_functional_group is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM reaction_species s WHERE s.source = r.source "
            "AND s.reaction_id = r.reaction_id AND s.derived_role = 'product' "
            "AND s.functional_groups @> ARRAY[%(group)s]::text[])"
        )
        params["group"] = facet.product_functional_group
    return ("".join(f" AND {clause}" for clause in clauses), params)


def _in_scope(row: ReactionLabel, facet: Facet) -> bool:
    """Whether this row belongs to the facet's *denominator* — the coverage question.

    Narrower than `_matches`: only narrowings an unlabelled row can answer count, so unlabelled
    reactions are not hidden from the coverage warning.
    """
    if facet.sources and row.source not in facet.sources:
        return False
    # In scope because it reads the reaction's own key, which an unlabelled row has; without it the
    # denominator would be the whole corpus against a handful of neighbours.
    return not facet.reaction_keys or (row.source, row.reaction_id) in facet.reaction_keys


def _matches(row: ReactionLabel, facet: Facet) -> bool:
    """Whether a labelled row satisfies every narrowing the facet sets."""
    if facet.named_reaction and (row.named_reaction or "").lower() != facet.named_reaction.lower():
        return False
    if facet.rxno_id and row.rxno_id != facet.rxno_id:
        return False
    if facet.reaction_keys and (row.source, row.reaction_id) not in facet.reaction_keys:
        return False
    if facet.species_smiles is not None and not any(
        s.smiles == facet.species_smiles
        and (not facet.species_roles or s.derived_role in facet.species_roles)
        for s in row.species
    ):
        return False
    products = [s for s in row.species if s.derived_role is SpeciesRole.PRODUCT]
    if facet.product_smiles and not any(s.smiles in facet.product_smiles for s in products):
        return False
    if facet.product_functional_group is not None and not any(
        facet.product_functional_group in s.functional_groups for s in products
    ):
        return False
    return True


def _roll_up(selection: FacetSelection, roles: frozenset[SpeciesRole]) -> FrequencyReport:
    """Count species by role over a selection, and attach the yields that go with them.

    The denominator is reactions that named a species in this role: an unrecorded ligand is not
    evidence that none was used.
    """
    counts: dict[tuple[SpeciesRole, str], list[float | None]] = {}
    denominator: dict[SpeciesRole, int] = {}
    for row in selection.rows:
        seen: set[tuple[SpeciesRole, str]] = set()
        for species in row.species:
            role = species.derived_role
            if role is None or (roles and role not in roles):
                continue
            key = (role, species.smiles)
            if key in seen:
                # A species charged twice in one run counts once for that reaction.
                continue
            seen.add(key)
            counts.setdefault(key, []).append(row.yield_percent)
        for role in {r for r, _ in seen}:
            denominator[role] = denominator.get(role, 0) + 1
    agents = [
        AgentCount(
            role=role,
            smiles=smiles,
            count=len(yields),
            share=len(yields) / denominator[role],
            median_yield_percent=_median([y for y in yields if y is not None]),
        )
        for (role, smiles), yields in counts.items()
    ]
    agents.sort(key=lambda a: (-a.count, a.role.value, a.smiles))
    return FrequencyReport(
        agents=agents,
        reactions_in_scope=len(selection.rows),
        coverage=selection.coverage,
        truncated=selection.truncated,
    )


def _median(values: list[float]) -> float | None:
    """The median of what was recorded, or `None` when nothing was.

    `None`, not 0.0: an unrecorded yield is not a yield of zero.
    """
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


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
