"""Browsing the calculation store: `CalculationQuery`, both backends, and the tool over them.

The store has always been addressable — give it the exact key and it hands back the result — and
that is all it was. "What have we already computed for this molecule" had no answer, so the only
way to reach a stored value was to ask for the identical calculation again and get a cache hit.
For xTB that is merely wasteful; for the DFT results W2.1 started persisting it means hours of
compute sitting in a table nothing could look into.

What is pinned here is the part that is easy to get wrong: `input_hash` is not reversible, so a
molecule is found by hashing the query the same way the key was built — which is also why an
equivalent SMILES for the same molecule has to find the same rows.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from chemclaw.connectors.calc.server import tools
from chemclaw.core import db
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.science.calc import store as store_module
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    InMemoryStore,
    StoredResult,
    molecule_hash,
)
from tests.pg import migrated_db_or_skip

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _stored(
    smiles: str,
    calc_type: str = "dft",
    calc_version: str = "b3lyp",
    at: datetime | None = None,
    **result: Any,
) -> StoredResult:
    """One persisted result for `smiles`, keyed exactly as a calculator would key it."""
    return StoredResult(
        key=CalculationKey.build(
            calc_type=calc_type,
            calc_version=calc_version,
            inputs={"smiles": require_canonical_smiles(smiles)},
        ),
        result=result or {"energy": -1.0},
        created_at=at,
    )


async def _populated() -> InMemoryStore:
    """A store holding three results across two molecules, two types and two versions."""
    store = InMemoryStore()
    await store.put(_stored("CCO", at=_NOW - timedelta(days=2), energy=-1.0))
    await store.put(_stored("CCO", calc_type="pka", calc_version="v3", at=_NOW - timedelta(days=1)))
    await store.put(_stored("CCN", at=_NOW))
    return store


def test_an_empty_query_returns_everything_newest_first() -> None:
    """No filter is "what is in the store", not an error — and order is the useful part."""

    async def _run() -> None:
        store = await _populated()
        found = await store.find(CalculationQuery())
        assert [s.key.calc_type for s in found] == ["dft", "pka", "dft"]
        dates = [s.created_at for s in found if s.created_at is not None]
        assert dates == sorted(dates, reverse=True)

    asyncio.run(_run())


def test_a_molecule_is_found_by_hashing_the_query_not_by_scanning() -> None:
    """`input_hash` is a hash of the input mapping and non-reversible; matching is equality."""

    async def _run() -> None:
        store = await _populated()
        found = await store.find(CalculationQuery(smiles="CCO"))
        assert len(found) == 2
        assert {s.key.input_hash for s in found} == {molecule_hash("CCO")}

    asyncio.run(_run())


def test_a_molecule_filter_is_refused_on_a_structure_keyed_family() -> None:
    """A molecule does not determine a 3-D structure, so it cannot address that family.

    The xTB task results key on `(structure_id, charge, multiplicity)`. Answering "nothing found"
    would read as "nothing has been computed", which is the one thing this tool cannot afford.
    """
    with pytest.raises(ValueError, match="keyed by 3-D structure"):
        CalculationQuery(smiles="CCO", calc_type="xtb.energy")
    # Without a molecule filter the same family is perfectly queryable.
    assert CalculationQuery(calc_type="xtb.energy").calc_type == "xtb.energy"


def test_an_equivalent_smiles_finds_the_same_rows() -> None:
    """The whole point of canonicalising in the query: "OCC" is ethanol too."""

    async def _run() -> None:
        store = await _populated()
        assert len(await store.find(CalculationQuery(smiles="OCC"))) == 2

    asyncio.run(_run())


def test_type_and_version_narrow_independently() -> None:
    """A version filter is what answers "is the old number still what we have on file"."""

    async def _run() -> None:
        store = await _populated()
        assert len(await store.find(CalculationQuery(calc_type="dft"))) == 2
        assert len(await store.find(CalculationQuery(calc_version="v3"))) == 1
        assert await store.find(CalculationQuery(calc_type="dft", calc_version="v3")) == []

    asyncio.run(_run())


def test_the_date_window_is_inclusive_at_both_ends() -> None:
    """A result computed exactly at the boundary is inside the window it names."""

    async def _run() -> None:
        store = await _populated()
        assert len(await store.find(CalculationQuery(since=_NOW))) == 1
        assert len(await store.find(CalculationQuery(until=_NOW - timedelta(days=2)))) == 1
        assert (
            len(await store.find(CalculationQuery(since=_NOW - timedelta(days=1), until=_NOW))) == 2
        )

    asyncio.run(_run())


def test_an_undated_result_falls_outside_every_window() -> None:
    """A result of unknown date fails a windowed query rather than passing it.

    It cannot be shown to fall inside the window, and a question about a period should not be
    answered with a result whose date nobody knows.
    """

    async def _run() -> None:
        store = InMemoryStore()
        await store.put(_stored("CCO"))  # no created_at
        assert await store.find(CalculationQuery(since=_NOW - timedelta(days=365))) == []
        assert len(await store.find(CalculationQuery())) == 1

    asyncio.run(_run())


def test_an_undated_result_sorts_ahead_of_every_dated_one() -> None:
    """Where an undated row lands in the ordering — and that mixing the two kinds does not crash.

    The in-memory store keeps no clock, so `find`'s docstring says insertion order stands in for
    time; the consistent reading is that a row nobody dated is the newest thing the store knows.
    Nothing stated it, and writing this test found out why it had never come up: the sentinel that
    expressed it, `created_at or datetime.max`, is **naive**, while every real `created_at` in this
    codebase is timezone-aware. One store holding one dated and one undated result raised
    `TypeError: can't compare offset-naive and offset-aware datetimes` — not a wrong order, no
    order at all. `test_an_undated_result_falls_outside_every_window` never saw it because a
    single-element list is never compared.

    This is also the only place the two backends *can* differ by construction — Postgres stamps
    `created_at` itself and has no undated row to place — so the in-memory choice is pinned here or
    nowhere. Reversing the partition (`dated + undated`) still fails this.
    """

    async def _run() -> None:
        store = await _populated()  # three dated results, newest at `_NOW`
        await store.put(_stored("CCC"))  # no created_at
        found = await store.find(CalculationQuery())
        assert found[0].created_at is None
        dated = [s.created_at for s in found[1:] if s.created_at is not None]
        assert len(dated) == len(found) - 1, "an undated row sorted in among the dated ones"
        assert dated == sorted(dated, reverse=True)

    asyncio.run(_run())


def test_limit_caps_the_page() -> None:
    """The store is never evicted (D-011), so an uncapped browse is a full scan of it."""

    async def _run() -> None:
        store = await _populated()
        assert len(await store.find(CalculationQuery(limit=1))) == 1

    asyncio.run(_run())


def test_the_tool_returns_records_carrying_a_citable_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`calc_ref` is the flat key a note's `calc_refs` cites, so a found value stays traceable."""

    async def _run() -> None:
        store = await _populated()
        monkeypatch.setattr(tools, "default_store", lambda: store)
        found = await tools.find_calculations(smiles="CCO", calc_type="dft")
        assert len(found.hits) == 1
        record = found.hits[0]
        assert record.calc_ref.startswith("dft@b3lyp:")
        assert record.result == {"energy": -1.0}
        assert record.calc_type == "dft"

    asyncio.run(_run())


