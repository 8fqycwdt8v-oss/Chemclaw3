"""Behavioral tests for detailed step-by-step recipe ingestion.

Two entry paths carry a development recipe into the canonical schema: the JSON adapter segments a
prose procedure into ordered steps and keeps the verbatim text (no SMILES guessed from prose), and
the ORD adapter maps a native ORD message into component-linked steps, converting units. Both
produce one `OrdReaction` through `sync_entries`. Runnable without a server or database.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.eln.json_adapter import JsonExportAdapter
from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    OutcomeClass,
    ReactionStep,
    Role,
    StepKind,
)
from chemclaw.ingest.eln.ord_adapter import OrdFormatError, OrdJsonAdapter
from chemclaw.ingest.eln.record import _stated_outcome, record_from_ord_reaction
from chemclaw.ingest.eln.records import (
    InMemoryReactionRecordStore,
    PostgresReactionRecordStore,
)
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.kg.note import ProcessConditions
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.pg import migrated_db_or_skip

_EPOCH = datetime.min.replace(tzinfo=UTC)
_ORD_EXAMPLE = Path("data/eln-exports/ord/ord-2026-001.json")

_DETAILED_PROCEDURE = (
    "1. Charge substrate and THF to the reactor. "
    "2. Cool to 0 °C. "
    "3. Add n-BuLi dropwise over 30 min. "
    "4. Warm to 25 °C and stir for 12 h. "
    "5. Quench with water and extract into EtOAc. "
    "6. Concentrate and recrystallize from heptane."
)


# --- free-text procedure segmentation -------------------------------------------------


def _prose_reaction(procedure: str) -> OrdReaction:
    """Map a minimal free-text entry whose only detail is the given procedure."""
    raw = RawEntry(
        entry_id="prose",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "procedure": procedure,
        },
    )
    return JsonExportAdapter().map_to_ord(raw)


def test_numbered_procedure_segments_into_ordered_labeled_steps() -> None:
    """A numbered development recipe becomes contiguous, coarsely-labeled steps."""
    reaction = _prose_reaction(_DETAILED_PROCEDURE)
    assert [s.index for s in reaction.steps] == [1, 2, 3, 4, 5, 6]
    assert [s.kind for s in reaction.steps] == [
        StepKind.ADDITION,
        StepKind.TEMPERATURE,
        StepKind.ADDITION,
        StepKind.TEMPERATURE,
        StepKind.WORKUP,
        StepKind.PURIFICATION,
    ]


def test_procedure_text_is_preserved_verbatim() -> None:
    """The full prose is kept — a detailed recipe is not reduced to headline conditions."""
    assert _prose_reaction(_DETAILED_PROCEDURE).procedure_text == _DETAILED_PROCEDURE


def test_per_step_conditions_are_extracted() -> None:
    """Each step carries the temperature/time found in its own segment."""
    steps = _prose_reaction(_DETAILED_PROCEDURE).steps
    assert steps[1].temperature_c == 0.0  # "Cool to 0 °C"
    assert steps[3].temperature_c == 25.0 and steps[3].duration_h == 12.0  # warm + 12 h


def test_free_text_steps_carry_no_guessed_components() -> None:
    """Prose steps never invent a SMILES — species linking is the LLM skill's job, not regex."""
    assert all(step.components == [] for step in _prose_reaction(_DETAILED_PROCEDURE).steps)


def test_unnumbered_prose_segments_on_sentences() -> None:
    """Without numbering, sentence boundaries delimit steps (still lossless)."""
    reaction = _prose_reaction("Cooled to 0 °C. Stirred for 2 h. Concentrated in vacuo.")
    assert [s.kind for s in reaction.steps] == [
        StepKind.TEMPERATURE,
        StepKind.STIR,
        StepKind.WORKUP,
    ]


def test_empty_procedure_yields_no_steps() -> None:
    """No procedure prose means no steps and no preserved text (headline-only entry)."""
    reaction = _prose_reaction("")
    assert reaction.steps == [] and reaction.procedure_text is None


def test_decimal_amounts_do_not_split_steps() -> None:
    """A decimal in the prose ("0.5 h", "2.0 g") is not mistaken for a numbered marker."""
    reaction = _prose_reaction("Added 2.0 g of reagent and stirred for 0.5 h at 40 °C.")
    assert len(reaction.steps) == 1
    assert reaction.steps[0].duration_h == 0.5


