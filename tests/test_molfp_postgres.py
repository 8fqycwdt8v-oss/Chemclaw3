"""Integration tests for the Postgres fingerprint store (plan steps 3.2/3.3).

Runs against a real pgvector database (CI provides one; the offline sandbox skips).
Proves the durable backend honors the same `FingerprintStore` contract as the in-memory
one: Tanimoto ranking in SQL returns most-similar-first, the threshold filters, and
substructure search works over it via the shared, backend-agnostic search functions.
"""

import asyncio
import random

import psycopg
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
    FingerprintRecord,
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

    Asserted as the **absence of a whole-table Sort** rather than as a duration: at fixture scale
    sorting a handful of rows is both correct and instant, so a timing assertion would see nothing.
    The sequential scan is disabled for the same reason as in `tests/test_reaction_records.py` — on
    one page the planner is right to scan, and the question here is what the schema offers it.

    **`Incremental Sort` is permitted and a plain `Sort` is not, and the difference is the whole
    property.** Since `094` the scan de-duplicates a shelved generation (`DISTINCT ON`, preferring
    this store's definition), so the second sort key is `(definition = …)` *within* one id — groups
    of one or two rows, ordered by the same `082` index and still streaming under the `LIMIT`. A
    plain `Sort` is the node that means the server ordered the whole table first, which is the
    defect `082` measured at 2 228 ms and 136 MB of temp.
    """

    async def _run() -> list[str]:
        store = await _store_or_skip()
        statement = f"{store._all} LIMIT %(limit)s"
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("SET LOCAL enable_seqscan = off")
            cursor = await conn.execute(
                f"EXPLAIN (FORMAT JSON) {statement}",
                {"limit": 5001, "definition": molecule_definition()},
            )
            row = await cursor.fetchone()
        nodes: list[str] = []
        pending = [row[0][0]["Plan"]] if row else []
        while pending:
            node = pending.pop()
            nodes.append(str(node["Node Type"]))
            pending.extend(node.get("Plans", []))
        return nodes

    nodes = asyncio.run(_run())
    assert "Sort" not in nodes, (
        f"the capped scan sorts the whole table before taking its slice: {nodes}"
    )
    assert any("Index Scan" in node for node in nodes), (
        f"the capped scan no longer reads in key order through an index: {nodes}"
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


def test_the_arm_survives_a_truncated_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path that actually happens: hits *and* truncation *and* the approximate arm.

    **The test above drives only the empty page, and that is why it could not see the defect.**
    `find_matches` asks for `top_k + 1`, so `hits_truncated` is the ordinary outcome of any query
    with neighbours — measured over 60 queries at the shipped defaults, the truncation branch fired
    60 times and the approximate branch **zero**, because the two were written as exclusive `if`s
    and truncation returned first. Both arms produced byte-identical text on every non-empty page.

    The two facts are independent and a chemist needs both: truncation is about *count* ("there may
    be more"), approximation is about *ranking* ("these may not be the closest"). Being told only
    the first, on the page where both are true, is the ranking risk arriving silently — which is
    the failure the exactness setting exists to prevent.
    """

    async def _run() -> tuple[str, str]:
        store = await _store_or_skip()
        # Enough near-identical neighbours that a top_k of 2 cannot hold them: truncation is
        # forced by the corpus rather than by a flag, so the fixture cannot drift away from the
        # condition it is about.
        for i, smiles in enumerate(("CCO", "CCCO", "CCCCO", "CCCCCO", "CCCCCCO")):
            await store.add(record_for(f"pg-trunc-{i}", smiles))

        monkeypatch.setattr(settings, "fingerprint_search_exactness", "approximate")
        approximate = await find_similar_molecules(store, "CCO", top_k=2, threshold=0.1)
        monkeypatch.setattr(settings, "fingerprint_search_exactness", "exact")
        exact = await find_similar_molecules(store, "CCO", top_k=2, threshold=0.1)

        assert approximate.hits, "the fixture must return hits, or it proves nothing"
        assert approximate.hits_truncated, "the fixture must truncate, or it proves nothing"
        return exact.model_dump()["verdict"], approximate.model_dump()["verdict"]

    exact_verdict, approximate_verdict = asyncio.run(_run())

    # The count warning is on both, because both truncated.
    assert "lower bound" in exact_verdict
    assert "lower bound" in approximate_verdict
    # The ranking warning is on the approximate one only — and it is *there*, which is the point.
    assert "closer one may exist" in approximate_verdict
    assert "closer one may exist" not in exact_verdict
    assert exact_verdict != approximate_verdict, (
        "both arms produced identical text on a truncated page — the arm is not reaching the model"
    )


def test_the_superseded_probe_agrees_with_the_reference_and_costs_no_scan() -> None:
    """The durable half of the partial-index probe, both of the properties it has to hold.

    `D-2026-09-09-a-rebuild-nothing-counts-reports-as-finished`.

    **Agreement**, because the in-memory backend is where every partial-index assertion in
    `tests/test_molfp.py` is made and it is only evidence about the deployment if the SQL matches
    it: a filtered Python list against `min(definition)`/`max(definition)`.

    **Cost**, because `has_superseded_records` runs on *every* similarity search, and the obvious
    spelling — `SELECT 1 … WHERE definition <> … LIMIT 1` — has to read every row before it can
    answer "none", which is exactly the healthy case. Measured on a live PostgreSQL 16.15 at
    200 000 rows all under the current definition: 26.32 ms for that form against 0.55 ms for the
    extremes, and the first grows with the corpus. What makes the difference is
    `molecule_fingerprints_definition_idx` (046), so the plan is what is asserted here rather than
    a timing that would depend on how much this test inserted.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        reference = InMemoryFingerprintStore(molecule_definition())
        current = record_for("pg-superseded-current", "CCO")
        old = record_for("pg-superseded-old", "CCCO")
        old = old.model_copy(update={"definition": old.definition + "-superseded"})

        for record in (current, old):
            await store.add(record)
            await reference.add(record)
        assert await store.has_superseded_records() is await reference.has_superseded_records()
        assert await store.has_superseded_records() is True
        # A count, not a boolean: the durable table is shared with the rest of this schema, so the
        # assertion is that this store's own superseded row is in it.
        assert await store.superseded_count() >= 1

        async with await db.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SET LOCAL enable_seqscan = off")
            await cur.execute(f"EXPLAIN (COSTS OFF) {store._definition_extremes}")
            plan = "\n".join(str(row[0]) for row in await cur.fetchall())
        assert "molecule_fingerprints_definition_idx" in plan, (
            "the superseded probe no longer reaches its index; the plan was:\n" + plan
        )
        assert "Seq Scan" not in plan

    asyncio.run(_run())


def test_a_fully_rebuilt_durable_index_reports_no_superseded_rows() -> None:
    """The counterfactual, on a table of this store's own rows only.

    Run against a scratch table rather than the shared one, because "no superseded rows anywhere"
    is not assertable in a schema every other test writes into — and it is the half that would
    otherwise pass on a build whose probe always answered True.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await db.connect(settings.postgres_dsn) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS molfp_rebuilt_probe "
                "(id TEXT NOT NULL, label TEXT NOT NULL, "
                f"bits BIT({settings.ecfp_bits}) NOT NULL, definition TEXT NOT NULL, "
                "PRIMARY KEY (id, definition))"
            )
            await conn.commit()
        try:
            store = PostgresFingerprintStore(
                "molfp_rebuilt_probe", settings.ecfp_bits, molecule_definition()
            )
            assert await store.has_superseded_records() is False, "an empty table holds nothing"
            for name, smiles in [("a", "CCO"), ("b", "CCCO")]:
                await store.add(record_for(name, smiles))
            assert await store.has_superseded_records() is False
            assert await store.superseded_count() == 0
            assert (await find_similar_molecules(store, "CCO")).index_partial is False
        finally:
            async with await db.connect(settings.postgres_dsn) as conn:
                await conn.execute("DROP TABLE IF EXISTS molfp_rebuilt_probe")
                await conn.commit()

    asyncio.run(_run())