def test_the_tool_clamps_a_limit_past_the_configured_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The argument is a request, not a permission: the cap is the deployment's, not the model's."""

    async def _run() -> None:
        seen: list[CalculationQuery] = []

        class _Recording(InMemoryStore):
            async def find(self, query: CalculationQuery) -> store_module.CalculationPage:
                seen.append(query)
                return await super().find(query)

        monkeypatch.setattr(tools, "default_store", _Recording)
        await tools.find_calculations(limit=10_000)
        assert seen[0].limit == settings.calc_find_max_results

        await tools.find_calculations(limit=0)
        assert seen[1].limit == 1  # a zero-row page is not a query either

    asyncio.run(_run())


def test_the_tool_refuses_a_date_it_cannot_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping it would answer a question about a window with results from outside it."""

    async def _run() -> None:
        monkeypatch.setattr(tools, "default_store", InMemoryStore)
        with pytest.raises(ValueError):
            await tools.find_calculations(since="last Tuesday")

    asyncio.run(_run())


def test_the_browse_marks_a_row_whose_epoch_was_never_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three standings, and only two of them can be a listing.

    `CALCULATION_EPOCH` rides inside `params_hash`, which is neither a filter nor invertible, so
    the browse used to hand a superseded row back beside its replacement with nothing to tell them
    apart — and `find_calculations` tells the model to use a listed value instead of recomputing
    and to cite its `calc_ref` in a knowledge note. A row whose *recorded* epoch is not the current
    one is now excluded outright (`store._matches`), because such a row is wrong rather than old.

    The third standing is the one this asserts: a row written before migration 090 records no
    epoch, cannot be classified after the fact, and is therefore returned and **marked**. Hiding it
    would answer "nothing found" about an entire existing store the day the migration ran; calling
    it current would be the claim the column exists to stop being guessed at.
    """

    async def _run() -> list[tuple[str, bool]]:
        store = InMemoryStore()
        current = _stored("CCO", calc_type="pka", calc_version="v3", energy=-2.0)
        await store.put(current.model_copy(update={"epoch": store_module.CALCULATION_EPOCH}))
        unrecorded = _stored("CCN", calc_type="pka", calc_version="v3", energy=-3.0)
        await store.put(unrecorded)
        superseded = _stored("CCC", calc_type="pka", calc_version="v3", energy=-4.0)
        await store.put(superseded.model_copy(update={"epoch": "0"}))

        monkeypatch.setattr(tools, "default_store", lambda: store)
        found = await tools.find_calculations(calc_type="pka")
        return [(record.calc_ref, record.epoch_recorded) for record in found.hits]

    listed = asyncio.run(_run())
    assert len(listed) == 2, f"a superseded epoch was listed, or a valid row was hidden: {listed}"
    assert sorted(flag for _, flag in listed) == [False, True], (
        f"the row with no recorded epoch was not marked as such: {listed}"
    )


# --- what a page does not say ------------------------------------------------------------------


async def _many(count: int, calc_type: str = "pka") -> InMemoryStore:
    """`count` stored results for one molecule, distinguished only by their parameters."""
    store = InMemoryStore()
    for index in range(count):
        await store.put(
            StoredResult(
                key=CalculationKey.build(
                    calc_type=calc_type,
                    calc_version="v3",
                    inputs={"smiles": require_canonical_smiles("CCO")},
                    params={"temperature_k": 298 + index},
                ),
                result={"pka": 15.9},
                created_at=_NOW - timedelta(minutes=index),
            )
        )
    return store


def test_a_capped_page_says_how_many_it_is_a_page_of(monkeypatch: pytest.MonkeyPatch) -> None:
    """30 stored rows answered as 20 records, with nothing saying ten more existed.

    The measured "before": `find_calculations(smiles="CCO", calc_type="pka")` over a store holding
    30 returned a bare `list` of 20 `CalculationRecord`s whose fields are `calc_ref`, `calc_type`,
    `calc_version`, `compute_seconds`, `computed_at`, `epoch_recorded`, `provenance`, `result` and
    `result_omitted` — a per-row payload marker and nothing about the *list*.
    """

    async def _run() -> tools.CalculationSearch:
        store = await _many(30)
        monkeypatch.setattr(tools, "default_store", lambda: store)
        # Annotated rather than returned straight through: the `@server.tool()` decorator erases
        # the return type, and `mypy --strict` refuses to return `Any` from a typed function.
        found: tools.CalculationSearch = await tools.find_calculations(
            smiles="CCO", calc_type="pka"
        )
        return found

    found = asyncio.run(_run())
    assert len(found.hits) == 20
    assert found.total_matched == 30
    assert found.hits_truncated is True
    assert "PARTIAL RESULT: 20 of 30" in found.verdict


def test_a_complete_page_says_it_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: an unqualified answer has to be available, or the flag means nothing."""

    async def _run() -> tools.CalculationSearch:
        store = await _many(3)
        monkeypatch.setattr(tools, "default_store", lambda: store)
        # Annotated rather than returned straight through: the `@server.tool()` decorator erases
        # the return type, and `mypy --strict` refuses to return `Any` from a typed function.
        found: tools.CalculationSearch = await tools.find_calculations(
            smiles="CCO", calc_type="pka"
        )
        return found

    found = asyncio.run(_run())
    assert (found.total_matched, found.hits_truncated) == (3, False)
    assert "complete answer" in found.verdict


