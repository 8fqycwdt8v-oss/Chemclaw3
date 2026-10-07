"""The canonical record carries when a reaction happened and what came out.

`OrdReaction` has a date (for recency ranking, bi-temporal validity and chain ordering) and purity
and impurities (late-stage process questions). Pinned: the fields, both adapters, the rendered
note, and that none of it changes structure — reaction SMILES and fingerprints stay identical.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.ingest.eln.adapter import DatedIngest, RawEntry
from chemclaw.ingest.eln.json_adapter import JsonExportAdapter
from chemclaw.ingest.eln.ord import Component, Impurity, OrdReaction, Role
from chemclaw.ingest.eln.ord_adapter import OrdJsonAdapter
from chemclaw.ingest.eln.record import record_from_ord_reaction


def _reaction(**overrides: object) -> OrdReaction:
    """A minimal valid reaction, with fields overridden per test."""
    base: dict[str, object] = {
        "reaction_id": "r-1",
        "inputs": [Component(smiles="CCO", role=Role.REACTANT)],
        "outcomes": [Component(smiles="CC=O", role=Role.PRODUCT)],
        "provenance": "eln:test",
    }
    return OrdReaction(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_fields_are_optional_so_existing_records_still_validate() -> None:
    """A source that reports none of this is still a valid reaction — additive, not breaking."""
    reaction = _reaction()
    assert reaction.performed_at is None
    assert reaction.purity_percent is None
    assert reaction.impurities == []


def test_purity_is_bounded_like_yield() -> None:
    """A percentage outside 0–100 is a data error, caught at the schema boundary."""
    with pytest.raises(ValidationError):
        _reaction(purity_percent=101.0)


def test_an_impurity_needs_an_identity() -> None:
    """A row with neither name nor structure records nothing a chemist could act on."""
    with pytest.raises(ValidationError):
        Impurity(area_percent=0.5)
    assert Impurity(name="des-methyl").name == "des-methyl"
    assert Impurity(smiles="CC").smiles == "CC"


def test_outcome_quality_never_leaks_into_structure() -> None:
    """Purity and impurities are outcomes, so the reaction SMILES must be identical without them.

    The load-bearing check: if these fed `reaction_smiles()` they would change every DRFP
    fingerprint, silently invalidating the whole structural-search index.
    """
    plain = _reaction()
    enriched = _reaction(
        purity_percent=99.1,
        impurities=[Impurity(name="dimer", smiles="CCCC", area_percent=0.4)],
        performed_at=date(2026, 3, 1),
    )
    assert plain.reaction_smiles() == enriched.reaction_smiles()
    assert [c.smiles for c in plain.compounds()] == [c.smiles for c in enriched.compounds()]


def test_the_record_time_scopes_itself_from_the_experiment_date() -> None:
    """The date a `since`/`until` window narrows on finally has a source (KNW-1)."""
    record = record_from_ord_reaction(_reaction(performed_at=date(2026, 3, 1)))
    assert record.performed_at == date(2026, 3, 1)
    # There is no `valid_to`: a result does not expire on its own, it is superseded — which is a
    # claim a human makes in a note, not something a transcription can say about itself.
    assert record.is_current(date(2026, 6, 1))
    assert not record.is_current(date(2026, 1, 1))  # not yet run at that time


def test_the_note_renders_purity_and_the_impurity_profile() -> None:
    """Retrieval reads bodies, so an impurity-driven question has to be able to match here."""
    note = record_from_ord_reaction(
        _reaction(
            yield_percent=88.0,
            purity_percent=98.4,
            impurities=[Impurity(name="des-methyl", smiles="CC", area_percent=0.8)],
        )
    )
    assert "purity: 98.4%" in note.body
    assert "## Impurities" in note.body
    assert "des-methyl" in note.body and "0.8% area" in note.body


def test_a_reaction_without_impurities_renders_no_empty_section() -> None:
    """An absent profile must not render a misleading empty heading."""
    assert "## Impurities" not in record_from_ord_reaction(_reaction(yield_percent=50.0)).body


def test_json_adapter_maps_date_purity_and_impurities(tmp_path: Path) -> None:
    """The free-text ELN source populates date, purity and impurities end to end.

    Through `DatedIngest`: this export has no experiment date, so the wrapper fills in the entry
    timestamp with `date_source="entry"`, distinguishable from a chemist-entered date.
    """
    entry = {
        "id": "e-1",
        "timestamp": "2026-03-01T09:00:00Z",
        "reactants": [{"smiles": "CCO", "role": "reactant"}],
        "products": [
            {
                "smiles": "CC=O",
                "yield_percent": 88,
                "purity_percent": 98.4,
                "impurities": [{"name": "des-methyl", "area_percent": 0.8}],
            }
        ],
        "procedure": "Stir at 20 °C for 2 h.",
        "operator": "aj",
    }
    (tmp_path / "e-1.json").write_text(json.dumps(entry))
    adapter = DatedIngest(JsonExportAdapter(str(tmp_path)))
    raw = RawEntry(entry_id="e-1", created_at=datetime(2026, 3, 1, 9, tzinfo=UTC), payload=entry)
    reaction = adapter.map_to_ord(raw)
    assert reaction.performed_at == date(2026, 3, 1) and reaction.date_source == "entry"
    assert reaction.purity_percent == 98.4
    assert [i.name for i in reaction.impurities] == ["des-methyl"]


def test_json_adapter_skips_an_unidentifiable_impurity_without_losing_the_reaction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One unusable row must not cost the whole entry — reject-and-continue, within an entry."""
    entry = {
        "id": "e-2",
        "timestamp": "2026-03-01T09:00:00Z",
        "reactants": [{"smiles": "CCO", "role": "reactant"}],
        "products": [
            {
                "smiles": "CC=O",
                "impurities": [{"area_percent": 0.2}, {"name": "real", "area_percent": 0.3}],
            }
        ],
        "procedure": "",
        "operator": "aj",
    }
    adapter = JsonExportAdapter(str(tmp_path))
    raw = RawEntry(entry_id="e-2", created_at=datetime(2026, 3, 1, 9, tzinfo=UTC), payload=entry)
    with caplog.at_level("WARNING"):
        reaction = adapter.map_to_ord(raw)
    assert [i.name for i in reaction.impurities] == ["real"]
    assert "neither name nor smiles" in caplog.text