# Two fingerprint definitions that are *both* plausible: a radius bump is a one-character config
# change (`CHEMCLAW_ECFP_RADIUS`), which is what makes a rolling upgrade able to run both at once.
_OLD_DEFINITION = "ecfp:r2:b2048"
_NEW_DEFINITION = "ecfp:r3:b2048"


def test_a_second_definitions_write_shelves_the_first_instead_of_deleting_it() -> None:
    """A definition change must *shelve* the rows it supersedes, not destroy them.

    `004_fingerprint_definition.sql` states the safety property as "a mismatched backfill only
    makes stale rows fall out of similarity search (safe), never returns a wrong score", and the
    constructor above repeats it: "the stale rows simply fall out of search until they are
    re-indexed". Both sentences describe rows that still exist.

    Measured before the fix, with the primary key on `id` alone and `definition` an ordinary column
    the upsert overwrote:

        after writer A (ecfp:r2:b2048):  rows=1   A.count=1   B.count=0
        after writer B (ecfp:r3:b2048):  rows=1   A.count=0   B.count=1
        table now: [('CCO', 'ethanol@B', 'ecfp:r3:b2048')]
        A superseded_count: 1   B superseded_count: 0

    Within one deployment mid-reindex that is invisible — the re-index is walking those rows
    anyway. The moment two writers with different definitions run at once (a rolling upgrade that
    changes `ecfp_radius`, two pods on different images, a second site) each write destroys the
    other's row, and each side's `superseded_count` then reports the *other's* population as
    "stale, re-index me" while neither index converges.

    So the key is `(id, definition)` — the shape `document_chunks` took in `041` and `note_index`
    in `039`, one directory over. The two generations coexist; each store answers over its own.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        old = PostgresFingerprintStore("molecule_fingerprints", settings.ecfp_bits, _OLD_DEFINITION)
        new = PostgresFingerprintStore("molecule_fingerprints", settings.ecfp_bits, _NEW_DEFINITION)
        # One id, two generations, deliberately different structures: which row answers is then
        # observable rather than inferred from a count.
        was = "Brc1ccc(cc1)C(=O)Nc1ccc(cc1)C(F)(F)F"
        now = "O=C(Nc1ccccc1)c1ccc(cc1)N1CCOCC1"
        await old.add(
            FingerprintRecord(
                id="pg-shelved", label=was, bits=ecfp_bitstring(was), definition=_OLD_DEFINITION
            )
        )
        await new.add(
            FingerprintRecord(
                id="pg-shelved", label=now, bits=ecfp_bitstring(now), definition=_NEW_DEFINITION
            )
        )

        after_new = await old.find_similar(ecfp_bitstring(was), 5, 0.99)
        assert [hit.id for hit in after_new] == ["pg-shelved"], (
            "the newer definition's write destroyed the older generation's row; there is no state "
            "left for either side to re-index from"
        )
        assert [hit.label for hit in after_new] == [was]
        # And the new generation is the one *its* store answers over — the shelf is scoped, not a
        # second copy of the same row.
        assert [hit.label for hit in await new.find_similar(ecfp_bitstring(now), 5, 0.99)] == [now]
        assert await new.find_similar(ecfp_bitstring(was), 5, 0.99) == []

        # And the other half of the key change, which is what keeps a re-index from doubling the
        # table on every sync: a re-write under *one* definition still updates in place.
        async with await db.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM molecule_fingerprints WHERE id = 'pg-shelved'")
            assert (await cur.fetchone())[0] == 2
        await new.add(
            FingerprintRecord(
                id="pg-shelved", label=now, bits=ecfp_bitstring(now), definition=_NEW_DEFINITION
            )
        )
        async with await db.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM molecule_fingerprints WHERE id = 'pg-shelved'")
            assert (await cur.fetchone())[0] == 2, "a repeat write under one definition inserted"

    asyncio.run(_run())


def test_a_shelved_generation_is_one_molecule_to_the_substructure_scan() -> None:
    """`all_records` is unfiltered by definition, so a shelf must not double the corpus.

    Two things break if it does, and both are chemist-visible. The scan's hits are built one per
    row, so a molecule held under two generations is reported twice; and
    `substructure_scan_max_records` bounds *rows*, so a corpus with a superseded generation on the
    shelf reaches the cap at half the molecules — `scan_truncated` on a corpus that fits.

    One row per key, then, with the searchable generation preferred: the scan re-matches the stored
    SMILES with RDKit and never touches the bits, so either generation's label is a correct
    substructure hit, and the current one is the structure this deployment standardized.

    **This one cannot fail against the pre-`094` source and that is not a defect in it**: before the
    key change a second definition *deleted* the first, so one molecule was one row by destroying
    the other. It is a guard on the consequence of the fix rather than a reproduction of the bug,
    and it does bite: driven against the widened key with the pre-`094` `_all` statement restored,
    five molecules held under two generations came back as **10 rows**, against 5 through the
    shipped `DISTINCT ON`.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        old = PostgresFingerprintStore("molecule_fingerprints", settings.ecfp_bits, _OLD_DEFINITION)
        current = await _store_or_skip()
        structure = "Ic1ccc(cc1)C(=O)N1CCN(CC1)C(=O)c1ccccc1"
        for definition, store in ((_OLD_DEFINITION, old), (molecule_definition(), current)):
            await store.add(
                FingerprintRecord(
                    id="pg-shelf-scan",
                    label=structure,
                    bits=ecfp_bitstring(structure),
                    definition=definition,
                )
            )

        rows = [r for r in await current.all_records(limit=10_000) if r.id == "pg-shelf-scan"]
        assert len(rows) == 1, f"one molecule reached the substructure scan as {len(rows)} rows"
        assert rows[0].definition == molecule_definition()

    asyncio.run(_run())


