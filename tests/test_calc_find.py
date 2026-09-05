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

from chemclaw.connectors.calc.server import tools
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import InMemoryArtifactStore
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    InMemoryStore,
    StoredResult,
    molecule_hash,
)

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
        assert len(found) == 1
        record = found[0]
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
            async def find(self, query: CalculationQuery) -> list[StoredResult]:
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


def test_an_unknown_calc_type_is_refused_naming_what_the_store_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most expensive wrong answer this tool can give is a confident empty list.

    Matching is exact equality, the real types are `xtb.sp`, `xtb.hess`, … , and this tool's own
    docstring used to offer `"xtb"` as a worked example — measured: `calc_type='xtb.sp'` found 1
    row, `calc_type='xtb'` found 0, and the docstring instructs the model to report 0 as "the store
    has nothing". A misspelt filter and an empty store were indistinguishable.
    """

    async def _run() -> None:
        store = await _populated()
        monkeypatch.setattr(tools, "default_store", lambda: store)
        with pytest.raises(ValueError, match="has ever been stored") as refused:
            await tools.find_calculations(calc_type="xtb")
        assert "dft" in str(refused.value) and "pka" in str(refused.value)

        # A type that exists but matches nothing under the other filters is still an empty answer.
        assert (
            await tools.find_calculations(calc_type="pka", since="2099-01-01T00:00:00+00:00") == []
        )

    asyncio.run(_run())


def test_an_empty_store_answers_with_an_empty_list_rather_than_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty store's empty list is exactly true, and must not become a refusal."""

    async def _run() -> None:
        monkeypatch.setattr(tools, "default_store", InMemoryStore)
        assert await tools.find_calculations(calc_type="pka") == []

    asyncio.run(_run())


def test_a_record_says_which_epoch_definition_its_row_was_computed_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three states, because a pre-column row's epoch is unrecoverable rather than old.

    The cache was never at risk — the epoch is folded into the key — but this listing served rows
    from two epochs for one subject with only `computed_at` to tell them apart, while the epoch log
    says the earlier ones carry a wrong linear-rotor entropy and an incomplete reactivity panel.
    """

    async def _run() -> None:
        store = InMemoryStore()
        current = _stored("CCO", calc_type="pka", calc_version="v3", at=_NOW)
        await store.put(current)
        await store.put(
            _stored("CCN", calc_type="pka", calc_version="v3", at=_NOW).model_copy(
                update={"epoch": "1"}
            )
        )
        await store.put(
            _stored("CCC", calc_type="pka", calc_version="v3", at=_NOW).model_copy(
                update={"epoch": ""}
            )
        )
        monkeypatch.setattr(tools, "default_store", lambda: store)
        statuses = {
            record.calc_ref: record.epoch_status
            for record in await tools.find_calculations(calc_type="pka")
        }
        assert statuses[current.key.as_str()] == "current"
        assert sorted(statuses.values()) == ["current", "superseded", "unrecorded"]

    asyncio.run(_run())


def test_a_listing_resolves_an_offloaded_array_and_drops_one_that_has_been_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`find_calculations` rendered a content hash `fetch_artifact` will not resolve.

    `fetch_artifact` takes `<calculation key>#<name>`; the row holds a bare digest, which no tool
    takes. And after `durable/artifact_eviction.py` reclaims the blob, `calculation_artifacts`
    cascades away while `calculation_results` keeps its row — measured, the offloading store's
    `get` correctly reported a miss and recomputed while the listing went on showing the digest as
    if it were an answer.
    """

    async def _run() -> None:
        results, blobs = InMemoryStore(), InMemoryArtifactStore()
        key = CalculationKey.build("xtb.hess", "v1", {"structure": "st_1"})
        ref = await blobs.put(key.as_str(), "hessian.npy", b"packed array")
        assert ref is not None
        await results.put(
            StoredResult(key=key, result={"hessian_artifact": ref.content_hash}, created_at=_NOW)
        )
        monkeypatch.setattr(tools, "default_store", lambda: results)
        monkeypatch.setattr(tools, "default_artifact_store", lambda: blobs)

        [record] = await tools.find_calculations(calc_type="xtb.hess")
        assert record.result["hessian_artifact"] == f"{key.as_str()}#hessian.npy"

        # Now the eviction sweep reclaims the blob and its link, leaving the result row behind.
        evicted = InMemoryArtifactStore()
        monkeypatch.setattr(tools, "default_artifact_store", lambda: evicted)
        [after] = await tools.find_calculations(calc_type="xtb.hess")
        assert "hessian_artifact" not in after.result

    asyncio.run(_run())