def test_ord_adapter_maps_date_and_purity(tmp_path: Path) -> None:
    """The structured ORD source reads PURITY through the same measurement path as YIELD.

    Through `DatedIngest` for the reason the JSON case above gives: an ORD record's only date is
    `provenance.record_created.time`, which is when the record was written.
    """
    message = {
        "reaction_id": "ord-1",
        "inputs": {
            "a": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "reactionRole": "REACTANT",
                    }
                ]
            }
        },
        "outcomes": [
            {
                "products": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CC=O"}],
                        "measurements": [
                            {"type": "YIELD", "percentage": {"value": 90}},
                            {"type": "PURITY", "percentage": {"value": 97.5}},
                        ],
                    }
                ]
            }
        ],
        "provenance": {"record_created": {"time": {"value": "2026-03-01T09:00:00Z"}}},
    }
    adapter = DatedIngest(OrdJsonAdapter(str(tmp_path)))
    raw = RawEntry(
        entry_id="ord-1", created_at=datetime(2026, 3, 1, 9, tzinfo=UTC), payload=message
    )
    reaction = adapter.map_to_ord(raw)
    assert reaction.yield_percent == 90.0
    assert reaction.purity_percent == 97.5
    assert reaction.performed_at == date(2026, 3, 1) and reaction.date_source == "entry"
    # ORD does not model an impurity profile directly; guessing one would be worse than none.
    assert reaction.impurities == []
