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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import (
    DatedIngest,
    ElnMappingError,
    RawEntry,
    Retraction,
    RetractionReport,
    fetch_retractions,
    fetch_was_truncated,
    parse_iso_utc,
)
from chemclaw.ingest.eln.ingest import IngestError, ingest_reaction
from chemclaw.ingest.eln.json_adapter import ElnFormatError, JsonExportAdapter
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
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import (
    InMemoryReactionRecordStore,
    PostgresReactionRecordStore,
    ReactionRecord,
)
from chemclaw.ingest.eln.sync import IngestSummary, sync_entries
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.kg.note import cited_ids, cited_links, note_id_for_reaction
from chemclaw.science.fingerprints.molfp.search import find_similar_molecules
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.pg import migrated_db_or_skip

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _labels() -> InMemoryLabelIndex:
    """A throwaway label index for a test that only cares that the record phase is written.

    Named rather than inlined because every ingest call site needs one, and a test that had to
    construct it positionally would drift from the production signature the first time an
    argument moves.
    """
    return InMemoryLabelIndex()


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


def test_prose_conditions_stay_on_the_step_that_states_them() -> None:
    """A number read out of prose is the *step's*, and it is not promoted to the run's setpoint.

    The transcription tier is ungated on the recorded grounds that it infers nothing
    (`D-2026-08-25`), and this is the line where that stopped being true: the headline conditions
    fell back to the **first** regex match in the whole procedure, so a run charged at 65 °C over
    2.5 h and then held at 140 °C for 18 h was stored — in the typed columns a chemist compares
    runs on, with nothing marking it as derived — as a reaction run at 65 °C for 2.5 h. The prose
    is preserved verbatim either way, and each segment keeps the numbers *it* states, which is the
    scope those numbers actually have.
    """
    raw = RawEntry(
        entry_id="e1",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO", "role": "reactant"}],
            "products": [{"smiles": "CCO", "yield_percent": 50}],
            "procedure": "1. Warmed to 65 °C over 2.5 h. 2. Held at 140 °C for 18 h.",
            "operator": "chemist-c",
        },
    )
    reaction = JsonExportAdapter().map_to_ord(raw)
    assert reaction.temperature_c is None, "the entry states no reaction temperature"
    assert reaction.time_h is None, "the entry states no reaction time"
    assert [(step.temperature_c, step.duration_h) for step in reaction.steps] == [
        (65.0, 2.5),
        (140.0, 18.0),
    ]
    assert reaction.procedure_text is not None and "140 °C" in reaction.procedure_text
    # And the record a chemist queries says nothing about conditions the entry never recorded.
    conditions = record_from_ord_reaction(reaction).conditions
    assert conditions is not None and conditions.temperature_c is None
    assert conditions.time_h is None
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


def _prose_temperature(procedure: str) -> float | None:
    """The temperature the prose states, read where it is kept: on the step that says it.

    Read through the adapter rather than off the pattern, because what is being pinned is what a
    reader of the record sees. Since `D-2026-08-26-a-transcription-may-not-infer-a-setpoint` that
    is the step's own value — the run's `temperature_c` stays absent unless the entry recorded one.
    """
    steps = JsonExportAdapter().map_to_ord(_prose_entry(procedure)).steps
    return steps[0].temperature_c if steps else None


def test_temperature_range_extracts_upper_bound_not_negative() -> None:
    """A range like "60-80 °C" yields 80 (the documented upper-bound reading), never -80."""
    assert _prose_temperature("heated at 60-80 °C overnight") == 80.0


def test_genuine_negative_temperature_still_extracted() -> None:
    """A real minus sign ("-10 °C") and a bare "0 °C" both still extract from prose."""
    assert _prose_temperature("cooled to -10 °C") == -10.0
    assert _prose_temperature("stirred at 0 °C") == 0.0


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


def test_record_from_ord_reaction() -> None:
    """A reaction becomes a transcription record with SMILES + conditions and no forged link."""
    record = record_from_ord_reaction(_ester())
    assert record.reaction_id == "rxn-1"
    assert record.source.startswith("eln:")
    assert "CCO.CC(=O)O>>CCOC(C)=O" in record.body
    assert "temperature: 80.0 °C" in record.body
    assert cited_ids(record.body) == []


def test_ingest_indexes_and_records() -> None:
    """A valid reaction is indexed (reaction + compounds) and stored as a queryable record."""

    async def _run() -> None:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        record = await ingest_reaction(
            _ester(), rxn, mol, rec, label_index=_labels(), source="test-eln"
        )
        assert record.reaction_id == "rxn-1"
        assert len(await rxn.all_records()) == 1  # the reaction fingerprint
        assert len(await mol.all_records()) == 3  # ethanol, acetic acid, ethyl acetate
        assert (await rec.read("rxn-1")) is not None  # readable at once, with no PR to merge

    asyncio.run(_run())


def test_ingest_rejects_invalid_without_side_effects() -> None:
    """An invalid reaction raises and writes nothing to the index or the corpus (G4)."""

    async def _run() -> None:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        bad = _ester().model_copy(
            update={"outcomes": [Component(smiles="CCCl", role=Role.PRODUCT)]}
        )
        with pytest.raises(IngestError, match="mass balance"):
            await ingest_reaction(bad, rxn, mol, rec, label_index=_labels(), source="test-eln")
        assert await rxn.all_records() == []
        assert await mol.all_records() == []
        assert await rec.all_records() == []

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

        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _Adapter(), rxn, mol, rec, _EPOCH, label_index=_labels(), source="test-eln"
        )

        assert summary.ingested == ["good"]  # the good entry survives both bad ones
        assert {r.entry_id for r in summary.rejected} == {"bad-balance", "unmappable"}
        reasons = {r.entry_id: r.reason for r in summary.rejected}
        assert "mass balance" in reasons["bad-balance"]
        assert "cannot map" in reasons["unmappable"]
        assert summary.next_cursor == datetime(2026, 3, 1, tzinfo=UTC)  # newest seen
        assert len(await rec.all_records()) == 1  # only the good entry became a record

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await sync_entries(
            _Adapter(), rxn, mol, rec, _EPOCH, label_index=_labels(), source="test-eln"
        )

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

        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _Adapter(), rxn, mol, rec, _EPOCH, label_index=_labels(), source="test-eln"
        )

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _ListAdapter([bad_id, good]),
            rxn,
            mol,
            rec,
            _EPOCH,
            label_index=_labels(),
            source="test-eln",
        )

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _ListAdapter([future, good]),
            rxn,
            mol,
            rec,
            _EPOCH,
            label_index=_labels(),
            source="test-eln",
        )

        assert summary.ingested == ["good"]
        assert [r.entry_id for r in summary.rejected] == ["future"]
        assert "future" in summary.rejected[0].reason
        assert summary.next_cursor == datetime(2026, 1, 1, tzinfo=UTC)  # not 2062

    asyncio.run(_run())


