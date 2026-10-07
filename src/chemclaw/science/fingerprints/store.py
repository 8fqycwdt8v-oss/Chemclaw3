"""Generic fingerprint store — Tanimoto search over any bit-fingerprinted record.

Shared by the molecule (ECFP4) and reaction (DRFP) capabilities: a record is an id, a label (a
SMILES or reaction SMILES) and a bit fingerprint. Each domain supplies only its fingerprint
function, table and bit width; ranking lives here once.
"""

import logging
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Generic, Literal, Protocol, TypeVar, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict, Field, computed_field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

log = logging.getLogger(__name__)

# Which corpus a search ran over; a closed set because it is interpolated into the sentence the
# model reads.
Subject = Literal["molecule", "reaction"]

# pgvector's hard ceiling on `hnsw.ef_search`; a property of the extension, not policy, so the
# configurable knobs are clamped against it.
_HNSW_MAX_EF_SEARCH = 1000


class FingerprintError(ChemclawError):
    """A fingerprint could not be computed or two fingerprints are incomparable (G4)."""


class FingerprintInputError(FingerprintError):
    """The *string* handed in is not something this domain can fingerprint (G4).

    A subclass because callers read the two cases oppositely: a bad argument may be answered with
    "nothing found", while the parent also covers an index that cannot be searched, which is an
    outage and must not read as "no precedent".
    """


def tanimoto(bits_a: str, bits_b: str) -> float:
    """Tanimoto (Jaccard) similarity of two equal-length fingerprint bitstrings.

    `intersection / union` of set bits; two all-zero fingerprints score 0.0, where pgvector's
    Jaccard would return NaN. Works on the stored bitstrings, so the in-memory backend ranks exactly
    as the Postgres backend does.
    """
    if len(bits_a) != len(bits_b):
        raise FingerprintError("cannot compare fingerprints of different widths")
    return tanimoto_bits(int(bits_a, 2), int(bits_b, 2))


def tanimoto_bits(a: int, b: int) -> float:
    """Tanimoto of two fingerprints already parsed into ints — the scoring half of `tanimoto`.

    The string parse costs more than the popcount, so callers comparing one value against many parse
    once and call this. No width check: ints have no width, so callers must pre-parse an equal-width
    corpus.
    """
    union = (a | b).bit_count()
    return (a & b).bit_count() / union if union else 0.0


class FingerprintRecord(BaseModel):
    """A stored entity: a stable id, its human label (SMILES/reaction SMILES), its bits.

    `definition` is the signature of the fingerprint parameters that produced `bits` (e.g.
    `ecfp:r2:b2048`, `drfp:b2048`). Bits of equal width but different definition (a changed
    Morgan radius) are the same length yet incomparable, which the width check cannot catch;
    carrying the definition lets the durable store refuse to rank across definitions. Defaults
    to empty for a record built without one (an ephemeral, single-definition index).

    `source` is the *other half of the id* for an index whose ids come from outside this system
    (D-2026-08-27). An ELN entry id is unique to one site, so two ELNs may legitimately both hold
    `EXP-1001` and the two are different runs; the reaction index keyed on the bare id let the
    second ingest overwrite the first, and the first site's chemistry stopped being findable at
    all. It is the registry source name — the token in `CHEMCLAW_DATA_SOURCES` — and it is the
    same string `reaction_labels.source` and `reaction_records.ingest_source` carry, so the four
    indexes an ingest writes agree on what a source is.

    **Empty is the right answer for an index whose ids are already global**, which is why it
    defaults to it rather than being required: a molecule record's id is its standardized SMILES,
    and two sources charging the same molecule *must* land on one row — splitting that index by
    source would duplicate every shared structure and answer "have we made this?" per site.
    """

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    bits: str = Field(min_length=1)
    definition: str = ""
    source: str = ""


class Match(BaseModel):
    """A structural-search hit: the entity, its Tanimoto similarity, and which corpus it is from.

    `source` carries the ingest source that stored the record, empty for an index whose ids are
    global (see `FingerprintRecord.source`). It is on the hit because a hit is what a citation is
    spelled from: with two sites behind one entry id, `note_id_for_reaction(hit.id)` alone names
    both runs and neither, and only the search knows which one it matched.
    """

    id: str
    label: str
    similarity: float
    source: str = ""


HitT = TypeVar("HitT", bound=BaseModel)

#: How many citation-only records a verdict names by id. A pointer to read, not a result page: the
#: model is sent to `expand_note` for each, so a handful is what a turn can act on, and the count
#: beside it says how many more there are.
CITATION_ONLY_NAMED_MAX = 5