# --- structured ORD adapter -----------------------------------------------------------


async def test_ord_adapter_maps_detailed_recipe() -> None:
    """The example ORD message maps to inputs, products, headline conditions, and steps."""
    adapter = OrdJsonAdapter(str(_ORD_EXAMPLE.parent))
    entries = await adapter.fetch_new_entries(_EPOCH)
    reaction = adapter.map_to_ord(entries[0])

    assert reaction.reaction_id == "ord-2026-001"
    assert {c.smiles for c in reaction.inputs} == {"CCO", "CC(=O)O", "OS(=O)(=O)O"}
    assert [c.smiles for c in reaction.outcomes] == ["CCOC(C)=O"]
    assert reaction.temperature_c == 80.0
    assert reaction.yield_percent == 85.0
    assert reaction.provenance == "ord:chemist-c"
    assert reaction.procedure_text is not None and "Charge ethanol" in reaction.procedure_text


def test_ord_addition_steps_link_components_and_convert_units() -> None:
    """ORD gives component-linked additions (unlike prose) with converted amounts/timing."""
    reaction = OrdJsonAdapter(str(_ORD_EXAMPLE.parent)).map_to_ord(_ord_example_entry())
    additions = [s for s in reaction.steps if s.kind == StepKind.ADDITION]
    assert len(additions) == 2
    # Ordered by ORD addition_order: ethanol (1) then acetic acid + catalyst (2, over 30 min).
    assert [c.smiles for c in additions[0].components] == ["CCO"]
    assert {c.smiles for c in additions[1].components} == {"CC(=O)O", "OS(=O)(=O)O"}
    assert additions[1].duration_h == pytest.approx(0.5)  # 30 MINUTE -> hours
    ethanol = additions[0].components[0]
    assert ethanol.mass_mg == pytest.approx(460.0)  # 0.46 GRAM -> mg


def test_ord_workup_sequence_becomes_ordered_steps() -> None:
    """The ORD workups[] list maps to ordered workup/purification steps after the conditions."""
    reaction = OrdJsonAdapter(str(_ORD_EXAMPLE.parent)).map_to_ord(_ord_example_entry())
    assert [s.index for s in reaction.steps] == list(range(1, len(reaction.steps) + 1))
    kinds = [s.kind for s in reaction.steps]
    assert StepKind.TEMPERATURE in kinds  # the 80 °C setpoint became a step
    assert kinds[-1] == StepKind.PURIFICATION  # the distillation is the final step
    quench = next(s for s in reaction.steps if "Quench" in s.text)
    assert [c.smiles for c in quench.components] == ["O"]  # workup reagent linked to its step


def _ord_multiproduct(*products: dict[str, Any]) -> OrdReaction:
    """One ORD reaction whose outcome carries the given products, mapped through the adapter."""
    payload = {
        "reaction_id": "ord-multi",
        "inputs": {
            "a": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "reaction_role": "REACTANT",
                    }
                ]
            }
        },
        "outcomes": [{"products": list(products)}],
        "notes": {"procedure_details": "Stir."},
    }
    return OrdJsonAdapter().map_to_ord(
        RawEntry(entry_id="ord-multi", created_at=_EPOCH, payload=payload)
    )


def _ord_product(
    smiles: str,
    *,
    yield_percent: float | None = None,
    purity_percent: float | None = None,
    desired: bool | None = None,
) -> dict[str, Any]:
    """An ORD `ProductCompound` carrying the measurements and desired-product marking given."""
    measurements: list[dict[str, Any]] = []
    if yield_percent is not None:
        measurements.append({"type": "YIELD", "percentage": {"value": yield_percent}})
    if purity_percent is not None:
        measurements.append({"type": "PURITY", "percentage": {"value": purity_percent}})
    product: dict[str, Any] = {
        "identifiers": [{"type": "SMILES", "value": smiles}],
        "measurements": measurements,
    }
    if desired is not None:
        product["is_desired_product"] = desired
    return product