def test_the_clamped_ceiling_is_visible_rather_than_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`limit=999` is clamped to the deployment's 50; the total is what reveals the clamp."""

    async def _run() -> tools.CalculationSearch:
        store = await _many(settings.calc_find_max_results + 10)
        monkeypatch.setattr(tools, "default_store", lambda: store)
        found: tools.CalculationSearch = await tools.find_calculations(
            smiles="CCO", calc_type="pka", limit=999
        )
        return found

    found = asyncio.run(_run())
    assert len(found.hits) == settings.calc_find_max_results
    assert found.total_matched == settings.calc_find_max_results + 10
    assert found.hits_truncated is True


def test_a_store_that_reports_no_page_is_read_as_possibly_incomplete() -> None:
    """`ResultStore` is a Protocol: a plain-list store may not be read as claiming completeness."""
    rows = [
        StoredResult(
            key=CalculationKey.build(
                calc_type="pka", calc_version="v3", inputs={"smiles": "CCO"}, params={"i": i}
            ),
            result={"pka": 15.9},
        )
        for i in range(5)
    ]
    assert store_module.as_page(rows, 5).truncated is True
    assert store_module.as_page(rows, 20).truncated is False
    assert store_module.as_page(rows, 20).total_matched == 5


async def _write_unreadable_row(key: CalculationKey) -> None:
    """Put a jsonb value that is not a result object into `calculation_results`, bypassing `put`.

    The column is bare `JSONB NOT NULL`, so a string, an array or a number is a value a restore, an
    operator or a calculation server returning an unchecked shape can leave behind — and the store's
    own `put` is exactly the path such a row did not take.
    """
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO calculation_results (key, calc_type, calc_version, input_hash, "
            "params_hash, result, provenance, epoch) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'computed', %s) "
            "ON CONFLICT (key) DO UPDATE SET result = EXCLUDED.result",
            (
                key.as_str(),
                key.calc_type,
                key.calc_version,
                key.input_hash,
                key.params_hash,
                Jsonb([1, 2, 3]),
                store_module.CALCULATION_EPOCH,
            ),
        )
        await conn.commit()