def test_a_table_still_keyed_without_its_definition_refuses_the_write() -> None:
    """Binding this store to a table whose key omits `definition` fails loudly, not quietly.

    The constructor says so about `source_keyed` and `094` says it about the definition half:
    "naming a key the table does not have is a write that fails to plan rather than one that
    silently mis-keys". Worth a test rather than a sentence, because the failure it replaces was
    silent — the pre-`094` key accepted every write and evicted a generation per definition.

    A scratch table with the *old* key, so what is asserted is the store's conflict target against
    a schema, not a statement against itself.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await db.connect(settings.postgres_dsn) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS molfp_old_key_probe "
                "(id TEXT PRIMARY KEY, label TEXT NOT NULL, "
                f"bits BIT({settings.ecfp_bits}) NOT NULL, definition TEXT NOT NULL)"
            )
            await conn.commit()
        try:
            store = PostgresFingerprintStore(
                "molfp_old_key_probe", settings.ecfp_bits, molecule_definition()
            )
            with pytest.raises(psycopg.errors.InvalidColumnReference) as refusal:
                await store.add(record_for("probe", "CCO"))
            assert "ON CONFLICT" in str(refusal.value)
        finally:
            async with await db.connect(settings.postgres_dsn) as conn:
                await conn.execute("DROP TABLE IF EXISTS molfp_old_key_probe")
                await conn.commit()

    asyncio.run(_run())
