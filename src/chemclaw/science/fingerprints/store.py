"""Generic fingerprint store — Tanimoto search over any bit-fingerprinted record.

Shared by the molecule (ECFP4) and reaction (DRFP) capabilities: the record shape, the
Tanimoto ranking, the store interface, and both backends are domain-neutral — a record
is an id, a human label (a SMILES or reaction SMILES), and a bit fingerprint. Each domain
supplies only its own fingerprint function, its table, and its bit width. This is the
Rule-of-Three extraction: the second fingerprint domain (reactions) made the duplication
real, so the ranking lives in exactly one place (DRY), just like the calculation store.
"""

import logging
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Generic, Literal, Protocol, TypeVar, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field, computed_field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

log = logging.getLogger(__name__)

# Which corpus a search ran over. A closed set rather than a free string: it is interpolated into
# the sentence the model reads, and there are exactly two fingerprint indexes.
Subject = Literal["molecule", "reaction"]


class FingerprintError(ChemclawError):
    """A fingerprint could not be computed or two fingerprints are incomparable (G4)."""


class FingerprintInputError(FingerprintError):
    """The *string* handed in is not something this domain can fingerprint (G4).

    A subclass rather than a flag, because the two facts `FingerprintError` used to carry are read
    by different callers for opposite reasons. This one is about the caller's own argument — a
    prose sentence where a reaction SMILES was expected, an OCR artefact in an ELN impurity list —
    and answering it with "nothing found" is correct. Its parent also covers the index refusing to
    be searched (`cannot compare fingerprints of different widths`), which is an outage: reported
    as "nothing found" it tells a chemist the company has no precedent, which is the failure
    `tests/test_gather_evidence_outage.py` exists to prevent.

    So a caller that means "a bad query is an empty answer" catches *this*; a caller that catches
    the parent is saying it can absorb a broken index too, and has to mean it.
    """


def tanimoto(bits_a: str, bits_b: str) -> float:
    """Tanimoto (Jaccard) similarity of two equal-length fingerprint bitstrings.

    `intersection / union` of set bits; two all-zero fingerprints are defined as 0.0
    (no shared structure). Works on the stored bitstrings directly, so the in-memory
    backend ranks without the source cheminformatics library — the same ordering the
    Postgres backend produces in SQL. (The all-zero case is a guard: a fingerprint from a
    real molecule/reaction always sets at least one bit, where pgvector's Jaccard would
    otherwise return NaN and the two backends could differ.)
    """
    if len(bits_a) != len(bits_b):
        raise FingerprintError("cannot compare fingerprints of different widths")
    return tanimoto_bits(int(bits_a, 2), int(bits_b, 2))


