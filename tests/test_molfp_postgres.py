"""Integration tests for the Postgres fingerprint store (plan steps 3.2/3.3).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the durable backend honors the same `FingerprintStore` contract as the in-memory
one: Tanimoto ranking in SQL returns most-similar-first, the threshold filters, and
substructure search works over it via the shared, backend-agnostic search functions.
"""

import asyncio
import random

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.search import (
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from chemclaw.science.fingerprints.store import (
    InMemoryFingerprintStore,
    PostgresFingerprintStore,
    find_matches,
)
from tests.pg import migrated_db_or_skip


async def _store_or_skip() -> PostgresFingerprintStore:
    """Return a migrated Postgres fingerprint store, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresFingerprintStore(
        "molecule_fingerprints", settings.ecfp_bits, molecule_definition()
    )


def test_similarity_ranking_in_sql() -> None:
    """The SQL backend ranks Tanimoto neighbors most-similar-first, honoring threshold."""

    async def _run() -> None:
        store = await _store_or_skip()
        for cid, smiles in [
            ("pg-ethanol", "CCO"),
            ("pg-propanol", "CCCO"),
            ("pg-butanol", "CCCCO"),
            ("pg-benzene", "c1ccccc1"),
        ]:
            await store.add(record_for(cid, smiles))

        hits = (await find_similar_molecules(store, "CCO", top_k=3, threshold=0.1)).hits
        assert hits[0].smiles == "CCO"
        assert hits[0].similarity == pytest.approx(1.0)
        assert "c1ccccc1" not in {h.smiles for h in hits}  # disjoint, below threshold
        assert all(
            (hits[i].similarity or 0.0) >= (hits[i + 1].similarity or 0.0)
            for i in range(len(hits) - 1)
        )

    asyncio.run(_run())


def test_tie_break_order_matches_the_in_memory_backend() -> None:
    """Equal-similarity hits come back in the same id order from both backends.

    The in-memory reference tie-breaks by Python's code-point sort; the SQL side must
    order identically (`id COLLATE "C"`), or the database's locale collation (e.g.
    en_US.UTF-8 puts 'a1' before 'B1') silently breaks the documented cross-backend
    determinism for mixed-case ids.

    Asserted at the *store* level (`find_matches`), which is where the collation lives and the
    only level that can see it: two records sharing one structure differ solely by id, and the
    molecule search presents a hit by its structure and the note it cites, not by its row id.
    """

    async def _run() -> None:
        pg_store = await _store_or_skip()
        mem_store = InMemoryFingerprintStore(definition=molecule_definition())
        octanol = "CCCCCCCCO"  # unique to this test so a high threshold isolates the tie
        for cid in ["pg-collate-a1", "pg-collate-B1"]:
            await pg_store.add(record_for(cid, octanol))
            await mem_store.add(record_for(cid, octanol))

        bits = ecfp_bitstring(octanol)
        pg_hits, _ = await find_matches(pg_store, bits, top_k=None, threshold=0.99)
        mem_hits, _ = await find_matches(mem_store, bits, top_k=None, threshold=0.99)
        pg_ids = [h.id for h in pg_hits if h.id.startswith("pg-collate-")]
        mem_ids = [h.id for h in mem_hits]
        assert pg_ids == mem_ids == ["pg-collate-B1", "pg-collate-a1"]  # code-point order

    asyncio.run(_run())


def test_the_durable_page_is_the_exact_top_k_not_an_approximation() -> None:
    """The durable search returns the *exact* page, ties included — the property an ANN loses.

    `PostgresFingerprintStore`'s docstring used to say this search was "accelerated by the table's
    HNSW `bit_jaccard_ops` index … approximate by design". Measured on 200 000 `bit(2048)` rows,
    the planner never takes that plan: the `definition` equality and the threshold predicate cost
    it the ordered index scan, so what ships is an exact sequential scan — 17.6 ms there, ~0.088
    µs/row, i.e. ~880 ms at the 10^7 rows Pistachio implies. Ordering by the index first and
    filtering afterwards is 14x faster (1.25 ms, roughly flat in N) and is **not** the same answer:
    over 60 queries at `hnsw.ef_search=200` with a 10x over-fetch it returned a different result
    set for 22 of them.

    The mechanism is ties rather than recall, which is why this test is written the way it is.
    Tanimoto over sparse bit vectors puts many rows at *identical* similarity, and `ORDER BY
    distance, id COLLATE "C"` breaks those ties across the whole table — something no truncated
    candidate set can reproduce. So: 200 rows of one structure, a page of 50. Exactly the 50
    lowest ids must come back, in order. An ANN would return 50 equally-similar rows in graph
    order, pass every similarity assertion in this file, and quietly answer a different question.

    That is a decision (a structural search that may silently miss a precedent) rather than a
    refactor, so it belongs in an ADR — and this test is what makes taking it deliberate. It pins
    the *contract*, not the plan: at 200 rows the planner would not choose an index anyway, so the
    assertion bites wherever a restructure makes an index-ordered candidate set the answer, which
    is every corpus large enough for the change to be worth making.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        mem_store = InMemoryFingerprintStore(definition=molecule_definition())
        # A structure with no near neighbour among this suite's fixtures, so a 0.99 threshold
        # isolates these rows from every other row in the shared table and the whole page is one
        # tie. A long alkanol is *not* usable here even though it looks unique: ECFP4 over a chain
        # of identical CH2 environments makes C8-ol and C13-ol tie at 1.0, and the sibling test's
        # octanol rows then take two slots in this page.
        structure = "Clc1ccc(cc1)C(=O)Nc1ccc(cc1)S(=O)(=O)N"
        ids = [f"pg-exact-{index:03d}" for index in range(200)]
        records = [record_for(cid, structure) for cid in ids]
        await store.add_many(records)
        for record in records:
            await mem_store.add(record)

        bits = ecfp_bitstring(structure)
        page, truncated = await find_matches(store, bits, top_k=50, threshold=0.99)
        reference, _ = await find_matches(mem_store, bits, top_k=50, threshold=0.99)

        assert [hit.id for hit in page] == sorted(ids)[:50], (
            "the durable page is not the exact lowest-id half of the tie — an approximate scan "
            "returns 50 equally-similar rows in whatever order it found them"
        )
        assert [hit.id for hit in page] == [hit.id for hit in reference], (
            "the two backends disagree about which 50 of 200 tied rows the page holds"
        )
        assert truncated, "150 rows over the page went unreported"

    asyncio.run(_run())


