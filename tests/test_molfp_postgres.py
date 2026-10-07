"""Integration tests for the Postgres fingerprint store.

Runs against a real pgvector database (skipped without one). The durable backend honours the
in-memory backend's `FingerprintStore` contract: Tanimoto ranking in SQL, threshold filtering,
substructure search through the shared functions, and definition-keyed generations.
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


async def test_similarity_ranking_in_sql() -> None:
    """The SQL backend ranks Tanimoto neighbors most-similar-first, honoring threshold."""
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
        (hits[i].similarity or 0.0) >= (hits[i + 1].similarity or 0.0) for i in range(len(hits) - 1)
    )


async def test_tie_break_order_matches_the_in_memory_backend() -> None:
    """Equal-similarity hits come back in the same id order from both backends.

    The SQL side orders by `id COLLATE "C"` to match Python's code-point sort; a locale collation
    would reorder mixed-case ids. Asserted at the store level (`find_matches`), where the collation
    lives.
    """
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


async def test_the_durable_page_is_the_exact_top_k_not_an_approximation() -> None:
    """The durable search returns the exact page, ties included, which an ANN cannot.

    Tanimoto over sparse bits puts many rows at identical similarity, and `ORDER BY distance,
    id COLLATE "C"` breaks ties across the whole table, which no truncated candidate set reproduces.
    200 rows of one structure, a page of 50: exactly the 50 lowest ids, in order. This pins the
    contract rather than the plan, so switching to an index-ordered candidate set is a deliberate
    decision.
    """
    store = await _store_or_skip()
    mem_store = InMemoryFingerprintStore(definition=molecule_definition())
    # A structure with no near neighbour among this suite's fixtures, so a 0.99 threshold isolates
    # these rows in the shared table. A long alkanol would not do: ECFP4 over repeated CH2
    # environments ties C8-ol and C13-ol at 1.0.
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


def test_the_capped_scan_reads_in_key_order_without_sorting_the_table() -> None:
    """`all_records(limit=...)` does not sort the whole table to return `limit` rows.

    The slice is ordered by `id COLLATE "C"`, which the collation-specific primary key cannot
    provide, so an index (`082`) supplies it. Asserted as the absence of a plain `Sort` node rather
    than a duration (sorting a fixture is instant); sequential scans are disabled to ask what the
    schema offers. `Incremental Sort` is permitted: `DISTINCT ON` de-duplicating shelved generations
    sorts groups of one or two rows within an id while streaming under the `LIMIT`.
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


async def test_upsert_and_substructure_over_postgres() -> None:
    """Re-adding an id replaces it; substructure search works over the durable backend."""
    store = await _store_or_skip()
    await store.add(record_for("pg-mol", "CCO"))
    await store.add(record_for("pg-mol", "CC(=O)O"))  # replace ethanol with acetic acid

    acids = {r.smiles for r in (await find_substructure_matches(store, "C(=O)[OH]")).hits}
    assert "CC(=O)O" in acids  # the replaced record now matches the acid pattern


async def test_emptiness_and_count_are_scoped_to_the_stores_definition() -> None:
    """Emptiness and count are scoped to the store's definition.

    Asserted through a store pinned to a definition nothing was indexed under, which is robust
    against the shared database and a real state after a definition change.
    """
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


_ANN_TABLE = "molfp_approximate_probe"
_ANN_ROWS = 10_000
_ANN_QUERIES = 40
_ANN_DEFINITION = "ecfp:r2:b2048"
# The floor this test ratchets: the mean fraction of the exact page the approximate arm returns over
# `_ANN_QUERIES` queries, pinned below the measured value so index/planner drift is not a failure.
# It does not claim the arms agree on the ordered page; that difference is ties, not misses.
_ANN_RECALL_FLOOR = 0.95


