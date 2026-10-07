"""The corpus-fidelity lane's own logic, offline.

What is testable without a live database is whether a green result means anything:

- a 0% yield must not be read as "unknown" through a truthiness test;
- a blank cell in a published table is an omitted reagent and must equal the seeded record's
  absent input, not an empty string;
- a citation-only dataset whose records arrive structured is a failure, since the structure was
  invented.
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
    check_named_species_arrive_verbatim,
    check_prose_yields_its_numbers,
    check_seeding_is_faithful,
    report,
)
from chemclaw.ingest.eln.ord import (
    Component,
    OrdReaction,
    RecordTier,
    Role,
    UnstructuredComponent,
)


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
        if dataset.tier is not RecordTier.STRUCTURED:
            assert dataset.tier_reason, "a citation-only dataset must say why in one line"
            assert dataset.named_only, "and name the species it expects to arrive as names"


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


def _reaction(*, named: str | None) -> OrdReaction:
    """A record whose coupling partner is drawn, or — with `named` — only named."""
    partner = [] if named else [Component(smiles="OB(O)c1ccccc1", role=Role.REACTANT)]
    return OrdReaction(
        reaction_id="r",
        inputs=[Component(smiles="CCO", role=Role.REACTANT), *partner],
        outcomes=[Component(smiles="CCC", role=Role.PRODUCT)],
        unstructured=[UnstructuredComponent(name=named, role=Role.REACTANT)] if named else [],
        provenance="test",
    )


def test_a_citation_only_dataset_whose_records_arrive_structured_is_a_failure() -> None:
    """The direction that matters most: a structured record there means an invented structure."""
    citation_only = Dataset(
        csv_name="x.csv",
        dataset_id="d",
        factors=(("base", "base_smiles"),),
        yield_column="y",
        tier=RecordTier.CITATION_ONLY,
        named_only=(("partner", "partner_name"),),
        tier_reason="no published structure for one component",
    )
    original = _DATASETS
    try:
        import chemclaw.cli.live_data as module

        module._DATASETS = (citation_only,)
        cited = _reaction(named="2a, Boronic Acid")
        assert check_adapter_matches_its_declaration({"d": [cited]}, {})[0].passed
        invented = _reaction(named=None)
        assert not check_adapter_matches_its_declaration({"d": [invented]}, {})[0].passed
        # A refusal is no longer the declared outcome for any dataset: it is a regression too.
        assert not check_adapter_matches_its_declaration({"d": [cited]}, {"d": 1})[0].passed
        assert not check_adapter_matches_its_declaration({}, {"d": 5})[0].passed
        # And a structured dataset whose records drop to citation-only is the ordinary regression.
        module._DATASETS = (
            Dataset(
                csv_name="x.csv",
                dataset_id="d",
                factors=(("base", "base_smiles"),),
                yield_column="y",
            ),
        )
        assert check_adapter_matches_its_declaration({"d": [invented]}, {})[0].passed
        assert not check_adapter_matches_its_declaration({"d": [cited]}, {})[0].passed
    finally:
        module._DATASETS = original


def test_a_named_species_must_arrive_as_the_exact_published_name(tmp_path: Any) -> None:
    """Multiset equality against the published column, so a swapped or normalised name is red."""
    (tmp_path / "x.csv").write_text(
        'base_smiles,partner_name,y\nCCO,"2a, Boronic Acid",1\nCCO,"2b, Boronic Ester",2\n',
        encoding="utf-8",
    )
    dataset = Dataset(
        csv_name="x.csv",
        dataset_id="d",
        factors=(("base", "base_smiles"),),
        yield_column="y",
        tier=RecordTier.CITATION_ONLY,
        named_only=(("partner", "partner_name"),),
        tier_reason="named only",
    )
    original = _DATASETS
    try:
        import chemclaw.cli.live_data as module

        module._DATASETS = (dataset,)
        faithful = [_reaction(named="2a, Boronic Acid"), _reaction(named="2b, Boronic Ester")]
        assert check_named_species_arrive_verbatim(tmp_path, {"d": faithful})[0].passed
        normalised = [_reaction(named="2a boronic acid"), _reaction(named="2b, Boronic Ester")]
        assert not check_named_species_arrive_verbatim(tmp_path, {"d": normalised})[0].passed
        one_lost = [_reaction(named="2a, Boronic Acid"), _reaction(named=None)]
        assert not check_named_species_arrive_verbatim(tmp_path, {"d": one_lost})[0].passed
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
    """A step reading "cooled to 0 °C" is read as 0, not as no condition."""
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
    """The default `--real-data` path, against the mock layout both lanes configure.

    A wrong walk-up depth fails silently: bring-up exits 0 and the ORD half of the corpus is never
    reached. The literals are transcribed from `Chemclaw3_mock/start.sh` and
    `infra/live/e2e-full-stack/up.sh` rather than imported, so the test cannot agree with a bug.
    """
    mock_repo = Path("/checkout/Chemclaw3_mock")
    ord_export_dir = mock_repo / "data" / "eln" / "exports" / "ord"

    assert _default_real_data(ord_export_dir) == mock_repo / "app" / "eln" / "real_data"


def test_the_factor_tables_are_not_looked_for_under_the_export_tree() -> None:
    """The factor tables are not looked for under the export tree, a plausible wrong path."""
    ord_export_dir = Path("/checkout/Chemclaw3_mock/data/eln/exports/ord")

    resolved = _default_real_data(ord_export_dir)

    # Narrowed rather than assumed: the helper returns `Path | None`, and an assertion written
    # against the optional would pass vacuously if it ever started returning `None` for the lane's
    # own layout — which is the one input this test exists to pin.
    assert resolved is not None
    assert "data/app" not in resolved.as_posix()
    assert resolved.parents[2].name != "data"


def test_the_shipped_default_export_dir_derives_no_tables_rather_than_raising() -> None:
    """The shipped relative `ord_export_dir` derives no tables rather than raising `IndexError`.

    `data/eln-exports/ord` has no fourth parent; an unguarded index would crash every run outside
    the four-repo lane with a message naming neither the setting nor the flag.
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
    """A setpoint derived from procedure prose fails the check.

    Reinstating a prose fallback (which reads "charge the vessel" as the reaction conditions) must
    turn this red.
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