def test_the_capped_scan_reads_in_key_order_without_sorting_the_table() -> None:
    """`all_records(limit=…)` must not sort the whole corpus to return `limit` rows.

    The slice is ordered by `id COLLATE "C"` — load-bearing, because it is what makes this backend
    order identically to the in-memory one (a database's default collation puts `a1` before `B1`).
    The primary key is a btree in the *database's* collation and therefore cannot satisfy that
    ordering, so before `082` the planner sorted every row in the table and then took the first
    `substructure_scan_max_records + 1`. Measured on 200 000 rows at the shipped cap of 5 000:
    `Sort (external merge, 136 MB to disk)`, 2 228 ms and 103 466 temp blocks written, against
    10.7 ms and no temp through the index. The cost grows with the corpus the cap exists to protect
    the process from, on a path the agent calls (`molfp.find_substructure_matches`).

    Asserted as the **absence of a Sort node** rather than as a duration: at fixture scale sorting
    a handful of rows is both correct and instant, so a timing assertion would see nothing. The
    sequential scan is disabled for the same reason as in `tests/test_reaction_records.py` — on one
    page the planner is right to scan, and the question here is what the schema offers it.
    """

    async def _run() -> list[str]:
        store = await _store_or_skip()
        statement = f"{store._all} ORDER BY {store._order} LIMIT %(limit)s"
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("SET LOCAL enable_seqscan = off")
            cursor = await conn.execute(f"EXPLAIN (FORMAT JSON) {statement}", {"limit": 5001})
            row = await cursor.fetchone()
        nodes: list[str] = []
        pending = [row[0][0]["Plan"]] if row else []
        while pending:
            node = pending.pop()
            nodes.append(str(node["Node Type"]))
            pending.extend(node.get("Plans", []))
        return nodes

    nodes = asyncio.run(_run())
    assert not any("Sort" in node for node in nodes), (
        f"the capped scan sorts the whole table before taking its slice: {nodes}"
    )