def _probe_bits(index: int) -> str:
    """A sparse fingerprint with the layered structure a real ECFP corpus has.

    Uniform random bits would make every pair equidistant. Three layers (scaffold, series, own
    substitution) give a continuum with a dense head, and an exact duplicate every fiftieth record
    gives the tie-break real ties.
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

    A table of its own: the index's candidates are filtered by `definition` afterwards, so other
    tests' rows would consume candidate slots and skew recall; and the corpus is large enough for
    the planner to choose the HNSW index, which would slow the shared table.
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
    """The approximate statement takes an HNSW Index Scan.

    Otherwise the planner could serve it with a sequential scan, return the exact answer, and make
    the recall measurement meaningless. The plan must name the table's `bit_jaccard_ops` index.
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

    Recall (how much of the exact page the approximate page contains) is what a chemist loses;
    ordered-page agreement is not, because ties broken by id across the whole table cannot be
    reproduced from a partial candidate set. Only recall is ratcheted, as a mean over queries, since
    HNSW recall is a distribution. The measured value is printed.
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
    """The answer says which arm answered it, end to end.

    An empty approximate page is a weaker claim than an empty exact page, so the sentence a
    chemist's answer is written from must say which arm ran.
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
    """The approximate-arm notice survives a truncated page.

    `find_matches` asks for `top_k + 1`, so truncation is the ordinary outcome of any query with
    neighbours. Truncation (there may be more) and approximation (these may not be the closest) are
    independent, and both must be said.
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


async def test_the_superseded_probe_agrees_with_the_reference_and_costs_no_scan() -> None:
    """The superseded-row probe agrees with the reference and costs no scan.

    Agreement: the in-memory backend is where partial-index assertions are made. Cost:
    `has_superseded_records` runs on every similarity search, and `WHERE definition <> ... LIMIT 1`
    reads every row in the healthy case; the min/max form uses
    `molecule_fingerprints_definition_idx`, so the plan is asserted.
    """
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


async def test_a_fully_rebuilt_durable_index_reports_no_superseded_rows() -> None:
    """A fully rebuilt durable index reports no superseded rows.

    Run on a scratch table, since "none anywhere" cannot be asserted in the shared table; this
    catches a probe that always answers True.
    """
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


# Two fingerprint definitions that are *both* plausible: a radius bump is a one-character config
# change (`CHEMCLAW_ECFP_RADIUS`), which is what makes a rolling upgrade able to run both at once.
_OLD_DEFINITION = "ecfp:r2:b2048"
_NEW_DEFINITION = "ecfp:r3:b2048"


async def test_a_second_definitions_write_shelves_the_first_instead_of_deleting_it() -> None:
    """A second definition's write shelves the first generation instead of deleting it.

    `004_fingerprint_definition.sql` promises stale rows fall out of search, i.e. still exist. With
    the key on `id` alone, two writers with different definitions (rolling upgrade, two images)
    destroy each other's rows and neither index converges. The key is `(id, definition)`; the
    generations coexist and each store answers over its own.
    """
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
        shelved = await cur.fetchone()
        assert shelved is not None and shelved[0] == 2
    await new.add(
        FingerprintRecord(
            id="pg-shelved", label=now, bits=ecfp_bitstring(now), definition=_NEW_DEFINITION
        )
    )
    async with await db.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM molecule_fingerprints WHERE id = 'pg-shelved'")
        after = await cur.fetchone()
        assert after is not None and after[0] == 2, "a repeat write under one definition inserted"


async def test_a_shelved_generation_is_one_molecule_to_the_substructure_scan() -> None:
    """A shelved generation is one molecule to the substructure scan.

    `all_records` is unfiltered by definition, so without de-duplication a molecule would be
    reported twice and `substructure_scan_max_records` would be reached at half the molecules. One
    row per key, preferring the searchable generation; either label is a correct hit since the scan
    re-matches SMILES.
    """
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


async def test_a_table_still_keyed_without_its_definition_refuses_the_write() -> None:
    """Binding the store to a table whose key omits `definition` fails loudly.

    The conflict target names a key the table lacks, so the write fails to plan rather than silently
    mis-keying. A scratch table with the old key is used.
    """
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