def tanimoto_bits(a: int, b: int) -> float:
    """Tanimoto of two fingerprints already parsed into ints — the scoring half of `tanimoto`.

    **Split out because the parse, not the popcount, is what a pairwise sweep repeats.** Each
    bitstring is 2,048 characters, and `int(bits, 2)` over one costs more than the two `bit_count`s
    that follow it — so a clustering pass over n fingerprints parsed n² strings to make n²/2
    comparisons of n distinct values (`memory/similarity.cluster_by_similarity`), and a query over a
    store re-parsed the *query* once per stored record. Callers that hold many comparisons parse
    once and call this; `tanimoto` stays the two-string form for everyone else.

    No width check here: an int has no width, so the only place that check can be made is where the
    strings still are. Callers of this form are pre-parsing their own equal-width corpus.
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
    # True only when the index holds nothing searchable — never a hit list that merely came back
    # short. Probed (cheaply) at the one moment it can change the meaning of the result: no hits.
    index_empty: bool = False
    # The two ways a search can stop early, carried in the payload for the same reason
    # `index_empty` is: a truncation known only to the log cannot reach the model that writes the
    # answer. `scan_truncated` = not every stored record was examined — the record cap cut the scan
    # short, or a row's stored structure no longer parses and could not be matched — so an empty
    # result is not evidence of absence; `hits_truncated` = more matched than the result list could
    # hold (so the count is a floor, not a total). Both default False, which is the *common* case
    # and not a convention the entry points may lean on: every search that can truncate sets them,
    # similarity search included — its page cap is `fingerprint_top_k`, and a page of 10 out of 18
    # read as a total for exactly as long as this comment said the omission was correct.
    scan_truncated: bool = False
    hits_truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence the model must read before it writes an answer.

        `computed_field`, not a bare `property`, and that is the whole point of this method. A
        plain property is *not serialized*: `model_dump()` would return `{"subject": …, "hits": [],
        "index_empty": true}` and the sentence explaining what that means would never leave this
        process. **This is the in-tree statement of that lesson, and three other `summary` fields
        cite it.** It was learned on the hazard screen — `ScreenResult.verdict`, now
        `Chemclaw3-mcp:servers/safety/src/chemclaw_mcp_safety/engine/screen.py` — which carried
        its "this is not a safety assessment" disclaimer as a bare property for exactly this
        reason and had **zero** production callers, until a live run showed a chemist being told
        "no hazards detected" six times. The tool docstring is read once when the tool is defined;
        the result payload is what sits in the context window when the answer is written.

        Truncation is answered here for the same reason emptiness is: a scan the record cap cut
        short returned `hits: []`, `index_empty: false` and the sentence "this is a genuine
        negative result" over a corpus whose one match it had never looked at.
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
            if self.scan_truncated:
                return (
                    f"SEARCH INCOMPLETE: not every stored {self.subject} was examined — the scan "
                    "stopped at its record cap, or a stored record could not be read — and "
                    "nothing that was examined matched. This is NOT evidence that no such "
                    f"{self.subject} exists. Report the search as inconclusive and say an operator "
                    "must raise the scan cap or repair the index (the connector log names which)."
                )
            return (
                f"No indexed {self.subject} matched this query. The {self.subject} fingerprint "
                "index holds records and was searched, so this is a genuine negative result."
            )
        matched = f"{len(self.hits)} indexed {self.subject}(s) matched this query."
        if self.scan_truncated or self.hits_truncated:
            return (
                f"PARTIAL RESULT: {matched} The scan stopped early "
                f"({'record cap' if self.scan_truncated else 'result cap'}), so this is a lower "
                "bound and further matches may exist. Do not report it as the complete set."
            )
        return matched


@runtime_checkable
class FingerprintStore(Protocol):
    """Persistence + similarity-search contract. Backends implement this."""

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by its key.

        The key is `(source, id)` for an index whose ids come from outside this system, and the
        bare id everywhere else — see `FingerprintRecord.source`.
        """
        ...

    async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
        """Insert or replace a batch of fingerprints, atomically where the backend can.

        On the interface rather than left to each caller looping over `add`, because the cost that
        makes it worth having is the backend's and invisible from outside: the Postgres store takes
        a pooled connection and commits per call. Measured against a live database inside
        `db.pooling()`, 200 rows, three trials: **3.0 ms/row** one at a time against **1.15 ms/row**
        batched, a stable 2.6x. `CorpusMolecules.add_many` is the same method for the same reason
        one table over; this is the half `FingerprintStore` was missing.

        **The commit is the part this removes, and it is not the whole cost** — 1.15 ms/row remains,
        so the 13M-row corpus `ingest/labels/corpus.py` sizes against is ~4 hours of writes either
        way rather than ~11. Worth saying, because a reader who took this for the fix to bulk-load
        throughput would be surprised; what it buys is the 2.6x, not a different order of magnitude.
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

        Deliberately *not* `count() == 0`, which is the same question asked expensively: this runs
        on the "no hits" path of every similarity search, and an exact count over a million-row
        fingerprint table is a sequential scan. Existence stops at the first row.
        """
        ...

    async def count(self) -> int:
        """How many records this store can search — the operator-facing number, not a hot path.

        Separate from `is_empty` because it answers a different question for a different reader: an
        operator needs "3 of 10,000 reactions are indexed" (a half-finished backfill looks exactly
        like a healthy one to a boolean), and pays for the scan once per process start.
        """
        ...