def test_a_future_amendment_stamp_costs_the_cursor_and_not_the_entry() -> None:
    """A typo in an amendment date must not delete a real experiment from the corpus.

    The guard exists to keep an implausible timestamp out of the *stored cursor*, because nothing
    ever lowers one. It was moved onto `entry_window` — the value the cursor takes — and that
    silently gave it a second job: an entry created in 2026 and amended with a typo'd 2062 was
    rejected outright, and, because the fetch filters on the same watermark, re-fetched and
    re-rejected on every run, forever. Its `created_at` is perfectly sane and its chemistry is
    real. So the entry ingests and only the cursor refuses the value.

    The other half — a *creation* date beyond the wall clock, which means the record is not about
    anything that has happened — is still a rejection, and
    `test_future_dated_entry_is_rejected_and_does_not_poison_cursor` pins it.
    """

    async def _run() -> None:
        amended = _good_entry("amended", datetime(2026, 1, 1, tzinfo=UTC)).model_copy(
            update={"modified_at": datetime(2062, 7, 23, tzinfo=UTC)}
        )
        good = _good_entry("good", datetime(2026, 2, 1, tzinfo=UTC))
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _ListAdapter([amended, good]),
            rxn,
            mol,
            rec,
            _EPOCH,
            label_index=_labels(),
            source="test-eln",
        )

        assert summary.ingested == ["amended", "good"]
        assert summary.rejected == []
        # The cursor is what the batch's *plausible* entries reached. The amended one contributes
        # nothing to it — not even its own sane `created_at`, because the fetch filters on the
        # watermark and the simplest safe answer is to leave the cursor where the rest of the
        # batch put it. So the entry is fetched again next run, as the warning says.
        assert summary.next_cursor == datetime(2026, 2, 1, tzinfo=UTC)  # not 2062

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            adapter, rxn, mol, rec, cursor, label_index=_labels(), source="test-eln"
        )

        assert adapter.fetched_since == [cursor - timedelta(seconds=1800)]
        assert summary.ingested == ["late"]
        assert summary.skipped_existing == []  # its note is not merged yet, so it ingests
        # And it is flagged as awaiting merge, which is the honest report even on a first sync:
        # the entry sits inside the replay window with no merged note, so the *next* run fetches
        # and proposes it again. "Will come back until someone merges it" is what a single run can
        # establish; "was proposed before" is not (this entry never was).
        assert summary.next_cursor == cursor  # the cursor never moves backwards

    asyncio.run(_run())


# The registry source name these sync tests run under. Seeding a record under a *different* name
# than the sync passes would make every replay look new, which is the collision the row key now
# carries (`D-2026-08-26-a-transcription-is-keyed-by-its-source`) rather than a fixture detail.
_SEED_SOURCE = "test-eln"


async def _seed_record(store: InMemoryReactionRecordStore, entry: RawEntry) -> None:
    """Store the record for `entry` — exactly what an earlier sync run leaves behind.

    Rendered from the entry rather than stubbed, because the sync compares the stored *body* and
    not only its id: a stub would make "already ingested" and "unchanged" indistinguishable, which
    is what dropped every in-place ELN amendment before the body comparison existed.
    """
    await store.record(
        [record_from_ord_reaction(JsonExportAdapter().map_to_ord(entry))], _SEED_SOURCE
    )


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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await _seed_record(rec, late)
        summary = await sync_entries(
            _ListAdapter([late]), rxn, mol, rec, cursor, label_index=_labels(), source="test-eln"
        )

        assert summary.skipped_existing == ["late"]
        assert summary.ingested == []  # a replay skip is not a fresh ingest
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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await _seed_record(rec, new)
        summary = await sync_entries(
            _ListAdapter([new]), rxn, mol, rec, cursor, label_index=_labels(), source="test-eln"
        )

        assert summary.ingested == ["new"]
        assert summary.skipped_existing == []
        assert len(await rec.all_records()) == 1

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            adapter,
            rxn,
            mol,
            rec,
            cursor,
            apply_overlap=False,
            label_index=_labels(),
            source="test-eln",
        )

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _ListAdapter([replayed, fresh]),
            rxn,
            mol,
            rec,
            since,
            label_index=_labels(),
            source="test-eln",
        )
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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await sync_entries(
            _ListAdapter([forged]), rxn, mol, rec, _EPOCH, label_index=_labels(), source="test-eln"
        )

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
    assert record_from_ord_reaction(_ester()).compound_smiles == "CCOC(C)=O"


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
    assert record_from_ord_reaction(two_products).compound_smiles is None


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
    assert "C1CCOC1" in record_from_ord_reaction(reaction).body


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
        corrected = original.model_copy(
            update={
                "payload": {
                    **original.payload,
                    "products": [{"smiles": "CCOC(C)=O", "yield_percent": 31}],
                },
                "modified_at": cursor + timedelta(hours=1),
            }
        )
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await _seed_record(rec, original)
        summary = await sync_entries(
            _ListAdapter([corrected]),
            rxn,
            mol,
            rec,
            cursor,
            label_index=_labels(),
            source="test-eln",
        )

        assert summary.ingested == ["amended"]
        assert summary.skipped_existing == []
        # Not awaiting merge: a merged predecessor is proof the review queue moves, so this is new
        # content going in front of a human rather than the same claim going round again.
        stored = await rec.read("amended")
        assert stored is not None and "31" in stored.body

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _ListAdapter([bad]), rxn, mol, rec, cursor, label_index=_labels(), source="test-eln"
        )

        assert [entry.entry_id for entry in summary.rejected] == ["bad"]
        assert summary.ingested == []

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
        touched = entry.model_copy(update={"modified_at": cursor + timedelta(hours=1)})

        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await _seed_record(rec, entry)
        summary = await sync_entries(
            _ListAdapter([touched]), rxn, mol, rec, cursor, label_index=_labels(), source="test-eln"
        )

        assert summary.skipped_existing == ["touched"]
        assert summary.ingested == []
        assert await rxn.all_records() == []  # no fingerprint re-upserts either

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


