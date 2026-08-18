"""The corpus-fidelity lane's own logic, offline.

`make live-data` is by definition run against a seeded checkout and a live database, so what is
testable here is not "did the corpus arrive" — that needs the corpus. What is testable is the part
that decides *whether a green result means anything*, and every invariant below is one this lane
already got wrong once while being written:

- A 0% yield read through a truthiness test becomes "unknown". The first verification script had
  exactly that bug and mis-reported 21 of 400 records; 644 of the seeded corpus are 0.00%.
- A blank cell in a published table is an omitted reagent — a real control condition, 480 no-ligand
  and 720 no-base rows in one screen — and has to compare equal to the seeded record's *absent*
  input rather than to an empty string. The first version raised `KeyError` and could not read a
  fifth of that dataset at all.
- A dataset declared unreachable that starts being accepted is the failure that matters most,
  because the only way to accept it is to have invented a structure the source never published.
  A check that only looked for regressions in one direction would call that green.
"""

from __future__ import annotations

from typing import Any

from chemclaw.cli.live_data import (
    _DATASETS,
    Check,
    DataRun,
    Dataset,
    _identifier,
    _published_key,
    _seeded_yield,
    check_adapter_matches_its_declaration,
    check_seeding_is_faithful,
    report,
)
from chemclaw.ingest.eln.ord import Component, OrdReaction, Role


def _payload(**inputs: str | None) -> dict[str, Any]:
    """An ORD-shaped export carrying those named inputs; a None value omits the input entirely."""
    return {
        "inputs": {
            name: {"components": [{"identifiers": [{"type": "SMILES", "value": value}]}]}
            for name, value in inputs.items()
            if value is not None
        }
    }


def test_the_binding_names_a_dataset_id_for_every_published_row() -> None:
    """Every dataset resolves to at least one ORD `datasetId`, by name or by partition.

    A dataset that resolved to none would silently contribute an empty seeded side and pass its
    own faithfulness check by comparing nothing against nothing.
    """
    for dataset in _DATASETS:
        assert dataset.dataset_ids(), dataset.csv_name
        assert dataset.yield_column
        assert dataset.factors
        if dataset.partition_column is not None:
            assert dataset.partitions
        if not dataset.reachable:
            assert dataset.refusal, "an unreachable dataset must say why in one line"


def test_a_zero_yield_reads_as_zero_and_not_as_missing() -> None:
    """0.0 is a measurement — the combination failed — and must not collapse into None.

    The whole reason `_seeded_yield` tests `is not None` rather than truthiness.
    """
    zero = {
        "outcomes": [
            {"products": [{"measurements": [{"type": "YIELD", "percentage": {"value": 0.0}}]}]}
        ]
    }
    assert _seeded_yield(zero) == 0.0
    assert _seeded_yield({"outcomes": []}) is None


def test_an_absent_input_is_a_control_condition_not_an_error() -> None:
    """A reagent the screen deliberately omitted reads as None, on both sides of the comparison."""
    assert _identifier(_payload(base="[OH-].[Na+]"), "base") == "[OH-].[Na+]"
    assert _identifier(_payload(base=None), "base") is None

    dataset = Dataset(
        csv_name="x.csv",
        dataset_id="d",
        factors=(("base", "base_smiles"),),
        yield_column="y",
    )
    assert _published_key(dataset, {"base_smiles": "  ", "y": "5"}) == ("d", None, 5.0)


def test_a_swapped_yield_fails_faithfulness_even_though_the_count_is_right(tmp_path: Any) -> None:
    """Multiset equality, not a row count: two rows with exchanged yields must not pass."""
    csv_path = tmp_path / "x.csv"
    csv_path.write_text("base_smiles,y\nCCO,10\nCCC,20\n", encoding="utf-8")
    dataset = Dataset(
        csv_name="x.csv", dataset_id="d", factors=(("base", "base_smiles"),), yield_column="y"
    )

    def seeded(first: float, second: float) -> dict[str, list[dict[str, Any]]]:
        rows = []
        for smiles, value in (("CCO", first), ("CCC", second)):
            payload = _payload(base=smiles)
            payload["datasetId"] = "d"
            payload["outcomes"] = [
                {
                    "products": [
                        {"measurements": [{"type": "YIELD", "percentage": {"value": value}}]}
                    ]
                }
            ]
            rows.append(payload)
        return {"d": rows}

    original = _DATASETS
    try:
        import chemclaw.cli.live_data as module

        module._DATASETS = (dataset,)
        assert check_seeding_is_faithful(tmp_path, seeded(10.0, 20.0))[0].passed
        assert not check_seeding_is_faithful(tmp_path, seeded(20.0, 10.0))[0].passed
    finally:
        module._DATASETS = original


def test_a_dataset_declared_unreachable_that_starts_mapping_is_a_failure() -> None:
    """The direction that matters most: accepting a refused record means inventing a structure."""
    refused_dataset = Dataset(
        csv_name="x.csv",
        dataset_id="d",
        factors=(("base", "base_smiles"),),
        yield_column="y",
        reachable=False,
        refusal="no published structure for one component",
    )
    original = _DATASETS
    try:
        import chemclaw.cli.live_data as module

        module._DATASETS = (refused_dataset,)
        assert check_adapter_matches_its_declaration({}, {"d": 5})[0].passed
        invented = OrdReaction(
            reaction_id="r",
            inputs=[Component(smiles="CCO", role=Role.REACTANT)],
            outcomes=[Component(smiles="CCC", role=Role.PRODUCT)],
            provenance="a structure nobody published",
        )
        assert not check_adapter_matches_its_declaration({"d": [invented]}, {"d": 5})[0].passed
        # And a reachable dataset that stops mapping is the ordinary regression.
        module._DATASETS = (
            Dataset(
                csv_name="x.csv",
                dataset_id="d",
                factors=(("base", "base_smiles"),),
                yield_column="y",
            ),
        )
        assert not check_adapter_matches_its_declaration({}, {"d": 5})[0].passed
    finally:
        module._DATASETS = original


def test_a_run_is_ok_only_when_every_check_passed() -> None:
    """The exit code follows this and nothing else, so it is worth pinning."""
    assert DataRun(checks=[Check("a", True, "")]).ok
    assert not DataRun(checks=[Check("a", True, ""), Check("b", False, "")]).ok


def test_the_report_names_every_failed_check() -> None:
    """A report that summarised only the count would leave a red run undiagnosable."""
    run = DataRun(checks=[Check("seeding faithful", False, "3 missing")])
    text = report(run)
    assert "seeding faithful" in text and "3 missing" in text and "0/1 checks passed" in text