class CitationOnlyRecords(BaseModel):
    """The ELN records no structure index holds, and which of them give the queried structure.

    **Why every structural answer carries this.** A citation-only record — the source named at
    least one species without its structure — contributes no fingerprint, molecule or label row,
    not even for the species it *does* draw
    (`D-2026-09-27-a-reaction-without-a-structure-is-citable-not-searchable`, step 4). That is the
    decided tier and it is unchanged here. What was wrong is that no verdict said so: once the
    indexes were complete, "COMPLETE: all 4282 …" and "a genuine negative result" told the model
    the *corpus* had been searched, and it reported "no in-house data" for 6-iodoquinoline while
    `reaction-suzuki-flow-hte-01243` (6-iodoquinoline drawn, its boronic-acid partner only named,
    67.76 %) sat outside the denominator. So the denominator is now stated as what it is.

    `giving_query` is a **text check, not a structure search**: it counts citation-only records
    whose body lists one of the query's spellings (as given, RDKit-canonical, standardized) as a
    drawn species. That keeps the tier out of every index, as decided, and points the model to the
    one route the tier has — citation through `expand_note`. Its blind side is said in the verdict:
    a source that spelled the structure differently, or a substructure, is not found by it.

    `outside_index` and `giving_query` count records that are not withdrawn by their source, since
    those are the only ones a chemist could be told to cite.
    """

    model_config = ConfigDict(frozen=True)

    outside_index: int = Field(
        ge=0,
        description="Citation-only records on file (not withdrawn) that no structure index holds.",
    )
    query_checked: bool = Field(
        default=False,
        description="Whether the query's spellings were looked for among their drawn species.",
    )
    giving_query: int = Field(
        default=0, ge=0, description="How many of them list a spelling of the query as drawn."
    )
    examples: list[str] = Field(
        default_factory=list,
        description=f"Note ids of up to {CITATION_ONLY_NAMED_MAX} of those, for `expand_note`.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The clause a structural verdict appends; `""` when no record sits outside the index.

        A `computed_field` so it is serialized with the result (see
        `FingerprintSearch._indexed_verdict`).
        """
        if self.outside_index == 0:
            return ""
        outside = (
            f"NOT SEARCHED: {self.outside_index} citation-only ELN record(s) are outside this "
            "search — the source named at least one of their species without a structure, so none "
            "of them is fingerprinted or labelled, and no structure or similarity search reaches "
            "them, not even through the species they do draw. This answer says nothing about them."
        )
        if not self.query_checked:
            return outside
        if self.giving_query:
            named = ", ".join(self.examples)
            more = (
                f" (and {self.giving_query - len(self.examples)} more)"
                if self.giving_query > len(self.examples)
                else ""
            )
            return (
                f"{outside} {self.giving_query} of them list the queried structure as a drawn "
                f"species: {named}{more}. They are in-house evidence for this question: read them "
                "with expand_note and cite them for what they state (yields, conditions, the "
                "species they name). Do NOT report that no in-house precedent exists."
            )
        return (
            f"{outside} None of them lists the queried structure under the spellings checked (as "
            "given, canonical and standardized) — a text check, so a record spelling it "
            "differently, or containing it only as a substructure, is not ruled out."
        )


class FingerprintSearch(BaseModel, Generic[HitT]):
    """One search over a fingerprint index: the hits, **and whether the index could answer**.

    Why this is not a bare `list`: an empty list meant two things a chemist must never see
    conflated — "we have no precedent for this structure" and "nothing has been indexed". A live
    run hit exactly that (`docs/archive/live-grounded-2026-08-03.md`, finding 6): 1,025 notes were
    indexed, the fingerprint tables were never backfilled, and `similar_reactions` answered
    `{"result": []}` — read by the model, and then by the chemist, as "we have never made anything
    like this". On the one tool whose entire job is "have we seen this before", an unanswerable
    question must not render as a negative answer.

    Generic over the hit type because both fingerprint domains need the same distinction and their
    hits differ (`MoleculeHit` cites a compound note, `Match` carries a reaction's index id).
    """

    subject: Subject
    hits: list[HitT] = Field(default_factory=list)
    # Whether the index answered approximately (`fingerprint_search_exactness`). Then an empty
    # result is not proof of absence, since only an index-proposed candidate set was scanned, so the
    # payload must say so.
    approximate: bool = False
    # True only when the index holds nothing searchable — never a hit list that merely came back
    # short. Probed (cheaply) at the one moment it can change the meaning of the result: no hits.
    index_empty: bool = False
    # True when the index also holds records under a superseded fingerprint definition, which this
    # search could not compare. Mid-rebuild, one rebuilt row makes `index_empty` False while most of
    # the corpus is invisible. A boolean, not a count, because it is read on every search; the count
    # goes to the connector log.
    #
    # It stays True after a finished rebuild, because the superseded generation is shelved until an
    # operator disposes of it, so the clause says the page *may* be a fraction of the corpus.
    index_partial: bool = False
    # The two ways a search stops early, carried in the payload so the model sees them.
    # `scan_truncated`: not every stored record was examined (record cap, or an unparseable stored
    # structure), so an empty result is not evidence of absence. `hits_truncated`: more matched than
    # the page holds, so the count is a floor. Every search that can truncate sets them.
    scan_truncated: bool = False
    hits_truncated: bool = False
    # The ELN records no fingerprint index holds (citation-only), and which of them list the queried
    # structure. `None` means the caller did not ask; every MCP tool serving this model asks.
    unsearched: CitationOnlyRecords | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """`_indexed_verdict`, with what the index cannot hold said beside it.

        When a citation-only record lists the queried structure, its clause comes first, so a model
        that stops reading early does not write "no in-house precedent" about a run on file.
        """
        indexed = self._indexed_verdict()
        outside = self.unsearched.verdict if self.unsearched is not None else ""
        if not outside:
            return indexed
        if self.unsearched is not None and self.unsearched.giving_query:
            return f"{outside} {indexed}"
        return f"{indexed} {outside}"

    def _indexed_verdict(self) -> str:
        """The one sentence the model must read about what the index itself answered.

        A `computed_field`, not a bare `property`, because a plain property is not serialized:
        `model_dump()` would drop the sentence that explains an empty result. The tool docstring is
        read once; the result payload is what sits in context when the answer is written.

        It says "a genuine negative result" only when the index was populated, fully scanned, fully
        comparable and searched exactly. Truncation, a partial index and approximation are
        independent qualifications and are stated together.
        """
        if self.index_empty:
            return (
                f"SEARCH NOT RUN: the {self.subject} fingerprint index is empty — it holds no "
                "searchable record, so the query was compared against nothing. This is NOT "
                f"evidence that no similar {self.subject} exists; the question was not answered. "
                "Report that the fingerprint index has not been built and that an operator must "
                "populate it. Do not say that nothing similar was found."
            )
        if not self.hits:
            caveats = self._incomplete_clauses()
            if not caveats:
                return (
                    f"No indexed {self.subject} matched this query. The {self.subject} fingerprint "
                    "index holds records and was searched exactly — every stored record was "
                    "compared — so this is a genuine negative result over the indexed records."
                )
            return " ".join(
                [
                    "SEARCH INCOMPLETE: nothing that was compared matched this query, and not "
                    f"every stored {self.subject} was compared. This is NOT evidence that no such "
                    f"{self.subject} exists — report the search as inconclusive.",
                    *caveats,
                ]
            )
        matched = f"{len(self.hits)} indexed {self.subject}(s) matched this query."
        # Independent facts, so independent clauses, not exclusive branches: truncation is about
        # count, a superseded fraction about what was compared, approximation about ranking.
        # `hits_truncated` is the ordinary outcome (`find_matches` asks for `k + 1`), so a branch on
        # it would hide the others. A partial index shares "PARTIAL" with truncation because both
        # make the page a lower bound.
        lower_bound = self.scan_truncated or self.hits_truncated or self.index_partial
        labels = ["PARTIAL" if lower_bound else "", "APPROXIMATE" if self.approximate else ""]
        heading = " AND ".join(label for label in labels if label)
        if not heading:
            return matched
        return " ".join([f"{heading} RESULT: {matched}", *self._incomplete_clauses()])

    def _incomplete_clauses(self) -> list[str]:
        """Every reason this search saw less than the whole index, one clause each.

        One list for both the hits and no-hits arms so neither can shadow a fact with another.
        Ordered by how much the reader is missing: a superseded fraction first, as the largest and
        the only one an operator can fix.
        """
        clauses: list[str] = []
        if self.index_partial:
            clauses.append(
                f"Part of the {self.subject} index is stored under a SUPERSEDED fingerprint "
                "definition and was not compared at all, so the query may have been answered over "
                "the re-indexed fraction of the corpus rather than over the corpus. Report the "
                "search as answered over a possibly incomplete index and that an operator must "
                "check it (the connector log says how many records are under which definition, "
                "and whether what remains is a rebuild to finish or a superseded generation to "
                "dispose of)."
            )
        if self.scan_truncated or self.hits_truncated:
            cap = "record cap" if self.scan_truncated else "result cap"
            clauses.append(
                f"The scan stopped early ({cap}), so this is a lower bound and further matches "
                "may exist. Do not report it as the complete set."
                + (
                    " An operator must raise the scan cap or repair the index (the connector log "
                    "names which)."
                    if self.scan_truncated
                    else ""
                )
            )
        if self.approximate:
            clauses.append(
                f"This deployment searches the {self.subject} index APPROXIMATELY — it ranks "
                "candidates proposed by the similarity index rather than comparing every stored "
                "record — so what came back is what the index proposed rather than provably the "
                "best on file, and a closer one may exist. This is NOT proof that no closer "
                f"{self.subject} is on file; an exact search would be needed to rule a precedent "
                "out."
            )
        return clauses


@runtime_checkable
class FingerprintStore(Protocol):
    """Persistence + similarity-search contract. Backends implement this."""

    @property
    def approximate(self) -> bool:
        """Whether `find_similar` may miss a true neighbour — the property, not the technique.

        A property of the index this store is bound to, not of one query; entry points copy it onto
        `FingerprintSearch.approximate` so the answer carries it.
        """
        ...

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by its key.

        The key is `(source, id)` for an index whose ids come from outside this system, and the
        bare id everywhere else — see `FingerprintRecord.source`.
        """
        ...

    async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
        """Insert or replace a batch of fingerprints, atomically where the backend can.

        On the interface because the saving (one connection and one commit per batch) is the
        backend's and invisible from outside.
        """
        ...

    async def all_records(self, limit: int | None = None) -> list[FingerprintRecord]:
        """Return stored records (used for substructure scans); at most `limit` when set.

        When `limit` is set the rows are the first `limit` in deterministic id order, so a
        bounded scan is reproducible across backends.
        """
        ...

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first."""
        ...

    async def is_empty(self) -> bool:
        """Whether this store holds nothing it could search — asked only when a search found none.

        Not `count() == 0`: this runs on the no-hits path of every search, and existence stops at
        the first row.
        """
        ...

    async def count(self) -> int:
        """How many records this store can search — the operator-facing number, not a hot path.

        Separate from `is_empty` because a half-finished backfill looks healthy to a boolean.
        """
        ...

    async def has_superseded_records(self) -> bool:
        """Whether the table holds a record this store's definition cannot compare — asked always.

        Runs on every similarity search, since a partly re-indexed index answers over a fraction of
        the corpus, so a backend must answer it without a scan.
        """
        ...

    async def superseded_count(self) -> int:
        """How many records are stored under a definition this store cannot search.

        The operator's number, paid once per process beside `count`. Stays non-zero after a finished
        rebuild because the superseded generation is shelved, not overwritten.
        """
        ...


class InMemoryFingerprintStore:
    """Process-local `FingerprintStore` — the reference the SQL one is written to match.

    A differential oracle, not a deployment backend: no configuration returns it
    (`tests/test_reference_stores.py` holds that). It ranks exact Tanimoto with the same threshold
    and tie-break as the Postgres backend.

    Keyed by `(source, id, definition)` like the durable table: re-adding a record under one
    definition replaces it, a second source with the same id gets its own row, and a second
    definition shelves the generation it supersedes. With no source and one definition this is
    keying by id.
    """

    def __init__(self, definition: str | None = None) -> None:
        """Start with an empty index.

        If `definition` is set, similarity search returns only records built under it (the durable
        store's cross-definition guard). `None` ranks every record, for an ephemeral index populated
        in one configuration.
        """
        self._records: dict[tuple[str, str, str], FingerprintRecord] = {}
        self._definition = definition

    @property
    def approximate(self) -> bool:
        """Never — this backend scores every searchable record."""
        return False

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by `(source, id, definition)`, superseding its twin.

        A sourced write drops the unsourced twin of its own definition, so a migrated index does not
        hold one entry twice; rows under another definition are shelved generations, not twins.
        """
        self._records[(record.source, record.id, record.definition)] = record
        if record.source:
            self._records.pop(("", record.id, record.definition), None)

    async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
        """Insert or replace a batch — `add` per record, since there is nothing here to batch.

        Delegating keeps this class's supersede rule in one place. The Postgres store deliberately
        does not drop the unsourced twin (no DELETE privilege), so the backends differ there.
        """
        for record in records:
            await self.add(record)

    async def all_records(self, limit: int | None = None) -> list[FingerprintRecord]:
        """Return stored records; at most `limit` (first in id order) when set.

        Unbounded keeps insertion order; bounded sorts by `(source, id)` first to match the Postgres
        `ORDER BY ... COLLATE "C" LIMIT` (code-point order equals UTF-8 byte order). One row per
        `(source, id)` across shelved generations, preferring this store's definition, matching the
        durable backend's `DISTINCT ON`.
        """
        records = self._one_per_key()
        if limit is None:
            return records
        return sorted(records, key=lambda r: (r.source, r.id))[:limit]

    def _one_per_key(self) -> list[FingerprintRecord]:
        """The generation this store shows for each `(source, id)` — the `DISTINCT ON` in Python.

        This store's own definition where held, the first-added generation otherwise; a store
        pinning no definition keeps the first row. One pass over the records.
        """
        chosen: dict[tuple[str, str], FingerprintRecord] = {}
        for (source, id_, _), record in self._records.items():
            held = chosen.get((source, id_))
            if held is None or record.definition == self._definition != held.definition:
                chosen[(source, id_)] = record
        return list(chosen.values())

    def _searchable(self) -> list[FingerprintRecord]:
        """The records this store may rank: its own definition's, or all when it pins none.

        The one definition of "in this index", so `find_similar`, `is_empty` and `count` cannot
        disagree.
        """
        return [
            r
            for r in self._records.values()
            if self._definition is None or r.definition == self._definition
        ]

    async def is_empty(self) -> bool:
        """Whether nothing here is searchable under this store's definition."""
        return not self._searchable()

    async def count(self) -> int:
        """How many records are searchable under this store's definition."""
        return len(self._searchable())

    def _superseded(self) -> list[FingerprintRecord]:
        """The records held here that this store's definition cannot compare against.

        The complement of `_searchable`; a store pinning no definition has nothing superseded.
        """
        if self._definition is None:
            return []
        return [r for r in self._records.values() if r.definition != self._definition]

    async def has_superseded_records(self) -> bool:
        """Whether anything here is stored under a definition this store cannot compare."""
        return bool(self._superseded())

    async def superseded_count(self) -> int:
        """How many records here are stored under a definition this store cannot compare."""
        return len(self._superseded())

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Rank stored records by Tanimoto to `query_bits`, filtered and truncated.

        Records under another definition are excluded. Ties break by `(source, id)` in code-point
        order, matching the Postgres `ORDER BY similarity DESC, source COLLATE "C", id COLLATE "C"`.
        The query is parsed once; each record's width is checked before it is scored.
        """
        query = int(query_bits, 2)
        scored = []
        for record in self._searchable():
            if len(record.bits) != len(query_bits):
                raise FingerprintError("cannot compare fingerprints of different widths")
            scored.append(
                Match(
                    id=record.id,
                    label=record.label,
                    similarity=tanimoto_bits(query, int(record.bits, 2)),
                    source=record.source,
                )
            )
        hits = [m for m in scored if m.similarity >= threshold]
        hits.sort(key=lambda m: (-m.similarity, m.source, m.id))
        return hits[:top_k]


def _matches_from(rows: Sequence[TupleRow]) -> list[Match]:
    """Turn `(source, id, label, similarity)` rows into hits — the one row shape both arms return.

    Shared so a hit never differs by which similarity arm found it.
    """
    return [Match(source=r[0], id=r[1], label=r[2], similarity=float(r[3])) for r in rows]


class PostgresFingerprintStore:
    """Durable `FingerprintStore` backed by Postgres + pgvector, over one table.

    Table and bit width are constructor parameters, so one class serves the molecule and reaction
    tables. Similarity is Tanimoto (1 - Jaccard distance) in SQL.

    A deployment picks one of two similarity searches (`fingerprint_search_exactness`, default
    `exact`), written as separate statements because they answer different questions:

    - **exact** (`_find_similar_exact`) compares the query against every row under this definition.
      Its `WHERE` predicates keep the planner off the HNSW index, so it returns the true top-k at a
      cost linear in corpus size.
    - **approximate** (`_find_similar_approximate`) asks the HNSW index for `top_k x
      fingerprint_approximate_overfetch` candidates, then applies definition scope, threshold and
      the exact tie-break to those. Roughly flat in corpus size, but it can disagree with exact on
      ties, which Tanimoto over sparse bits produces often.

    Every search reports which arm ran (`approximate`), because under the second an empty result is
    not evidence of absence.

    `source_keyed` says whether the table carries the `source` half of the key
    (`reaction_fingerprints` does, `molecule_fingerprints` must not). It only changes the key
    columns; reads project a constant `''` for a table without the column, so rows reach Python in
    one shape.
    """

    def __init__(
        self,
        table: str,
        width: int,
        definition: str,
        dsn: str | None = None,
        *,
        source_keyed: bool = False,
    ) -> None:
        """Bind to `table` with fingerprint `width` and `definition`, on the configured DSN.

        `table` and `width` are trusted internal constants interpolated into SQL; the identifier
        check enforces that boundary. A `width` that disagrees with the `bit(N)` column fails loudly
        in Postgres.

        `definition` is the fingerprint-parameter signature (e.g. `ecfp:r2:b2048`). Every row
        records it and similarity search filters to it, so incomparable same-width bits are never
        ranked together. It is part of the table key, so a write under a second definition shelves
        beside the first instead of overwriting it, and two writers with different definitions do
        not evict each other.

        `source_keyed` must match the table: it decides the `ON CONFLICT` target, and a wrong key
        fails to plan rather than silently mis-keying.
        """
        if not table.isidentifier():
            raise ValueError(f"table must be a plain SQL identifier, got {table!r}")
        self._table = table
        self._definition = definition
        self._source_keyed = source_keyed
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        # A projection, not a filter: a table without the column answers a constant, so readers
        # unpack the same positions either way.
        self._source_read = "source" if source_keyed else "''"
        insert_columns = "source, id" if source_keyed else "id"
        insert_values = "%(source)s, %(id)s" if source_keyed else "%(id)s"
        # The definition is in the key, so a second definition inserts beside the first; a re-write
        # under one definition updates in place.
        conflict = "(source, id, definition)" if source_keyed else "(id, definition)"
        # Ties break by the whole key, matching the in-memory backend's `(source, id)` sort.
        self._order = 'source COLLATE "C", id COLLATE "C"' if source_keyed else 'id COLLATE "C"'
        self._upsert = (
            f"INSERT INTO {table} ({insert_columns}, label, bits, definition) "
            f"VALUES ({insert_values}, %(label)s, %(bits)s::bit({width}), %(definition)s) "
            f"ON CONFLICT {conflict} DO UPDATE SET "
            f"label = EXCLUDED.label, bits = EXCLUDED.bits"
        )
        # No statement here deletes an unsourced twin: the runtime role has no DELETE on this table
        # (`app_privileges.sql`), so it would fail every sourced write. An unsourced row is also not
        # reliably a twin; it may be another site's only fingerprint.

        # One row per key, not per stored generation. The substructure scan reads this unfiltered by
        # definition (a superseded row's SMILES is still a correct hit), so without de-duplication a
        # shelved molecule would be reported twice and the record cap would cover half the
        # molecules. `DISTINCT ON` prefers this store's definition, matching the in-memory backend.
        self._all = (
            f"SELECT DISTINCT ON ({self._order}) "
            f"{self._source_read}, id, label, bits::text, definition FROM {table} "
            f"ORDER BY {self._order}, (definition = %(definition)s) DESC"
        )
        # Scoped to this store's definition: superseded rows are not searchable here, so counting
        # them would report a populated index whose searches return nothing.
        self._exists = f"SELECT 1 FROM {table} WHERE definition = %(definition)s LIMIT 1"
        self._count = f"SELECT count(*) FROM {table} WHERE definition = %(definition)s"
        # The complement, as two statements because only one may scan. `_superseded_count` is the
        # operator's, run once per process. `_definition_extremes` runs on every search: `min`/`max`
        # over `<table>_definition_idx` are two index probes, and extremes both equal to this
        # store's definition prove nothing else is stored. A `WHERE definition <> ... LIMIT 1` would
        # read every row on a healthy index.
        self._superseded_count = f"SELECT count(*) FROM {table} WHERE definition <> %(definition)s"
        self._definition_extremes = f"SELECT min(definition), max(definition) FROM {table}"
        # `<%%>` is pgvector's Jaccard-distance operator (`%` doubled for psycopg). Threshold and
        # definition filter first, then rank and truncate, like the in-memory backend. Ties break by
        # id under `COLLATE "C"` (byte order) to match Python's code-point sort regardless of the
        # database collation. These predicates also keep the planner off the HNSW index. Hoisting
        # the distance into a subquery is slower because it materializes.
        self._similar_exact = (
            f"SELECT {self._source_read}, id, label, "
            f"1 - (bits <%%> %(q)s::bit({width})) AS similarity "
            f"FROM {table} "
            f"WHERE definition = %(definition)s "
            f"AND 1 - (bits <%%> %(q)s::bit({width})) >= %(threshold)s "
            f"ORDER BY bits <%%> %(q)s::bit({width}), {self._order} "
            f"LIMIT %(k)s"
        )
        # The approximate arm. The inner query is a bare `ORDER BY <distance> LIMIT`, the only form
        # the HNSW index can serve; definition and threshold filters sit outside it, because inside
        # they would defeat the index scan. The cost: superseded rows consume candidate slots, so
        # each shelved generation divides the effective over-fetch until an operator disposes of it.
        # The outer `ORDER BY` repeats the exact tie-break.
        candidate_columns = "source, id, label" if source_keyed else "id, label"
        self._similar_approximate = (
            f"SELECT {self._source_read}, id, label, 1 - distance AS similarity FROM ("
            f"SELECT {candidate_columns}, definition, "
            f"bits <%%> %(q)s::bit({width}) AS distance "
            f"FROM {table} ORDER BY bits <%%> %(q)s::bit({width}) LIMIT %(candidates)s"
            ") candidates "
            f"WHERE definition = %(definition)s AND distance <= 1 - %(threshold)s "
            f"ORDER BY distance, {self._order} "
            f"LIMIT %(k)s"
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled when the process opened a pool (`chemclaw.core.db.pooling`), otherwise a dedicated
        connect. An unreachable database reports "Postgres unreachable at <host>", and a hung query
        is cancelled.
        """
        async with db.connection(self._dsn) as conn:
            yield conn

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by this table's key, in one transaction.

        The single-record case of `add_many`, so the upsert and the source refusal have one
        definition.
        """
        await self.add_many([record])

    async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
        """Insert or replace a batch on one connection, in one transaction.

        A record carrying a source on a store that is not `source_keyed` is refused, since the value
        would otherwise be dropped silently; every record is checked before the connection is taken,
        so a bad batch writes nothing. An empty batch takes no connection.

        Bulk-load cost is dominated by maintaining the live HNSW index, not by round trips; the
        drop-load-rebuild path needs DDL this role does not hold.
        """
        if not records:
            return
        for record in records:
            if record.source and not self._source_keyed:
                raise FingerprintError(
                    f"{self._table} is not keyed by source, so a record from {record.source!r} "
                    "cannot be stored in it without losing which corpus it came from"
                )
        async with self._connection() as conn:
            for record in records:
                await conn.execute(
                    self._upsert,
                    {
                        "id": record.id,
                        "label": record.label,
                        "bits": record.bits,
                        "definition": record.definition,
                        **({"source": record.source} if self._source_keyed else {}),
                    },
                )
            await conn.commit()

    async def all_records(self, limit: int | None = None) -> list[FingerprintRecord]:
        """Return stored records (bits as text), regardless of definition; capped at `limit`.

        Unfiltered by definition because the only consumer is substructure search, which re-matches
        the stored SMILES label and never reads the bits; one row per key, so a shelved generation
        does not appear twice. With `limit`, the scan is a deterministic `ORDER BY <key> LIMIT`
        slice served by an index in `COLLATE "C"` order, so it streams rather than sorting the
        table. Unbounded, it sorts the table.

        The `bits` column is the bulk of the transfer and the substructure scan does not need it;
        removing it is the caller's job.
        """
        params: dict[str, object] = {"definition": self._definition}
        sql = self._all if limit is None else f"{self._all} LIMIT %(limit)s"
        if limit is not None:
            params["limit"] = limit
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        return [
            FingerprintRecord(source=r[0], id=r[1], label=r[2], bits=r[3], definition=r[4])
            for r in rows
        ]

    async def is_empty(self) -> bool:
        """Whether this table holds no row under this store's definition.

        `SELECT 1 ... LIMIT 1`, not `count(*)`: it runs on the no-hits path of a live search.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._exists, {"definition": self._definition})
                return await cur.fetchone() is None

    async def count(self) -> int:
        """Exact number of searchable rows — the operator's number (see the protocol's note)."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._count, {"definition": self._definition})
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def has_superseded_records(self) -> bool:
        """Whether any row is under another definition — two index probes, not a scan.

        The extremes of `definition` equal this store's definition if and only if nothing else is
        stored; a `NULL` min is an empty table.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._definition_extremes)
                row = await cur.fetchone()
        if row is None or row[0] is None:
            return False
        return bool(row[0] != self._definition or row[1] != self._definition)

    async def superseded_count(self) -> int:
        """Exact number of rows this store's definition cannot compare — the operator's number."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._superseded_count, {"definition": self._definition})
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    @property
    def approximate(self) -> bool:
        """Whether this deployment's similarity search may miss a true neighbour.

        Read per call, not frozen at construction, so this property and `find_similar` cannot
        disagree about which arm ran.
        """
        return settings.fingerprint_search_exactness == "approximate"

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first.

        The one place the deployment's exactness choice is read. Both arms honour the same
        threshold, ordering and tie-break; they differ in whether they rank every record.
        """
        if self.approximate:
            return await self._find_similar_approximate(query_bits, top_k, threshold)
        return await self._find_similar_exact(query_bits, top_k, threshold)

    async def _find_similar_exact(
        self, query_bits: str, top_k: int, threshold: float
    ) -> list[Match]:
        """Rank every stored record under this definition — the true top-k, at a linear cost."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._similar_exact,
                    {
                        "q": query_bits,
                        "threshold": threshold,
                        "k": top_k,
                        "definition": self._definition,
                    },
                )
                rows = await cur.fetchall()
        return _matches_from(rows)

    async def _find_similar_approximate(
        self, query_bits: str, top_k: int, threshold: float
    ) -> list[Match]:
        """Rank an over-fetched HNSW candidate set — flat in corpus size, and not provably top-k.

        Fetches `top_k x fingerprint_approximate_overfetch` candidates so threshold and tie-break
        cut into slack, and raises `hnsw.ef_search` to at least that many, since the graph traversal
        cannot return more than it kept. Both are clamped to pgvector's ceiling.

        `set_config(..., true)` is `SET LOCAL` with a bound parameter, so the setting does not leak
        to later borrowers of the pooled connection; the transaction commit scopes it.
        """
        candidates = min(top_k * settings.fingerprint_approximate_overfetch, _HNSW_MAX_EF_SEARCH)
        ef_search = min(
            max(settings.fingerprint_approximate_ef_search, candidates), _HNSW_MAX_EF_SEARCH
        )
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT set_config('hnsw.ef_search', %s, true)", (str(ef_search),)
                )
                await cur.execute(
                    self._similar_approximate,
                    {
                        "q": query_bits,
                        "threshold": threshold,
                        "k": top_k,
                        "candidates": candidates,
                        "definition": self._definition,
                    },
                )
                rows = await cur.fetchall()
        return _matches_from(rows)


async def find_matches(
    store: FingerprintStore,
    query_bits: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> tuple[list[Match], bool]:
    """Search a store with the configured `top_k`/`threshold` defaults applied.

    Returns the page and whether more records qualified than it holds: one extra row is requested
    and dropped, so a full page is reported as a floor. The flag is exact on exact search; on the
    approximate arm `False` only means the candidate set held no further row, which is why
    `approximate` travels separately.

    The single chokepoint where model-supplied knobs are bounded: `top_k` is clamped to `[1,
    fingerprint_max_top_k]` (it lands in a SQL `LIMIT`) and `threshold` to `[0, 1]` (Tanimoto's
    range). NaN is refused with a plain `ValueError` rather than clamped, because `max`/`min` pass
    NaN through and every comparison with it is False, which would silently report "no precedent"
    for an exact match. It is not a `FingerprintInputError`, which callers treat as an empty answer.
    `±inf` clamps to its nearest bound.
    """
    k = top_k if top_k is not None else settings.fingerprint_top_k
    k = min(max(k, 1), settings.fingerprint_max_top_k)
    t = threshold if threshold is not None else settings.fingerprint_similarity_threshold
    if math.isnan(t):
        raise ValueError(
            "threshold must be a Tanimoto similarity between 0 and 1, but got NaN, which "
            "compares False against every stored fingerprint and would report an empty index "
            "as a genuine negative result; omit it to search at the configured default of "
            f"{settings.fingerprint_similarity_threshold}"
        )
    t = min(max(t, 0.0), 1.0)
    found = await store.find_similar(query_bits, k + 1, t)
    return found[:k], len(found) > k


async def index_is_empty(store: FingerprintStore, hits: Sequence[BaseModel]) -> bool:
    """Whether an empty result means "nothing is indexed" rather than "nothing matched".

    The store is probed only when there are no hits; a search that found something already proved
    the index is populated.
    """
    return not hits and await store.is_empty()


async def index_is_partial(store: FingerprintStore) -> bool:
    """Whether the index also holds records this search could not compare — asked every time.

    Unlike `index_is_empty`, not conditioned on the hit list: a superseded fraction changes the
    reading of a full page as well as an empty one. Affordable because `has_superseded_records` must
    answer without a scan.
    """
    return await store.has_superseded_records()


async def log_index_size(store: FingerprintStore, subject: Subject) -> None:
    """Log how many records a fingerprint index holds — loudly when it holds none.

    Run once at the owning connector's startup so an index that was never backfilled is visible to
    an operator before a chemist meets it. WARNING for an empty index; never fatal, so an
    unreachable database is logged and the server starts anyway.
    """
    try:
        records = await store.count()
        superseded = await store.superseded_count()
    except (ChemclawError, ConnectionError, psycopg.Error) as exc:
        log.warning("cannot report the %s fingerprint index size: %s", subject, exc)
        return
    if not records:
        log.warning(
            "%s fingerprint index is EMPTY: 0 records indexed under the current definition, so "
            "every %s similarity search will report that it could not be answered. `make reindex` "
            "rebuilds the *note* index only — the fingerprint index is populated by the ELN sync "
            "(ElnSyncWorkflow), and rows predating a definition change need re-indexing too "
            "(docs/guides/runbook.md (vi)).%s",
            subject,
            subject,
            _waiting(superseded, subject),
        )
    elif superseded:
        log.warning(
            "%s fingerprint index is PARTIAL: %d record(s) indexed under the current definition "
            "and %d under a superseded one, which no %s search can compare against — so every one "
            "of them says so. Two states look like this and the operator action differs, and the "
            "first number tells them apart because a finished rebuild counts the whole corpus. "
            "UNFINISHED: the searchable corpus is the first number, not the total; finish it by "
            "re-running the sync (docs/guides/runbook.md (vi)). FINISHED, superseded rows shelved: "
            "since 094 a definition change no longer overwrites the rows it retires and the "
            "runtime role holds no DELETE here, so disposing of them is one statement run under "
            "the principal that owns the schema, beside `make db-migrate`.",
            subject,
            records,
            superseded,
            subject,
        )
    else:
        log.info(
            "%s fingerprint index: %d record(s) indexed under the current definition",
            subject,
            records,
        )


def _waiting(superseded: int, subject: Subject) -> str:
    """The clause the EMPTY warning gains when the table is full of superseded rows.

    Appended rather than replacing the warning: the rows exist and need rebuilding, not
    re-ingesting, which is a different operator action.
    """
    if not superseded:
        return ""
    return (
        f" {superseded} record(s) are stored under a superseded definition — the whole {subject} "
        "index is waiting to be rebuilt, not missing."
    )


def default_molecule_store() -> PostgresFingerprintStore:
    """The production molecule (ECFP4) store — one place pairs table, width, and definition."""
    from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition

    return PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )


def default_reaction_store() -> PostgresFingerprintStore:
    """The production reaction (DRFP) store — one place pairs table, width, and definition."""
    from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition

    return PostgresFingerprintStore(
        "reaction_fingerprints",
        settings.drfp_bits,
        reaction_definition(),
        source_keyed=True,
    )