# --- ORD malformed-shape robustness (Ingest-1) ----------------------------------------


def _ord_reaction_with(**overrides: object) -> dict[str, object]:
    """A minimal, otherwise-valid ORD reaction payload with the given top-level overrides."""
    payload = _ord_payload([{"type": "SMILES", "value": "CCO"}])
    payload.update(overrides)
    return payload


def test_ord_malformed_component_amount_is_treated_as_absent_not_crashed(tmp_path: Path) -> None:
    """A component whose `amount` is a list (not an object) never crashes the mapper.

    A real exporter can produce this shape error. `_amount` used to call `.get()` straight on
    the value, so this raised a bare `AttributeError` that escaped `map_to_ord`'s except clause
    and would have aborted the whole sync batch (Ingest-1). Mirroring the sibling helpers
    (`_measure`/`_temperature`), a malformed shape is treated the same as an absent one: the
    component still maps, just with no mass/mole data.
    """
    payload = _ord_reaction_with(
        inputs={
            "m1": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "reactionRole": "REACTANT",
                        "amount": [1, 2, 3],  # malformed: should be an Amount object
                    }
                ]
            }
        }
    )
    (tmp_path / "bad_amount.json").write_text(json.dumps(payload), encoding="utf-8")

    async def _run() -> OrdReaction:
        adapter = OrdJsonAdapter(str(tmp_path))
        entries = await adapter.fetch_new_entries(_EPOCH)
        return adapter.map_to_ord(entries[0])

    reaction = asyncio.run(_run())
    assert reaction.inputs[0].mass_mg is None
    assert reaction.inputs[0].amount_mmol is None


def test_ord_malformed_workup_input_is_treated_as_absent_not_crashed(tmp_path: Path) -> None:
    """A workup whose `input` is a list (not an object) never crashes the mapper.

    `_components` used to call `.get()` straight on the value, so a malformed workup `input`
    raised a bare `AttributeError` reached through `_workup_step` (Ingest-1). Mirroring the
    sibling helpers, the malformed shape now yields no components for that step rather than
    crashing.
    """
    payload = _ord_reaction_with(workups=[{"type": "FILTRATION", "input": [1, 2, 3]}])
    (tmp_path / "bad_workup.json").write_text(json.dumps(payload), encoding="utf-8")

    async def _run() -> OrdReaction:
        adapter = OrdJsonAdapter(str(tmp_path))
        entries = await adapter.fetch_new_entries(_EPOCH)
        return adapter.map_to_ord(entries[0])

    reaction = asyncio.run(_run())
    workup_steps = [s for s in reaction.steps if s.kind == StepKind.PURIFICATION]
    assert len(workup_steps) == 1
    assert workup_steps[0].components == []


class _OrdListAdapter:
    """A fake adapter serving fixed ORD `RawEntry`s through the real `OrdJsonAdapter` mapper."""

    def __init__(self, entries: list[RawEntry]) -> None:
        self.entries = entries

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        return [e for e in self.entries if e.created_at >= since]

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        return OrdJsonAdapter().map_to_ord(raw)


def test_ord_malformed_entry_does_not_abort_the_sync_batch() -> None:
    """The batch-level proof: a malformed nested field never aborts the whole sync run.

    Reproduces the sync-aborting shape (Ingest-1) end to end through `sync_entries`: without the
    `isinstance` guards in `_amount`/`_components`, the malformed entry's bare `AttributeError`
    would escape `sync_entries`'s `except (ChemclawError, ValidationError)` entirely — aborting
    the batch before the second entry is ever reached. With the guards, the malformed field maps
    to "absent" (matching the sibling helpers) and both entries ingest in order.
    """
    malformed_payload = _ord_reaction_with(
        reactionId="malformed-amount",
        inputs={
            "m1": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": "CCO"}],
                        "reactionRole": "REACTANT",
                        "amount": [1, 2, 3],
                    }
                ]
            }
        },
    )
    good_payload = _ord_payload([{"type": "SMILES", "value": "CCO"}])
    good_payload["reactionId"] = "good"

    async def _run() -> None:
        malformed = RawEntry(
            entry_id="malformed-amount",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload=malformed_payload,
        )
        good = RawEntry(
            entry_id="good", created_at=datetime(2026, 2, 1, tzinfo=UTC), payload=good_payload
        )
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        summary = await sync_entries(
            _OrdListAdapter([malformed, good]),
            rxn,
            mol,
            rec,
            _EPOCH,
            label_index=_labels(),
            source="test-eln",
        )

        # Both land: the malformed field never poisoned the batch, and the second entry
        # (which the un-guarded AttributeError would never have let the run reach) ingests too.
        assert summary.ingested == ["malformed-amount", "good"]
        assert summary.rejected == []

    asyncio.run(_run())


def test_a_search_hit_id_is_the_note_id_the_ingest_wrote() -> None:
    """The round trip a chemist takes: a `similar_reactions` hit handed to `expand_note`.

    `connectors.rxnfp.similar_reactions` used to return the fingerprint index's own key while the
    ELN ingest stored the note under a `reaction-` prefix, so a hit could not be opened and the
    chemist was told the procedure was not in the graph — with the note on disk. Asserted as an
    equality between the two ends rather than against a literal, so the test still holds if the
    prefix ever changes and fails if only one end changes.

    The record is keyed on the bare ELN id and the citation carries the prefix, so the round trip
    is now that `expand_note` strips exactly what `note_id_for_reaction` adds.
    """
    reaction = _ester()
    cited = note_id_for_reaction(record_from_ord_reaction(reaction).reaction_id)
    assert cited.removeprefix("reaction-") == reaction.reaction_id


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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        # Diethyl ether — an ether by-product of the esterification, charged nowhere in the record.
        await ingest_reaction(
            _with_impurities(Impurity(name="ether", smiles="CCOCC")),
            rxn,
            mol,
            rec,
            label_index=_labels(),
            source="test-eln",
        )
        hits = (await find_similar_molecules(mol, "CCOCC", threshold=0.99)).hits
        assert [hit.smiles for hit in hits] == ["CCOCC"]

    asyncio.run(_run())