def test_a_dropped_row_is_counted_rather_than_leaving_a_shorter_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_readable_row`'s own docstring measured "seven rows of which one held a jsonb string".

    It fixed the crash — the browse no longer answers zero — and left the reader unable to tell six
    of seven from seven of seven. A calculation whose row cannot be read back still exists, and a
    model told "we have two" about three is being handed the same authoritative absence the row cap
    hands it.
    """

    async def _run() -> tools.CalculationSearch:
        await migrated_db_or_skip()
        store = PostgresStore()
        for index in range(2):
            await store.put(
                StoredResult(
                    key=CalculationKey.build(
                        calc_type="probe.unreadable",
                        calc_version="v1",
                        inputs={"smiles": require_canonical_smiles("CCO")},
                        params={"i": index},
                    ),
                    result={"pka": 15.9},
                    epoch=store_module.CALCULATION_EPOCH,
                )
            )
        await _write_unreadable_row(
            CalculationKey.build(
                calc_type="probe.unreadable",
                calc_version="v1",
                inputs={"smiles": require_canonical_smiles("CCO")},
                params={"i": "corrupt"},
            )
        )
        monkeypatch.setattr(tools, "default_store", PostgresStore)
        found: tools.CalculationSearch = await tools.find_calculations(calc_type="probe.unreadable")
        return found

    found = asyncio.run(_run())
    assert len(found.hits) == 2, "the unreadable row was handed back as a result"
    assert found.total_matched == 3, "the row that could not be read stopped being counted at all"
    assert found.rows_unreadable == 1
    assert "1 matching row(s) could not be read back" in found.verdict