def test_the_headline_yield_is_the_products_the_source_marked_desired() -> None:
    """The headline yield is the product the source marked desired, not the first one listed.

    `ProductCompound.is_desired_product` is the source's own statement; picking by array position is
    an inference that looks right when wrong. Fallbacks: unmarked with several yields is `None`,
    unmarked with exactly one yield keeps it, and a single product is unambiguous.
    """
    by_product = _ord_product("CC=O", yield_percent=12.0, purity_percent=90.0, desired=False)
    desired = _ord_product("CC(=O)OCC", yield_percent=85.0, purity_percent=99.0, desired=True)

    marked = _ord_multiproduct(by_product, desired)
    assert marked.yield_percent == 85.0, "the by-product listed first became the reaction's yield"
    assert marked.purity_percent == 99.0, "purity must describe the same compound as the yield"
    assert [c.smiles for c in marked.outcomes] == ["CC=O", "CC(=O)OCC"], (
        "both products stay in the record; only the headline figures are chosen"
    )

    unmarked = _ord_multiproduct(
        _ord_product("CC=O", yield_percent=12.0),
        _ord_product("CC(=O)OCC", yield_percent=85.0),
    )
    assert unmarked.yield_percent is None, "with nothing marked, a positional guess is not a yield"

    one_yield = _ord_multiproduct(
        _ord_product("CC=O"),
        _ord_product("CC(=O)OCC", yield_percent=85.0, purity_percent=99.0),
    )
    assert (one_yield.yield_percent, one_yield.purity_percent) == (85.0, 99.0)

    single = _ord_multiproduct(_ord_product("CC(=O)OCC", yield_percent=85.0))
    assert single.yield_percent == 85.0

    # Several marked desired: the source contradicts itself and states nothing readable, so falling
    # through to the measurement count would reintroduce the positional pick.
    contradicted = _ord_multiproduct(
        _ord_product("CC=O", yield_percent=12.0, desired=True),
        _ord_product("CC(=O)OCC", yield_percent=85.0, desired=True),
    )
    assert contradicted.yield_percent is None, (
        "two products marked desired is the source contradicting itself, not a vote the first wins"
    )


def test_a_multi_product_record_the_source_measured_once_keeps_that_measurement() -> None:
    """A multi-product record measured once keeps that measurement, whatever kind it is.

    The screen is "has a measurement", not "has a yield", so a purity-only record keeps its purity.
    With two products measured different ways no single candidate exists, and both stay `None`
    rather than being assembled from two compounds.
    """
    one_measured = _ord_multiproduct(
        _ord_product("CC=O"),
        _ord_product("CC(=O)OCC", purity_percent=99.2),
    )
    assert (one_measured.yield_percent, one_measured.purity_percent) == (None, 99.2), (
        "the one product the source measured is the candidate, whichever measurement it states"
    )

    two_measured = _ord_multiproduct(
        _ord_product("CC=O", yield_percent=40.0),
        _ord_product("CC(=O)OCC", purity_percent=99.2),
    )
    assert (two_measured.yield_percent, two_measured.purity_percent) == (None, None), (
        "a yield from one compound and a purity from another is a headline pair describing two"
    )


def test_ord_adapter_tolerates_camelcase_field_names() -> None:
    """protobuf-exported ORD JSON (camelCase) maps identically to snake_case."""
    payload = {
        "reactionId": "ord-cc",
        "inputs": {
            "a": {
                "additionOrder": 1,
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "reactionRole": "REACTANT",
                    }
                ],
            }
        },
        "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
        "notes": {"procedureDetails": "Stir."},
    }
    reaction = OrdJsonAdapter().map_to_ord(
        RawEntry(entry_id="ord-cc", created_at=_EPOCH, payload=payload)
    )
    assert reaction.inputs[0].smiles == "CCO"
    assert reaction.procedure_text == "Stir."


def test_ord_unresolvable_identifier_is_a_mapping_error() -> None:
    """A compound with neither a resolvable identifier nor a name is an `OrdFormatError`.

    An unresolvable name is carried verbatim and makes the reaction citation-only
    (`tests/test_ord_citation_tier.py`); only a compound offering nothing to show is refused.
    """
    payload = {
        "inputs": {
            "a": {
                "components": [
                    {"identifiers": [{"type": "INCHI", "value": "InChI=1S/not-a-structure"}]}
                ]
            }
        },
        "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
    }
    with pytest.raises(OrdFormatError, match="no resolvable structure identifier"):
        OrdJsonAdapter().map_to_ord(RawEntry(entry_id="x", created_at=_EPOCH, payload=payload))