def test_an_impurity_with_no_structure_is_skipped_not_fatal() -> None:
    """An ELN routinely records only a chromatographic name; that is not an error (KNW-2)."""

    async def _run() -> None:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        await ingest_reaction(
            _with_impurities(Impurity(name="RRT 0.82")),
            rxn,
            mol,
            rec,
            label_index=_labels(),
            source="test-eln",
        )
        # The three reaction compounds, and nothing minted from a nameless chromatographic peak.
        assert len(await mol.all_records()) == 3
        assert await rec.all_records()  # the run was still recorded

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
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        bad = Impurity(name="garbled", smiles="C1CC")
        with caplog.at_level(logging.WARNING, logger="chemclaw.ingest.eln.ingest"):
            await ingest_reaction(
                _with_impurities(bad), rxn, mol, rec, label_index=_labels(), source="test-eln"
            )
        assert len(await mol.all_records()) == 3
        assert await rec.all_records()
        assert "unparseable impurity SMILES" in caplog.text

    asyncio.run(_run())


# --- the note carries its project, and its scale --------------------------------------


def test_a_reaction_record_is_reachable_by_its_project_tag() -> None:
    """`gather_evidence(tag=…)` is documented as the project filter and was inert on reactions.

    Proven through the store's own eligibility gate rather than by reading a field: the project is
    worth recording only because a filtered sweep reaches the record, and a wrong tag must still
    exclude it.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        record = record_from_ord_reaction(_ester().model_copy(update={"project": "prj-alpha"}))
        await store.record([record], "eln-json")
        assert await store.eligible(["rxn-1"], {"tag": "prj-alpha"}) == {"rxn-1"}
        assert await store.eligible(["rxn-1"], {"tag": "prj-beta"}) == set()

    asyncio.run(_run())


def test_a_reaction_with_no_project_invents_no_tag() -> None:
    """A record without a project gets no project — never a placeholder a filter would match."""

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        await store.record([record_from_ord_reaction(_ester())], "eln-json")
        stored = await store.read("rxn-1")
        assert stored is not None and stored.project is None
        # No project means no tag can match it — not that every tag matches.
        assert await store.eligible(["rxn-1"], {"tag": "prj-alpha"}) == set()

    asyncio.run(_run())


def _charged(*inputs: Component) -> OrdReaction:
    """The esterification re-charged with explicit amounts (mass balance is unaffected)."""
    return _ester().model_copy(update={"inputs": list(inputs)})


def test_the_notes_scale_is_the_reactant_charge_not_the_flask() -> None:
    """A 5 g run and a 2 kg run were indistinguishable without reading the prose.

    The solvent here outweighs the reactants nine to one, so any implementation that sums the
    whole charge reports ~100 g for a 10.6 g run.
    """
    note = record_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600),
            Component(smiles="CC(=O)O", role=Role.REACTANT, mass_mg=6000),
            Component(smiles="Cc1ccccc1", role=Role.SOLVENT, mass_mg=90000),
        )
    )
    assert "- scale: 10.6 g of reactants charged\n" in note.body


def test_scale_falls_back_to_millimoles_when_no_mass_was_recorded() -> None:
    """An ELN records mass or moles, not always both; the note reports whichever it has."""
    note = record_from_ord_reaction(
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
    note = record_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600),
            Component(smiles="CC(=O)O", role=Role.REACTANT, amount_mmol=120),
        )
    )
    assert "- scale: 4.6 g + 120 mmol of reactants charged\n" in note.body


def test_a_reactant_recording_both_units_is_counted_once() -> None:
    """Mass is the preferred form, so a species carrying both must not also swell the mmol half."""
    note = record_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600, amount_mmol=100),
            Component(smiles="CC(=O)O", role=Role.REACTANT, amount_mmol=120),
        )
    )
    assert "- scale: 4.6 g + 120 mmol of reactants charged\n" in note.body


def test_the_charge_sheet_lists_every_input_with_what_was_recorded() -> None:
    """The machine-legible form behind the one-line scale: who carried the mass, per species."""
    note = record_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=4600, amount_mmol=100),
            Component(smiles="Cc1ccccc1", role=Role.SOLVENT),
        )
    )
    assert "- `CCO` (reactant): 4600 mg, 100 mmol\n" in note.body
    assert "- `Cc1ccccc1` (solvent): amount not recorded\n" in note.body


def test_a_record_with_no_amounts_says_nothing_about_scale() -> None:
    """Silence, not a fabricated zero: nothing was charged *on the record*, so nothing is said."""
    body = record_from_ord_reaction(
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
    note = record_from_ord_reaction(
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


def test_one_non_utf8_ord_export_does_not_abort_the_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file the codec cannot read is skipped, like every other unreadable export.

    The sibling of `test_ord_malformed_entry_does_not_abort_the_sync_batch`, one layer lower and
    with a cause the enumerated `except` genuinely did not cover. `UnicodeDecodeError` derives from
    `ValueError`, so it is a *sibling* of `json.JSONDecodeError` rather than a child, and it is not
    an `OSError` — the file opens and reads fine; the bytes are simply not UTF-8. One export written
    by a tool that emitted latin-1 therefore escaped the handler and aborted the whole fetch,
    contradicting the method's own skip-and-continue contract.

    The ordering is what makes this a real check: the bad file sorts *first*, so under the defect
    the good one is never reached at all and the assertion below fails on an empty list rather than
    on a warning that did not appear.
    """
    (tmp_path / "a-bad.json").write_bytes(
        '{"reaction_id": "bad", "notes": {"procedure_details": "caf\xe9"}}'.encode("latin-1")
    )
    (tmp_path / "b-good.json").write_text(
        json.dumps(
            {
                "reaction_id": "ord-good",
                "provenance": {"record_created": {"time": {"value": "2026-06-01T00:00:00Z"}}},
                "inputs": {},
                "outcomes": [],
            }
        ),
        encoding="utf-8",
    )

    async def _run() -> None:
        with caplog.at_level(logging.WARNING):
            entries = await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(
                datetime(2026, 1, 1, tzinfo=UTC)
            )
        assert [entry.entry_id for entry in entries] == ["ord-good"], (
            "the unreadable export must cost itself and nothing else"
        )
        assert any("a-bad.json" in record.getMessage() for record in caplog.records), (
            "a skipped export is skipped loudly — silence here is the same loss with no record"
        )

    asyncio.run(_run())


