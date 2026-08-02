"""Behavioral tests for ELN ingestion (plan Phase 4), all runnable without a server.

Covers the ORD schema, the RDKit+mass-balance validator, the JSON adapter (structured and
free-text mapping), the reaction-note mapping, and the ingest + sync flow into in-memory
fingerprint stores and a fake PR-gate — the CHECKMATE 4 chain "ELN entry → validated note +
fingerprint-indexed", proven end to end without a database or git.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import RawEntry, parse_iso_utc
from chemclaw.ingest.eln.ingest import IngestError, ingest_reaction
from chemclaw.ingest.eln.json_adapter import ElnFormatError, JsonExportAdapter
from chemclaw.ingest.eln.note import note_from_ord_reaction
from chemclaw.ingest.eln.ord import (
    Component,
    Impurity,
    OrdReaction,
    ReactionStep,
    Role,
    StepKind,
)
from chemclaw.ingest.eln.ord_adapter import OrdFormatError, OrdJsonAdapter
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.kg.render import render_note
from chemclaw.retrieval.retrievers import GraphRetriever
from chemclaw.science.fingerprints.molfp.search import find_similar_molecules
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
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


def test_parse_iso_utc_normalizes_to_tz_aware_utc() -> None:
    """The shared timestamp helper (CON-3) reads Z, offsets, and naive strings as tz-aware UTC."""
    # Trailing 'Z' → UTC.
    assert parse_iso_utc("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    # Explicit offset is honored (and remains offset-aware).
    assert parse_iso_utc("2026-01-02T03:04:05+02:00").utcoffset() is not None
    # Naive (no offset) is read as UTC, never left naive.
    assert parse_iso_utc("2026-01-02T03:04:05").tzinfo is UTC
    # An unparseable string raises ValueError for the caller to wrap in its format error.
    with pytest.raises(ValueError):
        parse_iso_utc("not-a-timestamp")


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
    # The source system and the entry id, not only the operator: with two ELN sources enabled,
    # colliding entry ids produced the same note id with nothing saying they came from different
    # systems.
    assert reaction.provenance == "eln-json:e1:chemist-c"


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


def test_the_entrys_hypothesis_is_carried_onto_the_record() -> None:
    """The question the run's conditions answer must survive ingestion (D-162)."""
    raw = RawEntry(
        entry_id="e-hyp",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "hypothesis": "does dropping to 60 °C suppress the des-bromo impurity?",
            "procedure": "Stirred at 60 °C for 4 h.",
        },
    )
    reaction = JsonExportAdapter().map_to_ord(raw)
    assert reaction.hypothesis == "does dropping to 60 °C suppress the des-bromo impurity?"


def test_an_entry_without_a_hypothesis_does_not_get_one_from_the_prose() -> None:
    """Never inferred: an extracted motive would be indistinguishable from a chemist's own."""
    raw = RawEntry(
        entry_id="e-nohyp",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO"}],
            "procedure": "Lowered the temperature to see whether the impurity went away.",
        },
    )
    assert JsonExportAdapter().map_to_ord(raw).hypothesis is None


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


def test_fetch_logs_the_skipped_corrupt_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A dropped export file names itself at WARNING — the one admin signal it was skipped."""

    async def _run() -> None:
        (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
        await JsonExportAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    assert "corrupt.json" in caplog.text  # the specific file is identified, not silently lost


def _set_mtime(path: Path, moment: datetime) -> None:
    """Stamp a file's modification time — how a late *arrival* is distinguished from old data."""
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


def test_late_arriving_export_is_reported_not_silently_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that lands after the cursor with an older payload timestamp warns by name.

    This is the silent-data-loss case: the entry is filtered out on this run and on every run
    after it, so without this warning an operator has no way to learn the reaction was lost.
    """

    async def _run() -> None:
        _write_entry(tmp_path / "late.json", "late", "2026-01-01T00:00:00Z")
        _set_mtime(tmp_path / "late.json", datetime(2026, 6, 1, tzinfo=UTC))  # arrived late
        new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(
            datetime(2026, 3, 1, tzinfo=UTC)
        )
        assert new == []  # unchanged behavior: it is still (correctly) not ingested

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    assert "late.json" in caplog.text
    assert "not ingested" in caplog.text


def test_genuinely_old_export_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An old file that was already there before the cursor is silent — no false alarm.

    A warning that fires for ordinary already-ingested history would be ignored within a week,
    taking the real late-arrival signal with it.
    """

    async def _run() -> None:
        _write_entry(tmp_path / "old.json", "old", "2026-01-01T00:00:00Z")
        _set_mtime(tmp_path / "old.json", datetime(2026, 1, 1, tzinfo=UTC))
        assert (
            await JsonExportAdapter(str(tmp_path)).fetch_new_entries(
                datetime(2026, 3, 1, tzinfo=UTC)
            )
            == []
        )

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    assert caplog.text == ""


