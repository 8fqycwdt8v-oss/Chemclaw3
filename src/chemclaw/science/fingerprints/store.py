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

# pgvector's own hard ceiling on `hnsw.ef_search` (0.8.0: `SET hnsw.ef_search = 1001` is an error).
# Here rather than in config because it is a property of the extension, not a policy this
# deployment gets to choose — the two settings that *are* policy are clamped against it.
_HNSW_MAX_EF_SEARCH = 1000


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
    # Whether the index answered *approximately* — the store's own `approximate`, carried into the
    # payload for exactly the reason `index_empty` is. A deployment may trade exactness for a flat
    # search cost (`fingerprint_search_exactness`), and under that trade an empty result is no
    # longer proof that the corpus holds no analog: the scan looked at a candidate set the index
    # proposed, not at every row. A chemist reading "no precedent found" must be told which of the
    # two questions was answered, and so must a caller that stores the answer — the same rule
    # `Chemclaw3-mcp`'s `props` follows by returning `method` and `caveat` beside every value.
    # Defaults to the shipped arm, so a hit list built by hand in a test is what it looks like.
    approximate: bool = False
    # True only when the index holds nothing searchable — never a hit list that merely came back
    # short. Probed (cheaply) at the one moment it can change the meaning of the result: no hits.
    index_empty: bool = False
    # True when the index also holds records under a *superseded* fingerprint definition, which
    # this search could not compare against. `index_empty` is the same fact at its extreme, and a
    # definition bump walks a corpus from one to the other: at the instant of the bump every row
    # is superseded and `index_empty` is True (honest), and the first re-indexed row flips it to
    # False while 99% of the corpus is still invisible. Measured on the `std6`→`std7` bump of
    # `D-2026-09-09-a-map-number-is-not-a-molecule`: 50 stale rows plus 1 rebuilt row answered
    # "1 indexed molecule(s) matched this query" with no qualifier at all — a complete-looking
    # answer to "have we made this before?" drawn from 2% of the corpus. A **boolean** and not the
    # count, deliberately: the count is a `count(*)` over the whole table and this is read on every
    # search, while the boolean is two index probes (see `has_superseded_records`). The number is
    # the operator's and goes to the connector log, which is where the re-index is run from.
    #
    # **Since `094` it stays True after a *finished* rebuild**, and that is the shelf rather than a
    # regression: a definition change no longer overwrites the generation it retires, so the table
    # holds rows this search could not compare until an operator disposes of them — which the
    # runtime role cannot do (no DELETE) and the connector log names. The clause below therefore
    # says the page *may* be a fraction of the corpus rather than that it is; over-cautious is the
    # right direction for the one tool whose job is "have we seen this before", and the log is
    # where the two states are told apart.
    index_partial: bool = False
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

        **Approximation is the third way this sentence can be a lie, and it is the quietest.** A
        deployment on `fingerprint_search_exactness=approximate` searched a candidate set the HNSW
        index proposed rather than the corpus, so "a genuine negative result" would claim a
        completeness no ANN can offer — and unlike an empty index or a truncated scan, nothing
        about the result *looks* different. So the approximate arm never says "genuine", and says
        so **alongside** a truncation notice rather than instead of one.

        That "alongside" is the correction, and it mattered. This sentence used to end "and says so
        on a full page too", describing exclusive branches in which truncation returned first — and
        `find_matches` asks for `k + 1`, so a truncated page is the *ordinary* outcome. Measured
        over 60 queries at the shipped defaults: the truncated branch fired 60 times and this one
        **zero**, so both arms produced byte-identical text and the property this docstring claims
        was never once exercised on the path that happens. Truncation is about count and
        approximation is about ranking; a chemist told "further matches may exist" has not been
        told the ten in hand may not be the nearest ten.
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
                    "compared — so this is a genuine negative result."
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
        # **Independent facts, so independent clauses — not branches.** Truncation is about
        # *count* ("there may be more"); a superseded fraction is about *what was compared at all*;
        # approximation is about *ranking* ("these may not be the closest"). Written as exclusive
        # `if`/`return` the first shadowed the rest, and `find_matches` asks for `k + 1` so
        # `hits_truncated` is the *ordinary* outcome: measured over 60 queries at the shipped
        # defaults, the truncated branch fired 60 times and the approximate branch **zero**,
        # leaving both arms byte-identical. A chemist reading "further matches may exist" was not
        # being told the ten in hand might not be the nearest ten, which is the whole risk the
        # approximate arm trades for its speed.
        #
        # The heading says which *kind* of qualification applies and `_incomplete_clauses` says
        # what each one is; a partial index shares "PARTIAL" with truncation because both make the
        # page a lower bound, and its clause is what distinguishes them.
        lower_bound = self.scan_truncated or self.hits_truncated or self.index_partial
        labels = ["PARTIAL" if lower_bound else "", "APPROXIMATE" if self.approximate else ""]
        heading = " AND ".join(label for label in labels if label)
        if not heading:
            return matched
        return " ".join([f"{heading} RESULT: {matched}", *self._incomplete_clauses()])

    def _incomplete_clauses(self) -> list[str]:
        """Every reason this search saw less than the whole index, one clause each.

        **One list for both arms, because the two arms had drifted.** The hits arm was already
        clause-composed for the reason its comment gives — truncation is about *count* and
        approximation about *ranking*, so an `if`/`return` chain lets the first shadow the second —
        and the no-hits arm below it was still that exact chain, three exclusive branches. Nothing
        caught it because the two facts it could shadow cannot co-occur today (`scan_truncated` is
        set only by the substructure scan, `approximate` only by similarity search); a third one
        that co-occurs with both is what made the shape matter rather than merely look wrong.

        Ordered by how much of the index the reader is missing: a superseded fraction is the
        largest and the only one an operator can fix, so it is stated before the two that are
        properties of how this query ran.
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

        On the store rather than returned by the search, because it is a property of the *index
        this store is bound to* and not of one query: it decides how an empty result must be read,
        and the entry points copy it onto `FingerprintSearch.approximate` so the answer carries it.
        Phrased as "may miss", so a backend added later has to answer the question a chemist
        actually asks rather than declare which algorithm it runs.
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

    async def has_superseded_records(self) -> bool:
        """Whether the table holds a record this store's definition cannot compare — asked always.

        The searchable half of the same question `superseded_count` answers for an operator, and
        split from it for exactly the reason `is_empty` is split from `count`: this one runs on
        **every** similarity search, because a partly re-indexed index answers a chemist over a
        fraction of the corpus and nothing else about the result would show it, and an exact count
        on that path is a full scan. A backend must answer it without one.
        """
        ...

    async def superseded_count(self) -> int:
        """How many records are stored under a definition this store cannot search.

        The operator's number, paid once per process beside `count`: "3 indexed, 9,997 waiting"
        is what says a re-index is unfinished, and a boolean cannot say how far it got. Since `094`
        it also stays non-zero after a *finished* rebuild, because the superseded generation is
        shelved rather than overwritten — which is why `log_index_size` reports the two counts and
        not a share, and names both actions an operator might owe.
        """
        ...


class InMemoryFingerprintStore:
    """Process-local `FingerprintStore` — the reference the SQL one is written to match.

    **A differential oracle, not a deployment backend.** No configuration returns it — every
    `default_*()` in this tree resolves to the Postgres implementation — and that is deliberate
    (`D-2026-09-07-a-reference-implementation-is-a-test-oracle-not-a-backend`). It stays in
    `src/` because it is the executable statement of the contract its Postgres sibling is written
    to reproduce, and it is read beside that sibling; `tests/test_reference_stores.py` holds both
    halves of that — the absence of a shipped caller, and the absence of this claim.

    Computes exact Tanimoto ranking without a database — the reference the Postgres
    backend matches (same threshold and tie-break, exactly for small corpora, up to HNSW
    recall for large ones). Keyed by `(source, id, definition)`, so re-adding one source's record
    under one definition replaces it, a second source using the same entry id gets its own row
    (D-2026-08-27), and a *second definition* shelves the generation it supersedes rather than
    evicting it (`094`). An index whose records all leave `source` empty and all carry one
    definition — every molecule index, and every ephemeral one built in a test — behaves exactly
    as it did when the key was the bare id, because the other two thirds are then constant.

    The definition belongs in this key for the reason every part of this class exists: keyed
    without it, this store reproduced the durable defect exactly, and a partial-index assertion
    made here would have been evidence about a deployment that behaves differently.
    """

    def __init__(self, definition: str | None = None) -> None:
        """Start with an empty index.

        If `definition` is set, similarity search returns only records built under that
        same fingerprint definition — the durable store's cross-definition guard, made
        testable without a database. Left `None` it ranks every record, which is correct
        for an ephemeral index always populated in a single configuration (tests, demo).
        """
        self._records: dict[tuple[str, str, str], FingerprintRecord] = {}
        self._definition = definition

    @property
    def approximate(self) -> bool:
        """Never — this backend scores every searchable record.

        Which is what makes it the reference the durable backend's exact arm is asserted against.
        """
        return False

    async def add(self, record: FingerprintRecord) -> None:
        """Insert or replace a fingerprint by `(source, id, definition)`, superseding its twin.

        The supersede is the in-memory half of the durable store's, and it is what keeps a
        migrated index from holding one entry twice — see `PostgresFingerprintStore.add` for the
        row it is about. Here it costs one dict lookup and keeps the two backends' contents
        identical, which is the property every ordering assertion in this module rests on.

        **Scoped to one definition**, like the key it is part of: a sourced write supersedes the
        unsourced twin *of its own generation* and leaves the shelf alone, because a row under
        another definition is not a twin of anything — it is what the current generation replaced.
        """
        self._records[(record.source, record.id, record.definition)] = record
        if record.source:
            self._records.pop(("", record.id, record.definition), None)

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

        **One row per `(source, id)`, whichever generations are shelved**, matching the durable
        backend's `DISTINCT ON` — a molecule is one molecule to the substructure scan, and this
        store's definition wins where both are held. On a single-generation index (every existing
        caller) the dedupe is the identity.
        """
        records = self._one_per_key()
        if limit is None:
            return records
        return sorted(records, key=lambda r: (r.source, r.id))[:limit]

    def _one_per_key(self) -> list[FingerprintRecord]:
        """The generation this store shows for each `(source, id)` — the `DISTINCT ON` in Python.

        This store's own definition where it is held, and the first-added generation otherwise:
        the durable backend's `ORDER BY …, (definition = <this store's>) DESC`. A store that pins
        no definition has no preference to express, so the first row stands. One pass over the
        records rather than a scan per record, because the caller's cap is in the thousands.
        """
        chosen: dict[tuple[str, str], FingerprintRecord] = {}
        for (source, id_, _), record in self._records.items():
            held = chosen.get((source, id_))
            if held is None or record.definition == self._definition != held.definition:
                chosen[(source, id_)] = record
        return list(chosen.values())

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

    def _superseded(self) -> list[FingerprintRecord]:
        """The records held here that this store's definition cannot compare against.

        The complement of `_searchable`, over the same one definition of "in this index", so the
        two cannot disagree about a record. A store pinning no definition ranks everything, so
        nothing is superseded for it — which is the same rule `_searchable` states from the
        other side.
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


def _matches_from(rows: Sequence[TupleRow]) -> list[Match]:
    """Turn `(source, id, label, similarity)` rows into hits — the one row shape both arms return.

    Extracted because the two similarity statements are deliberately separate and their *result*
    deliberately is not: a hit that differs by which arm found it would put the arm inside every
    downstream consumer instead of on the search that ran.
    """
    return [Match(source=r[0], id=r[1], label=r[2], similarity=float(r[3])) for r in rows]


class PostgresFingerprintStore:
    """Durable `FingerprintStore` backed by Postgres + pgvector, over one table.

    Table and bit width are constructor parameters (both trusted internal constants), so
    the same class serves the molecule and reaction fingerprint tables. Similarity is
    Tanimoto (= 1 - Jaccard distance) in SQL.
    A short-lived connection per call (KISS — the calc store's choice).

    **This backend has two similarity searches and a deployment picks one**
    (`fingerprint_search_exactness`, default `exact`). They are written as two statements and two
    methods rather than one statement with a flag in it, because they answer different questions
    and the difference is the whole decision:

    - **exact** (`_find_similar_exact`) compares the query against every row under this store's
      definition. The `definition` equality and the threshold predicate are what keep the planner
      off the HNSW index — measured against a live PostgreSQL 16.15 / pgvector 0.8.0 on 200 000
      `bit(2048)` rows it takes a Seq Scan, 17.6 ms, and returns the true top-k. Linear:
      ~0.088 µs/row, so ~880 ms at 10^6 and ~8.8 s at 10^7 rows, and `CLAUDE.md` names Pistachio
      (order 10^7 reactions) as the first live integration. Three sentences here used to claim
      this statement rode the HNSW index and was "approximate by design"; it never has.
    - **approximate** (`_find_similar_approximate`) asks the HNSW index for
      `top_k × fingerprint_approximate_overfetch` candidates in distance order, then applies the
      definition scope, the threshold and the exact tie-break to *those*. ~1.25 ms at 200 000 rows
      and roughly flat in corpus size — a 14x that widens as the corpus grows.

    **What the second arm costs is agreement, and it is ties rather than recall.** Over 60 queries
    at `hnsw.ef_search=200` with a 10x over-fetch the returned page differed from the exact page
    for 22 of them. Tanimoto over sparse bit vectors puts many rows at *identical* similarity, and
    the exact `ORDER BY distance, id COLLATE "C"` breaks those ties across the whole table — which
    no truncated candidate set can reproduce. `tests/test_molfp_postgres.py` measures both halves:
    the exact arm is pinned against the in-memory reference, and the approximate arm is pinned by a
    floor on how far from exact it is, so a recall regression is visible rather than believed.

    Every search says which arm ran (`approximate` below, copied onto
    `FingerprintSearch.approximate`), because under the second one an empty result stops being
    evidence of absence — the failure this whole module is arranged against, a chemist told there
    is no precedent for the structure they are holding.

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

        **That last sentence describes rows that still exist, and until `094` they did not.**
        The definition was an ordinary column and the upsert overwrote it, so a write under a
        second definition *replaced* the first generation's row rather than shelving it beside it.
        Invisible inside one deployment mid-rebuild — the re-index is walking those rows anyway —
        and not invisible the moment two writers hold different definitions at once: measured, two
        stores over one table left `rows=1`, each side's `count()` at 0 while the other's was 1,
        and each side's `superseded_count()` reporting the other's population as "stale, re-index
        me" with neither index ever converging. The definition is part of the key now
        (`(id, definition)`, `(source, id, definition)`), the shape `041` gave `document_chunks`.

        `source_keyed` binds this store to a table whose key leads with `source`. Set it
        only for a table that actually has the column: it decides the `ON CONFLICT` target, and
        naming a key the table does not have is a write that fails to plan rather than one that
        silently mis-keys. The same is true of the definition half, which is why `094` names every
        table this class writes: bind a *fourth* table whose key is still the bare id and the first
        write fails to plan rather than quietly evicting a generation.
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
        # The definition is in the key, so a write under a *second* definition inserts beside the
        # first instead of replacing it (`094`). A re-write under one definition still updates in
        # place, which is what keeps a re-index from doubling the table.
        conflict = "(source, id, definition)" if source_keyed else "(id, definition)"
        # Ties break by the whole key, so two sources holding one entry id still order
        # deterministically — and identically to the in-memory backend's `(source, id)` sort.
        self._order = 'source COLLATE "C", id COLLATE "C"' if source_keyed else 'id COLLATE "C"'
        self._upsert = (
            f"INSERT INTO {table} ({insert_columns}, label, bits, definition) "
            f"VALUES ({insert_values}, %(label)s, %(bits)s::bit({width}), %(definition)s) "
            f"ON CONFLICT {conflict} DO UPDATE SET "
            f"label = EXCLUDED.label, bits = EXCLUDED.bits"
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

        # **One row per key, not one row per stored generation** (`094`). This statement is the
        # substructure scan's, and it is unfiltered by definition on purpose — a superseded row's
        # stored SMILES is still a correct substructure hit, so a corpus mid-rebuild is searched
        # whole by that entry point and only by it. Once a definition change *shelves* the
        # generation it supersedes instead of overwriting it, "unfiltered" starts meaning "twice",
        # and both halves of that are chemist-visible: the scan builds one hit per row, so a
        # molecule held under two generations is reported twice, and `substructure_scan_max_records`
        # bounds *rows*, so the cap arrives at half the molecules and a corpus that fits comes back
        # `scan_truncated`.
        #
        # `DISTINCT ON` over the store's own key ordering, preferring this store's definition —
        # never an arbitrary generation, so the two backends still hold the same rows and the row a
        # chemist is shown is the structure this deployment standardized.
        self._all = (
            f"SELECT DISTINCT ON ({self._order}) "
            f"{self._source_read}, id, label, bits::text, definition FROM {table} "
            f"ORDER BY {self._order}, (definition = %(definition)s) DESC"
        )
        # Both scoped to this store's definition, for the reason `find_similar` is: rows indexed
        # under a superseded definition are not searchable here, so counting them would report a
        # populated index to an operator whose searches all return nothing.
        self._exists = f"SELECT 1 FROM {table} WHERE definition = %(definition)s LIMIT 1"
        self._count = f"SELECT count(*) FROM {table} WHERE definition = %(definition)s"
        # The complement, and the two shapes are deliberately different statements rather than one
        # with a `<>` in it, because they are read on different paths and only one of them may
        # scan.
        #
        # `_superseded_count` is the operator's, run once per process beside `_count`, and a
        # `count(*)` there is the same cost that one already pays.
        #
        # `_definition_extremes` is the searcher's, and it is `min`/`max` rather than the obvious
        # `SELECT 1 … WHERE definition <> … LIMIT 1` for a reason that only shows up when the index
        # is *healthy*: with every row under one definition, that `LIMIT 1` has to read every row
        # before it can answer "none". `min(definition)`/`max(definition)` are two `Limit`s over
        # `<table>_definition_idx` (`046`) instead — O(log n) index probes, and an index whose
        # extremes are both this store's definition holds nothing else by construction. Measured on
        # a live PostgreSQL 16.15 at 200 000 rows all under the current definition: **26.32 ms**
        # for the `LIMIT 1` form and **0.55 ms** for this one, and the first is linear in the
        # corpus while the second is not. `corpus_molecules`/`corpus_reactions` carry no such index
        # and would seq-scan; neither builds a `FingerprintSearch`, so neither is on this path.
        self._superseded_count = f"SELECT count(*) FROM {table} WHERE definition <> %(definition)s"
        self._definition_extremes = f"SELECT min(definition), max(definition) FROM {table}"
        # `<%%>` is pgvector's Jaccard-distance operator (`%` doubled to escape psycopg).
        # Threshold-filter first (and to this store's definition), then rank by distance and
        # truncate — the in-memory backend's "threshold then top-k"; ties break by id under
        # COLLATE "C" (byte order), because the database's default text collation (e.g.
        # en_US.UTF-8/ICU) orders mixed-case ids differently from Python's code-point sort
        # and would silently break the documented cross-backend ordering parity.
        #
        # Those two predicates are also what keep the planner off the HNSW index, which is the
        # whole difference between this statement and the one below. Every row here computes the
        # distance three times (projection, filter, order), which looks like the cheap win and is
        # not — hoisting it into a subquery so it is computed once measured 28.7 ms against
        # 18.0 ms, because the subquery materializes.
        self._similar_exact = (
            f"SELECT {self._source_read}, id, label, "
            f"1 - (bits <%%> %(q)s::bit({width})) AS similarity "
            f"FROM {table} "
            f"WHERE definition = %(definition)s "
            f"AND 1 - (bits <%%> %(q)s::bit({width})) >= %(threshold)s "
            f"ORDER BY bits <%%> %(q)s::bit({width}), {self._order} "
            f"LIMIT %(k)s"
        )
        # The approximate arm, deliberately the same shape read top to bottom. The inner query is
        # bare `ORDER BY <distance> LIMIT` — the only form pgvector's HNSW index can serve — and
        # everything the exact statement puts in its `WHERE` moves *outside* it, because either
        # predicate inside would cost the ordered index scan and turn this back into the statement
        # above. That is also its one structural cost: rows under a superseded definition consume
        # candidate slots, so an index mid-reindex answers from a narrower set than it looks like.
        # The outer `ORDER BY` repeats the exact tie-break so that a candidate set which *does*
        # contain a whole tie group orders it identically to the exact arm.
        #
        # **`094` makes that structural cost outlive the rebuild**, and it is the price of shelving
        # rather than overwriting: a superseded generation stays in the table until an operator
        # disposes of it, so on a deployment running this arm the effective over-fetch is divided by
        # the number of generations held — two generations, half the candidates, permanently. The
        # exact arm above is unaffected (its `WHERE definition` is inside the scan), and it is the
        # shipped default. Disposal is the remedy and `log_index_size` is where it is named.
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

        **What this loop is *not* is the cost of a bulk load, and that was worth measuring before
        anyone optimises it.** A re-index writes into a table whose HNSW index is live, and that
        index is the whole bill: driven server-side so no round trip, no psycopg loop and no event
        loop is inside either number, 50 000 `bit(2048)` rows on one PostgreSQL 16.15 took
        **450.1 s (111 rows/s)** as `INSERT … SELECT` into an HNSW-indexed table, and **0.5 s
        (94 855 rows/s)** into the same table with no index. Building the index afterwards took a
        further 132.2 s — with parallel maintenance workers off, because this container ships
        docker's 64 MB `/dev/shm` and a parallel build asks for more, so that half is the *slow*
        build — for **132.7 s (377 rows/s) end to end, 3.4x**.

        So the round trips this method batches are ~0.4% of a loaded re-index and `executemany`
        would buy nothing measurable; the standard bulk path (drop the index, load, rebuild) is
        where the 3.4x is. That path needs DDL this role does not hold, and there is no re-index
        job to put it in — `docs/planning/BACKLOG.md` is where it belongs, not here.
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

        **Unfiltered, and one row per key**: since `094` a definition change shelves the generation
        it supersedes rather than overwriting it, so "every definition" would otherwise mean "every
        molecule twice". The constructor's `_all` says what that costs and which generation wins.
        Under a `limit` the de-duplication rides `082`'s index and still streams (an `Incremental
        Sort` inside each id, groups of one or two rows — `tests/test_molfp_postgres.py` pins the
        absence of a whole-table `Sort`). **Unbounded, it does sort the table**, which it did not
        before: that call has no caller in `src/` and already materializes every row into the heap,
        so the ordering is the smaller half of what it costs.

        **Bounded in the *heap*; it was not bounded in the database, and migration `082` is what
        made the second half true.** The ordering is `COLLATE "C"` — deliberately, so this backend
        sorts identically to the in-memory one — and the primary key is a btree in the database's
        own collation, so no index could satisfy it: the server sorted every row in the table and
        then returned `limit` of them. Measured on 200 000 rows at the shipped cap of 5 000, that
        was an external merge spilling 136 MB to disk, 2 228 ms, against 10.7 ms through the index
        `082` adds.

        The row's own weight is the caller's problem and stays one: `bits` is 2 048 characters per
        record and the substructure scan reads only `label`, so a 5 001-row slice ships 10.4 MB it
        discards — measured at 2 055 ms against 261 ms for the same rows without that column. This
        method cannot drop it (a `FingerprintRecord` carries its bits by definition); what removes
        it is the caller asking for only what it reads.
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

    async def has_superseded_records(self) -> bool:
        """Whether any row is under another definition — two index probes, not a scan.

        Exact, and cheap for the reason the constructor's comment measures: the extremes of
        `definition` bound every value in the column, so they are equal to this store's definition
        if and only if nothing else is stored. A `NULL` min is an empty table.
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

        Read per call rather than frozen at construction, so a reloaded or monkeypatched setting
        is honoured and — more importantly — so this property and `find_similar` cannot disagree
        about which arm ran. The two of them are the reason the answer can say which question it
        answered; a cached copy here is how they would drift apart.
        """
        return settings.fingerprint_search_exactness == "approximate"

    async def find_similar(self, query_bits: str, top_k: int, threshold: float) -> list[Match]:
        """Return up to `top_k` records with Tanimoto >= `threshold`, most similar first.

        The one place the deployment's choice is read (see the class docstring for the trade and
        its measurements). Both arms return the same shape and honour the same threshold, ordering
        and tie-break; what differs is whether the records they rank are all of them.
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

        Two knobs, both set so the arm is as close to exact as an ANN arm can be. It asks the
        index for `top_k × fingerprint_approximate_overfetch` candidates, so the threshold and the
        tie-break cut into slack rather than into the page; and it raises `hnsw.ef_search` to at
        least that many, because pgvector's graph traversal cannot return more good candidates
        than it kept — a probe narrower than the fetch silently degrades recall rather than
        failing. Both are clamped to pgvector's own ceiling on `ef_search`.

        `SET LOCAL` rather than `SET`: the connection is borrowed from a shared pool, and a
        session-level GUC left behind on it would follow every later borrower of that connection.
        `set_config(..., true)` is the parameterizable spelling of `SET LOCAL` — `SET` itself takes
        no bound parameters — and `db.connection` commits the surrounding transaction on exit,
        which is what scopes it.
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

    Returns the page **and whether more records qualified than it could hold**, because a page is
    not a total: `fingerprint_top_k` defaults to 10, and 18 molecules over the threshold rendered
    as "10 indexed molecule(s) matched this query" — a floor read as a count, which is the defect
    `hits_truncated` exists to name on the substructure entry point. One extra row is asked for and
    dropped, the same probe the bounded substructure scan uses.

    The flag is exact on the in-memory backend and on the durable backend's exact arm: `k + 1`
    rows come back whenever `k + 1` qualify. On the durable backend's **approximate** arm it is a
    floor in the other direction too — a `False` means the over-fetched candidate set held no
    further qualifying row, not that the corpus holds none — which is why the arm that ran travels
    out separately on `FingerprintSearch.approximate` instead of being folded into this flag. Two
    different uncertainties compressed into one boolean is how the empty-list defect this module
    is arranged against got made in the first place.

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


async def index_is_partial(store: FingerprintStore) -> bool:
    """Whether the index also holds records this search could not compare — asked every time.

    The sibling of `index_is_empty`, and deliberately *not* conditioned on the hit list the way
    that one is. Emptiness only changes the reading of an empty result, so probing for it on a
    page of hits would buy nothing; a superseded fraction changes the reading of **both** — an
    empty page is not evidence of absence, and a full page is a page out of the rebuilt fraction.
    Measured on the `std6`→`std7` bump: 50 superseded rows and 1 rebuilt one answered "1 indexed
    molecule(s) matched this query", which is the sentence a chemist acts on.

    What makes that affordable is the store contract, not this function: `has_superseded_records`
    is required to answer without a scan, and the durable backend does it in two index probes
    (0.55 ms at 200 000 rows against 26.32 ms for the obvious `LIMIT 1` form).
    """
    return await store.has_superseded_records()


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

    Appended rather than replacing that warning, because "0 searchable records" is the true and
    already-honest reading of a corpus at the instant of a definition bump — what it could not say
    is that the rows are *there* and need rebuilding rather than re-ingesting from the source,
    which is a different operator action.
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