def test_eln_free_text_cannot_forge_a_knowledge_graph_relation() -> None:
    """A chemist's prose reaches the note body verbatim, so it must not be able to spell a link.

    `kg.note` parses a body for `[[rel:id]]`, and `contradicts`/`supersedes` are in the allowed
    vocabulary — so a free-text field could forge a real edge. The transcription is no longer a
    note, which makes this *more* important rather than less: the body is served verbatim by
    `expand_note` and quoted into report drafts, and nothing reviews it on the way. Every free-text
    field is checked here rather than one, because the escape is applied to the assembled body and
    each field is a way in.
    """
    reaction = _ester()
    reaction.hypothesis = "this run [[contradicts:reaction-1234]] the earlier one"
    reaction.failure_reason = "[[supersedes:reaction-9]]"
    reaction.outcome_class = OutcomeClass.FAILURE
    reaction.steps = [
        ReactionStep(index=1, kind=StepKind.ADDITION, text="charge [[supersedes:reaction-7]]")
    ]
    reaction.attributes = {"[[contradicts:reaction-5]]": "v", "note": "[[contradicts:reaction-6]]"}

    record = record_from_ord_reaction(reaction)

    assert cited_links(record.body) == [], "the ELN must not be able to author a graph edge"
    # Neutralized, not deleted: a reader still sees what the chemist actually wrote.
    assert "contradicts:reaction-1234" in record.body


@pytest.mark.parametrize("brackets", [2, 3, 4, 5, 6])
def test_no_depth_of_opening_bracket_spells_a_relation(brackets: int) -> None:
    """`[[[` is the spelling that defeated the first version of this escape, so depth is a case.

    `str.replace("[[", "[ [")` consumes the first two brackets and leaves the third untouched, so
    `[[[x]]` came out as `[ [[x]]` — a *new* valid delimiter, manufactured by the neutralizer
    itself, and the edge was forged anyway. The test that shipped with that fix asserted only the
    two-bracket spelling, so the suite was green while the control did not work. Parametrized
    rather than fixed at three, because the property is "no depth works", not "three does not".
    """
    reaction = _ester()
    reaction.hypothesis = "[" * brackets + "contradicts:reaction-1234]]"

    record = record_from_ord_reaction(reaction)

    assert not cited_links(record.body), f"{brackets} opening brackets forged an edge"


def test_mass_balance_catches_a_new_element_and_nothing_weaker() -> None:
    """The check's real reach, pinned in both directions so its docstring cannot overstate it.

    Element-set subsumption is a sound *necessary* condition and nothing more: it rejects a product
    introducing an element no input supplies, and admits every fabrication assembled from elements
    already present. Asserting the misses deliberately — a test that only showed the catch would
    read as though mass balance validated the chemistry, which is what the docstring used to imply
    and what a reviewer must not be told.

    The backlog's own example for this gap (`benzene + methanol >> paracetamol`) is in the *caught*
    group: paracetamol has nitrogen and neither input supplies it. Swapping benzene for aniline is
    what actually gets through, which is why the example is here rather than in the row.
    """

    def rx(inputs: list[str], outcomes: list[str]) -> OrdReaction:
        return OrdReaction(
            reaction_id="t",
            inputs=[Component(smiles=s, role=Role.REACTANT) for s in inputs],
            outcomes=[Component(smiles=s, role=Role.PRODUCT) for s in outcomes],
            provenance="p",
        )

    paracetamol = "CC(=O)Nc1ccc(O)cc1"
    caught = validate_ord(rx(["c1ccccc1", "CO"], [paracetamol]))
    assert caught == ["mass balance: products contain N but no input supplies it"]

    # Every one of these is chemically fabricated and every one validates.
    assert validate_ord(rx(["Nc1ccccc1", "CO"], [paracetamol])) == []
    assert validate_ord(rx(["C"], ["CCCCCCCCCCCCCCCCCCCC"])) == []


# Every dash that stands in for a minus sign in a real procedure. ACS and RSC typeset cryogenic
# temperatures with U+2212; Word's autocorrect produces U+2013 from a typed hyphen; the rest turn up
# in text pasted between systems. Only U+002D used to be read as a sign.
_MINUS_DASHES = [
    pytest.param("-", id="ascii-hyphen-minus"),
    pytest.param("−", id="minus-sign"),
    pytest.param("–", id="en-dash"),
    pytest.param("—", id="em-dash"),
    pytest.param("‐", id="hyphen"),
    pytest.param("‑", id="non-breaking-hyphen"),
    pytest.param("‒", id="figure-dash"),
    pytest.param("―", id="horizontal-bar"),
]


@pytest.mark.parametrize("dash", _MINUS_DASHES)
def test_a_typographic_minus_before_a_temperature_is_still_a_minus(dash: str) -> None:
    """`−78 °C` must not be ingested as `+78 °C`.

    A dry-ice/acetone lithiation is one of the most common cryogenic conditions in synthesis, and
    the sign used to be `-?` — U+002D alone. Seven of these eight characters were therefore not
    consumed at all and the number was read bare, so `−78 °C` became `78.0`: a 156-degree error in
    the wrong direction, rendered into the proposed note as `temperature: 78.0 °C`, and entirely
    plausible to the reviewer at the PR-gate because the verbatim prose beside it still reads `−78`.
    """
    raw = RawEntry(
        entry_id="e-cryo",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO", "role": "reactant"}],
            "products": [{"smiles": "CCO", "yield_percent": 50}],
            "procedure": f"Cooled to {dash}78 °C, then n-BuLi was added dropwise.",
            "operator": "chemist-c",
        },
    )

    assert JsonExportAdapter().map_to_ord(raw).steps[0].temperature_c == -78.0


@pytest.mark.parametrize("dash", _MINUS_DASHES)
def test_a_dash_between_two_numbers_is_a_range_and_not_a_sign(dash: str) -> None:
    """The control the lookbehind exists for: `60–80 °C` is the upper bound, never `-80`."""
    raw = RawEntry(
        entry_id="e-range",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO", "role": "reactant"}],
            "products": [{"smiles": "CCO", "yield_percent": 50}],
            "procedure": f"Heated to 60{dash}80 °C over 2 h.",
            "operator": "chemist-c",
        },
    )

    assert JsonExportAdapter().map_to_ord(raw).steps[0].temperature_c == 80.0


def test_a_typographic_minus_survives_step_segmentation_too() -> None:
    """`_segment_steps` runs the regex unconditionally, so every step of every entry was hit."""
    from chemclaw.ingest.eln.json_adapter import _segment_steps

    steps = _segment_steps("Cool the solution to −78 °C. Add n-BuLi dropwise. Warm to 20 °C.")

    assert [step.temperature_c for step in steps] == [-78.0, None, 20.0]


