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
from mcp_servers.fpstore import InMemoryFingerprintStore
from tests.conftest import FakeSubmitter

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


def test_dimerization_passes_mass_balance() -> None:
    """2 A → A–A with A listed once (normal ELN convention) is valid.

    The export carries no stoichiometric coefficients, so only element presence — not
    atom counts — is checked.
    """
    dimerization = OrdReaction(
        reaction_id="rxn-dimer",
        inputs=[Component(smiles="C=C", role=Role.REACTANT)],
        outcomes=[Component(smiles="C=CCC", role=Role.PRODUCT)],  # doubled carbons
        provenance="eln:chemist-a",
    )
    assert validate_ord(dimerization) == []


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


def test_unknown_role_is_a_mapping_error_not_a_crash() -> None:
    """An unknown role becomes an ElnFormatError (so the sync can reject-and-continue)."""
    raw = RawEntry(
        entry_id="e4",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO", "role": "base"}],  # 'base' is not a Role
            "products": [{"smiles": "CCO"}],
        },
    )
    with pytest.raises(ElnFormatError, match="cannot map"):
        JsonExportAdapter().map_to_ord(raw)


def test_non_dict_component_is_a_mapping_error() -> None:
    """A bare-string species (e.g. "reactants": ["CCO"]) is an ElnFormatError.

    Previously it raised AttributeError, escaping the sync's reject-and-continue
    handler (G4).
    """
    for key in ("reactants", "products"):
        payload: dict[str, object] = {
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
        }
        payload[key] = ["CCO"]  # a string where an object is expected
        raw = RawEntry(entry_id=f"bad-{key}", created_at=_EPOCH, payload=payload)
        with pytest.raises(ElnFormatError, match="not an object"):
            JsonExportAdapter().map_to_ord(raw)


def test_zero_celsius_structured_field_is_preserved() -> None:
    """A structured 0 °C (ice bath) is kept, not discarded as falsy and overwritten by prose."""
    raw = RawEntry(
        entry_id="e5",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "temperature_c": 0,
            "procedure": "then warmed to 80 °C",
        },
    )
    assert JsonExportAdapter().map_to_ord(raw).temperature_c == 0.0


def test_temperature_regex_ignores_nmr_labels() -> None:
    """Prose like '13C NMR' does not fabricate a 13 °C temperature (needs the degree sign)."""
    raw = RawEntry(
        entry_id="e6",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "procedure": "Characterized by 13C NMR; adjusted to pH 7 C.",
        },
    )
    assert JsonExportAdapter().map_to_ord(raw).temperature_c is None


def _prose_entry(procedure: str) -> RawEntry:
    """A minimal entry whose only condition source is the given procedure prose."""
    return RawEntry(
        entry_id="prose",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "procedure": procedure,
        },
    )


def test_temperature_range_extracts_upper_bound_not_negative() -> None:
    """A range like "60-80 °C" yields 80 (the documented upper-bound reading), never -80."""
    reaction = JsonExportAdapter().map_to_ord(_prose_entry("heated at 60-80 °C overnight"))
    assert reaction.temperature_c == 80.0


def test_genuine_negative_temperature_still_extracted() -> None:
    """A real minus sign ("-10 °C") and a bare "0 °C" both still extract from prose."""
    assert JsonExportAdapter().map_to_ord(_prose_entry("cooled to -10 °C")).temperature_c == -10.0
    assert JsonExportAdapter().map_to_ord(_prose_entry("stirred at 0 °C")).temperature_c == 0.0


def test_fetch_only_returns_entries_after_cursor(tmp_path: Path) -> None:
    """fetch_new_entries returns only entries at or after `since`, oldest first."""

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


def _write_entry(path: Path, entry_id: str, timestamp: str) -> None:
    """Write a minimal valid export file for the fetch tests."""
    path.write_text(
        json.dumps(
            {
                "id": entry_id,
                "timestamp": timestamp,
                "reactants": [{"smiles": "CCO"}],
                "products": [{"smiles": "CCO"}],
            }
        ),
        encoding="utf-8",
    )


def test_fetch_includes_entry_exactly_at_cursor(tmp_path: Path) -> None:
    """An entry stamped exactly at the cursor is fetched (inclusive boundary).

    A same-second entry exported after a sync run must not be skipped forever;
    re-ingesting a boundary entry is idempotent, so inclusivity is safe.
    """

    async def _run() -> None:
        _write_entry(tmp_path / "a.json", "a", "2026-03-01T00:00:00Z")
        new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(
            datetime(2026, 3, 1, tzinfo=UTC)
        )
        assert [e.entry_id for e in new] == ["a"]

    asyncio.run(_run())