def test_ord_unknown_units_is_a_mapping_error() -> None:
    """An unknown amount unit is rejected rather than silently mis-scaled (G4)."""
    payload = {
        "inputs": {
            "a": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "amount": {"mass": {"value": 1, "units": "STONE"}},
                    }
                ]
            }
        },
        "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
    }
    with pytest.raises(OrdFormatError, match="units"):
        OrdJsonAdapter().map_to_ord(RawEntry(entry_id="x", created_at=_EPOCH, payload=payload))


def test_ord_non_scalar_quantity_is_a_mapping_error() -> None:
    """An ORD quantity whose `value` is an object/list is an OrdFormatError, not a TypeError.

    `float(dict)` raises TypeError; escaping the mapping boundary would abort the whole
    sync batch instead of rejecting the one malformed entry (G4).
    """
    payload = {
        "inputs": {
            "a": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "amount": {"mass": {"value": [460], "units": "GRAM"}},
                    }
                ]
            }
        },
        "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
    }
    with pytest.raises(OrdFormatError, match="cannot map"):
        OrdJsonAdapter().map_to_ord(RawEntry(entry_id="x", created_at=_EPOCH, payload=payload))


def test_ord_auxiliary_role_collapses_to_reagent_not_reactant() -> None:
    """A stated role outside the subset (INTERNAL_STANDARD) maps to REAGENT, unstated to REACTANT.

    An internal standard read as a REACTANT would fabricate causal chain edges in
    `chemclaw.memory.chains` (which keys handoffs on REACTANT only).
    """
    payload = {
        "reaction_id": "ord-aux",
        "inputs": {
            "a": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "reaction_role": "INTERNAL_STANDARD",
                    },
                    {"identifiers": [{"type": "SMILES", "value": "CC(=O)O"}]},
                ]
            }
        },
        "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
    }
    reaction = OrdJsonAdapter().map_to_ord(
        RawEntry(entry_id="ord-aux", created_at=_EPOCH, payload=payload)
    )
    roles = {c.smiles: c.role for c in reaction.inputs}
    assert roles["CCO"] == Role.REAGENT  # stated auxiliary role → reagent, per _ROLES
    assert roles["CC(=O)O"] == Role.REACTANT  # unstated role → the input default


async def test_ord_fetch_skips_file_without_timestamp(tmp_path: Path) -> None:
    """An ORD file with no creation time is skipped, not allowed to abort the fetch (G4)."""
    (tmp_path / "no-time.json").write_text(json.dumps({"inputs": {}}), encoding="utf-8")
    (tmp_path / "ok.json").write_text(
        json.dumps(
            {
                "reaction_id": "ok",
                "inputs": {
                    "a": {"components": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}
                },
                "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
                "provenance": {"record_created": {"time": {"value": "2026-01-01T00:00:00Z"}}},
            }
        ),
        encoding="utf-8",
    )
    entries = await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
    assert [e.entry_id for e in entries] == ["ok"]


def _ord_example_entry() -> RawEntry:
    """The example ORD message as a RawEntry, for the mapping tests."""
    payload = json.loads(_ORD_EXAMPLE.read_text(encoding="utf-8"))
    return RawEntry(entry_id="ord-2026-001", created_at=_EPOCH, payload=payload)


# --- schema, validation, note rendering -----------------------------------------------


def test_non_contiguous_step_indices_are_rejected() -> None:
    """A malformed step ordering (gap or wrong start) fails the schema validator (G4)."""
    with pytest.raises(ValueError, match="contiguous"):
        OrdReaction(
            reaction_id="x",
            inputs=[Component(smiles="CCO", role=Role.REACTANT)],
            outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
            provenance="p",
            steps=[ReactionStep(index=2, kind=StepKind.STIR, text="stir")],
        )


def test_workup_reagent_satisfies_mass_balance() -> None:
    """A product element supplied only by a workup-step reagent does not fail the balance.

    Element subsumption folds in step components, so chloride entering during a workup balances.
    """
    reaction = OrdReaction(
        reaction_id="wk",
        inputs=[Component(smiles="CCO", role=Role.REACTANT)],
        outcomes=[Component(smiles="CCCl", role=Role.PRODUCT)],
        provenance="p",
        steps=[
            ReactionStep(
                index=1,
                kind=StepKind.WORKUP,
                text="quench with HCl",
                components=[Component(smiles="Cl", role=Role.REAGENT)],
            )
        ],
    )
    assert validate_ord(reaction) == []


