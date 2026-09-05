"""What plan Postgres actually chooses for the fingerprint store's similarity search.

**A two-row unit test cannot see any of this, and that is the point.** Every other test of
`PostgresFingerprintStore` asserts the *rows* the statement returns, and the statement returns the
right rows either way — the defect this file exists to catch is that it returned them by reading
the whole table. `tests/test_molfp_postgres.py` seeds four molecules, where a sequential scan and
an HNSW index scan are indistinguishable in both output and latency; the cost is O(N), and the
table this statement also serves (`corpus_reactions`) is sized for Pistachio's ~10^7 patent
reactions, one conversational tool call at a time.

So the assertion here is on the **executed plan**, over a corpus large enough that the planner has
a real choice: `EXPLAIN (ANALYZE)` must show the Jaccard HNSW index driving the ordering, and the
scan must not have touched every row. This is the same property `tests/test_retention.py` asserts
about the retention sweep's thread query, by the same means, and the same correction
`retrieval/vector_index.py` and `ingest/documents/index.py` already carry: a tie-break written into
the *inner* `ORDER BY` makes the ordering underivable from the vector index, and the planner
abandons the index entirely.
"""

import asyncio
import random
from typing import Any

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.store import PostgresFingerprintStore
from tests.pg import migrated_db_or_skip

# Large enough that the planner prefers a sequential scan over the index when the ordering is not
# derivable from it, and small enough that seeding it (an HNSW insert per row) stays inside a unit
# test's budget. Measured on this database: the plan flips at ~2,000 rows already; the shipped
# statement read all 5,000 here and 20,000 at 20,000.
_CORPUS = 5000
_TOP_K = 11  # `find_matches` asks for k+1 at the default `fingerprint_top_k` of 10.


def _random_bits(rng: random.Random, width: int, set_bits: int = 60) -> str:
    """A `width`-bit string with `set_bits` ones — the density an ECFP4 of a small molecule has."""
    bits = ["0"] * width
    for index in rng.sample(range(width), set_bits):
        bits[index] = "1"
    return "".join(bits)


async def _seed(table: str, width: int, definition: str, *, source_keyed: bool) -> str:
    """Fill `table` with `_CORPUS` random fingerprints, ANALYZE it, and return a query bitstring.

    `ANALYZE` is not optional: the measured cause of a vector query taking the wrong plan is far
    more often stale statistics than the shape of the SQL, so a plan assertion on an unanalyzed
    table would be asserting the wrong thing (`retrieval/vector_index.py` records 13/20 queries
    short before `ANALYZE`, 0/20 after).
    """
    rng = random.Random(11)
    columns = (
        "source, id, label, bits, definition" if source_keyed else "id, label, bits, definition"
    )
    values = (
        f"%s, %s, %s, %s::bit({width}), %s" if source_keyed else f"%s, %s, %s::bit({width}), %s"
    )
    rows = [
        (("corpus", f"fp-{i:07d}", f"L{i}", _random_bits(rng, width), definition))
        if source_keyed
        else (f"fp-{i:07d}", f"L{i}", _random_bits(rng, width), definition)
        for i in range(_CORPUS)
    ]
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for start in range(0, len(rows), 1000):
                await cur.executemany(
                    f"INSERT INTO {table} ({columns}) VALUES ({values})", rows[start : start + 1000]
                )
            await conn.commit()
            await cur.execute(f"ANALYZE {table}")
        await conn.commit()
    return _random_bits(rng, width)


async def _drop_seed(table: str) -> None:
    """Remove this file's fixture rows again.

    Every Postgres test in the suite shares one isolation schema, so a five-thousand-row corpus
    left behind is not inert: `find_substructure_matches` scans up to
    `substructure_scan_max_records` rows and reports the scan incomplete when it hits the cap, so
    leaving these here turned `tests/test_molfp_postgres.py`'s substructure assertion red without
    touching a line of it. Measured — that is how this cleanup came to exist.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"DELETE FROM {table} WHERE id LIKE 'fp-%%'")
        await conn.commit()


async def _plan_of(store: PostgresFingerprintStore, query_bits: str) -> dict[str, Any]:
    """The executed plan tree of this store's shipped similarity statement.

    Runs `store._similar` verbatim rather than a transcription of it, because a transcribed
    statement is a second declaration of the thing under test and would keep passing after the
    shipped one regressed.
    """
    params = {
        "q": query_bits,
        "threshold": 0.0,
        "k": _TOP_K,
        "definition": store._definition,
    }
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF) " + store._similar.replace("%%", "%%"),
            params,
        )
        row = await cur.fetchone()
    assert row is not None
    plan: dict[str, Any] = row[0][0]["Plan"]
    return plan


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node of a plan tree, parents before children."""
    found = [plan]
    for child in plan.get("Plans", []):
        found.extend(_nodes(child))
    return found


def _rows_examined(plan: dict[str, Any]) -> int:
    """How many rows the plan's scan nodes read, rows discarded by their filters included.

    `Actual Rows` alone undercounts by exactly the amount that matters: a `Seq Scan` whose filter
    rejects almost everything reports few rows out having read the whole table.
    """
    return sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _nodes(plan)
        if node["Node Type"].endswith("Scan")
    )


@pytest.mark.parametrize(
    ("table", "width_setting", "definition_factory", "source_keyed", "index"),
    [
        pytest.param(
            "molecule_fingerprints",
            "ecfp_bits",
            molecule_definition,
            False,
            "molecule_fingerprints_jaccard_idx",
            id="unsourced",
        ),
        pytest.param(
            "corpus_reactions",
            "drfp_bits",
            reaction_definition,
            True,
            "corpus_reactions_jaccard_idx",
            id="source-keyed",
        ),
    ],
)
def test_similarity_search_is_answered_by_the_jaccard_index(
    table: str, width_setting: str, definition_factory: Any, source_keyed: bool, index: str
) -> None:
    """The shipped statement rides the HNSW index and never materialises the table.

    Both key shapes, because the tie-break the fix moves is the one place they diverge: the
    source-keyed form sorts by `source, id` and the unsourced form by `id` alone, so an outer sort
    that referenced a column the subquery does not project would fail on one table and not the
    other. `corpus_reactions` is also the table this matters most on — it is sized for a ~10^7-row
    patent corpus, where O(N) per conversational tool call is the whole cost of the defect.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        width = int(getattr(settings, width_setting))
        definition = definition_factory()
        store = PostgresFingerprintStore(table, width, definition, source_keyed=source_keyed)
        query_bits = await _seed(table, width, definition, source_keyed=source_keyed)
        try:
            plan = await _plan_of(store, query_bits)
            node_types = [node["Node Type"] for node in _nodes(plan)]
            assert "Seq Scan" not in node_types, (
                f"the similarity statement read all of {table} instead of using {index}: "
                f"{node_types}"
            )
            assert any(node.get("Index Name") == index for node in _nodes(plan)), (
                f"no scan of {index} in the plan: {node_types}"
            )
            examined = _rows_examined(plan)
            assert examined < _CORPUS // 10, (
                f"the scan touched {examined} of {_CORPUS} rows — the cost this statement's "
                "shape is supposed to bound is O(N)"
            )
        finally:
            await _drop_seed(table)

    asyncio.run(_run())
