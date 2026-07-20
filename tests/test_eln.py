"""Behavioral tests for ELN ingestion (plan Phase 4), all runnable without a server.

Covers the ORD schema, the RDKit+mass-balance validator, the JSON adapter (structured and
free-text mapping), the reaction-note mapping, and the ingest + sync flow into in-memory
fingerprint stores and a fake PR-gate — the CHECKMATE 4 chain "ELN entry → validated note +
fingerprint-indexed", proven end to end without a database or git.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eln.adapter import RawEntry
from eln.ingest import IngestError, ingest_reaction
from eln.json_adapter import ElnFormatError, JsonExportAdapter
from eln.note import note_from_ord_reaction
from eln.ord import Component, OrdReaction, Role
from eln.sync import sync_entries
from eln.validate import validate_ord
from kg.pr_gate import NoteSubmission
from mcp_servers.fpstore import InMemoryFingerprintStore

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _ester() -> OrdReaction:
    """A valid, mass-balanced esterification used across the tests."""
    return OrdReaction(
        reaction_id="rxn-1",
        inputs=[
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=460),
            Component(smiles="CC(=O)O", role=Role.REACTANT, mass_mg=600),
        ],
        outcomes=[Component(smiles="CCOC(C)=O", role=Role.PRODUCT)],
        temperature_c=80.0,
        yield_percent=85.0,
        provenance="eln:chemist-a",
    )


class _FakeSubmitter:
    """Captures submissions instead of pushing a git branch."""

    def __init__(self) -> None:
        self.captured: list[NoteSubmission] = []

    async def submit(self, submission: NoteSubmission) -> str:
        self.captured.append(submission)
        return f"pr://{submission.branch}"


# --- schema ---------------------------------------------------------------------------


def test_reaction_smiles_and_role_validation() -> None:
    """reaction_smiles joins inputs>>products; a product among inputs is rejected (G4)."""
    assert _ester().reaction_smiles() == "CCO.CC(=O)O>>CCOC(C)=O"
    with pytest.raises(ValueError, match="input component has role 'product'"):
        OrdReaction(
            reaction_id="x",
            inputs=[Component(smiles="CCO", role=Role.PRODUCT)],
            outcomes=[Component(smiles="CCO", role=Role.PRODUCT)],
            provenance="p",
        )


# --- validator ------------------------------------------------------------------------


def test_valid_reaction_has_no_problems() -> None:
    """A parseable, mass-balanced reaction validates clean."""
    assert validate_ord(_ester()) == []


def test_unparseable_smiles_is_a_problem() -> None:
    """A bad SMILES is reported, and balance is not checked on a broken structure (G4)."""
    reaction = _ester().model_copy(
        update={"outcomes": [Component(smiles="not-a-mol(((", role=Role.PRODUCT)]}
    )
    problems = validate_ord(reaction)
    assert any("unparseable SMILES" in p for p in problems)


def test_mass_balance_violation_is_a_problem() -> None:
    """A product containing an element the inputs never supply fails mass balance."""
    reaction = _ester().model_copy(
        update={"outcomes": [Component(smiles="CCCl", role=Role.PRODUCT)]}  # Cl not in inputs
    )
    problems = validate_ord(reaction)
    assert any("mass balance" in p and "Cl" in p for p in problems)


# --- adapter --------------------------------------------------------------------------


def test_adapter_extracts_conditions_from_free_text() -> None:
    """Missing structured conditions are recovered from the procedure prose (step 4.4)."""
    raw = RawEntry(
        entry_id="e1",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO", "role": "reactant"}],
            "products": [{"smiles": "CCO", "yield_percent": 50}],
            "procedure": "Warmed to 65 °C for 2.5 h.",
            "operator": "chemist-c",
        },
    )
    reaction = JsonExportAdapter().map_to_ord(raw)
    assert reaction.temperature_c == 65.0  # from prose
    assert reaction.time_h == 2.5  # from prose
    assert reaction.yield_percent == 50.0  # from structured field
    assert reaction.provenance == "eln:chemist-c"


def test_structured_field_wins_over_free_text() -> None:
    """A structured condition takes precedence over the prose fallback."""
    raw = RawEntry(
        entry_id="e2",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "temperature_c": 100,
            "procedure": "ran at 80 °C",
        },
    )
    assert JsonExportAdapter().map_to_ord(raw).temperature_c == 100.0


def test_adapter_rejects_malformed_entry() -> None:
    """An entry without products is a clear ElnFormatError (G4)."""
    raw = RawEntry(entry_id="e3", created_at=_EPOCH, payload={"reactants": [{"smiles": "CCO"}]})
    with pytest.raises(ElnFormatError, match="products"):
        JsonExportAdapter().map_to_ord(raw)


def test_fetch_only_returns_entries_after_cursor(tmp_path: Path) -> None:
    """fetch_new_entries returns only entries strictly newer than `since`, oldest first."""

    async def _run() -> None:
        for name, ts in [("a", "2026-01-01T00:00:00Z"), ("b", "2026-06-01T00:00:00Z")]:
            (tmp_path / f"{name}.json").write_text(
                json.dumps(
                    {
                        "id": name,
                        "timestamp": ts,
                        "reactants": [{"smiles": "CCO"}],
                        "products": [{"smiles": "CCO"}],
                    }
                ),
                encoding="utf-8",
            )
        adapter = JsonExportAdapter(str(tmp_path))
        cutoff = datetime(2026, 3, 1, tzinfo=UTC)
        new = await adapter.fetch_new_entries(cutoff)
        assert [e.entry_id for e in new] == ["b"]  # only the June entry

    asyncio.run(_run())


# --- note + ingest + sync -------------------------------------------------------------


def test_note_from_ord_reaction() -> None:
    """A reaction becomes an agent `reaction` note with SMILES + conditions, no dangling link."""
    note = note_from_ord_reaction(_ester())
    assert note.type == "reaction"
    assert note.created_by == "agent"
    assert note.id == "reaction-rxn-1"
    assert "CCO.CC(=O)O>>CCOC(C)=O" in note.body
    assert "temperature: 80.0 °C" in note.body
    assert note.outgoing_links() == []


def test_ingest_indexes_and_proposes() -> None:
    """A valid reaction is indexed (reaction + compounds) and proposed via the PR-gate."""

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), _FakeSubmitter()
        ref = await ingest_reaction(_ester(), rxn, mol, sub)
        assert ref == "pr://note/reaction-rxn-1"
        assert len(await rxn.all_records()) == 1  # the reaction fingerprint
        assert len(await mol.all_records()) == 3  # ethanol, acetic acid, ethyl acetate
        assert sub.captured[0].path.startswith("knowledge/reaction/reaction-rxn-1")

    asyncio.run(_run())


def test_ingest_rejects_invalid_without_side_effects() -> None:
    """An invalid reaction raises and writes nothing to the index or the graph (G4)."""

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), _FakeSubmitter()
        bad = _ester().model_copy(
            update={"outcomes": [Component(smiles="CCCl", role=Role.PRODUCT)]}
        )
        with pytest.raises(IngestError, match="mass balance"):
            await ingest_reaction(bad, rxn, mol, sub)
        assert await rxn.all_records() == []
        assert await mol.all_records() == []
        assert sub.captured == []

    asyncio.run(_run())


def test_sync_ingests_batch_and_skips_bad_entries() -> None:
    """sync_entries ingests the good entry, records the bad one, and reports the next cursor."""

    async def _run() -> None:
        good = RawEntry(
            entry_id="good",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload={
                "id": "good",
                "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
                "products": [{"smiles": "CCOC(C)=O"}],
            },
        )
        bad = RawEntry(
            entry_id="bad",
            created_at=datetime(2026, 2, 1, tzinfo=UTC),
            payload={"reactants": [{"smiles": "CCO"}], "products": [{"smiles": "CCCl"}]},
        )

        class _Adapter:
            async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
                return [good, bad]

            def map_to_ord(self, raw: RawEntry) -> OrdReaction:
                return JsonExportAdapter().map_to_ord(raw)

        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), _FakeSubmitter()
        summary = await sync_entries(_Adapter(), rxn, mol, sub, _EPOCH)

        assert summary.ingested == ["good"]
        assert [r.entry_id for r in summary.rejected] == ["bad"]
        assert "mass balance" in summary.rejected[0].reason
        assert summary.next_cursor == datetime(2026, 2, 1, tzinfo=UTC)  # newest seen
        assert len(sub.captured) == 1  # only the good entry proposed a note

    asyncio.run(_run())
