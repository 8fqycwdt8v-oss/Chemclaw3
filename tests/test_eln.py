"""Behavioral tests for ELN ingestion, all runnable without a server.

Covers the ORD schema, the RDKit + mass-balance validator, the JSON and ORD adapters, the
reaction-record mapping, and the ingest and sync flow into in-memory stores.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.ingest.eln.adapter import (
    DatedIngest,
    RawEntry,
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
    RecordTier,
    Role,
    StepKind,
)
from chemclaw.ingest.eln.ord_adapter import OrdFormatError, OrdJsonAdapter
from chemclaw.ingest.eln.record import record_from_ord_reaction
from chemclaw.ingest.eln.records import (
    AmbiguousReactionRecord,
    InMemoryReactionRecordStore,
    PostgresReactionRecordStore,
    ReactionRecord,
)
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.ingest.eln.validate import validate_ord
from chemclaw.kg.note import (
    ProcessConditions,
    cited_ids,
    cited_links,
    external_record_ref,
    note_id_for_reaction,
)
from chemclaw.retrieval.retrievers import FingerprintReactionRetriever
from chemclaw.science.fingerprints.molfp.search import find_similar_molecules
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.pg import migrated_db_or_skip

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _labels() -> InMemoryLabelIndex:
    """A throwaway label index for a test that only cares that the record phase is written.

    Named so every call site follows the production signature when an argument moves.
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
    """A number read out of prose stays on the step that states it, not on the run's setpoint.

    The transcription infers nothing: a run charged at 65 °C and then held at 140 °C must not be
    stored as a 65 °C run in the typed columns chemists compare on.
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

    Read through the adapter, since what is pinned is what a reader of the record sees; the run's
    `temperature_c` stays absent unless the entry recorded one.
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


async def test_fetch_only_returns_entries_after_cursor(tmp_path: Path) -> None:
    """fetch_new_entries returns only entries at or after `since`, oldest first."""
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


async def test_fetch_includes_entry_exactly_at_cursor(tmp_path: Path) -> None:
    """An entry stamped exactly at the cursor is fetched (inclusive boundary).

    A same-second entry exported after a sync run must not be skipped forever;
    re-ingesting a boundary entry is idempotent, so inclusivity is safe.
    """
    _write_entry(tmp_path / "a.json", "a", "2026-03-01T00:00:00Z")
    new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(datetime(2026, 3, 1, tzinfo=UTC))
    assert [e.entry_id for e in new] == ["a"]


async def test_fetch_skips_corrupt_json_file(tmp_path: Path) -> None:
    """One corrupt export file is skipped, not allowed to abort the whole fetch (G4)."""
    (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
    _write_entry(tmp_path / "good.json", "good", "2026-01-01T00:00:00Z")
    new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
    assert [e.entry_id for e in new] == ["good"]


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


def _filed_refusals(monkeypatch: pytest.MonkeyPatch, module: str) -> dict[str, dict[str, str]]:
    """Capture what an adapter files in the rejection ledger, without a database under it.

    A refused entry must be queryable, not just a log line. Patched at the adapter's own import site
    so the asserted call is the one that adapter makes.
    """
    filed: dict[str, dict[str, str]] = {}

    async def _spy(source: str, refusals: dict[str, str]) -> None:
        filed.setdefault(source, {}).update(refusals)

    monkeypatch.setattr(f"chemclaw.ingest.eln.{module}.record_refusals", _spy)
    return filed


async def test_one_non_utf8_json_export_does_not_abort_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One non-UTF-8 JSON export costs only itself, and is filed in the rejection ledger.

    `UnicodeDecodeError` is a `ValueError` but neither a `JSONDecodeError` nor an `OSError`, so it
    needs its own handling. The bad file sorts in the middle, so an abort would lose the first file
    too. The failure is permanent, which is why the ledger row matters.
    """
    filed = _filed_refusals(monkeypatch, "json_adapter")
    _write_entry(tmp_path / "a-good.json", "a", "2026-01-01T00:00:00Z")
    (tmp_path / "b-latin1.json").write_bytes(
        json.dumps(
            {"id": "b", "timestamp": "2026-01-01T00:00:00Z", "procedure": "heat to 60°C"},
            ensure_ascii=False,
        ).encode("latin-1")
    )
    _write_entry(tmp_path / "c-good.json", "c", "2026-01-01T00:00:00Z")

    entries = await JsonExportAdapter(str(tmp_path), name="eln-json").fetch_new_entries(_EPOCH)

    assert [entry.entry_id for entry in entries] == ["a", "c"], (
        "the unreadable export must cost itself and nothing else"
    )
    assert "b-latin1" in filed["eln-json"], (
        "a skipped export reaches the rejection ledger; a WARNING alone is not an answer a chemist "
        "can be given"
    )
    assert "utf-8" in filed["eln-json"]["b-latin1"]


async def test_two_json_exports_sharing_one_entry_id_are_both_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two JSON exports claiming one entry id are both refused.

    Records are keyed `(ingest_source, reaction_id)` and refreshed on conflict, so the second would
    silently replace the first. Keeping either is a coin flip; the ledger row names both files.
    """
    filed = _filed_refusals(monkeypatch, "json_adapter")
    for name in ("batch1_run7.json", "batch2_run7.json"):
        _write_entry(tmp_path / name, "EXP-88", "2026-01-01T00:00:00Z")
    _write_entry(tmp_path / "batch3_run8.json", "EXP-89", "2026-01-01T00:00:00Z")

    entries = await JsonExportAdapter(str(tmp_path), name="eln-json").fetch_new_entries(_EPOCH)

    assert [entry.entry_id for entry in entries] == ["EXP-89"], (
        "an id two files claim names no run; the unambiguous entry beside it is unaffected"
    )
    reason = filed["eln-json"]["EXP-88"]
    assert "batch1_run7.json" in reason and "batch2_run7.json" in reason


async def test_two_ord_exports_sharing_one_reaction_id_are_both_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ORD adapter reaches the same duplicate-id refusal.

    Not parametrised with the JSON case: the two adapters read a different id field out of a
    different shape, and each must reach the shared refusal.
    """
    filed = _filed_refusals(monkeypatch, "ord_adapter")
    for name in ("one.json", "two.json"):
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "reaction_id": "ord-7",
                    "provenance": {"record_created": {"time": {"value": "2026-06-01T00:00:00Z"}}},
                    "inputs": {},
                    "outcomes": [],
                }
            ),
            encoding="utf-8",
        )

    entries = await OrdJsonAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)

    assert entries == []
    assert "one.json" in filed["eln-ord"]["ord-7"]