def test_late_arrival_warning_is_one_bounded_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Many late files produce a single WARNING with a capped name list and the full count.

    A permanently-late file re-qualifies on every sync run, so per-file lines would grow into a
    storm; one bounded line per fetch stays readable.
    """

    async def _run() -> None:
        for index in range(12):
            path = tmp_path / f"late-{index:02d}.json"
            _write_entry(path, f"late-{index:02d}", "2026-01-01T00:00:00Z")
            _set_mtime(path, datetime(2026, 6, 1, tzinfo=UTC))
        await JsonExportAdapter(str(tmp_path)).fetch_new_entries(datetime(2026, 3, 1, tzinfo=UTC))

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    assert len(caplog.records) == 1
    assert "12 export file(s)" in caplog.text
    assert "+2 more" in caplog.text  # names capped, count preserved


def test_ord_adapter_reports_late_arrivals_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The ORD adapter shares the late-arrival check — the guard lives once, not per adapter."""

    async def _run() -> None:
        path = tmp_path / "late-ord.json"
        path.write_text(
            json.dumps(
                {
                    "reaction_id": "ord-late",
                    "provenance": {"record_created": {"time": {"value": "2026-01-01T00:00:00Z"}}},
                    "inputs": {},
                    "outcomes": [],
                }
            ),
            encoding="utf-8",
        )
        _set_mtime(path, datetime(2026, 6, 1, tzinfo=UTC))
        assert (
            await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(datetime(2026, 3, 1, tzinfo=UTC))
            == []
        )

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    assert "late-ord.json" in caplog.text


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
        assert sub.submissions[0].files[0].path.startswith("knowledge/reaction/reaction-rxn-1")

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


def test_sync_logs_the_outcome_and_each_rejection(caplog: pytest.LogCaptureFixture) -> None:
    """A sync run logs its ingested/rejected counts and a WARNING per rejected entry."""
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
        entry_id="bad-balance",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        payload={"reactants": [{"smiles": "CCO"}], "products": [{"smiles": "CCCl"}]},
    )

    class _Adapter:
        async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
            return [good, bad]

        def map_to_ord(self, raw: RawEntry) -> OrdReaction:
            return JsonExportAdapter().map_to_ord(raw)

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        await sync_entries(_Adapter(), rxn, mol, sub, _EPOCH)

    with caplog.at_level(logging.INFO):
        asyncio.run(_run())
    assert "ingested=1 rejected=1" in caplog.text  # the run outcome, without opening the result
    assert "bad-balance" in caplog.text  # the specific rejected entry is named at WARNING


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


def _good_entry(entry_id: str, created_at: datetime) -> RawEntry:
    """A valid, mass-balanced esterification entry for the sync boundary tests."""
    return RawEntry(
        entry_id=entry_id,
        created_at=created_at,
        payload={
            "id": entry_id,
            "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
            "products": [{"smiles": "CCOC(C)=O"}],
        },
    )


class _ListAdapter:
    """A fake adapter serving a fixed entry list and recording the fetch `since` it saw."""

    def __init__(self, entries: list[RawEntry]) -> None:
        self.entries = entries
        self.fetched_since: list[datetime] = []

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        self.fetched_since.append(since)
        return [e for e in self.entries if e.created_at >= since]

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        return JsonExportAdapter().map_to_ord(raw)


def test_sync_rejects_non_slug_entry_id_without_aborting_batch() -> None:
    """An entry id that is not a valid note slug is one rejection, never a batch abort (G4).

    `Note(id="reaction-EXP 2024/001")` raises a pydantic ValidationError, which is not a
    ChemclawError — it must still be caught per entry, or one routinely-named ELN entry
    permanently halts the whole sync source.
    """

    async def _run() -> None:
        bad_id = _good_entry("EXP 2024/001", datetime(2026, 1, 1, tzinfo=UTC))
        good = _good_entry("good", datetime(2026, 2, 1, tzinfo=UTC))
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([bad_id, good]), rxn, mol, sub, _EPOCH)

        assert summary.ingested == ["good"]
        assert [r.entry_id for r in summary.rejected] == ["EXP 2024/001"]
        assert "slug" in summary.rejected[0].reason
        assert summary.next_cursor == datetime(2026, 2, 1, tzinfo=UTC)

    asyncio.run(_run())


def test_future_dated_entry_is_rejected_and_does_not_poison_cursor() -> None:
    """A typo'd future year is a visible rejection and never becomes the high-water cursor.

    If it advanced the cursor, every later real entry would be silently skipped forever
    (the persisted cursor is never lowered by any code path).
    """

    async def _run() -> None:
        future = _good_entry("future", datetime(2062, 7, 23, tzinfo=UTC))
        good = _good_entry("good", datetime(2026, 1, 1, tzinfo=UTC))
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([future, good]), rxn, mol, sub, _EPOCH)

        assert summary.ingested == ["good"]
        assert [r.entry_id for r in summary.rejected] == ["future"]
        assert "future" in summary.rejected[0].reason
        assert summary.next_cursor == datetime(2026, 1, 1, tzinfo=UTC)  # not 2062

    asyncio.run(_run())