class InMemoryFingerprintStore:
    """Process-local `FingerprintStore` for tests and single-run use.

    Computes exact Tanimoto ranking without a database — the reference the Postgres
    backend matches (same threshold and tie-break, exactly for small corpora, up to HNSW
    recall for large ones). Keyed by `(source, id)`, so re-adding one source's record replaces
    it and a second source using the same entry id gets its own row (D-2026-08-27) — the same
    key the reaction table carries. An index whose records all leave `source` empty (every
    molecule index, and every ephemeral one built in a test) behaves exactly as it did when the
    key was the bare id, because the first half of the pair is then constant.
    """

    def __init__(self, definition: str | None = None) -> None:
        """Start with an empty index.

        If `definition` is set, similarity search returns only records built under that
        same fingerprint definition — the durable store's cross-definition guard, made
        testable without a database. Left `None` it ranks every record, which is correct
        for an ephemeral index always populated in a single configuration (tests, demo).
        """
        self._records: dict[tuple[str, str], FingerprintRecord] = {}
        self._definition = definition

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by `(source, id)`, superseding its unsourced twin.

        The supersede is the in-memory half of the durable store's, and it is what keeps a
        migrated index from holding one entry twice — see `PostgresFingerprintStore.add` for the
        row it is about. Here it costs one dict lookup and keeps the two backends' contents
        identical, which is the property every ordering assertion in this module rests on.
        """
        self._records[(record.source, record.id)] = record
        if record.source:
            self._records.pop(("", record.id), None)

    async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
        """Insert or replace a batch — `add` per record, since there is nothing here to batch.

        Delegating rather than writing the dict again, so this backend's supersede rule has one
        definition. **Not a parity argument**, which is what an earlier draft of this docstring
        claimed: the two backends genuinely differ here. `add` above pops the unsourced twin;
        `PostgresFingerprintStore` deliberately does not, because the runtime role holds no `DELETE`
        on that table and an unsourced row is not reliably a twin — the constructor there says so at
        length. Writing that rule twice *within this class* would still be two places to change.
        """
        for record in records:
            await self.add(record)

    async def all_records(self, limit: int | None = None) -> list[FingerprintRecord]:
        """Return stored records; at most `limit` (first in id order) when set.

        Unbounded (`limit=None`) keeps insertion order for byte-identical legacy behavior; a
        bounded scan sorts by `(source, id)` first so the truncated slice matches the Postgres
        backend's `ORDER BY source COLLATE "C", id COLLATE "C" LIMIT` (Python's code-point sort
        equals byte order under UTF-8, which is order-preserving — the default database collation
        is not). On an index whose records carry no source that is the previous ordering exactly,
        because a constant leading key changes nothing.
        """
        records = list(self._records.values())
        if limit is None:
            return records
        return sorted(records, key=lambda r: (r.source, r.id))[:limit]

    def _searchable(self) -> list[FingerprintRecord]:
        """The records this store may rank: its own definition's, or all when it pins none.

        One place decides what "in this index" means, so `find_similar`, `is_empty` and `count`
        cannot disagree — an index full of stale-definition rows is unsearchable, and reporting it
        as populated would be the same lie in a different place.
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

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Rank stored records by Tanimoto to `query_bits`, filtered and truncated.

        Records whose definition differs from this store's (when one is set) are excluded —
        their equal-width bits are not comparable. Ties break by `(source, id)` so the ordering
        is deterministic and matches the Postgres `ORDER BY similarity DESC, source COLLATE "C",
        id COLLATE "C"` (code-point order — the locale-independent ordering both backends can
        share).

        The query is parsed once rather than once per record, which is what `tanimoto`'s two-string
        form would have done here — see `tanimoto_bits`. The width check moves with it: it is made
        against each record's string before that record is scored, so a mismatched width still
        raises, and still names the same failure.
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