def test_note_renders_numbered_procedure() -> None:
    """A reaction with steps renders a numbered Procedure section in its note body."""
    reaction = _prose_reaction(_DETAILED_PROCEDURE)
    body = record_from_ord_reaction(reaction).body
    assert "## Procedure" in body
    assert "1. Charge substrate and THF to the reactor (_addition_)" in body
    assert "6. Concentrate and recrystallize from heptane (_purification_)" in body


async def test_ord_recipe_flows_through_sync() -> None:
    """An ORD-format entry ingests through the same sync pipeline as free-text entries."""
    adapter = OrdJsonAdapter(str(_ORD_EXAMPLE.parent))
    rxn, mol, rec = (
        InMemoryFingerprintStore(),
        InMemoryFingerprintStore(),
        InMemoryReactionRecordStore(),
    )
    summary = await sync_entries(
        adapter, rxn, mol, rec, _EPOCH, label_index=InMemoryLabelIndex(), source="eln-ord"
    )
    assert summary.ingested == ["ord-2026-001"]
    assert summary.rejected == []
    assert len(await rec.all_records()) == 1
    assert "## Procedure" in (await rec.all_records())[0].body  # recipe reached the record


def _warehouse_shaped(procedure: str) -> OrdReaction:
    """A reaction as a warehouse binding produces one: prose recorded, `steps` never mapped.

    The binding excludes `steps` from `_MAPPABLE_FIELDS`, so this is the real shape.
    """
    return OrdReaction(
        reaction_id="WH-1",
        inputs=[Component(smiles="c1ccccc1Br", role=Role.REACTANT)],
        outcomes=[Component(smiles="c1ccccc1-c1ccccc1", role=Role.PRODUCT)],
        provenance="warehouse:eln",
        procedure_text=procedure,
    )


def test_a_recorded_procedure_reaches_the_note_when_the_source_maps_no_steps() -> None:
    """A recorded procedure reaches the note when the source maps no steps.

    The warehouse path's protocol must survive to `expand_note`, not stop at the schema.
    """
    procedure = (
        "Charge the aryl bromide (1.0 equiv) and boronic acid (1.2 equiv). Add Pd(dppf)Cl2 "
        "(2 mol%). Degas, heat to 90 C for 12 h. Filter through Celite, recrystallise."
    )
    body = record_from_ord_reaction(_warehouse_shaped(procedure)).body
    assert "## Procedure" in body
    assert "Filter through Celite" in body


def test_a_note_carries_no_procedure_section_when_the_source_recorded_none() -> None:
    """Absent stays absent — the branch above must not invent an empty heading."""
    reaction = _warehouse_shaped("")
    assert reaction.procedure_text is None or not reaction.procedure_text
    assert "## Procedure" not in record_from_ord_reaction(reaction).body


def test_segmented_steps_do_not_also_render_the_prose_they_were_cut_from() -> None:
    """`json_adapter`'s steps *are* the prose recut, so rendering both would duplicate the recipe.

    Measured on the shipped export: 0.992 similarity between the joined steps and the prose, and
    every step's text verbatim inside it.
    """
    payload = json.loads(Path("data/eln-exports/eln-2026-002.json").read_text())
    raw = RawEntry(entry_id="e1", created_at=datetime.now(UTC), payload=payload)
    body = record_from_ord_reaction(JsonExportAdapter().map_to_ord(raw)).body
    assert "## Procedure" in body
    assert "### Procedure as recorded" not in body


def test_derived_steps_do_not_swallow_the_chemists_own_account() -> None:
    """Derived steps do not replace the chemist's own prose account.

    Steps derived from structured fields read `Add CCO` where the prose names the catalyst and
    addition time; rendering steps alone would drop that.
    """
    payload = json.loads(_ORD_EXAMPLE.read_text())
    raw = RawEntry(entry_id="e1", created_at=datetime.now(UTC), payload=payload)
    reaction = OrdJsonAdapter().map_to_ord(raw)
    body = record_from_ord_reaction(reaction).body
    assert reaction.steps, "the fixture must exercise the both-present branch"
    assert "### Procedure as recorded" in body
    assert "catalytic amount of sulfuric acid" in body