def test_sync_fetches_an_overlap_window_behind_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late-landing export file stamped just before the cursor is still ingested.

    The fetch reaches `since - eln_sync_overlap_seconds` (re-fetching is free — ingestion
    is idempotent), and the returned cursor never regresses below `since`.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))  # no merged notes
        monkeypatch.setattr(settings, "eln_sync_overlap_seconds", 1800.0)
        cursor = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        late = _good_entry("late", cursor - timedelta(minutes=20))
        adapter = _ListAdapter([late])
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(adapter, rxn, mol, sub, cursor)

        assert adapter.fetched_since == [cursor - timedelta(seconds=1800)]
        assert summary.ingested == ["late"]
        assert summary.skipped_existing == []  # its note is not merged yet, so it ingests
        # And it is flagged as awaiting merge, which is the honest report even on a first sync:
        # the entry sits inside the replay window with no merged note, so the *next* run fetches
        # and proposes it again. "Will come back until someone merges it" is what a single run can
        # establish; "was proposed before" is not (this entry never was).
        assert summary.awaiting_merge == ["late"]
        assert summary.next_cursor == cursor  # the cursor never moves backwards

    asyncio.run(_run())


def _write_merged_note(knowledge: Path, entry: RawEntry) -> None:
    """Lay the merged reaction note for `entry` — exactly what an approved PR leaves behind.

    Rendered from the entry rather than stubbed, because the sync now compares the note's *body*
    and not only its id: a stub would make "already merged" and "unchanged" indistinguishable
    again, which is the very thing being fixed.
    """
    note = note_from_ord_reaction(JsonExportAdapter().map_to_ord(entry))
    note_dir = knowledge / "reaction"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / f"{note.id}.md").write_text(render_note(note), encoding="utf-8")


def test_sync_skips_overlap_entry_whose_note_already_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlap-window entry whose note is already merged is skipped, not re-ingested.

    The hourly overlap replay must not pay fingerprint upserts plus a full PR-gate git
    cycle per already-ingested entry. What proves the entry was fully ingested is a merged note
    whose **body matches** — not merely one with the same id, which is what this checked before and
    is why every in-place ELN amendment was dropped. An unchanged entry costs a lookup and is
    reported under `skipped_existing`, never inflating `ingested`.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        late = _good_entry("late", cursor - timedelta(hours=2))
        _write_merged_note(tmp_path, late)
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([late]), rxn, mol, sub, cursor)

        assert summary.skipped_existing == ["late"]
        assert summary.ingested == []  # a replay skip is not a fresh ingest
        assert sub.submissions == []  # no PR-gate git cycle
        assert await rxn.all_records() == []  # no fingerprint re-upserts
        assert summary.next_cursor == cursor

    asyncio.run(_run())


def test_sync_still_ingests_new_entry_even_if_its_note_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged-note short-circuit applies only to the overlap replay, never past the cursor.

    An entry *after* `since` is deliberate work (e.g. a manual backfill re-run): it must
    take the full idempotent ingest path even when a note with its id already exists.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        new = _good_entry("new", cursor + timedelta(hours=2))
        _write_merged_note(tmp_path, new)
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([new]), rxn, mol, sub, cursor)

        assert summary.ingested == ["new"]
        assert summary.skipped_existing == []
        assert len(sub.submissions) == 1

    asyncio.run(_run())