def test_upsert_and_substructure_over_postgres() -> None:
    """Re-adding an id replaces it; substructure search works over the durable backend."""

    async def _run() -> None:
        store = await _store_or_skip()
        await store.add(record_for("pg-mol", "CCO"))
        await store.add(record_for("pg-mol", "CC(=O)O"))  # replace ethanol with acetic acid

        acids = {r.smiles for r in (await find_substructure_matches(store, "C(=O)[OH]")).hits}
        assert "CC(=O)O" in acids  # the replaced record now matches the acid pattern

    asyncio.run(_run())


def test_emptiness_and_count_are_scoped_to_the_stores_definition() -> None:
    """The durable backend must answer "is anything searchable here?" as honestly as memory does.

    Asserted through a store pinned to a definition nothing was ever indexed under, which is both
    the robust way to test emptiness against a shared database (other tests' rows are invisible to
    it) and a real deployment state: after a fingerprint-definition change every existing row falls
    out of search (runbook (vi)), so a table full of stale rows is an index that answers nothing.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        orphaned = PostgresFingerprintStore(
            "molecule_fingerprints", settings.ecfp_bits, "ecfp:never-indexed:b2048"
        )
        assert await orphaned.is_empty() is True
        assert await orphaned.count() == 0
        # And the honesty travels all the way out to the search a chemist sees.
        search = await find_similar_molecules(orphaned, "CCO", threshold=0.1)
        assert search.hits == []
        assert search.index_empty is True
        assert "SEARCH NOT RUN" in search.model_dump()["verdict"]

        current = await _store_or_skip()
        await current.add(record_for("pg-count", "CCO"))
        assert await current.is_empty() is False
        assert await current.count() >= 1
        populated = await find_similar_molecules(current, "CCO", threshold=0.1)
        assert populated.index_empty is False

    asyncio.run(_run())


_ANN_TABLE = "molfp_approximate_probe"
_ANN_ROWS = 10_000
_ANN_QUERIES = 40
_ANN_DEFINITION = "ecfp:r2:b2048"
# The floor this test ratchets: the mean fraction of the exact page the approximate arm returns,
# over `_ANN_QUERIES` queries on the corpus built below. Measured at 1.000 on the shipped
# `fingerprint_approximate_overfetch = 10`; pinned below that so ordinary index/planner drift is
# not a failure while a real recall regression is. What it is NOT is a claim that the two arms
# agree on the *ordered page* — they do not, and the assertion below says so in the other
# direction, because that disagreement is ties rather than misses.
_ANN_RECALL_FLOOR = 0.95


def _probe_bits(index: int) -> str:
    """A sparse fingerprint with the layered structure a real ECFP corpus has.

    Not a uniform random bitstring, which would make every pair equidistant and the recall
    measurement meaningless: a real corpus is scaffolds inside series inside analogs, so the
    similarity distribution is a continuum with a dense head — which is exactly what an HNSW graph
    is good and bad at in interesting ways. Three layers (scaffold, series, own substitution) plus
    a deliberate exact duplicate every fiftieth record, so the page a query gets back contains real
    ties and the tie-break the exact arm applies across the whole table has something to bite on.
    """
    if index % 50 == 0:  # an exact duplicate of its predecessor: a guaranteed tie at 1.0
        index -= 1
    scaffold = random.Random(90_000 + index // 500)
    series = random.Random(50_000 + index // 20)
    own = random.Random(index)
    on = {scaffold.randrange(2048) for _ in range(12)}
    on |= {series.randrange(2048) for _ in range(10)}
    on |= {own.randrange(2048) for _ in range(8)}
    row = ["0"] * 2048
    for bit in on:
        row[bit] = "1"
    return "".join(row)


async def _approximate_probe_store() -> PostgresFingerprintStore:
    """Build (once) a corpus with an HNSW index and return a store bound to it.

    A table of its own rather than the shipped `molecule_fingerprints`, for two reasons that both
    decide the number this test reports. The candidate set the index proposes is filtered by
    `definition` *afterwards* — that is what keeps the ordered index scan — so rows other tests
    left in the shared table would consume candidate slots and make the measured recall depend on
    which tests ran first. And 10 000 rows is what makes the planner choose the HNSW index at all;
    pushing that into the shared table would slow every other test in this file for the life of
    the database.
    """
    await migrated_db_or_skip()
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(f"SELECT to_regclass('{_ANN_TABLE}')")
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            await conn.execute(
                f"CREATE TABLE {_ANN_TABLE} ("
                "id TEXT PRIMARY KEY, label TEXT NOT NULL, "
                f"bits bit({settings.ecfp_bits}) NOT NULL, definition TEXT NOT NULL)"
            )
            async with conn.cursor() as cur:
                async with cur.copy(
                    f"COPY {_ANN_TABLE} (id, label, bits, definition) FROM STDIN"
                ) as copy:
                    for index in range(_ANN_ROWS):
                        await copy.write_row(
                            (
                                f"probe-{index:06d}",
                                f"probe-molecule-{index}",
                                _probe_bits(index),
                                _ANN_DEFINITION,
                            )
                        )
            await conn.execute(
                f"CREATE INDEX {_ANN_TABLE}_jaccard_idx "
                f"ON {_ANN_TABLE} USING hnsw (bits bit_jaccard_ops)"
            )
    return PostgresFingerprintStore(_ANN_TABLE, settings.ecfp_bits, _ANN_DEFINITION)


def test_the_approximate_arm_actually_rides_the_index_it_trades_exactness_for() -> None:
    """The approximate statement must take an HNSW Index Scan, or its recall number is a fiction.

    This is the assertion that makes the next test mean something. Both arms return the same
    columns and honour the same threshold and tie-break, so an approximate arm the planner quietly
    served with a sequential scan would return the *exact* answer, measure 100% agreement, and
    prove nothing at all — while a deployment that turned the setting on for the speed got neither
    the speed nor a signal that it did not. So: the plan, on a corpus large enough for the planner
    to have a choice, must name the table's `bit_jaccard_ops` index.
    """

    async def _run() -> list[str]:
        store = await _approximate_probe_store()
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("SELECT set_config('hnsw.ef_search', '200', true)")
            cursor = await conn.execute(
                f"EXPLAIN (FORMAT JSON) {store._similar_approximate}",
                {
                    "q": _probe_bits(7),
                    "definition": _ANN_DEFINITION,
                    "threshold": 0.3,
                    "k": 11,
                    "candidates": 110,
                },
            )
            row = await cursor.fetchone()
        names: list[str] = []
        pending = [row[0][0]["Plan"]] if row else []
        while pending:
            node = pending.pop()
            names.append(f"{node['Node Type']}:{node.get('Index Name', '')}")
            pending.extend(node.get("Plans", []))
        return names

    nodes = asyncio.run(_run())
    assert any(f"{_ANN_TABLE}_jaccard_idx" in node for node in nodes), (
        f"the approximate arm is not using the HNSW index, so it is not approximate: {nodes}"
    )


def test_how_far_from_exact_the_approximate_arm_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measure the approximate arm against the exact one and pin a floor under its recall.

    The interesting question about an ANN is not whether it is fast — it is how much of the true
    answer it gives back, and nothing in this repository was measuring that. So: the same 40
    queries through the same store, once per arm, comparing the pages.

    Two numbers come out and they say different things. **Recall** — how much of the exact page the
    approximate page contains — is what a chemist loses: a precedent that exists and was not
    returned. **Ordered-page agreement** is not, and conflating them is what made this look like a
    recall problem when it is a tie problem: Tanimoto over sparse bits puts many rows at identical
    similarity, the exact arm breaks those ties by id across the *whole* table, and a candidate set
    that holds only part of a tie group cannot reproduce that however good its recall is. The two
    pages are then equally good answers to a chemist's question and different answers to a
    byte-comparison, so only the first is ratcheted.

    The floor is deliberately a mean over queries rather than a per-query minimum: HNSW recall is a
    distribution, one unlucky graph traversal is not a regression, and a per-query assertion would
    be a flake generator. The measured value is printed so a run that passes still says what it
    measured.
    """

    async def _run() -> tuple[float, float, int, int]:
        store = await _approximate_probe_store()
        chooser = random.Random(4)
        queries = [_probe_bits(chooser.randrange(_ANN_ROWS)) for _ in range(_ANN_QUERIES)]

        monkeypatch.setattr(settings, "fingerprint_search_exactness", "exact")
        assert store.approximate is False
        exact = [await store.find_similar(q, 11, 0.3) for q in queries]

        monkeypatch.setattr(settings, "fingerprint_search_exactness", "approximate")
        assert store.approximate is True
        approximate = [await store.find_similar(q, 11, 0.3) for q in queries]

        recalls = []
        identical = 0
        for exact_page, approximate_page in zip(exact, approximate, strict=True):
            exact_ids = {hit.id for hit in exact_page}
            approximate_ids = {hit.id for hit in approximate_page}
            recalls.append(len(exact_ids & approximate_ids) / len(exact_ids) if exact_ids else 1.0)
            identical += [h.id for h in exact_page] == [h.id for h in approximate_page]
        return (
            sum(recalls) / len(recalls),
            min(recalls),
            identical,
            sum(len(page) for page in exact),
        )

    mean_recall, worst_recall, identical, exact_hits = asyncio.run(_run())
    print(
        f"\napproximate arm over {_ANN_QUERIES} queries / {_ANN_ROWS} rows: "
        f"mean recall {mean_recall:.4f}, worst query {worst_recall:.3f}, "
        f"ordered page identical to exact {identical}/{_ANN_QUERIES}, "
        f"{exact_hits} exact hits compared"
    )
    assert exact_hits >= _ANN_QUERIES, "the corpus produced no neighbours to recall"
    assert mean_recall >= _ANN_RECALL_FLOOR, (
        f"approximate search recall fell to {mean_recall:.4f}, below the {_ANN_RECALL_FLOOR} this "
        "test ratchets — a deployment on the approximate arm is now missing precedents it used to "
        "return"
    )