def test_fetch_skips_corrupt_json_file(tmp_path: Path) -> None:
    """One corrupt export file is skipped, not allowed to abort the whole fetch (G4)."""

    async def _run() -> None:
        (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
        _write_entry(tmp_path / "good.json", "good", "2026-01-01T00:00:00Z")
        new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
        assert [e.entry_id for e in new] == ["good"]

    asyncio.run(_run())


def test_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    """A timestamp without an offset is treated as UTC.

    A naive datetime would later raise TypeError when compared against the sync's
    offset-aware cursor.
    """

    async def _run() -> None:
        _write_entry(tmp_path / "naive.json", "naive", "2026-01-01T00:00:00")  # no offset
        new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
        assert [e.entry_id for e in new] == ["naive"]
        assert new[0].created_at == datetime(2026, 1, 1, tzinfo=UTC)

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
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        ref = await ingest_reaction(_ester(), rxn, mol, sub)
        assert ref == "pr://note/reaction-rxn-1"
        assert len(await rxn.all_records()) == 1  # the reaction fingerprint
        assert len(await mol.all_records()) == 3  # ethanol, acetic acid, ethyl acetate
        assert sub.submissions[0].path.startswith("knowledge/reaction/reaction-rxn-1")

    asyncio.run(_run())


def test_ingest_rejects_invalid_without_side_effects() -> None:
    """An invalid reaction raises and writes nothing to the index or the graph (G4)."""

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        bad = _ester().model_copy(
            update={"outcomes": [Component(smiles="CCCl", role=Role.PRODUCT)]}
        )
        with pytest.raises(IngestError, match="mass balance"):
            await ingest_reaction(bad, rxn, mol, sub)
        assert await rxn.all_records() == []
        assert await mol.all_records() == []
        assert sub.submissions == []

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
        bad_balance = RawEntry(
            entry_id="bad-balance",
            created_at=datetime(2026, 2, 1, tzinfo=UTC),
            payload={"reactants": [{"smiles": "CCO"}], "products": [{"smiles": "CCCl"}]},
        )
        # An unmappable entry (unknown role) must be rejected, not abort the whole batch.
        unmappable = RawEntry(
            entry_id="unmappable",
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
            payload={
                "reactants": [{"smiles": "CCO", "role": "base"}],
                "products": [{"smiles": "CCO"}],
            },
        )

        class _Adapter:
            async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
                return [good, bad_balance, unmappable]

            def map_to_ord(self, raw: RawEntry) -> OrdReaction:
                return JsonExportAdapter().map_to_ord(raw)

        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_Adapter(), rxn, mol, sub, _EPOCH)

        assert summary.ingested == ["good"]  # the good entry survives both bad ones
        assert {r.entry_id for r in summary.rejected} == {"bad-balance", "unmappable"}
        reasons = {r.entry_id: r.reason for r in summary.rejected}
        assert "mass balance" in reasons["bad-balance"]
        assert "cannot map" in reasons["unmappable"]
        assert summary.next_cursor == datetime(2026, 3, 1, tzinfo=UTC)  # newest seen
        assert len(sub.submissions) == 1  # only the good entry proposed a note

    asyncio.run(_run())


def test_sync_rejects_degenerate_reaction_without_aborting_batch() -> None:
    """A degenerate reaction (CCO>>CCO) with no computable fingerprint is a rejection.

    It is schema-valid and passes validation, but fingerprinting fails; that must be a
    per-entry rejection — the batch continues and the cursor still advances (G4).
    """

    async def _run() -> None:
        degenerate = RawEntry(
            entry_id="degenerate",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload={"reactants": [{"smiles": "CCO"}], "products": [{"smiles": "CCO"}]},
        )
        good = RawEntry(
            entry_id="good",
            created_at=datetime(2026, 2, 1, tzinfo=UTC),
            payload={
                "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
                "products": [{"smiles": "CCOC(C)=O"}],
            },
        )

        class _Adapter:
            async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
                return [degenerate, good]

            def map_to_ord(self, raw: RawEntry) -> OrdReaction:
                return JsonExportAdapter().map_to_ord(raw)

        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_Adapter(), rxn, mol, sub, _EPOCH)

        assert summary.ingested == ["good"]
        assert [r.entry_id for r in summary.rejected] == ["degenerate"]
        assert "fingerprint" in summary.rejected[0].reason
        assert summary.next_cursor == datetime(2026, 2, 1, tzinfo=UTC)  # cursor advanced

    asyncio.run(_run())