def test_ingesting_a_reaction_writes_the_label_index_record_phase() -> None:
    """The half of the label row that cannot be reconstructed later is written at ingest.

    Two things are asserted rather than one, and the second is the point: the row carries the
    **record** form (`reactants>agents>products`, agents kept), not the fingerprint form. The
    fingerprint deliberately drops solvent and catalyst — it has to, or a solvent swap dominates
    DRFP similarity — and an index built from it could never answer "which solvent", which is
    half of what the precedent questions ask.
    """

    async def _run() -> None:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        labels = InMemoryLabelIndex()
        reaction = _ester()
        await ingest_reaction(reaction, rxn, mol, rec, label_index=labels, source="eln-json")

        [row] = await labels.stale("any-version", limit=10)
        assert (row.source, row.reaction_id) == ("eln-json", reaction.reaction_id)
        assert row.record_smiles == reaction.reaction_smiles()
        assert row.citation == note_id_for_reaction(reaction.reaction_id)
        # Every component, with the role the record stated and nothing derived from it yet.
        assert [(s.ordinal, s.role) for s in row.species] == [
            (i, c.role.value) for i, c in enumerate(reaction.compounds())
        ]
        assert row.labeller_version is None

    asyncio.run(_run())


def test_the_label_row_keeps_the_agents_the_fingerprint_drops() -> None:
    """The measured difference the two-phase design exists for, asserted rather than argued.

    `reaction_fingerprints` stores `transformation_smiles()`; the label index stores
    `reaction_smiles()`. On a reaction with a solvent, those are not the same string, and only one
    of them can be asked which solvent was used.
    """

    async def _run() -> None:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        labels = InMemoryLabelIndex()
        solvent = Component(smiles="CC#N", role=Role.SOLVENT)
        reaction = _ester().model_copy(update={"inputs": [*_ester().inputs, solvent]})
        await ingest_reaction(reaction, rxn, mol, rec, label_index=labels, source="eln-json")

        [row] = await labels.stale("any-version", limit=10)
        assert "CC#N" in row.record_smiles
        assert "CC#N" not in reaction.transformation_smiles()
        assert "CC#N" in {s.smiles for s in row.species}

    asyncio.run(_run())


