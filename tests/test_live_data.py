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

from pathlib import Path
from typing import Any

import pytest

from chemclaw.cli.live_data import (
    _DATASETS,
    _PROSE_TEMPERATURE,
    _PROSE_TIME,
    Check,
    DataRun,
    Dataset,
    _default_real_data,
    _identifier,
    _published_key,
    _seeded_yield,
    check_adapter_matches_its_declaration,
    check_prose_yields_its_numbers,
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


def test_a_procedure_run_at_zero_degrees_is_read_as_zero() -> None:
    """A step reading "cooled to 0 °C" is a condition, not the absence of one.

    One seeded fixture runs at exactly 0 °C. Anywhere a truthiness test stands in for
    `is not None` — in the regex handling, in the comparison, in the record — that record's
    temperature disappears and the extraction reads as a failure it is not.
    """
    match = _PROSE_TEMPERATURE.search("The mixture was cooled to 0 °C and stirred for 3.0 h.")
    assert match is not None
    assert float(match.group(1)) == 0.0
    time_match = _PROSE_TIME.search("cooled to 0 °C and stirred for 3.0 h.")
    assert time_match is not None and float(time_match.group(1)) == 3.0


def test_a_temperature_is_only_read_from_a_temperature() -> None:
    """The units anchor the match, so a mass or an NMR shift cannot become a temperature."""
    assert _PROSE_TEMPERATURE.search("charged with 1071.0 mg of the carbamate") is None
    assert _PROSE_TIME.search("1H NMR (400 MHz) delta 7.4") is None


def test_the_factor_tables_are_found_from_the_export_dir_the_lane_actually_sets() -> None:
    """The default `--real-data` path, against the mock layout both lanes really configure.

    This is the check that was missing when the derivation walked up three levels instead of four
    and produced `<repo>/data/app/eln/real_data`. Nothing failed loudly: `up.sh` logged a warning
    naming a log file, the bring-up exited 0, and the ORD half of the corpus was simply never
    reachable — which is the shape of failure this whole lane exists to catch in the *data*, so it
    is worth catching in the lane itself.

    The literals below are transcribed from `Chemclaw3_mock/start.sh` and
    `infra/live/e2e-full-stack/up.sh` rather than imported, deliberately and for the reason
    `Chemclaw3-mcp/tests/test_identity_contract.py` gives about header spellings: importing this
    module's own constant would let the test agree with the bug.
    """
    mock_repo = Path("/checkout/Chemclaw3_mock")
    ord_export_dir = mock_repo / "data" / "eln" / "exports" / "ord"

    assert _default_real_data(ord_export_dir) == mock_repo / "app" / "eln" / "real_data"


def test_the_factor_tables_are_not_looked_for_under_the_export_tree() -> None:
    """The specific wrong answer, named so a regression cannot pass by being merely plausible.

    An off-by-one here stays inside the mock checkout and still *looks* like a reasonable path,
    which is why the original survived review — `<repo>/data/app/eln/real_data` reads fine until
    somebody lists the directory.
    """
    ord_export_dir = Path("/checkout/Chemclaw3_mock/data/eln/exports/ord")

    resolved = _default_real_data(ord_export_dir)

    # Narrowed rather than assumed: the helper returns `Path | None`, and an assertion written
    # against the optional would pass vacuously if it ever started returning `None` for the lane's
    # own layout — which is the one input this test exists to pin.
    assert resolved is not None
    assert "data/app" not in resolved.as_posix()
    assert resolved.parents[2].name != "data"


def test_the_shipped_default_export_dir_derives_no_tables_rather_than_raising() -> None:
    """The shipped `ord_export_dir` is relative and three parts deep — the common case, not an edge.

    `data/eln-exports/ord` (`core/config/eln.py`) has no fourth parent, and the first version of
    this fix indexed `parents[3]` unguarded: every invocation outside the four-repo lane died with
    a bare `IndexError: 3` raised from inside `pathlib`, which names neither the setting that was
    wrong nor the flag that fixes it.

    This is the input the two tests above could not read. They were written from the lane's layout,
    which is the same understanding that produced the off-by-one, so they agreed with it about
    everything except the count — exactly the failure `tasks/lessons.md` records for tests written
    alongside their own change.
    """
    assert _default_real_data(Path("data/eln-exports/ord")) is None


def test_a_shallow_absolute_export_dir_also_derives_nothing() -> None:
    """Absolute but too shallow — the other half of the domain outside the lane's layout."""
    assert _default_real_data(Path("/exports/ord")) is None
    assert _default_real_data(Path("/a/b/c/d/exports/ord")) is not None


def _entry_stating_conditions_only_in_prose() -> dict[str, Any]:
    """An entry whose conditions exist *only* as a sentence — the case the check is about.

    No `temperature_c`, no `time_h`. If the prose is not read into a step the condition is simply
    gone, and nothing downstream can tell "ran at 82 °C" from "temperature unrecorded".
    """
    return {
        "id": "prose-only-1",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "reactants": [{"smiles": "CCO", "role": "reactant"}],
        "products": [{"smiles": "CCOC", "yield_percent": 71.0}],
        "procedure": (
            "1. Charge the vessel and cool to 0 °C. "
            "2. Stir at 82 °C for 4.0 h under nitrogen. "
            "3. Quench and extract."
        ),
    }


def _write_entry(directory: Any, payload: dict[str, Any]) -> None:
    import json

    (directory / f"{payload['id']}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.anyio
async def test_a_prose_condition_reaches_a_step(tmp_path: Any) -> None:
    """The recovery half: a number stated only in a sentence lands on the step it scopes to."""
    _write_entry(tmp_path, _entry_stating_conditions_only_in_prose())

    check = await check_prose_yields_its_numbers(tmp_path)

    assert check.passed, check.observed
    assert "1/1" in check.observed


@pytest.mark.anyio
async def test_a_setpoint_derived_from_prose_fails_the_check(tmp_path: Any) -> None:
    """The half that guards the decision, and the one this check was missing.

    `D-2026-08-26-a-transcription-may-not-infer-a-setpoint` removed the headline prose fallback
    after measuring what it stored: a reaction run at 80 °C for 12 h, recorded as 0 °C for 0.5 h,
    because a procedure begins by charging a vessel. Reinstating that fallback must turn this red
    — otherwise the check passes for the wrong reason and the retraction is unguarded offline.
    """
    _write_entry(tmp_path, _entry_stating_conditions_only_in_prose())

    import chemclaw.ingest.eln.json_adapter as adapter_module

    original = adapter_module.JsonExportAdapter._build

    def _build_with_the_retracted_fallback(
        self: Any, raw: Any
    ) -> Any:  # pragma: no cover - exercised via the check
        reaction = original(self, raw)
        first = next((s for s in reaction.steps if s.temperature_c is not None), None)
        return reaction.model_copy(
            update={
                "temperature_c": first.temperature_c if first else None,
                "time_h": next(
                    (s.duration_h for s in reaction.steps if s.duration_h is not None), None
                ),
            }
        )

    adapter_module.JsonExportAdapter._build = _build_with_the_retracted_fallback  # type: ignore[method-assign]
    try:
        check = await check_prose_yields_its_numbers(tmp_path)
    finally:
        adapter_module.JsonExportAdapter._build = original  # type: ignore[method-assign]

    assert not check.passed
    assert "D-2026-08-26" in check.observed