def test_the_numbers_a_chemist_compares_reach_the_note_as_numbers() -> None:
    """Setpoints and outcomes survive ingestion as data, not only as sentences.

    `OrdReaction` is transient, so without typed conditions a run comparison at turn time would have
    to re-derive numbers from prose.
    """
    reaction = OrdReaction(
        reaction_id="R1",
        inputs=[Component(smiles="c1ccccc1Br", role=Role.REACTANT)],
        outcomes=[Component(smiles="c1ccccc1-c1ccccc1", role=Role.PRODUCT)],
        provenance="warehouse:eln",
        temperature_c=90.0,
        time_h=12.0,
        yield_percent=78.0,
        purity_percent=99.1,
        impurities=[
            Impurity(name="des-bromo", area_percent=0.7),
            Impurity(name="homocoupling", area_percent=0.2),
        ],
    )
    conditions = record_from_ord_reaction(reaction).conditions
    assert conditions is not None
    assert (conditions.temperature_c, conditions.time_h) == (90.0, 12.0)
    assert (conditions.yield_percent, conditions.purity_percent) == (78.0, 99.1)
    # Ranked by area%, which is the number process development actually chases.
    assert conditions.major_impurity == "des-bromo"
    assert conditions.impurity_area_percent == 0.7


def test_a_note_about_no_recorded_run_carries_no_conditions_block() -> None:
    """`conditions: {}` would claim the question was asked and answered emptily."""
    reaction = OrdReaction(
        reaction_id="R2",
        inputs=[Component(smiles="CC", role=Role.REACTANT)],
        outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
        provenance="x",
    )
    assert record_from_ord_reaction(reaction).conditions is None


def test_the_frontmatter_tells_three_outcomes_apart() -> None:
    """The frontmatter tells stated success, stated failure and "not stated" apart.

    Silence has its own value, so a run recorded as successful is distinguishable from one nobody
    assessed.
    """

    def _run(**extra: Any) -> ProcessConditions:
        reaction = OrdReaction(
            reaction_id="R3",
            inputs=[Component(smiles="CC", role=Role.REACTANT)],
            outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
            provenance="x",
            yield_percent=12.0,
            **extra,
        )
        conditions = record_from_ord_reaction(reaction).conditions
        assert conditions is not None
        return conditions

    assert _run().outcome is None, "unstated stays unstated"
    assert _run(outcome_class=OutcomeClass.SUCCESS).outcome == "success"
    failed = _run(outcome_class=OutcomeClass.FAILURE, failure_reason="decomposed on scale")
    assert failed.outcome == "failure"


async def test_the_conditions_block_round_trips_through_the_stored_form() -> None:
    """The conditions block round-trips through the stored form.

    Asserted against both backends, because the in-memory one is what every other test here uses and
    a divergence would make those tests prove something the deployment does not do.
    """
    reaction = OrdReaction(
        reaction_id="R4",
        inputs=[Component(smiles="CC", role=Role.REACTANT)],
        outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
        provenance="x",
        temperature_c=-78.0,
        time_h=0.5,
        yield_percent=61.5,
    )
    record = record_from_ord_reaction(reaction)
    assert record.conditions is not None

    memory = InMemoryReactionRecordStore()
    await memory.record([record], "eln-json")
    from_memory = await memory.read("R4")
    assert from_memory is not None and from_memory.conditions == record.conditions

    await migrated_db_or_skip()
    durable = PostgresReactionRecordStore()
    await durable.record([record], "eln-json")
    from_pg = await durable.read("R4")
    assert from_pg is not None and from_pg.conditions == record.conditions


def test_a_new_outcome_class_member_fails_the_type_check_rather_than_the_sync() -> None:
    """Every `OutcomeClass` member has a frontmatter spelling.

    mypy does not exhaustiveness-check dict keys, so the mapping uses `assert_never`; a missing
    member would otherwise raise `KeyError` outside the per-entry rejection path and abort the sync.
    This is the runtime half of that check.
    """
    for member in OutcomeClass:
        spelled = _stated_outcome(member)
        assert spelled == member.value, f"{member} has no frontmatter spelling"
    assert _stated_outcome(None) is None