class PostgresFingerprintStore:
    """Durable `FingerprintStore` backed by Postgres + pgvector, over one table.

    Table and bit width are constructor parameters (both trusted internal constants), so
    the same class serves the molecule and reaction fingerprint tables. Similarity is
    Tanimoto (= 1 - Jaccard distance) in SQL.
    A short-lived connection per call (KISS — the calc store's choice).

    **This search is exact and linear, and three sentences here used to say the opposite.** They
    read: "accelerated by the table's HNSW `bit_jaccard_ops` index; the ranking semantics match the
    in-memory backend up to HNSW recall … the `WHERE` filter applies *after* the ordered HNSW scan,
    so a selective threshold can return fewer than `top_k` rows … approximate by design." Measured
    against a live PostgreSQL 16.15 / pgvector 0.8.0 on 200 000 `bit(2048)` rows, the planner
    **never runs an HNSW scan for this statement**: with the `definition` equality and the
    threshold predicate present it takes a Seq Scan (17.6 ms) or, with `enable_seqscan=off`, the
    `definition` btree — and the rows that come back are the exact top-k, not an approximation.
    The index exists (`002`, `003`) and this query does not use it.

    **The obvious restructure is fast and does not preserve the answer, which is why it is not
    here.** Ordering by the index first and filtering afterwards — the shape the old docstring
    described — measures **1.25 ms against 17.6 ms (14x)**, roughly flat in corpus size against a
    scan that costs ~0.088 µs/row (~88 ms at 1M rows, ~880 ms at 10M, and `CLAUDE.md` names
    Pistachio, order 10^7 reactions, as the first live integration). But over 60 queries at
    `hnsw.ef_search=200` and a 10x over-fetch it returned a **different result set for 22 of
    them** — not from poor recall but from ties: Tanimoto over sparse bit vectors produces many
    rows at identical similarity, `ORDER BY distance, id` breaks those ties across the *whole
    table*, and no truncated candidate set can reproduce that. So the choice is exact-and-linear
    against approximate-and-flat, which is a decision about what a structural search may silently
    fail to find — the same failure `find_matches` refuses NaN to prevent, a chemist told there is
    no precedent for the structure they are holding — and a decision needs an ADR rather than a
    refactor. `tests/test_fingerprint_store.py` pins the exactness so that whoever takes it has to
    take it deliberately.

    `source_keyed` says whether the table carries the `source` half of the key
    (D-2026-08-27) — `reaction_fingerprints` does since `063`, `molecule_fingerprints` does not
    and must not. It is a constructor flag rather than two classes because everything that makes
    this backend worth having is identical either way: the Jaccard SQL, the ordering, the
    definition scoping and the pooling. What it changes is confined to the key columns the
    statements below are built from, and every read projects a constant `''` for a table without
    the column, so a row reaches Python in one shape whichever table it came from.
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

        `table` and `width` come from trusted domain constants, never user input, so
        interpolating them into the SQL is safe; the identifier check below enforces
        that trust boundary against any future caller. If `width` disagrees with the
        table's `bit(N)` column, Postgres raises a bit-length error (a loud failure,
        not a silent pad).

        `definition` is the current fingerprint-parameter signature (e.g. `ecfp:r2:b2048`).
        Every row records the definition it was indexed under; similarity search filters to
        this store's definition, so changing the definition and re-indexing alongside older
        rows can never silently rank incomparable (same-width, different-radius) bits — the
        stale rows simply fall out of search until they are re-indexed.

        `source_keyed` binds this store to a table whose primary key is `(source, id)`. Set it
        only for a table that actually has the column: it decides the `ON CONFLICT` target, and
        naming a key the table does not have is a write that fails to plan rather than one that
        silently mis-keys.
        """
        if not table.isidentifier():
            raise ValueError(f"table must be a plain SQL identifier, got {table!r}")
        self._table = table
        self._definition = definition
        self._source_keyed = source_keyed
        self._dsn = dsn if dsn is not None else settings.postgres_dsn
        # The one place the two shapes diverge. `_source_read` is a *projection*, not a filter:
        # a table without the column answers a constant, so `all_records` and `find_similar`
        # unpack the same five/four positions either way and no caller branches on the flag.
        self._source_read = "source" if source_keyed else "''"
        insert_columns = "source, id" if source_keyed else "id"
        insert_values = "%(source)s, %(id)s" if source_keyed else "%(id)s"
        conflict = "(source, id)" if source_keyed else "(id)"
        # Ties break by the whole key, so two sources holding one entry id still order
        # deterministically — and identically to the in-memory backend's `(source, id)` sort.
        self._order = 'source COLLATE "C", id COLLATE "C"' if source_keyed else 'id COLLATE "C"'
        self._upsert = (
            f"INSERT INTO {table} ({insert_columns}, label, bits, definition) "
            f"VALUES ({insert_values}, %(label)s, %(bits)s::bit({width}), %(definition)s) "
            f"ON CONFLICT {conflict} DO UPDATE SET "
            f"label = EXCLUDED.label, bits = EXCLUDED.bits, definition = EXCLUDED.definition"
        )
        # There is deliberately no statement here that deletes the unsourced row `063` could not
        # backfill, and the reason is a privilege rather than a preference. `app_privileges.sql`
        # grants this table INSERT and UPDATE only, in the group whose comment says withholding
        # DELETE is what makes `retention.py`'s refusal to prune enforced rather than intended. A
        # `DELETE` here therefore raises `permission denied` for the runtime role on every sourced
        # write — and because it shares the upsert's transaction, the fingerprint would not land
        # either. That is every ELN and corpus ingest on any deployment that runs `make db-grants`,
        # which no test in this tree could see: the suite connects as the owner.
        #
        # It would also have been wrong where it worked. An unsourced row is not a *twin* — it is
        # whichever site synced last before `063`, or a pre-051 entry that merely shares an id — so
        # deleting it on a same-id write from another source destroys that site's only fingerprint.
        # The unsourced population is finite, shrinks only on a reindex, and is exactly the
        # pre-`063` behaviour for those rows.
        self._all = f"SELECT {self._source_read}, id, label, bits::text, definition FROM {table}"
        # Both scoped to this store's definition, for the reason `find_similar` is: rows indexed
        # under a superseded definition are not searchable here, so counting them would report a
        # populated index to an operator whose searches all return nothing.
        self._exists = f"SELECT 1 FROM {table} WHERE definition = %(definition)s LIMIT 1"
        self._count = f"SELECT count(*) FROM {table} WHERE definition = %(definition)s"
        # `<%%>` is pgvector's Jaccard-distance operator (`%` doubled to escape psycopg).
        # Threshold-filter first (and to this store's definition), then rank by distance and
        # truncate — the in-memory backend's "threshold then top-k"; ties break by id under
        # COLLATE "C" (byte order), because the database's default text collation (e.g.
        # en_US.UTF-8/ICU) orders mixed-case ids differently from Python's code-point sort
        # and would silently break the documented cross-backend ordering parity.
        #
        # Those two predicates are also what keep the planner off the HNSW index, and the class
        # docstring carries the measurement plus the reason that is a decision rather than a
        # defect: the tie-break above is *across the table*, and no ANN candidate set reproduces
        # it. Every row here computes the distance three times (projection, filter, order), which
        # looks like the cheap win and is not — hoisting it into a subquery so it is computed once
        # measured 28.7 ms against 18.0 ms, because the subquery materializes.
        self._similar = (
            f"SELECT {self._source_read}, id, label, "
            f"1 - (bits <%%> %(q)s::bit({width})) AS similarity "
            f"FROM {table} "
            f"WHERE definition = %(definition)s "
            f"AND 1 - (bits <%%> %(q)s::bit({width})) >= %(threshold)s "
            f"ORDER BY bits <%%> %(q)s::bit({width}), {self._order} "
            f"LIMIT %(k)s"
        )

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled per process when the process opened a pool (`chemclaw.core.db.pooling`), so a
        request path pays no TCP+auth handshake; a dedicated connect otherwise. Either way a
        down or misconfigured database reports "Postgres unreachable at <host>" rather than a
        raw psycopg traceback, and a hung query is cancelled rather than pinning the enclosing
        activity for its whole budget.
        """
        async with db.connection(self._dsn) as conn:
            yield conn

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by this table's key, in one transaction.

        The single-record case of `add_many`, so the upsert and the source refusal below have
        exactly one definition rather than two that can drift.
        """
        await self.add_many([record])

    async def add_many(self, records: Sequence[FingerprintRecord]) -> None:
        """Insert or replace a batch on one connection, in one transaction.

        One checkout and one commit for the whole batch rather than per record. Measured against a
        live database inside `db.pooling()`, 200 rows, three trials: 3.0 ms/row one at a time
        against 1.15 ms/row batched, a stable 2.6x. `CorpusMolecules.add_many` is the same method
        for the same reason one table over.

        A record carrying a source on a store that is not `source_keyed` is refused rather than
        written, because the column it would need does not exist and the value would otherwise
        be dropped on the floor — the silent half-write the key change is about. Checked for every
        record *before* the connection is taken, so a bad batch costs no partial write: the refusal
        is about a mis-wired store rather than about one row, and half-applying it would leave the
        caller unable to say what landed.

        An empty batch takes no connection at all: the drain calls this once per page, and a page
        that recorded nothing must not pay a checkout to write nothing.
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

        Unfiltered by definition on purpose: the only consumer is substructure search, which
        re-matches the stored SMILES label with RDKit and never touches the bits, so a
        stale-definition row is still a correct substructure hit. When `limit` is set the scan
        is `ORDER BY <key> LIMIT` — a bounded, deterministic slice so a huge corpus is never
        materialized whole into the worker heap (the caller warns when the cap truncates).
        """
        if limit is None:
            sql, params = self._all, None
        else:
            sql = f"{self._all} ORDER BY {self._order} LIMIT %(limit)s"
            params = {"limit": limit}
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

        `SELECT 1 … LIMIT 1`, not `count(*)`: Postgres stops at the first matching row, so the
        probe costs one index/heap fetch however large the corpus is. It runs on the no-hits path
        of a live similarity search, which is exactly where a full scan would be a defect.
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

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                params = {
                    "q": query_bits,
                    "threshold": threshold,
                    "k": top_k,
                    "definition": self._definition,
                }
                await cur.execute(self._similar, params)
                rows = await cur.fetchall()
        return [Match(source=r[0], id=r[1], label=r[2], similarity=float(r[3])) for r in rows]