def test_sync_without_overlap_fetches_from_the_cursor_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apply_overlap=False` fetches from `since` (still inclusive), not the overlap floor.

    The workflow's chunk loop passes this for every chunk after the first, so a backlog
    drain replays the overlap window once per run instead of once per chunk — while the
    inclusive same-second boundary entry is still picked up, preserving the cursor contract.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))  # no merged notes
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        adapter = _ListAdapter([_good_entry("boundary", cursor)])
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(adapter, rxn, mol, sub, cursor, apply_overlap=False)

        assert adapter.fetched_since == [cursor]  # no reach behind the cursor
        assert summary.ingested == ["boundary"]  # inclusive boundary still processed
        assert summary.next_cursor == cursor

    asyncio.run(_run())


def test_overlap_rerejection_logs_debug_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replayed rejection (inside the overlap window) logs at DEBUG, a fresh one at WARNING.

    The cursor advances past sane-timestamped rejections, so an overlap-window rejection was
    already warned about when first seen — re-warning it hourly would bury real new failures.
    Both still appear in the summary, so the run's report stays complete.
    """
    since = datetime(2026, 1, 2, tzinfo=UTC)
    bad_payload = {"reactants": [{"smiles": "CCO"}]}  # missing products → rejected
    replayed = RawEntry(
        entry_id="replayed-bad", created_at=since - timedelta(hours=1), payload=bad_payload
    )
    fresh = RawEntry(
        entry_id="fresh-bad", created_at=since + timedelta(hours=1), payload=bad_payload
    )

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([replayed, fresh]), rxn, mol, sub, since)
        assert {r.entry_id for r in summary.rejected} == {"replayed-bad", "fresh-bad"}

    with caplog.at_level(logging.DEBUG, logger="chemclaw.ingest.eln.sync"):
        asyncio.run(_run())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("fresh-bad" in message for message in warnings)  # first seen → WARNING
    assert not any("replayed-bad" in message for message in warnings)
    assert any("replayed-bad" in message for message in debugs)  # replay → DEBUG only


def test_sync_log_sanitizes_external_entry_ids(caplog: pytest.LogCaptureFixture) -> None:
    """Control characters in an external entry id cannot forge log lines (trust boundary)."""
    forged = RawEntry(
        entry_id="bad\nFORGED line",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={"reactants": [{"smiles": "CCO"}]},  # missing products → rejected
    )

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        await sync_entries(_ListAdapter([forged]), rxn, mol, sub, _EPOCH)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())
    assert "bad FORGED line" in caplog.text  # newline collapsed, id still identifiable


def test_nested_condition_object_is_a_mapping_error() -> None:
    """A structured field that is an object (`{"temperature_c": {"value": 80}}`) is rejected.

    `float(dict)` raises TypeError, which must become an ElnFormatError so the sync treats
    the entry as one rejection instead of aborting the batch (G4).
    """
    for field, value in [("temperature_c", {"value": 80}), ("time_h", [2.5])]:
        raw = RawEntry(
            entry_id=f"nested-{field}",
            created_at=_EPOCH,
            payload={
                "reactants": [{"smiles": "CCO"}],
                "products": [{"smiles": "CCO"}],
                field: value,
            },
        )
        with pytest.raises(ElnFormatError, match="cannot map"):
            JsonExportAdapter().map_to_ord(raw)


def test_nested_yield_object_is_a_mapping_error() -> None:
    """A non-scalar `yield_percent` is an ElnFormatError, not an escaping TypeError (G4)."""
    raw = RawEntry(
        entry_id="nested-yield",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [{"smiles": "CCO", "yield_percent": {"value": 85}}],
        },
    )
    with pytest.raises(ElnFormatError, match="cannot map"):
        JsonExportAdapter().map_to_ord(raw)


# The ELN-specific adapter registry (`eln/registry.py`) was removed in DUP-1: source selection is
# unified in `ingest/sources/registry.py` (config-driven via `data_sources`), covered by
# `tests/test_datasource_seam.py`. Both adapters are still exercised directly throughout this file.


def test_a_single_product_reaction_note_says_which_compound_it_is_about() -> None:
    """The largest note class in the graph carried no `compound_smiles` at all.

    Nothing that groups by compound could therefore ever see a reaction: `kg.conflicts` groups on
    `(type, compound_smiles)`, and every by-compound question starts there. The structure was in
    the record the whole time — it goes into the body as part of the reaction SMILES — it simply
    never reached the field.
    """
    assert note_from_ord_reaction(_ester()).compound_smiles == "CCOC(C)=O"


def test_a_multi_product_reaction_names_no_principal_compound() -> None:
    """A wrong `compound_smiles` is worse than none: it is what a by-compound search returns.

    "The molecule this note is about" has no honest answer for a run reporting a product and a
    by-product, and an ELN frequently omits the amounts that would rank them.
    """
    two_products = _ester().model_copy(
        update={
            "outcomes": [
                Component(smiles="CCOC(C)=O", role=Role.PRODUCT),
                Component(smiles="CCOCC", role=Role.PRODUCT),
            ]
        }
    )
    assert note_from_ord_reaction(two_products).compound_smiles is None


def test_solvent_and_catalyst_go_in_the_agent_slot() -> None:
    """The **record** form shows the solvent and the catalyst in the slot that says what they are.

    A notation claim and only that. This docstring used to say the three-part form was what stopped
    the solvent dominating DRFP similarity; it never did — `DrfpEncoder.internal_encode` folds the
    agent slot back onto the reactants, so the two forms encode identically. What changes the bits
    is `transformation_smiles`, which leaves those species out; the measurements are in
    `tests/test_rxnfp.py`.

    A *reagent* stays on the left in both forms: a base or an oxidant participates
    stoichiometrically and is part of what the transformation is.
    """
    reaction = OrdReaction(
        reaction_id="rxn-agents",
        inputs=[
            Component(smiles="Brc1ccccc1", role=Role.REACTANT),
            Component(smiles="OB(O)c1ccccc1", role=Role.REACTANT),
            Component(smiles="[K+].[OH-]", role=Role.REAGENT),
            Component(smiles="C1CCOC1", role=Role.SOLVENT),
            Component(smiles="[Pd]", role=Role.CATALYST),
        ],
        outcomes=[Component(smiles="c1ccc(-c2ccccc2)cc1", role=Role.PRODUCT)],
        provenance="eln:chemist-a",
    )

    assert reaction.reaction_smiles() == (
        "Brc1ccccc1.OB(O)c1ccccc1.[K+].[OH-]>C1CCOC1.[Pd]>c1ccc(-c2ccccc2)cc1"
    )


def test_a_reaction_with_no_agents_still_renders_the_three_part_form() -> None:
    """An empty agent slot is the convention's own shape, not a special case to branch on."""
    assert _ester().reaction_smiles() == "CCO.CC(=O)O>>CCOC(C)=O"


def test_the_record_form_keeps_the_solvent_the_fingerprint_form_drops_it() -> None:
    """The two forms are two questions, and a note must keep answering the first one.

    `reaction_smiles` is what a reaction note, a campaign step list and a playbook's representative
    reaction render — and the solvent is a headline condition of a process-development run, so a
    note that no longer named it would be a real loss in the graph's largest note class. That is
    the whole reason the exclusion is a second method rather than an edit to this one.
    """
    reaction = OrdReaction(
        reaction_id="rxn-two-forms",
        inputs=[
            Component(smiles="Brc1ccccc1", role=Role.REACTANT),
            Component(smiles="OB(O)c1ccccc1", role=Role.REACTANT),
            Component(smiles="C1CCOC1", role=Role.SOLVENT),
        ],
        outcomes=[Component(smiles="c1ccc(-c2ccccc2)cc1", role=Role.PRODUCT)],
        provenance="eln:chemist-a",
    )

    assert "C1CCOC1" in reaction.reaction_smiles()
    assert "C1CCOC1" not in reaction.transformation_smiles()
    # And the note a human reviews still shows it, which is what the split is protecting.
    assert "C1CCOC1" in note_from_ord_reaction(reaction).body


def test_an_amended_entry_is_re_proposed_rather_than_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A yield corrected after assay must reach the graph, not vanish into `skipped_existing`.

    The check was on the note *id*, which treats "already seen" and "unchanged" as the same thing.
    They are not: an ELN amends an entry in place — a yield revised, an impurity added, a
    retraction — while keeping its `created_at`, so every correction was silently dropped and
    reported as an already-ingested replay. The justification given was "ELN exports are
    immutable", which is an assumption about someone else's system.

    The corrected entry is simply re-proposed, so the PR-gate shows a reviewer the diff. That is
    what a git-backed graph is for, and why an amendment needs no separate note-versioning scheme.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        original = _good_entry("amended", cursor - timedelta(hours=2))
        _write_merged_note(tmp_path, original)

        corrected = original.model_copy(
            update={
                "payload": {
                    **original.payload,
                    "products": [{"smiles": "CCOC(C)=O", "yield_percent": 31}],
                },
                "modified_at": cursor + timedelta(hours=1),
            }
        )
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([corrected]), rxn, mol, sub, cursor)

        assert summary.ingested == ["amended"]
        assert summary.skipped_existing == []
        # Not awaiting merge: a merged predecessor is proof the review queue moves, so this is new
        # content going in front of a human rather than the same claim going round again.
        assert summary.awaiting_merge == []
        assert len(sub.submissions) == 1
        assert "31" in sub.submissions[0].files[0].content

    asyncio.run(_run())


def test_an_entry_whose_note_never_merged_is_reported_as_awaiting_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A proposal nobody merged is re-proposed forever, and the summary has to say so.

    `ingested` counts entries whose note was *proposed* through the PR-gate — proposed, because
    that is as far as an automated step may take a knowledge claim. So an entry whose PR the
    `kg-validate` hazard gate blocks, or that a reviewer simply never got to, is re-proposed on
    every subsequent run and counted again each time, while an operator reading the summary sees a
    steady ingest count and no indication that nothing is landing.

    Asserted through a real `sync_entries` run over an empty knowledge dir (nothing merged) with
    the entry inside the replay window, plus the WARNING that carries the same fact to an operator
    who reads logs rather than workflow results.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))  # nothing merged, ever
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        blocked = _good_entry("blocked", cursor - timedelta(hours=2))
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        with caplog.at_level(logging.WARNING):
            summary = await sync_entries(_ListAdapter([blocked]), rxn, mol, sub, cursor)

        assert summary.ingested == ["blocked"]  # the proposal really was made again
        assert summary.awaiting_merge == ["blocked"]  # and it accomplished nothing new
        assert len(sub.submissions) == 1
        assert "blocked" in caplog.text and "re-proposed every run" in caplog.text

    asyncio.run(_run())


def test_a_first_time_entry_is_not_reported_as_awaiting_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new entry's note is unmerged too, and reporting it would make the field noise.

    Every fresh proposal is unmerged for as long as review takes; what `awaiting_merge` reports is
    the narrower thing an operator can act on — an entry the sync will keep re-proposing because it
    is inside the replay window. A new entry is past the cursor, so the next run never sees it
    again, and it belongs in `ingested` alone.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        fresh = _good_entry("fresh", cursor + timedelta(hours=1))
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([fresh]), rxn, mol, sub, cursor)

        assert summary.ingested == ["fresh"]
        assert summary.awaiting_merge == []

    asyncio.run(_run())


def test_an_entry_that_fails_to_ingest_is_only_reported_as_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two reports are exclusive: a rejection must not also show up as awaiting merge.

    The unmerged-replay flag is decided before `ingest_reaction` runs (it needs the mapped note)
    and recorded after it, so a bad entry inside the replay window — which is exactly where a
    rejection is deterministic and repeats every run — reports one outcome, not two.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        bad = RawEntry(
            entry_id="bad",
            created_at=cursor - timedelta(hours=2),
            payload={"reactants": [{"smiles": "CCO"}], "products": [{"smiles": "CCCl"}]},
        )
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([bad]), rxn, mol, sub, cursor)

        assert [entry.entry_id for entry in summary.rejected] == ["bad"]
        assert summary.ingested == [] and summary.awaiting_merge == []

    asyncio.run(_run())


def test_an_unchanged_entry_reported_as_amended_still_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that stamps `modified` on every export must not re-propose the whole corpus.

    The comparison is on content, not on the presence of a modification timestamp — otherwise an
    exporter that touches every record would turn each sync into a full re-submission, which is a
    worse failure than the one being fixed because it is loud and continuous.
    """

    async def _run() -> None:
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        cursor = datetime(2026, 1, 2, tzinfo=UTC)
        entry = _good_entry("touched", cursor - timedelta(hours=2))
        _write_merged_note(tmp_path, entry)
        touched = entry.model_copy(update={"modified_at": cursor + timedelta(hours=1)})

        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        summary = await sync_entries(_ListAdapter([touched]), rxn, mol, sub, cursor)

        assert summary.skipped_existing == ["touched"]
        assert sub.submissions == []

    asyncio.run(_run())


def test_an_amended_export_re_enters_the_fetch_window(tmp_path: Path) -> None:
    """An adapter filtering on creation time alone can never see an in-place correction.

    This is the half upstream of the sync's content check: an ELN amends an entry and leaves its
    `timestamp` alone, so an entry created before the cursor is never fetched again no matter what
    changed in it. `entry_window` filters on the later of the two, which is what brings the
    corrected record back into view.
    """
    created = datetime(2026, 1, 1, tzinfo=UTC)
    cursor = datetime(2026, 1, 5, tzinfo=UTC)
    (tmp_path / "amended.json").write_text(
        json.dumps(
            {
                "id": "amended",
                "timestamp": created.isoformat(),
                "modified": (cursor + timedelta(hours=1)).isoformat(),
                "reactants": [{"smiles": "CCO"}],
                "products": [{"smiles": "CCOC(C)=O"}],
            }
        ),
        encoding="utf-8",
    )

    entries = asyncio.run(JsonExportAdapter(str(tmp_path)).fetch_new_entries(cursor))

    assert [entry.entry_id for entry in entries] == ["amended"]
    assert entries[0].created_at == created  # the cursor still advances on the entry's own time
    assert entries[0].modified_at is not None


def test_an_old_unamended_export_stays_out_of_the_window(tmp_path: Path) -> None:
    """The guard on the above: widening the window must not re-fetch the whole corpus."""
    (tmp_path / "old.json").write_text(
        json.dumps(
            {
                "id": "old",
                "timestamp": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                "reactants": [{"smiles": "CCO"}],
                "products": [{"smiles": "CCOC(C)=O"}],
            }
        ),
        encoding="utf-8",
    )

    assert (
        asyncio.run(
            JsonExportAdapter(str(tmp_path)).fetch_new_entries(datetime(2026, 1, 5, tzinfo=UTC))
        )
        == []
    )


# --- ORD identifier resolution --------------------------------------------------------


def _ord_payload(reactant_identifiers: list[dict[str, str]]) -> dict[str, object]:
    """A minimal ORD reaction whose single reactant carries `reactant_identifiers`."""
    return {
        "reactionId": "ord-ident-1",
        "provenance": {"recordCreated": {"time": {"value": "2026-01-01T00:00:00Z"}}},
        "inputs": {
            "m1": {
                "components": [
                    {"identifiers": reactant_identifiers, "reactionRole": "REACTANT"},
                ]
            }
        },
        "outcomes": [
            {
                "products": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCOC(C)=O"}],
                        "reactionRole": "PRODUCT",
                    }
                ]
            }
        ],
    }


def _map_ord(tmp_path: Path, identifiers: list[dict[str, str]]) -> OrdReaction:
    """Write one ORD entry with those reactant identifiers and map it through the adapter."""
    (tmp_path / "ident.json").write_text(json.dumps(_ord_payload(identifiers)), encoding="utf-8")

    async def _run() -> OrdReaction:
        adapter = OrdJsonAdapter(str(tmp_path))
        entries = await adapter.fetch_new_entries(_EPOCH)
        return adapter.map_to_ord(entries[0])

    return asyncio.run(_run())


def test_ord_compound_resolves_from_inchi_when_no_smiles_is_given(tmp_path: Path) -> None:
    """ORD's identifier union allows InChI, and converting it is exact, not a guess.

    Ethanol's InChI, so the assertion is on the *structure recovered*, not on a round trip
    through the code under test.
    """
    reaction = _map_ord(tmp_path, [{"type": "INCHI", "value": "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"}])
    assert [c.smiles for c in reaction.inputs] == ["CCO"]


def test_ord_compound_resolves_from_a_known_reagent_name(tmp_path: Path) -> None:
    """A NAME-only component resolves through the same table `resolve_compound` serves."""
    reaction = _map_ord(tmp_path, [{"type": "NAME", "value": "acetonitrile"}])
    assert [c.smiles for c in reaction.inputs] == ["CC#N"]


def test_ord_compound_with_no_resolvable_identifier_is_still_refused(tmp_path: Path) -> None:
    """A paper's internal shorthand is not a structure, and inventing one would be worse.

    This is the real Perera flow-Suzuki case: the source spreadsheet publishes the second
    coupling partner only as `2a, Boronic Acid`. Widening the identifier union must not turn an
    honest refusal into a fabricated structure.
    """
    with pytest.raises(OrdFormatError, match="no resolvable structure identifier"):
        _map_ord(tmp_path, [{"type": "NAME", "value": "2a, Boronic Acid"}])


def test_a_search_hit_id_is_the_note_id_the_ingest_wrote() -> None:
    """The round trip a chemist takes: a `similar_reactions` hit handed to `expand_note`.

    `connectors.rxnfp.similar_reactions` used to return the fingerprint index's own key while the
    ELN ingest stored the note under a `reaction-` prefix, so a hit could not be opened and the
    chemist was told the procedure was not in the graph — with the note on disk. Asserted as an
    equality between the two ends rather than against a literal, so the test still holds if the
    prefix ever changes and fails if only one end changes.
    """
    reaction = _ester()
    written = note_from_ord_reaction(reaction).id
    assert note_id_for_reaction(reaction.reaction_id) == written


# --- impurity structures reach the molecule index -------------------------------------


def _with_impurities(*impurities: Impurity) -> OrdReaction:
    """The esterification, plus an observed impurity profile (the KNW-2 half of an outcome)."""
    return _ester().model_copy(update={"impurities": list(impurities)})


def test_an_identified_impurity_is_findable_by_structure() -> None:
    """An impurity question — "have we seen this one before?" — is a structure question.

    An impurity's SMILES used to reach the note *text* only, so the molecule it names was findable
    by lexical search and invisible to `similar_molecules`/`substructure_matches` — the exact
    inverse of the question. Asserted through the search, not through a record count: what matters
    is that a chemist querying the structure gets the run back.
    """

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        # Diethyl ether — an ether by-product of the esterification, charged nowhere in the record.
        await ingest_reaction(
            _with_impurities(Impurity(name="ether", smiles="CCOCC")), rxn, mol, sub
        )
        hits = await find_similar_molecules(mol, "CCOCC", threshold=0.99)
        assert [hit.smiles for hit in hits] == ["CCOCC"]

    asyncio.run(_run())


def test_an_impurity_with_no_structure_is_skipped_not_fatal() -> None:
    """An ELN routinely records only a chromatographic name; that is not an error (KNW-2)."""

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        await ingest_reaction(_with_impurities(Impurity(name="RRT 0.82")), rxn, mol, sub)
        # The three reaction compounds, and nothing minted from a nameless chromatographic peak.
        assert len(await mol.all_records()) == 3
        assert sub.submissions  # the run was still proposed

    asyncio.run(_run())


def test_an_unparseable_impurity_structure_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed trace-impurity string must not cost the whole experiment.

    `validate_ord` checks the reaction's own components, never the impurity profile, so a
    structure the analytics software garbled reaches the fingerprinter unchecked. Dropping it
    keeps the run; logging it keeps the drop visible.
    """

    async def _run() -> None:
        rxn, mol, sub = InMemoryFingerprintStore(), InMemoryFingerprintStore(), FakeSubmitter()
        bad = Impurity(name="garbled", smiles="C1CC")
        with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.eln.ingest"):
            await ingest_reaction(_with_impurities(bad), rxn, mol, sub)
        assert len(await mol.all_records()) == 3
        assert sub.submissions
        assert "unparseable impurity SMILES" in caplog.text

    asyncio.run(_run())


# --- the note carries its project, and its scale --------------------------------------


def test_a_reaction_note_is_reachable_by_its_project_tag(tmp_path: Path) -> None:
    """`gather_evidence(tag=…)` is documented as the project filter and was inert on reactions.

    Proven through the retriever's own eligibility gate rather than by reading `note.tags`: the
    tag is worth setting only because a filtered sweep reaches the note, and a wrong tag must
    still exclude it.
    """

    async def _run() -> None:
        note = note_from_ord_reaction(_ester().model_copy(update={"project": "prj-alpha"}))
        (tmp_path / "reaction").mkdir()
        (tmp_path / "reaction" / f"{note.id}.md").write_text(render_note(note), encoding="utf-8")
        retriever = GraphRetriever(str(tmp_path))
        found = await retriever.retrieve("eln entry", {"tag": "prj-alpha"})
        assert [chunk.source_note_id for chunk in found] == [note.id]
        assert await retriever.retrieve("eln entry", {"tag": "prj-beta"}) == []

    asyncio.run(_run())


def test_a_reaction_with_no_project_invents_no_tag() -> None:
    """A record without a project gets no tag — never a placeholder that a filter would match."""
    assert note_from_ord_reaction(_ester()).tags == []


def _charged(*inputs: Component) -> OrdReaction:
    """The esterification re-charged with explicit amounts (mass balance is unaffected)."""
    return _ester().model_copy(update={"inputs": list(inputs)})


def test_the_notes_scale_is_the_reactant_charge_not_the_flask() -> None:
    """A 5 g run and a 2 kg run were indistinguishable without reading the prose.

    The solvent here outweighs the reactants nine to one, so any implementation that sums the
    whole charge reports ~100 g for a 10.6 g run.
    """
    note = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600),
            Component(smiles="CC(=O)O", role=Role.REACTANT, mass_mg=6000),
            Component(smiles="Cc1ccccc1", role=Role.SOLVENT, mass_mg=90000),
        )
    )
    assert "- scale: 10.6 g of reactants charged\n" in note.body


def test_scale_falls_back_to_millimoles_when_no_mass_was_recorded() -> None:
    """An ELN records mass or moles, not always both; the note reports whichever it has."""
    note = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, amount_mmol=100),
            Component(smiles="CC(=O)O", role=Role.REACTANT, amount_mmol=120),
        )
    )
    assert "- scale: 220 mmol of reactants charged\n" in note.body


def test_a_mixed_unit_record_reports_both_charges_rather_than_dropping_one() -> None:
    """A record charging one reactant by mass and another by moles must report both.

    `Component` allows mass and moles independently, so "an ELN records one or the other" is true
    of records and not of the schema — and preferring mass whenever *any* reactant had one dropped
    the rest.

    Here the 120 mmol of acetic acid is ~7.2 g, so the old rule reported "4.6 g" for an 11.8 g
    charge: a 2.5x under-report of the single number this bullet exists to make legible, in the
    direction that makes a pilot batch read as a bench run.
    """
    note = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600),
            Component(smiles="CC(=O)O", role=Role.REACTANT, amount_mmol=120),
        )
    )
    assert "- scale: 4.6 g + 120 mmol of reactants charged\n" in note.body


def test_a_reactant_recording_both_units_is_counted_once() -> None:
    """Mass is the preferred form, so a species carrying both must not also swell the mmol half."""
    note = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600, amount_mmol=100),
            Component(smiles="CC(=O)O", role=Role.REACTANT, amount_mmol=120),
        )
    )
    assert "- scale: 4.6 g + 120 mmol of reactants charged\n" in note.body


def test_the_charge_sheet_lists_every_input_with_what_was_recorded() -> None:
    """The machine-legible form behind the one-line scale: who carried the mass, per species."""
    note = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600, amount_mmol=100),
            Component(smiles="Cc1ccccc1", role=Role.SOLVENT),
        )
    )
    assert "- `CCO` (reactant): 4600 mg, 100 mmol\n" in note.body
    assert "- `Cc1ccccc1` (solvent): amount not recorded\n" in note.body


def test_a_record_with_no_amounts_says_nothing_about_scale() -> None:
    """Silence, not a fabricated zero: nothing was charged *on the record*, so nothing is said."""
    body = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT),
            Component(smiles="CC(=O)O", role=Role.REACTANT),
        )
    ).body
    assert "scale:" not in body
    assert "## Charge" not in body


def test_scale_survives_the_retrieval_excerpt_of_a_procedure_heavy_note() -> None:
    """Why scale leads the conditions: an excerpt is a blind character prefix of the body.

    `retrieval.retrievers._excerpt` truncates at `note_excerpt_chars`, so a figure appended after
    the procedure is invisible to exactly the notes that carry the most detail — the ones a
    process chemist most needs to place on a scale.
    """
    steps = [
        ReactionStep(
            index=i,
            kind=StepKind.STIR,
            text=f"Step {i}: age the batch and monitor conversion by HPLC until complete.",
        )
        for i in range(1, 13)
    ]
    note = note_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600),
            Component(smiles="CC(=O)O", role=Role.REACTANT, mass_mg=6000),
        ).model_copy(update={"steps": steps})
    )
    excerpt = note.body[: settings.note_excerpt_chars]
    assert "scale: 10.6 g of reactants charged" in excerpt
    # The tail of the same note is past the cut — which is where a scale figure appended after
    # the procedure would have sat, invisible to every hit on a detailed run.
    assert "Step 12" in note.body and "Step 12" not in excerpt
