"""Behavioral tests for detailed step-by-step recipe ingestion (plan Phase 4).

Two entry paths must carry a full development recipe into the canonical schema:

* **free text** — the JSON adapter segments a prose procedure into ordered, labeled steps
  and preserves the verbatim text (no SMILES is guessed from prose);
* **structured ORD** — the ORD adapter maps a native Open Reaction Database message into
  component-linked addition/condition/workup steps, converting units.

Both produce the same `OrdReaction` and flow through the one `sync_entries` pipeline, and the
reaction note renders the numbered procedure so the recipe survives to the graph. All runnable
without a server, database, or git.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eln.adapter import RawEntry
from eln.json_adapter import JsonExportAdapter
from eln.note import note_from_ord_reaction
from eln.ord import Component, OrdReaction, ReactionStep, Role, StepKind
from eln.ord_adapter import OrdFormatError, OrdJsonAdapter
from eln.sync import sync_entries
from eln.validate import validate_ord
from mcp_servers.fpstore import InMemoryFingerprintStore
from tests.conftest import FakeSubmitter

_EPOCH = datetime.min.replace(tzinfo=UTC)
_ORD_EXAMPLE = Path("eln/exports/ord/ord-2026-001.json")

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


def test_ord_adapter_maps_detailed_recipe() -> None:
    """The example ORD message maps to inputs, products, headline conditions, and steps."""

    async def _run() -> None:
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

    asyncio.run(_run())


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


def test_ord_missing_smiles_is_a_mapping_error() -> None:
    """A compound without a SMILES identifier is an OrdFormatError, not a crash (G4)."""
    payload = {
        "inputs": {"a": {"components": [{"identifiers": [{"type": "NAME", "value": "ethanol"}]}]}},
        "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}],
    }
    with pytest.raises(OrdFormatError, match="SMILES"):
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
    `memory.chains` (which keys handoffs on REACTANT only).
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


def test_ord_fetch_skips_file_without_timestamp(tmp_path: Path) -> None:
    """An ORD file with no creation time is skipped, not allowed to abort the fetch (G4)."""

    async def _run() -> None:
        (tmp_path / "no-time.json").write_text(json.dumps({"inputs": {}}), encoding="utf-8")
        (tmp_path / "ok.json").write_text(
            json.dumps(
                {
                    "reaction_id": "ok",
                    "inputs": {
                        "a": {"components": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}
                    },
                    "outcomes": [
                        {"products": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}
                    ],
                    "provenance": {"record_created": {"time": {"value": "2026-01-01T00:00:00Z"}}},
                }
            ),
            encoding="utf-8",
        )
        entries = await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
        assert [e.entry_id for e in entries] == ["ok"]

    asyncio.run(_run())


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

    Chlorination where the chloride enters during a workup: the product's Cl comes from a
    step reagent, not a reaction input, and must still balance (element subsumption folds in
    step components).
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
    body = note_from_ord_reaction(reaction).body
    assert "## Procedure" in body
    assert "1. Charge substrate and THF to the reactor (_addition_)" in body
    assert "6. Concentrate and recrystallize from heptane (_purification_)" in body


def test_ord_recipe_flows_through_sync() -> None:
    """An ORD-format entry ingests through the same sync pipeline as free-text entries."""

    async def _run() -> None:
        adapter = OrdJsonAdapter(str(_ORD_EXAMPLE.parent))
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(adapter, rxn, mol, sub, _EPOCH)
        assert summary.ingested == ["ord-2026-001"]
        assert summary.rejected == []
        assert len(sub.submissions) == 1
        assert "## Procedure" in sub.submissions[0].content  # recipe reached the note

    asyncio.run(_run())