async def find_matches(
    store: FingerprintStore,
    query_bits: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> tuple[list[Match], bool]:
    """Search a store with the configured `top_k`/`threshold` defaults applied.

    Returns the page **and whether more records qualified than it could hold**, because a page is
    not a total: `fingerprint_top_k` defaults to 10, and 18 molecules over the threshold rendered
    as "10 indexed molecule(s) matched this query" — a floor read as a count, which is the defect
    `hits_truncated` exists to name on the substructure entry point. One extra row is asked for and
    dropped, the same probe the bounded substructure scan uses.

    The flag is exact on both backends, and the caveat that used to stand here was not. It said
    the durable backend can under-report because "the threshold filter applies *after* pgvector's
    ordered HNSW scan" — measured, that plan is never taken (see `PostgresFingerprintStore`), the
    statement is an exact scan, and `k + 1` rows come back whenever `k + 1` qualify. The caveat
    becomes true again the day this search is moved onto the index, which is the decision that
    docstring records.

    The one place the generic search knobs fall back to config, so the molecule
    and reaction entry points cannot drift in how they default (DRY). `top_k` may arrive
    from the model (the `molfp`/`rxnfp` bundles' MCP tools) and lands in a SQL `LIMIT`, so it is
    clamped to `[1, fingerprint_max_top_k]` here — the fingerprint-search analog of `graph_max_hops`
    clamp on `expand_note`, applied at the single chokepoint both entry points share.
    `threshold` is equally model-supplied and lands in the SQL similarity comparison, so it
    is clamped to `[0, 1]` — the same bound the config default carries (Tanimoto's range);
    outside it, a negative value blesses disjoint structures as neighbors and >1 silently
    returns "no precedent" instead of an exact match.

    **That clamp is total only over *ordered* values, and NaN is not one.** Every comparison
    with NaN is False, so `max` and `min` both keep it: `min(max(nan, 0.0), 1.0)` is `nan`, and
    a value that stepped over both bounds in silence reached the SQL `>= %(threshold)s` (and the
    in-memory `>=`), where every candidate row compares False. Measured, an exact self-match of
    an indexed molecule came back `hits: []` with `index_empty: false`, so `verdict` announced "a
    genuine negative result" — a chemist told we have no precedent for the structure we are
    holding. That is the one outcome this whole module is arranged against, arriving *through*
    the guard written to prevent it, which is why NaN is refused here rather than clamped: it has
    no nearest bound to clamp toward, and substituting the configured default would answer a
    question the caller never asked in a way they could not tell from one they did. `±inf` still
    clamps, because an infinity *does* have a nearest bound — 1.0 and 0.0 are what it means.

    A plain `ValueError` deliberately, not a `FingerprintInputError`: that subclass is the one
    `retrieval.retrievers` catches to mean "a bad query is an empty answer", so raising it here
    would convert the refusal straight back into the silent empty result being fixed.

    `top_k` needs no companion guard and gets none: it is typed `int` at every entry point, and
    an int cannot be NaN. The check is not merely static — pydantic refuses a non-finite float
    for an `int` field (`finite_number`) before either MCP tool body runs — so its clamp is
    already total over everything that can reach it.
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

    The one place the cost of asking is decided, so all three search entry points share it: the
    store is probed **only** when there are no hits. A search that found something has already
    proved the index is populated, and a `COUNT`/`EXISTS` on every call would be a performance
    defect on the hot path — the answer would also be redundant.
    """
    return not hits and await store.is_empty()


async def log_index_size(store: FingerprintStore, subject: Subject) -> None:
    """Log how many records a fingerprint index holds — loudly when it holds none.

    The operator half of the same defect: `similar_reactions` returning nothing because the
    fingerprint backfill was never run is visible to a *chemist* mid-conversation, and by then it
    has already cost an answer. Run once at the owning connector's startup, so the pod that serves
    the index says on its first line whether it has anything to serve.

    WARNING (not INFO) for an empty index because it is actionable and wrong, and never fatal:
    diagnostics must not be able to take down a connector, so an unreachable database is reported
    here and the server starts anyway — the tools themselves fail loudly if it is really down.
    """
    try:
        records = await store.count()
    except (ChemclawError, ConnectionError, psycopg.Error) as exc:
        log.warning("cannot report the %s fingerprint index size: %s", subject, exc)
        return
    if records:
        log.info(
            "%s fingerprint index: %d record(s) indexed under the current definition",
            subject,
            records,
        )
    else:
        log.warning(
            "%s fingerprint index is EMPTY: 0 records indexed under the current definition, so "
            "every %s similarity search will report that it could not be answered. `make reindex` "
            "rebuilds the *note* index only — the fingerprint index is populated by the ELN sync "
            "(ElnSyncWorkflow), and rows predating a definition change need re-indexing too "
            "(docs/guides/runbook.md (vi)).",
            subject,
            subject,
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