def test_the_answer_says_which_arm_answered_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search carries the arm that ran all the way out to the sentence the model reads.

    The point of the whole split: an empty page from the approximate arm is not the same claim as
    an empty page from the exact one, and a payload that does not say which is a "we have no
    precedent for this structure" waiting to happen. Driven end to end through the real entry
    point, on a query with no neighbour on file, so what is asserted is the sentence a chemist's
    answer is written from rather than a flag on a store.
    """

    async def _run() -> tuple[str, str]:
        store = await _store_or_skip()
        await store.add(record_for("pg-arm-benzene", "c1ccccc1"))
        # A perfluorinated cage shares no ECFP environment with anything else this suite indexes,
        # so both arms genuinely find nothing and the two verdicts differ only in what they claim.
        query = "FC1(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C1(F)F"

        monkeypatch.setattr(settings, "fingerprint_search_exactness", "exact")
        exact = await find_similar_molecules(store, query, threshold=0.9)
        monkeypatch.setattr(settings, "fingerprint_search_exactness", "approximate")
        approximate = await find_similar_molecules(store, query, threshold=0.9)

        assert exact.hits == [] and approximate.hits == []
        assert exact.approximate is False and approximate.approximate is True
        return exact.model_dump()["verdict"], approximate.model_dump()["verdict"]

    exact_verdict, approximate_verdict = asyncio.run(_run())
    assert "genuine negative result" in exact_verdict
    assert "genuine negative" not in approximate_verdict
    assert "NOT proof" in approximate_verdict