@pytest.mark.parametrize(
    ("stated", "expected_id"),
    [
        (0, "0"),
        (12, "12"),
        ("", None),
        ("   ", None),
        (False, None),
    ],
)
async def test_a_falsy_stated_entry_id_is_not_silently_the_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stated: object, expected_id: str | None
) -> None:
    """A falsy stated entry id is not silently replaced by the file name.

    An integer `0` is an id; a blank string or a JSON boolean names nothing and is refused, rather
    than storing the record under an id the source never used.
    """
    filed = _filed_refusals(monkeypatch, "json_adapter")
    payload: dict[str, Any] = {
        "id": stated,
        "timestamp": "2026-01-01T00:00:00Z",
        "reactants": [{"smiles": "CCO"}],
        "products": [{"smiles": "CCO"}],
    }
    (tmp_path / "EXP_2026_0412.json").write_text(json.dumps(payload), encoding="utf-8")

    entries = await JsonExportAdapter(str(tmp_path), name="eln-json").fetch_new_entries(_EPOCH)

    assert [entry.entry_id for entry in entries] == ([expected_id] if expected_id else [])
    if expected_id is None:
        assert "names no entry" in filed["eln-json"]["EXP_2026_0412"]
    else:
        assert not filed.get("eln-json"), "a stated id is transcribed, not refused"


async def test_an_entry_with_no_id_field_at_all_is_still_named_by_its_file(tmp_path: Path) -> None:
    """An export with no `id` key at all is still named by its file.

    The documented fallback, pinned beside the refusal above so the two cannot merge.
    """
    (tmp_path / "EXP_2026_0412.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "reactants": [{"smiles": "CCO"}],
                "products": [{"smiles": "CCO"}],
            }
        ),
        encoding="utf-8",
    )
    entries = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
    assert [entry.entry_id for entry in entries] == ["EXP_2026_0412"]


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