def test_the_validator_checks_the_sources_that_are_attached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`eln-validate` asks the registry what is enabled instead of naming two adapters.

    It constructed `JsonExportAdapter` and `OrdJsonAdapter` by name, which was right while they were
    the only two and became a gate looking somewhere other than where the data comes in the moment
    an ELN could be attached through a manifest (D-120). A site whose ELN arrives that way was
    outside the only check that maps and mass-balances entries before they land — and this printed
    `OK` regardless, which is the shape `CLAUDE.md` records as "a README is not a gate", in the one
    file whose whole job is being one (D-2026-08-26-silence-is-not-a-successful-run).

    Two properties, and the second is the one that bites: the failure is labelled with the *source
    name*, so an operator is sent to the manifest to fix rather than to a format; and an empty
    enabled set does not print `OK`.
    """
    from chemclaw.ingest.eln.validate import main

    export = tmp_path / "drop"
    export.mkdir()
    # A product carrying an element no input supplies: the mass-balance check's own case, so the
    # entry maps cleanly and is rejected on chemistry rather than on parsing.
    (export / "bad.json").write_text(
        json.dumps(
            {
                "id": "eln-bad",
                "timestamp": "2026-05-04T09:00:00Z",
                "reactants": [{"smiles": "CCO", "role": "reactant"}],
                "products": [{"smiles": "CCBr"}],
                "procedure": "Heat.",
            }
        ),
        encoding="utf-8",
    )
    manifests = tmp_path / "manifests"
    (manifests / "eln-under-test").mkdir(parents=True)
    (manifests / "eln-under-test" / "datasource.yaml").write_text(
        "name: eln-under-test\n"
        "description: The ELN this deployment actually attached.\n"
        "ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter\n"
        f"config:\n  export_dir: {export}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(manifests))
    monkeypatch.setattr(settings, "data_sources", "eln-under-test")

    assert main() == 1
    reported = capsys.readouterr().out
    assert "eln-under-test/eln-bad" in reported, "the manifest's name, not 'free-text'"
    assert "mass balance" in reported

    monkeypatch.setattr(settings, "data_sources", "")
    assert main() == 0, "a retrieve-only deployment is a configuration, not a failure"
    nothing = capsys.readouterr().out
    assert "not a pass" in nothing, "but it must never read as one"
    assert "OK" not in nothing


def test_the_validator_does_not_report_ok_over_a_source_that_yielded_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An OK line over zero entries is the empty-enabled-set defect, one level down.

    The problem counter was the only signal, so a source offering no entries produced zero problems
    and a success line whose own text is the tell nobody reads in CI. A directory that does not
    exist behaves identically — the adapter yields nothing rather than raising — so a typo'd
    `export_dir`, or an ORD export that was not mounted into the image, reported OK while the
    structure and mass-balance gate on everything entering the graph and the fingerprint index had
    silently stopped running.

    **Zero is not legitimate here, and that is the difference from an empty enabled set.** No
    sources enabled is a configuration a deployment chose and can be read straight off
    `CHEMCLAW_DATA_SOURCES`, so `main` states it and exits 0. A source that *is* attached and
    supplies nothing is a claim that failed, and nothing in the adapter can tell an empty ELN from
    a mis-mounted one — so it is reported and the gate fails.
    """
    from chemclaw.ingest.eln.validate import main

    manifests = tmp_path / "manifests"
    (manifests / "eln-empty").mkdir(parents=True)
    (manifests / "eln-empty" / "datasource.yaml").write_text(
        "name: eln-empty\n"
        "description: an ELN whose export directory was never mounted.\n"
        "ingest: chemclaw.ingest.eln.json_adapter:JsonExportAdapter\n"
        f"config:\n  export_dir: {tmp_path / 'never-mounted'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(manifests))
    monkeypatch.setattr(settings, "data_sources", "eln-empty")

    assert main() == 1
    printed = capsys.readouterr().out
    assert "OK:" not in printed, printed
    assert "eln-empty" in printed and "no entries" in printed, printed


# --- Retractions (D-2026-08-27-a-withdrawn-entry-is-a-fact-the-sync-must-carry) ----------------
#
# The property every test below exists to protect: a withdrawal is a fact the **source reports**,
# and an entry's absence from a delta fetch is never evidence of one.


class _RetractingAdapter(_ListAdapter):
    """A fake adapter whose source *can* report withdrawals — the optional capability."""

    def __init__(
        self,
        entries: list[RawEntry],
        retractions: list[Retraction] | None = None,
        *,
        complete: bool = True,
        fails: Exception | None = None,
    ) -> None:
        super().__init__(entries)
        self._retractions = retractions or []
        self._complete = complete
        self._fails = fails
        self.retraction_fetches: list[datetime] = []

    async def fetch_retractions(self, since: datetime) -> RetractionReport:
        self.retraction_fetches.append(since)
        if self._fails is not None:
            raise self._fails
        return RetractionReport(retractions=self._retractions, complete=self._complete)


async def _sync(
    adapter: object, store: InMemoryReactionRecordStore, since: datetime
) -> IngestSummary:
    """One sync pass against `store`, with throwaway fingerprint indexes."""
    return await sync_entries(
        adapter,  # type: ignore[arg-type]  # a structural fake, not an ElnAdapter subclass
        InMemoryFingerprintStore(),
        InMemoryFingerprintStore(),
        store,
        since,
        label_index=_labels(),
        source="test-eln",
    )


def test_a_reported_retraction_retires_the_run_and_is_counted() -> None:
    """A withdrawal the source reports leaves current evidence, and the pass says how many did."""

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        entry = _good_entry("withdrawn", datetime(2026, 1, 1, tzinfo=UTC))
        first = await _sync(_RetractingAdapter([entry]), store, _EPOCH)
        assert first.ingested == ["withdrawn"]
        assert first.retracted == 0 and first.retraction_refusal == ""

        retracted_at = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
        second = await _sync(
            _RetractingAdapter([], [Retraction(entry_id="withdrawn", retracted_at=retracted_at)]),
            store,
            first.next_cursor,
        )
        assert second.retracted == 1 and second.retraction_refusal == ""
        record = await store.read("withdrawn")
        assert record is not None and record.retracted_at == retracted_at

    asyncio.run(_run())


def test_a_retracted_run_stops_being_current_but_stays_readable_as_of_earlier() -> None:
    """The whole point of a tombstone over a delete: the row answers about the past, not the now.

    `read` still serves it — a `reaction-<id>` citation in a merged playbook must keep resolving,
    and "what did we think we knew, and when did we stop" is unanswerable once the row is gone —
    while `eligible`, which every current-evidence sweep resolves through, drops it.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        entry = _good_entry("withdrawn", datetime(2026, 1, 1, tzinfo=UTC))
        first = await _sync(_RetractingAdapter([entry]), store, _EPOCH)
        assert await store.eligible(["withdrawn"], {}) == {"withdrawn"}

        retracted_at = datetime(2026, 3, 4, tzinfo=UTC)
        await _sync(
            _RetractingAdapter([], [Retraction(entry_id="withdrawn", retracted_at=retracted_at)]),
            store,
            first.next_cursor,
        )
        record = await store.read("withdrawn")
        assert record is not None, "a retracted run is never deleted"
        assert record.body, "and it is still readable in full"
        assert not record.is_current(date(2026, 3, 4)), "not current on the day it was withdrawn"
        assert not record.is_current(date(2026, 6, 1))
        assert record.is_current(date(2026, 3, 3)), "still current as of the day before"
        assert await store.eligible(["withdrawn"], {}) == set()

    asyncio.run(_run())


def test_an_adapter_that_cannot_report_retractions_never_retires_anything() -> None:
    """**The delta trap, pinned.** Absence is not a withdrawal, and no run count makes it one.

    This is the test that stands between this repository and a future session "finishing" the
    retraction work by porting `prune_share`'s mark-and-sweep wholesale. That sweep is safe on the
    document share because a crawl is a *full enumeration*; an ELN fetch is a **delta**, so an
    entry ingested last month is absent from every subsequent fetch by design. Mark-and-sweep here
    does not merely risk retiring a valid corpus — it retires all of it, on the first pass, and the
    only visible symptom is that the corpus stopped answering.

    Five passes with an adapter that has no retraction capability at all, its entries vanishing
    from the export after the first: nothing is retired, ever, and the refusal names the reason.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        entries = [
            _good_entry("alpha", datetime(2026, 1, 1, tzinfo=UTC)),
            _good_entry("beta", datetime(2026, 1, 2, tzinfo=UTC)),
        ]
        summary = await _sync(_ListAdapter(entries), store, _EPOCH)
        assert sorted(summary.ingested) == ["alpha", "beta"]

        cursor = summary.next_cursor
        for _ in range(5):
            # The export is now empty — every entry has "disappeared" from the source's answer,
            # which is exactly what a cursor-based fetch looks like on any quiet day.
            summary = await _sync(_ListAdapter([]), store, cursor)
            cursor = summary.next_cursor
            assert summary.retracted == 0
            assert summary.retraction_refusal == "this adapter cannot report retractions"

        assert await store.eligible(["alpha", "beta"], {}) == {"alpha", "beta"}
        for reaction_id in ("alpha", "beta"):
            record = await store.read(reaction_id)
            assert record is not None and record.retracted_at is None

    asyncio.run(_run())


def test_a_partial_or_failed_retraction_report_retires_nothing() -> None:
    """`prune_share`'s other two refusals: half a report is not a report, and neither is an outage.

    Both name a withdrawal that is genuinely in the source. Acting on either would be acting on
    evidence the pass does not have — the report was cut short, or the source never answered — and
    the refusal has to be distinguishable from "nothing was withdrawn", which is what
    `retraction_refusal` beside `retracted=0` is for.
    """

    async def _run() -> None:
        real = Retraction(entry_id="alpha", retracted_at=datetime(2026, 3, 4, tzinfo=UTC))
        for adapter, expected in (
            (_RetractingAdapter([], [real], complete=False), "partial"),
            (_RetractingAdapter([], [real], fails=ElnMappingError("feed unreachable")), "asked"),
        ):
            store = InMemoryReactionRecordStore()
            first = await _sync(
                _ListAdapter([_good_entry("alpha", datetime(2026, 1, 1, tzinfo=UTC))]),
                store,
                _EPOCH,
            )
            summary = await _sync(adapter, store, first.next_cursor)
            assert summary.retracted == 0
            assert expected in summary.retraction_refusal
            record = await store.read("alpha")
            assert record is not None and record.retracted_at is None

        # And the honest empty answer is *not* a refusal: a source that reports withdrawals and
        # had none this window is the one case where `retracted=0` means the corpus is intact.
        store = InMemoryReactionRecordStore()
        quiet = await _sync(_RetractingAdapter([]), store, _EPOCH)
        assert quiet.retracted == 0 and quiet.retraction_refusal == ""

    asyncio.run(_run())


def test_a_retraction_is_idempotent_and_does_not_move_the_cursor() -> None:
    """Re-reporting a withdrawal costs nothing and never creeps its timestamp forward.

    A retraction deliberately does not advance the sync cursor — a future-stamped one would
    otherwise poison the fetch window the way a future-stamped *entry* does — so the same
    withdrawal is re-reported on every pass until the entry stream carries the cursor past it. The
    count must therefore mean "rows this pass retired", not "withdrawals I was told about", or an
    operator reads a steady trickle of retirements that are not happening.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        entry = _good_entry("alpha", datetime(2026, 1, 1, tzinfo=UTC))
        first = await _sync(_RetractingAdapter([entry]), store, _EPOCH)

        first_report = Retraction(entry_id="alpha", retracted_at=datetime(2026, 3, 4, tzinfo=UTC))
        later = Retraction(entry_id="alpha", retracted_at=datetime(2026, 5, 5, tzinfo=UTC))
        one = await _sync(_RetractingAdapter([], [first_report]), store, first.next_cursor)
        two = await _sync(_RetractingAdapter([], [later]), store, one.next_cursor)

        assert (one.retracted, two.retracted) == (1, 0), "the second pass retired nothing new"
        assert one.next_cursor == two.next_cursor == first.next_cursor
        record = await store.read("alpha")
        assert record is not None and record.retracted_at == first_report.retracted_at

    asyncio.run(_run())


def test_re_ingesting_a_soft_deleted_entry_does_not_resurrect_it() -> None:
    """An ELN that keeps exporting a withdrawn entry must not un-retire it on the next replay.

    This is the failure the upsert's omission of `retracted_at` exists to prevent, and it is not
    hypothetical: a soft-deleting source keeps the row in its export, so the overlap window
    re-fetches it every single run. An amended body must still overwrite the transcription — the
    correction is real — while the tombstone survives it.
    """

    async def _run() -> None:
        store = InMemoryReactionRecordStore()
        entry = _good_entry("alpha", datetime(2026, 1, 1, tzinfo=UTC))
        first = await _sync(_RetractingAdapter([entry]), store, _EPOCH)
        retraction = Retraction(entry_id="alpha", retracted_at=datetime(2026, 3, 4, tzinfo=UTC))
        await _sync(_RetractingAdapter([entry], [retraction]), store, first.next_cursor)

        amended = RawEntry(
            entry_id="alpha",
            created_at=entry.created_at,
            payload={
                "id": "alpha",
                "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
                "products": [{"smiles": "CCOC(C)=O"}],
                "procedure": "Yield corrected after assay.",
            },
        )
        await _sync(_RetractingAdapter([amended], [retraction]), store, first.next_cursor)

        record = await store.read("alpha")
        assert record is not None
        assert record.retracted_at == retraction.retracted_at, "the tombstone survived the replay"
        assert "corrected after assay" in record.body.lower(), "and the amendment landed"

    asyncio.run(_run())


def test_the_seam_wrapper_does_not_swallow_an_optional_capability() -> None:
    """`DatedIngest` must not narrow what the adapter it wraps can do — measured, it did.

    The registry hands the durable sync `DatedIngest(...)` for *every* source, and a
    `runtime_checkable` Protocol is structural, so a wrapper that does not redeclare a method
    simply does not have it. `fetch_was_truncated` therefore answered `False` in every deployment,
    including for the warehouse adapter that implements `fetch_truncated` precisely so the
    workflow comes back for the truncated remainder. Both optional capabilities are asserted here,
    because the point is the *rule* (read through `inner`), not either capability.
    """

    async def _run() -> None:
        retraction = Retraction(entry_id="alpha", retracted_at=datetime(2026, 3, 4, tzinfo=UTC))

        class _Bounded(_RetractingAdapter):
            def fetch_truncated(self) -> bool:
                return True

        wrapped = DatedIngest(_Bounded([], [retraction]))
        assert fetch_was_truncated(wrapped) is True
        report = await fetch_retractions(wrapped, _EPOCH)
        assert report is not None and report.retractions == [retraction]

        # And a wrapper around an adapter that cannot report still says "cannot say", not "none".
        assert await fetch_retractions(DatedIngest(_ListAdapter([])), _EPOCH) is None
        assert fetch_was_truncated(DatedIngest(_ListAdapter([]))) is False

    asyncio.run(_run())


def test_the_postgres_store_retires_a_run_exactly_as_the_in_memory_one_does() -> None:
    """The store half proven against a real database, or the sweep's tests prove only the fake.

    Both backends are handed the same two runs and the same report, including an id the corpus
    does not hold (a source may withdraw an entry this deployment never ingested) and a re-report
    of one already retracted. The counts, the tombstones and the eligibility narrowing — written
    twice, once as `ReactionRecord.passes` and once as SQL — must agree.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        durable = PostgresReactionRecordStore()
        memory = InMemoryReactionRecordStore()
        records = [
            ReactionRecord(reaction_id="rt-alpha", body="alpha", source="eln:test"),
            ReactionRecord(reaction_id="rt-beta", body="beta", source="eln:test"),
        ]
        at = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
        results = []
        for store in (durable, memory):
            await store.record(records, "rt-eln")
            first = await store.retract({"rt-alpha": at, "rt-absent": at}, "rt-eln")
            again = await store.retract({"rt-alpha": datetime(2026, 5, 5, tzinfo=UTC)}, "rt-eln")
            other_site = await store.retract({"rt-beta": at}, "rt-other-eln")
            alpha = await store.read("rt-alpha")
            assert alpha is not None
            results.append(
                (
                    first,
                    again,
                    other_site,
                    alpha.retracted_at,
                    await store.eligible(["rt-alpha", "rt-beta"], {}),
                )
            )

        assert results[0] == results[1], "the two backends must answer alike"
        assert results[0] == (1, 0, 0, at, {"rt-beta"})

    asyncio.run(_run())