def test_the_late_arrival_line_names_the_source_rather_than_the_format(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The late-arrival warning names the source rather than the format.

    A deployment with two drop directories must be able to tell which one has files that will never
    be ingested.
    """

    async def _run() -> None:
        entry = tmp_path / "late.json"
        _write_entry(entry, "late-1", "2026-01-01T00:00:00Z")
        _set_mtime(entry, datetime(2026, 6, 1, tzinfo=UTC))
        await JsonExportAdapter(str(tmp_path), name="eln-site-a").fetch_new_entries(
            datetime(2026, 3, 1, tzinfo=UTC)
        )

        ord_dir = tmp_path / "ord"
        ord_dir.mkdir()
        ord_file = ord_dir / "late-ord.json"
        ord_file.write_text(
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
        _set_mtime(ord_file, datetime(2026, 6, 1, tzinfo=UTC))
        await OrdJsonAdapter(str(ord_dir), name="ord-site-b").fetch_new_entries(
            datetime(2026, 3, 1, tzinfo=UTC)
        )

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())

    late = [
        record.getMessage() for record in caplog.records if "arrived after" in record.getMessage()
    ]
    assert len(late) == 2
    assert late[0].startswith("eln-site-a:"), late[0]
    assert late[1].startswith("ord-site-b:"), late[1]


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


async def test_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    """A timestamp without an offset is treated as UTC.

    A naive datetime would later raise TypeError when compared against the sync's
    offset-aware cursor.
    """
    _write_entry(tmp_path / "naive.json", "naive", "2026-01-01T00:00:00")  # no offset
    new = await JsonExportAdapter(str(tmp_path)).fetch_new_entries(_EPOCH)
    assert [e.entry_id for e in new] == ["naive"]
    assert new[0].created_at == datetime(2026, 1, 1, tzinfo=UTC)


# --- note + ingest + sync -------------------------------------------------------------


def test_record_from_ord_reaction() -> None:
    """A reaction becomes a transcription record with SMILES + conditions and no forged link."""
    record = record_from_ord_reaction(_ester())
    assert record.reaction_id == "rxn-1"
    assert record.source.startswith("eln:")
    assert "CCO.CC(=O)O>>CCOC(C)=O" in record.body
    assert "temperature: 80 °C" in record.body
    assert cited_ids(record.body) == []


async def test_ingest_indexes_and_records() -> None:
    """A valid reaction is indexed (reaction + compounds) and stored as a queryable record."""
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


async def test_ingest_rejects_invalid_without_side_effects() -> None:
    """An invalid reaction raises and writes nothing to the index or the corpus (G4)."""
    rxn, mol, rec = (
        InMemoryFingerprintStore(),
        InMemoryFingerprintStore(),
        InMemoryReactionRecordStore(),
    )
    bad = _ester().model_copy(update={"outcomes": [Component(smiles="CCCl", role=Role.PRODUCT)]})
    with pytest.raises(IngestError, match="mass balance"):
        await ingest_reaction(bad, rxn, mol, rec, label_index=_labels(), source="test-eln")
    assert await rxn.all_records() == []
    assert await mol.all_records() == []
    assert await rec.all_records() == []


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


async def test_sync_rejects_non_slug_entry_id_without_aborting_batch() -> None:
    """An entry id that is not a valid note slug is one rejection, never a batch abort.

    The pydantic `ValidationError` is not a `ChemclawError` and must still be caught per entry.
    """
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


async def test_a_nul_byte_in_free_text_is_one_rejection_not_a_half_written_batch() -> None:
    """Free text the corpus cannot store is refused per entry, before anything is written.

    Postgres refuses a NUL in `text` and `jsonb`, and `psycopg.DataError` at the last write would
    leave fingerprint and label rows committed, no ledger row, and the source's cursor stuck
    forever. Refusing at record construction makes it an ordinary rejection.
    """
    poisoned = RawEntry(
        entry_id="EXP-2",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={
            "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
            "products": [{"smiles": "CCOC(C)=O"}],
            "procedure": "Quenched with brine\x00 and dried over MgSO4.",
        },
    )
    good = _good_entry("EXP-3", datetime(2026, 2, 1, tzinfo=UTC))
    rxn, mol, rec = (
        InMemoryFingerprintStore(),
        InMemoryFingerprintStore(),
        InMemoryReactionRecordStore(),
    )
    summary = await sync_entries(
        _ListAdapter([poisoned, good]),
        rxn,
        mol,
        rec,
        _EPOCH,
        label_index=_labels(),
        source="test-eln",
    )

    assert summary.ingested == ["EXP-3"], "the entry after the poisoned one must still ingest"
    assert [r.entry_id for r in summary.rejected] == ["EXP-2"]
    assert "NUL" in summary.rejected[0].reason
    # Nothing of the refused entry reached any index: the refusal is at construction, so the
    # dangling half-write — findable by structure, not expandable to a record — cannot happen.
    assert [r.id for r in await rxn.all_records()] == ["EXP-3"]
    assert await rec.read("EXP-2") is None


def test_a_lone_surrogate_in_free_text_is_refused_the_same_way() -> None:
    r"""A lone surrogate in free text is refused the same way.

    `json.loads` accepts a truncated `\\u` escape, but psycopg cannot encode the result.
    """
    with pytest.raises(ValidationError) as raised:
        record_from_ord_reaction(
            OrdReaction(
                reaction_id="EXP-4",
                inputs=[Component(smiles="CCO", role=Role.REACTANT)],
                outcomes=[Component(smiles="CC=O", role=Role.PRODUCT)],
                provenance="test",
                procedure_text="dried \ud800 overnight",
            )
        )
    assert "UTF-8" in str(raised.value)


def test_a_nul_in_a_condition_the_body_never_renders_is_refused_too() -> None:
    """A NUL in a condition the body never renders is refused too.

    The check is on the record, because `conditions` is a JSONB column of its own.
    """
    with pytest.raises(ValidationError) as raised:
        ReactionRecord(
            reaction_id="EXP-5",
            body="a body with nothing wrong in it",
            source="test-eln",
            conditions=ProcessConditions(major_impurity="des-bromo\x00 adduct"),
        )
    assert "conditions.major_impurity" in str(raised.value)


def test_a_non_finite_condition_is_refused_where_a_nul_is() -> None:
    """A non-finite condition is refused at model validation, where a NUL is.

    `NaN` and `±Infinity` are not JSON and Postgres refuses them with an error sync does not catch.
    Bounded fields reject NaN by accident of comparison; `temperature_c` (no bounds) and `time_h`
    (`ge=0` admits `+Infinity`) need the explicit check, and asserting all of them keeps the
    accidental guards from being removed silently.
    """
    for field in (
        "temperature_c",
        "time_h",
        "yield_percent",
        "purity_percent",
        "impurity_area_percent",
    ):
        for value in (float("nan"), float("inf"), float("-inf")):
            # `model_validate` rather than `**{...}`: the same validation, without asking a
            # static checker to prove a dynamic field name against five different field types.
            with pytest.raises(ValidationError):
                ProcessConditions.model_validate({field: value})

    assert ProcessConditions(temperature_c=-78.0).temperature_c == -78.0, (
        "a finite setpoint stopped being storable"
    )


def test_the_next_field_added_to_a_record_cannot_forget_the_storable_check() -> None:
    """A field added to the record cannot skip the storable check.

    The walk must cover lists of strings and models as well as scalars. A subclass adding a field
    exercises the record's own validator.
    """

    class _RecordWithLists(ReactionRecord):
        tags: list[str] = []
        extras: list[ProcessConditions] = []

    with pytest.raises(ValidationError) as in_a_list:
        _RecordWithLists(
            reaction_id="EXP-6", body="fine", source="test-eln", tags=["clean", "des-bromo\x00"]
        )
    assert "tags[1]" in str(in_a_list.value)

    with pytest.raises(ValidationError) as in_a_nested_model:
        _RecordWithLists(
            reaction_id="EXP-7",
            body="fine",
            source="test-eln",
            extras=[ProcessConditions(major_impurity="des-bromo\x00 adduct")],
        )
    assert "extras[0].major_impurity" in str(in_a_nested_model.value)


async def test_future_dated_entry_is_rejected_and_does_not_poison_cursor() -> None:
    """A typo'd future year is a visible rejection and never becomes the high-water cursor.

    If it advanced the cursor, every later real entry would be silently skipped forever
    (the persisted cursor is never lowered by any code path).
    """
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


async def test_a_future_amendment_stamp_costs_the_cursor_and_not_the_entry() -> None:
    """A future-dated amendment stamp costs the cursor, not the entry.

    The guard keeps an implausible timestamp out of the stored cursor, which is never lowered; the
    entry's own `created_at` is sane, so it ingests. A future creation date is still a rejection
    (see `test_future_dated_entry_is_rejected_and_does_not_poison_cursor`).
    """
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
    # The cursor is what the batch's plausible entries reached; the amended entry contributes
    # nothing, so it is fetched again next run, as the warning says.
    assert summary.next_cursor == datetime(2026, 2, 1, tzinfo=UTC)  # not 2062


async def test_sync_fetches_an_overlap_window_behind_the_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late-landing export file stamped just before the cursor is still ingested.

    The fetch reaches `since - eln_sync_overlap_seconds` (re-fetching is free — ingestion
    is idempotent), and the returned cursor never regresses below `since`.
    """
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
    # Flagged as awaiting merge: inside the replay window with no merged note, the next run fetches
    # it again.
    assert summary.next_cursor == cursor  # the cursor never moves backwards


# The registry source name these sync tests run under. Seeding a record under a *different* name
# than the sync passes would make every replay look new, which is the collision the row key now
# carries (`D-2026-08-26-a-transcription-is-keyed-by-its-source`) rather than a fixture detail.
_SEED_SOURCE = "test-eln"


async def _seed_record(store: InMemoryReactionRecordStore, entry: RawEntry) -> None:
    """Store the record for `entry`, exactly what an earlier sync run leaves behind.

    Rendered from the entry because the sync compares the stored body, not only its id.
    """
    await store.record(
        [record_from_ord_reaction(JsonExportAdapter().map_to_ord(entry))], _SEED_SOURCE
    )


async def test_sync_skips_overlap_entry_whose_note_already_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlap-window entry already stored with the same body is skipped, not re-ingested.

    Proof of full ingestion is a matching body, not just a matching id, so in-place amendments still
    land. An unchanged entry costs a lookup and is reported under `skipped_existing`.
    """
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


async def test_sync_still_ingests_new_entry_even_if_its_note_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged-note short-circuit applies only to the overlap replay, never past the cursor.

    An entry *after* `since` is deliberate work (e.g. a manual backfill re-run): it must
    take the full idempotent ingest path even when a note with its id already exists.
    """
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


async def test_sync_without_overlap_fetches_from_the_cursor_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apply_overlap=False` fetches from `since` (still inclusive), not the overlap floor.

    The chunk loop passes this after the first chunk, so a backlog drain replays the window once per
    run while still picking up the same-second boundary entry.
    """
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


def test_overlap_rerejection_logs_debug_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A replayed rejection (inside the overlap window) logs at DEBUG, a fresh one at WARNING.

    Re-warning hourly would bury new failures; both still appear in the summary.
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
    """A single-product reaction note carries `compound_smiles`.

    Everything that groups by compound, `kg.conflicts` included, starts from that field.
    """
    assert record_from_ord_reaction(_ester()).compound_smiles == "CCOC(C)=O"


def test_the_file_drop_adapters_stamp_a_date_they_read_off_the_entry_as_entry_dated() -> None:
    """Both file-drop adapters mark a date read off the entry's write time as entry-dated.

    Neither shipped fixture carries an experiment date, so the date comes from the record's creation
    time, and `Progression.entry_dated` reads the stamp to weaken the run-order caveat.
    `DatedIngest` supplies both in one `model_copy` at the one construction point readers resolve
    through.
    """
    written = datetime(2026, 3, 10, 9, tzinfo=UTC)
    json_raw = RawEntry(
        entry_id="eln-2026-001",
        created_at=written,
        payload={"reactants": [{"smiles": "CCO"}], "products": [{"smiles": "CC=O"}]},
    )
    assert JsonExportAdapter().map_to_ord(json_raw).performed_at is None, (
        "the adapter alone has no experiment date to state: the entry does not carry one"
    )
    dated = DatedIngest(JsonExportAdapter()).map_to_ord(json_raw)
    assert dated.performed_at == date(2026, 3, 10) and dated.date_source == "entry"

    ord_raw = RawEntry(
        entry_id="ord-2026-001",
        created_at=written,
        payload={
            "inputs": {
                "a": {"components": [{"identifiers": [{"type": "SMILES", "value": "CCO"}]}]}
            },
            "outcomes": [{"products": [{"identifiers": [{"type": "SMILES", "value": "CC=O"}]}]}],
        },
    )
    assert OrdJsonAdapter().map_to_ord(ord_raw).performed_at is None
    ord_dated = DatedIngest(OrdJsonAdapter()).map_to_ord(ord_raw)
    assert ord_dated.performed_at == date(2026, 3, 10) and ord_dated.date_source == "entry"


def test_a_run_whose_only_recorded_conditions_are_zero_keeps_them() -> None:
    """A run whose only recorded conditions are zero keeps them.

    Absent means "not recorded", never zero: a 0 °C bath and a 0% yield are real values that a
    truthiness test cannot tell from silence.
    """
    ice_bath = OrdReaction(
        reaction_id="EXP-cryo",
        inputs=[Component(smiles="CCO", role=Role.REACTANT)],
        outcomes=[Component(smiles="CC=O", role=Role.PRODUCT)],
        provenance="test",
        temperature_c=0.0,
    )
    conditions = record_from_ord_reaction(ice_bath).conditions
    assert conditions is not None, "a recorded 0 °C is a recorded setpoint, not an absent one"
    assert conditions.temperature_c == 0.0

    failed = ice_bath.model_copy(update={"temperature_c": None, "yield_percent": 0.0})
    failed_conditions = record_from_ord_reaction(failed).conditions
    assert failed_conditions is not None and failed_conditions.yield_percent == 0.0

    nothing = ice_bath.model_copy(update={"temperature_c": None})
    assert record_from_ord_reaction(nothing).conditions is None, (
        "a run that recorded none of them still gets no block: `conditions: {}` would claim the "
        "question was asked and answered emptily"
    )


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
    """The record form puts the solvent and catalyst in the agent slot.

    A notation claim only: DRFP folds the agent slot back onto the reactants, so it does not change
    similarity (`transformation_smiles` does; see `tests/test_rxnfp.py`). A reagent stays on the
    left, since it participates stoichiometrically.
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
    """The record form keeps the solvent; the fingerprint form drops it.

    Notes, campaign step lists and playbooks render the record form, and the solvent is a headline
    condition, so the exclusion is a second method rather than an edit to this one.
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


async def test_an_amended_entry_is_re_proposed_rather_than_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An amended entry is re-written rather than dropped as already ingested.

    An ELN amends entries in place (a corrected yield, a retraction) while keeping `created_at`, so
    "already seen" is not "unchanged".
    """
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


async def test_an_entry_that_fails_to_ingest_is_only_reported_as_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that fails to ingest is reported only as rejected, not also as awaiting merge.

    The replay flag is decided before ingestion and recorded after it, so one outcome is reported.
    """
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


async def test_an_unchanged_entry_reported_as_amended_still_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged entry stamped `modified` still costs nothing.

    The comparison is on content, so an exporter that touches every record does not cause a full
    re-submission on each sync.
    """
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


def test_an_amended_export_re_enters_the_fetch_window(tmp_path: Path) -> None:
    """An amended export re-enters the fetch window.

    An ELN amends an entry without touching `timestamp`, so `entry_window` filters on the later of
    the two.
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


def test_ord_compound_known_only_by_a_shorthand_is_carried_as_that_name(tmp_path: Path) -> None:
    """An ORD compound known only by a paper's shorthand is carried as that name, with no structure.

    The reaction is citation-only; inventing a structure for the name would be worse than none.
    """
    reaction = _map_ord(tmp_path, [{"type": "NAME", "value": "2a, Boronic Acid"}])
    assert reaction.inputs == []
    assert [c.name for c in reaction.unstructured] == ["2a, Boronic Acid"]
    assert reaction.tier is RecordTier.CITATION_ONLY


def test_ord_compound_with_no_identifier_at_all_is_still_refused(tmp_path: Path) -> None:
    """Neither a structure nor a name: nothing a record could show, so still a refusal."""
    with pytest.raises(OrdFormatError, match="no resolvable structure identifier"):
        _map_ord(tmp_path, [{"type": "INCHI", "value": "InChI=1S/garbage"}])


# --- ORD malformed-shape robustness (Ingest-1) ----------------------------------------


def _ord_reaction_with(**overrides: object) -> dict[str, object]:
    """A minimal, otherwise-valid ORD reaction payload with the given top-level overrides."""
    payload = _ord_payload([{"type": "SMILES", "value": "CCO"}])
    payload.update(overrides)
    return payload


def test_ord_malformed_component_amount_is_treated_as_absent_not_crashed(tmp_path: Path) -> None:
    """A component whose `amount` is a list (not an object) never crashes the mapper.

    A malformed shape is treated as absent, like the sibling helpers: the component maps with no
    mass or mole data rather than raising an `AttributeError` that aborts the batch.
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


def _ord_charge(*amounts: dict[str, object]) -> dict[str, object]:
    """An ORD reaction charging one reactant per `amounts` entry, each an `Amount` message."""
    return _ord_reaction_with(
        inputs={
            "m1": {
                "components": [
                    {
                        "identifiers": [{"type": "SMILES", "value": smiles}],
                        "reactionRole": "REACTANT",
                        "amount": amount,
                    }
                    for smiles, amount in zip(("Nc1ccccc1", "CC(=O)Cl"), amounts, strict=False)
                ]
            }
        }
    )


def test_an_ord_reactant_charged_by_volume_reaches_the_record_and_its_scale(
    tmp_path: Path,
) -> None:
    """An ORD reactant charged by volume reaches the record and its scale.

    Neat liquids and solvents are charged by volume; ignoring it under-reports the scale. Not
    converted to grams, since that needs a density the record does not carry; it is a third labelled
    term.
    """
    payload = _ord_charge(
        {"volume": {"value": 10.0, "units": "MILLILITER"}},
        {"mass": {"value": 40.0, "units": "GRAM"}},
    )
    (tmp_path / "by_volume.json").write_text(json.dumps(payload), encoding="utf-8")

    async def _run() -> OrdReaction:
        adapter = OrdJsonAdapter(str(tmp_path))
        entries = await adapter.fetch_new_entries(_EPOCH)
        return adapter.map_to_ord(entries[0])

    reaction = asyncio.run(_run())
    assert [component.volume_ml for component in reaction.inputs] == [10.0, None]
    note = record_from_ord_reaction(reaction)
    assert "scale: 40 g + 10 mL of reactants charged" in note.body
    assert "amount not recorded" not in note.body, (
        "the source recorded this amount; saying it did not is the false half of the same defect"
    )


def test_an_ord_amount_the_source_declared_unmeasured_says_so(tmp_path: Path) -> None:
    """An ORD amount the source declared `unmeasured` says so.

    It is a statement, not an absence: carried as an attribute, so the charge row says why no amount
    was recorded.
    """
    payload = _ord_charge({"unmeasured": {"type": "SATURATED"}})
    (tmp_path / "unmeasured.json").write_text(json.dumps(payload), encoding="utf-8")

    async def _run() -> OrdReaction:
        adapter = OrdJsonAdapter(str(tmp_path))
        entries = await adapter.fetch_new_entries(_EPOCH)
        return adapter.map_to_ord(entries[0])

    reaction = asyncio.run(_run())
    assert reaction.inputs[0].attributes == {"amount_unmeasured": "saturated"}
    assert "amount_unmeasured: saturated" in record_from_ord_reaction(reaction).body


def test_an_ord_amount_of_a_kind_this_ingest_cannot_read_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """An ORD amount of a kind this ingest cannot read is refused by name.

    After the four known kinds an unread one is a vocabulary this code does not know, so it is
    refused into the ledger rather than silently under-reporting scale. A non-mapping `amount` is a
    shape error and stays "absent".
    """
    payload = _ord_charge({"activity": {"value": 3.0, "units": "UNIT"}})
    (tmp_path / "unknown_kind.json").write_text(json.dumps(payload), encoding="utf-8")

    async def _run() -> OrdReaction:
        adapter = OrdJsonAdapter(str(tmp_path))
        entries = await adapter.fetch_new_entries(_EPOCH)
        return adapter.map_to_ord(entries[0])

    with pytest.raises(OrdFormatError, match="states none of"):
        asyncio.run(_run())


def test_ord_malformed_workup_input_is_treated_as_absent_not_crashed(tmp_path: Path) -> None:
    """A workup whose `input` is a list (not an object) never crashes the mapper.

    The malformed shape yields no components for that step.
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


async def test_ord_malformed_entry_does_not_abort_the_sync_batch() -> None:
    """A malformed nested field never aborts the whole sync batch.

    End to end through `sync_entries`: without the `isinstance` guards the `AttributeError` escapes
    the per-entry handler. With them the field maps to absent and both entries ingest in order.
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


def test_a_search_hit_id_is_the_note_id_the_ingest_wrote() -> None:
    """A `similar_reactions` hit id opens with `expand_note`.

    Asserted as an equality between the two ends rather than a literal, so it fails if only one end
    changes: `expand_note` strips exactly what `note_id_for_reaction` adds.
    """
    reaction = _ester()
    cited = note_id_for_reaction(record_from_ord_reaction(reaction).reaction_id)
    assert cited.removeprefix("reaction-") == reaction.reaction_id


def test_an_impurity_known_only_by_its_rrt_is_named_rather_than_dropped() -> None:
    """An impurity known only by its RRT is named rather than dropped.

    `Impurity._identifiable` refuses an RRT-only row and prescribes naming it ("RRT 0.94 unknown");
    the adapter applies that, so unresolved peaks, often the largest, stay in the profile.
    """
    entry = RawEntry(
        entry_id="E-rrt",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={
            "id": "E-rrt",
            "timestamp": "2026-01-01T00:00:00Z",
            "reactants": [{"smiles": "CCO"}],
            "products": [
                {
                    "smiles": "CC=O",
                    "impurities": [
                        {"name": "des-bromo", "area_percent": 0.31},
                        {"rrt": 0.94, "area_percent": 1.9},
                        {"rrt": 1.0, "area_percent": 0.42},
                    ],
                }
            ],
        },
    )
    profile = JsonExportAdapter().map_to_ord(entry).impurities

    assert [imp.name for imp in profile] == ["des-bromo", "RRT 0.94 peak", "RRT 1 peak"]
    assert [imp.area_percent for imp in profile] == [0.31, 1.9, 0.42]
    # The RRT is kept on the row as well as spelled into the name: the name is what a reader and a
    # lexical search match, the field is what a query over the profile reads.
    assert [imp.rrt for imp in profile] == [None, 0.94, 1.0]


def test_an_impurity_row_that_identifies_nothing_at_all_is_still_dropped() -> None:
    """An impurity row that identifies nothing at all is still dropped.

    No name, no structure and no positive retention time asserts nothing. Dropped rather than
    rejected, so one such row cannot cost the reaction its record.
    """
    entry = RawEntry(
        entry_id="E-blank",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={
            "id": "E-blank",
            "timestamp": "2026-01-01T00:00:00Z",
            "reactants": [{"smiles": "CCO"}],
            "products": [
                {"smiles": "CC=O", "impurities": [{"area_percent": 0.5}, {"rrt": 0, "name": None}]}
            ],
        },
    )
    assert JsonExportAdapter().map_to_ord(entry).impurities == []


# --- impurity structures reach the molecule index -------------------------------------


def _with_impurities(*impurities: Impurity) -> OrdReaction:
    """The esterification, plus an observed impurity profile (the KNW-2 half of an outcome)."""
    return _ester().model_copy(update={"impurities": list(impurities)})


async def test_an_identified_impurity_is_findable_by_structure() -> None:
    """An identified impurity is findable by structure.

    "Have we seen this one before?" is a structure question; asserted through the search.
    """
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


async def test_an_impurity_with_no_structure_is_skipped_not_fatal() -> None:
    """An ELN routinely records only a chromatographic name; that is not an error (KNW-2)."""
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


async def test_an_unparseable_impurity_structure_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed trace-impurity structure is skipped and logged, not fatal to the experiment.

    `validate_ord` does not check the impurity profile, so the fingerprinter sees it unchecked.
    """
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


# --- the note carries its project, and its scale --------------------------------------


async def test_a_reaction_record_is_reachable_by_its_project_tag() -> None:
    """A reaction record is reachable by its project tag through `gather_evidence(tag=…)`.

    Proven through the store's eligibility gate; a wrong tag must still exclude it.
    """
    store = InMemoryReactionRecordStore()
    record = record_from_ord_reaction(_ester().model_copy(update={"project": "prj-alpha"}))
    await store.record([record], "eln-json")
    assert await store.eligible(["rxn-1"], {"tag": "prj-alpha"}) == {"rxn-1"}
    assert await store.eligible(["rxn-1"], {"tag": "prj-beta"}) == set()


async def test_a_reaction_with_no_project_invents_no_tag() -> None:
    """A record without a project gets no project — never a placeholder a filter would match."""
    store = InMemoryReactionRecordStore()
    await store.record([record_from_ord_reaction(_ester())], "eln-json")
    stored = await store.read("rxn-1")
    assert stored is not None and stored.project is None
    # No project means no tag can match it — not that every tag matches.
    assert await store.eligible(["rxn-1"], {"tag": "prj-alpha"}) == set()


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
    """A record charging one reactant by mass and another by moles reports both.

    `Component` allows either per row, and preferring mass whenever any row had one under-reports a
    mixed charge, making a pilot batch read as a bench run.
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


def test_a_number_this_system_computed_is_rendered_without_its_binary_tail() -> None:
    """A number this system computed is rendered without its binary tail.

    Kelvin-to-Celsius and unit scaling produce floats like `-77.99999999999997`. Computed numbers
    are rendered; numbers the source reported (yield, purity, area) are echoed verbatim. Frontmatter
    keeps the full double, which comparisons read; only the prose body is rounded.
    """
    kelvin_bath = _ester().model_copy(update={"temperature_c": 195.15 - 273.15, "time_h": 100 / 60})
    body = record_from_ord_reaction(kelvin_bath).body
    assert "- temperature: -78 °C\n" in body, f"the rendered conditions were:\n{body}"
    assert "- time: 1.66666666667 h\n" in body, (
        "a converted duration lost its magnitude or kept its noise"
    )
    conditions = record_from_ord_reaction(kelvin_bath).conditions
    assert conditions is not None and conditions.temperature_c == 195.15 - 273.15, (
        "the stored value was rounded to match the prose; the prose must not decide the record"
    )


def test_the_charge_sheet_keeps_the_magnitude_a_balance_actually_measured() -> None:
    """The charge sheet keeps the magnitude a balance actually measured.

    Twelve significant figures is past any balance and short of the binary tail; `:g`'s six would
    publish a kilo-scale charge in exponent form.
    """
    note = record_from_ord_reaction(
        _charged(
            Component(smiles="CCO", role=Role.REACTANT, mass_mg=1234567.8, amount_mmol=26.802345),
        )
    )
    assert "- `CCO` (reactant): 1234567.8 mg, 26.802345 mmol\n" in note.body
    assert "- scale: 1234.5678 g of reactants charged\n" in note.body


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
    """Scale survives the retrieval excerpt of a procedure-heavy note.

    `retrieval.retrievers._excerpt` truncates the body at `note_excerpt_chars`, so scale leads the
    conditions.
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


async def test_one_non_utf8_ord_export_does_not_abort_the_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One non-UTF-8 ORD export is skipped, like every other unreadable export.

    `UnicodeDecodeError` is neither a `JSONDecodeError` nor an `OSError`. The bad file sorts first,
    so an abort would fail the assertion on an empty list.
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


def test_eln_free_text_cannot_forge_a_knowledge_graph_relation() -> None:
    """ELN free text cannot forge a knowledge-graph relation.

    `kg.note` parses `[[rel:id]]` links, and the body is served verbatim by `expand_note` and quoted
    into reports with no review. Every free-text field is checked, since each is a way in.
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
    """No depth of opening brackets spells a relation.

    A naive `replace("[[", "[ [")` turns `[[[x]]` into `[ [[x]]`, a new valid delimiter.
    Parametrised because the property is that no depth works.
    """
    reaction = _ester()
    reaction.hypothesis = "[" * brackets + "contradicts:reaction-1234]]"

    record = record_from_ord_reaction(reaction)

    assert not cited_links(record.body), f"{brackets} opening brackets forged an edge"


def test_mass_balance_catches_a_new_element_and_nothing_weaker() -> None:
    """Mass balance catches a new element and nothing weaker, pinned in both directions.

    Element-set subsumption is a necessary condition only: it rejects a product introducing an
    element no input supplies and admits any fabrication from present elements. Asserting the misses
    keeps anyone from reading it as validating the chemistry.
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
    """A typographic minus before a temperature is still a minus.

    `−78 °C` read as `78.0` is a 156-degree error that looks plausible beside verbatim prose.
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


async def test_ingesting_a_reaction_writes_the_label_index_record_phase() -> None:
    """Ingesting a reaction writes the label index's record phase.

    The row carries the record form (agents kept), not the fingerprint form, which drops solvent and
    catalyst and so could never answer "which solvent".
    """
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
    # Qualified by source and asserted as a literal: deriving it from `note_id_for_reaction` would
    # move both sides together. A bare id held by two sources resolves to a refusal.
    assert row.citation == f"reaction-eln-json.{reaction.reaction_id}"
    # Every component, with the role the record stated and nothing derived from it yet.
    assert [(s.ordinal, s.role) for s in row.species] == [
        (i, c.role.value) for i, c in enumerate(reaction.compounds())
    ]
    assert row.labeller_version is None


async def test_the_label_row_keeps_the_agents_the_fingerprint_drops() -> None:
    """The label row keeps the agents the fingerprint drops.

    Fingerprints store `transformation_smiles()`, the label index `reaction_smiles()`; only the
    second can say which solvent was used.
    """
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


def test_the_validator_checks_the_sources_that_are_attached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`eln-validate` checks the sources the registry has enabled, not two named adapters.

    An ELN attached through a manifest must be inside the check. A failure names the source, sending
    the operator to the manifest; an empty enabled set neither prints `OK` nor exits 0, because CI
    reads the exit code.
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
    # A second manifest, declared up front because `discovered()` caches: a source that is *known
    # and enabled* while declaring no `ingest:` half is the third arm below, and adding its folder
    # after the first `main()` would never be seen.
    (manifests / "retrieve-only").mkdir(parents=True)
    (manifests / "retrieve-only" / "datasource.yaml").write_text(
        "name: retrieve-only\n"
        "description: A source with a retrieve half and no ingest half.\n"
        "retrieve: chemclaw.retrieval.retrievers:GraphRetriever\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(manifests))
    monkeypatch.setattr(settings, "data_sources", "eln-under-test")

    assert main() == 1
    reported = capsys.readouterr().out
    assert "eln-under-test/eln-bad" in reported, "the manifest's name, not 'free-text'"
    assert "mass balance" in reported

    monkeypatch.setattr(settings, "data_sources", "")
    assert main() == 1, "nothing checked is not a pass, and the exit code is the channel CI reads"
    nothing = capsys.readouterr().out
    assert "not a pass" in nothing, "and it must not read as one either"
    assert "OK" not in nothing

    # The same branch reached by accident: an enabled, known source with no `ingest:` half. An
    # unknown name already raises, but `graph` instead of `graph,eln-json` does not.
    monkeypatch.setattr(settings, "data_sources", "retrieve-only")
    assert main() == 1, "a deployment that meant to attach an ELN and did not must not go green"


def test_the_validator_does_not_report_ok_over_a_source_that_yielded_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The validator does not report OK over a source that yielded nothing.

    An attached source that supplies nothing (a typo'd `export_dir`, an unmounted export) is a
    failed claim, since the adapter cannot tell empty from mis-mounted. No sources enabled is a
    deployment's choice and exits 0 with a statement.
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


# --- The retraction seam ---


def test_the_seam_wrapper_does_not_swallow_an_optional_capability() -> None:
    """`DatedIngest` does not narrow what the adapter it wraps can do.

    A `runtime_checkable` Protocol is structural, so a wrapper that does not redeclare a method
    lacks it, and `fetch_was_truncated` would answer `False` for every source. Optional capabilities
    are read through the public `inner`.
    """

    class _Bounded(_ListAdapter):
        def fetch_truncated(self) -> bool:
            return True

    assert fetch_was_truncated(DatedIngest(_Bounded([]))) is True
    assert fetch_was_truncated(DatedIngest(_ListAdapter([]))) is False
    assert fetch_was_truncated(_ListAdapter([])) is False


def _withdrawal_entry(retracted_at: datetime | None, entry_id: str = "EXP-1001") -> RawEntry:
    """One ELN entry, optionally carrying the source's own withdrawal.

    `entry_id` is a parameter so tests sharing one schema do not answer each other.
    """
    return RawEntry(
        entry_id=entry_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={
            "id": entry_id,
            "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
            "products": [{"smiles": "CCOC(C)=O"}],
        },
        retracted_at=retracted_at,
    )


class _WithdrawingAdapter:
    """An adapter whose source re-exports an entry with a tombstone on it."""

    def __init__(self, entries: list[RawEntry]) -> None:
        """Serve exactly `entries` on every fetch."""
        self._entries = entries

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Every entry, every time — the overlap replay an amendment arrives through."""
        return self._entries

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """The shared JSON mapping; a withdrawal is not in the reaction."""
        return JsonExportAdapter().map_to_ord(raw)


def test_a_withdrawn_entry_leaves_the_evidence_set_on_every_reader() -> None:
    """A withdrawn entry leaves the evidence set on every reader, against a real database.

    - the producer is `RawEntry.retracted_at` on the exported delta, never an entry's absence;
    - the store persists it and `is_current`/`eligible` honour it;
    - the unfiltered retrieval sweep `gather_evidence` runs drops it;
    - the bundle tool `similar_reactions` drops it;
    - `expand_note` still resolves it and says it was withdrawn, so citations do not dangle.

    The first pass asserts the entry is served on every reader, so the second cannot pass on a
    broken retriever.
    """
    source = "retraction-probe"
    # The literal, not `note_id_for_reaction(...)`: deriving it from the function under test would
    # move both sides of the assertion together, and a sweep that stopped qualifying its citations
    # would still pass (`D-2026-09-13-a-citation-names-the-source-it-was-found-in`).
    cited = "reaction-retraction-probe.EXP-1001"

    async def _run() -> dict[str, object]:
        await migrated_db_or_skip()
        records = PostgresReactionRecordStore()
        reactions, molecules = InMemoryFingerprintStore(), InMemoryFingerprintStore()
        retriever = FingerprintReactionRetriever(reactions, records)
        query = "CCO.CC(=O)O>>CCOC(C)=O"

        async def _served() -> dict[str, object]:
            unfiltered = await retriever.retrieve(query, {})
            return {
                "record": await records.read("EXP-1001"),
                "eligible": await records.eligible(["EXP-1001"], {}),
                "retracted": await records.retracted([(source, "EXP-1001")]),
                "sweep": [chunk.source_note_id for chunk in unfiltered],
            }

        await sync_entries(
            _WithdrawingAdapter([_withdrawal_entry(None)]),
            reactions,
            molecules,
            records,
            _EPOCH,
            label_index=_labels(),
            source=source,
        )
        before = await _served()
        # The second pass is a replay, as a real one is: the cursor is past `created_at`, so the
        # withdrawal arrives through the unchanged-check branch and must not be skipped there.
        await sync_entries(
            _WithdrawingAdapter([_withdrawal_entry(datetime(2026, 3, 4, tzinfo=UTC))]),
            reactions,
            molecules,
            records,
            datetime(2026, 2, 1, tzinfo=UTC),
            label_index=_labels(),
            source=source,
        )
        after = await _served()
        return {"before": before, "after": after}

    outcome = asyncio.run(_run())
    before = cast("dict[str, Any]", outcome["before"])
    after = cast("dict[str, Any]", outcome["after"])

    today = date.today()
    assert before["record"] is not None and before["record"].is_current(today)
    assert before["eligible"] == {"EXP-1001"}
    assert before["retracted"] == set()
    assert cited in before["sweep"], (
        "the entry was never served in the first place, so its later absence proves nothing"
    )

    assert after["record"] is not None, (
        "the retracted row stopped resolving; a citation to a withdrawn run must not become a "
        "dangling link"
    )
    assert after["record"].retracted_at is not None
    assert not after["record"].is_current(today)
    assert after["eligible"] == set()
    assert after["retracted"] == {(source, "EXP-1001")}
    assert cited not in after["sweep"], (
        "the unfiltered sweep still serves a withdrawn run as current evidence — the exact "
        "measurement D-2026-08-27 recorded against the storage-only implementation"
    )


def test_a_source_that_republishes_an_entry_un_retracts_it() -> None:
    """A source that republishes an entry un-retracts it.

    The row is what the source last said: the upsert refreshes `retracted_at` like every other
    field, so one bad export is not permanent.
    """

    async def _run() -> tuple[bool, bool]:
        await migrated_db_or_skip()
        records = PostgresReactionRecordStore()
        reactions, molecules = InMemoryFingerprintStore(), InMemoryFingerprintStore()
        source = "unretraction-probe"

        async def _sync(retracted_at: datetime | None, since: datetime) -> None:
            await sync_entries(
                _WithdrawingAdapter([_withdrawal_entry(retracted_at, "EXP-2002")]),
                reactions,
                molecules,
                records,
                since,
                label_index=_labels(),
                source=source,
            )

        # A replay on both the withdrawal and the re-publication, because that is how each of them
        # reaches a corpus whose cursor has already passed the entry.
        await _sync(datetime(2026, 3, 4, tzinfo=UTC), _EPOCH)
        withdrawn = bool(await records.retracted([(source, "EXP-2002")]))
        await _sync(None, datetime(2026, 2, 1, tzinfo=UTC))
        still = bool(await records.retracted([(source, "EXP-2002")]))
        return withdrawn, still

    withdrawn, still = asyncio.run(_run())
    assert withdrawn, "the withdrawal never landed, so the reversal below tests nothing"
    assert not still, "a re-published entry stayed retracted; the withdrawal is a one-way door"


def test_a_json_export_stamped_withdrawn_is_fetched_and_carries_its_tombstone(
    tmp_path: Path,
) -> None:
    """A JSON export stamped withdrawn is fetched and carries its tombstone.

    A withdrawal joins the fetch window, or an entry behind the cursor is never seen again. A
    control entry from the same day, never withdrawn, must not come back, so an adapter that stopped
    filtering fails.
    """

    async def _run() -> list[RawEntry]:
        _write_entry(tmp_path / "pulled.json", "pulled", "2026-01-01T00:00:00Z")
        payload = json.loads((tmp_path / "pulled.json").read_text(encoding="utf-8"))
        (tmp_path / "pulled.json").write_text(
            json.dumps(payload | {"retracted": "2026-07-01T00:00:00Z"}), encoding="utf-8"
        )
        _write_entry(tmp_path / "kept.json", "kept", "2026-01-01T00:00:00Z")
        return await JsonExportAdapter(str(tmp_path)).fetch_new_entries(
            datetime(2026, 6, 1, tzinfo=UTC)
        )

    fetched = asyncio.run(_run())

    assert [entry.entry_id for entry in fetched] == ["pulled"], (
        "a withdrawal that does not move the fetch window is a tombstone nothing ever fetches"
    )
    assert fetched[0].retracted_at == datetime(2026, 7, 1, tzinfo=UTC)


def test_two_sources_behind_one_entry_id_are_two_citations_that_each_resolve() -> None:
    """Two sources behind one entry id are two citations that each resolve.

    Over a real database, through the shipped retriever and resolver:

    - the two hits carry different citations, each naming its site;
    - each expands to that site's own body;
    - the bare form still resolves, and refuses when two sources hold the id.

    The entries carry different operators so "each resolved to its own row" is a real distinction.
    """

    async def _run() -> tuple[list[str], list[str], object]:
        await migrated_db_or_skip()
        records = PostgresReactionRecordStore()
        reactions = InMemoryFingerprintStore()
        molecules = InMemoryFingerprintStore()
        entry = "EXP-9001"
        for site, operator in (("site-alpha", "a.chemist"), ("site-beta", "b.chemist")):
            await sync_entries(
                _ListAdapter(
                    [
                        RawEntry(
                            entry_id=entry,
                            created_at=datetime(2026, 1, 1, tzinfo=UTC),
                            payload={
                                "id": entry,
                                "operator": operator,
                                "reactants": [{"smiles": "CCO"}, {"smiles": "CC(=O)O"}],
                                "products": [{"smiles": "CCOC(C)=O"}],
                            },
                        )
                    ]
                ),
                reactions,
                molecules,
                records,
                _EPOCH,
                label_index=_labels(),
                source=site,
            )
        retriever = FingerprintReactionRetriever(reactions, records)
        chunks = await retriever.retrieve("CCO.CC(=O)O>>CCOC(C)=O", {})
        cited = sorted(chunk.source_note_id for chunk in chunks)
        bodies = []
        for note_id in cited:
            source, record_id = external_record_ref(note_id)
            record = await records.read(record_id, source)
            assert record is not None
            # The rendered provenance, which differs by operator between the two sites — the one
            # field that proves *which* row answered, where the transcription prose is identical.
            bodies.append(record.source)
        try:
            await records.read(entry)
            refusal: object = None
        except AmbiguousReactionRecord as exc:
            refusal = exc
        return cited, bodies, refusal

    cited, bodies, refusal = asyncio.run(_run())

    assert cited == ["reaction-site-alpha.EXP-9001", "reaction-site-beta.EXP-9001"], (
        "two sites behind one entry id still cite one id, so the chemist cannot open the run the "
        "search found"
    )
    assert bodies == ["eln-json:EXP-9001:a.chemist", "eln-json:EXP-9001:b.chemist"], (
        "the two citations resolved to the same row, so the qualification names a source the "
        "resolver does not use"
    )
    assert isinstance(refusal, AmbiguousReactionRecord), (
        "the bare form stopped refusing; a citation that does not name one run must not be "
        "answered by a guess"
    )


def test_one_sites_withdrawal_does_not_retract_the_other_sites_run() -> None:
    """One site's withdrawal does not retract another site's run.

    `retracted()` is keyed by `(source, id)`, like the index. Both directions are asserted, because
    "nothing was dropped" is satisfied by a filter that never runs.
    """

    async def _run() -> tuple[list[str], set[tuple[str, str]]]:
        await migrated_db_or_skip()
        records = PostgresReactionRecordStore()
        reactions, molecules = InMemoryFingerprintStore(), InMemoryFingerprintStore()
        entry = "EXP-9002"
        sites: tuple[tuple[str, datetime | None], ...] = (
            ("alpha-site", datetime(2026, 3, 4, tzinfo=UTC)),
            ("beta-site", None),
        )
        for site, withdrawn in sites:
            await sync_entries(
                _WithdrawingAdapter([_withdrawal_entry(withdrawn, entry)]),
                reactions,
                molecules,
                records,
                _EPOCH,
                label_index=_labels(),
                source=site,
            )
        retriever = FingerprintReactionRetriever(reactions, records)
        chunks = await retriever.retrieve("CCO.CC(=O)O>>CCOC(C)=O", {})
        return (
            sorted(chunk.source_note_id for chunk in chunks),
            await records.retracted([("alpha-site", entry), ("beta-site", entry)]),
        )

    cited, withdrawn = asyncio.run(_run())

    assert withdrawn == {("alpha-site", "EXP-9002")}, (
        "the withdrawal was attributed to both sites, so one site's retraction removes another "
        "site's run"
    )
    assert cited == ["reaction-beta-site.EXP-9002"], (
        "the unfiltered sweep dropped the wrong hit, or dropped both: exactly the site that "
        "withdrew its run must leave the evidence set, and exactly the other must stay"
    )


def test_an_impurity_carries_the_rrt_its_docstrings_have_always_named() -> None:
    """An impurity carries its RRT.

    RRT is how a chemist says which unresolved peak; without a field it fell to untyped attributes.
    Driven through the JSON adapter so the field has a producer.
    """
    raw = RawEntry(
        entry_id="rrt-1",
        created_at=_EPOCH,
        payload={
            "reactants": [{"smiles": "CCO"}],
            "products": [
                {
                    "smiles": "CC(=O)Oc1ccccc1C(=O)O",
                    "impurities": [
                        {"name": "RRT 0.94 unknown", "area_percent": 0.11, "rrt": 0.94},
                        {"name": "des-methyl impurity", "area_percent": 0.19, "rrt": 1.32},
                    ],
                }
            ],
        },
    )
    record = JsonExportAdapter().map_to_ord(raw)
    by_name = {impurity.name: impurity for impurity in record.impurities}
    assert by_name["RRT 0.94 unknown"].rrt == 0.94, (
        "the adapter dropped the RRT, so two unresolved peaks are distinguishable by area% alone "
        "and a chemist cannot say which one an answer is about"
    )
    assert by_name["des-methyl impurity"].rrt == 1.32


def test_an_rrt_alone_does_not_identify_an_impurity() -> None:
    """An RRT alone does not identify an impurity.

    It says where a peak eluted, not what it is; such a row must carry a name of that form
    ("RRT 0.94 unknown").
    """
    with pytest.raises(ValidationError):
        Impurity(rrt=0.94, area_percent=0.11)
