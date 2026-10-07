"""What the row builder writes beside the science: provenance, identity and electronic state.

`dialect.rows_for` turns one record into every table's rows. These tests are about columns that
must not be filled with values no producer supplied: the publication row carries the manifest's
tenant even for primitives, `structure.charge`/`multiplicity` come from the record (or stay
absent), and a column no writer can fill does not exist.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.chem import compound_id
from chemclaw.publish import record as record_module
from chemclaw.publish.dialect import (
    CONFLICT_KEYS,
    PRESERVE_ON_BLANK,
    TABLE_ORDER,
    rows_for,
    upsert_statement,
)
from chemclaw.publish.project import _fact, project
from chemclaw.publish.record import (
    Conditions,
    PropertyFact,
    Publication,
    ResultRecord,
    Subject,
    SubjectMember,
    TheoryLevel,
)
from chemclaw.science.calc.models import Conformer, ConformerEnsemble, Structure

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
_DDL = Path(__file__).resolve().parents[1] / "schema" / "result-store" / "001_core.sql"


def _record() -> ResultRecord:
    """A minimal record — this file is about the columns, not the chemistry."""
    return ResultRecord(
        calc_ref="pka@v1:a:b",
        calc_type="pka",
        subject=Subject(
            kind="molecule",
            members=[SubjectMember(ordinal=0, role="subject", smiles="CCO")],
            label="CCO",
        ),
        conditions=Conditions(),
        level=TheoryLevel(method="GFN2-xTB"),
    )


def _anion(z: float = 1.0) -> Structure:
    """A deprotonated phenolate-shaped geometry with charge -1.

    The electron count is even at -1, since `Structure` refuses an open-shell species declared a
    singlet; that check makes the charge meaningful.
    """
    return Structure(
        elements=[8, 6, 1, 1, 1],
        positions=[[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, z], [0, -1, 0]],
        charge=-1,
        smiles="[O-]c1ccccc1",
    )


def _anionic_ensemble() -> ResultRecord:
    """A conformer ensemble of an anion, projected exactly as the cache hook projects one."""
    conformers = [
        Conformer(relative_kcal=0.0, population=0.6, degeneracy=1, structure=_anion(1.0)),
        Conformer(relative_kcal=0.5, population=0.4, degeneracy=1, structure=_anion(1.1)),
    ]
    ensemble = ConformerEnsemble(
        smiles="[O-]c1ccccc1",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent="water",
        temperature_k=298.15,
        conformers=conformers,
        total_found=2,
        conformational_entropy_cal_per_mol_k=1.1,
        ensemble_correction_kcal=-0.3,
    )
    payload = ensemble.model_dump(mode="json")
    for dumped, conformer in zip(payload["conformers"], conformers, strict=True):
        dumped["structure"]["structure_id"] = conformer.structure.structure_id
    return project(
        calc_ref="xtb.conformers@v1:a:b",
        calc_type="xtb.conformers",
        payload=payload,
        payload_kind="ConformerEnsemble",
        computed_at=_NOW,
    )


def test_a_record_with_no_publication_still_names_its_tenant() -> None:
    """A record with no publication still names its tenant.

    The cache hook and `backfill_cached` construct no `Publication`, yet a site's grants and
    row-level security attach to that table and the manifest's `tenant_id` separates deployments
    sharing a database.
    """
    rows = rows_for(_record(), tenant_id="acme", writer_version="rev")["calculation_publication"]

    assert len(rows) == 1, (
        "a primitive publishes no `Publication`, so without a fallback its calc_ref appears in no "
        f"publication row at all and carries no tenant; got {rows}"
    )
    assert rows[0]["tenant_id"] == "acme"
    assert rows[0]["calc_ref"] == "pka@v1:a:b"
    # The actor half is genuinely unknown for a cached primitive — a calculation's identity
    # excludes who asked for it — and an empty string is the honest answer rather than a guess.
    assert rows[0]["actor"] == ""


def test_a_declared_publication_is_not_duplicated_by_the_fallback() -> None:
    """The fallback is for the *empty* case only, or every job would publish two rows."""
    record = _record().model_copy(
        update={"publications": [Publication(actor="chemist@example.com", job_id="job-1")]}
    )

    rows = rows_for(record, tenant_id="acme", writer_version="rev")["calculation_publication"]

    assert len(rows) == 1
    assert rows[0]["actor"] == "chemist@example.com"
    assert rows[0]["tenant_id"] == "acme", "an empty tenant still takes the sink's"


def test_a_charged_geometry_is_not_published_as_neutral() -> None:
    """A charged geometry is not published as neutral.

    Charge and multiplicity are in the `structure_id` hash and must reach the columns, or queries
    for anions return nothing and a re-run from a published geometry uses the wrong species.
    """
    record = _anionic_ensemble()
    rows = rows_for(record, tenant_id="t", writer_version="w")["structure"]

    published = {row["structure_id"]: (row["charge"], row["multiplicity"]) for row in rows}
    for structure_id in (fact.structure_id for fact in record.conformers):
        assert published[structure_id] == (-1, 1), (
            f"the geometry {structure_id} was computed at charge -1 and is published as "
            f"{published[structure_id]}"
        )


def test_an_unstated_electronic_state_is_absent_rather_than_neutral() -> None:
    """An unstated electronic state is absent rather than neutral.

    `0` and `1` are real values; fabricating them makes "not recorded" look like "neutral singlet"
    and, via upsert, overwrites a real value.
    """
    record = _record().model_copy(
        update={
            "subject": Subject(
                kind="geometry",
                members=[SubjectMember(ordinal=0, role="subject", structure_id="st_abc")],
                label="",
            )
        }
    )

    rows = rows_for(record, tenant_id="t", writer_version="w")["structure"]

    assert [(row["charge"], row["multiplicity"]) for row in rows] == [(None, None)]


def test_a_writer_that_does_not_know_the_state_cannot_erase_one_that_does() -> None:
    """A writer that does not know the state cannot erase one that does.

    A subject member may know only the address while a conformer knows the charge;
    `PRESERVE_ON_BLANK` keeps the known value whichever row lands second.
    """
    assert "charge" in PRESERVE_ON_BLANK["structure"]
    assert "multiplicity" in PRESERVE_ON_BLANK["structure"]
    statement = upsert_statement("structure", ("structure_id", "charge", "multiplicity"))
    assert "COALESCE(NULLIF(EXCLUDED.charge, NULL), structure.charge)" in statement
    assert "COALESCE(NULLIF(EXCLUDED.multiplicity, NULL), structure.multiplicity)" in statement


def test_no_table_is_required_for_a_fact_nothing_can_write() -> None:
    """No table is required for a fact nothing can write.

    `calculation_artifact` had no producer, yet `_known_columns` would refuse to deliver to a site
    that had not created it. An absence test, so re-adding it without a producer fails
    (`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`).
    """
    assert "calculation_artifact" not in TABLE_ORDER, (
        "a table in `TABLE_ORDER` is a table the site must create for delivery to work; do not "
        "put this back without a projector that emits artifacts and a `project()` that reads them"
    )
    assert "artifacts" not in ResultRecord.model_fields
    assert not hasattr(record_module, "ArtifactFact")
    assert "calculation_artifact" not in _DDL.read_text(encoding="utf-8")


def test_every_table_the_row_builder_emits_is_ordered_and_keyed() -> None:
    """Every table the row builder emits is in `TABLE_ORDER` and `CONFLICT_KEYS`.

    The probe reads `TABLE_ORDER` and the upsert reads `CONFLICT_KEYS`, so the three declarations
    must agree.
    """
    built: dict[str, list[dict[str, Any]]] = rows_for(
        _anionic_ensemble(), tenant_id="t", writer_version="w"
    )
    assert set(built) == set(TABLE_ORDER), (
        "the row builder and the dependency order disagree about which tables exist: "
        f"{sorted(set(built) ^ set(TABLE_ORDER))}"
    )
    assert set(TABLE_ORDER) <= set(CONFLICT_KEYS), (
        f"no primary key declared for {sorted(set(TABLE_ORDER) - set(CONFLICT_KEYS))}, so its "
        "upsert cannot be built at all"
    )
    assert set(CONFLICT_KEYS) <= set(TABLE_ORDER), (
        f"{sorted(set(CONFLICT_KEYS) - set(TABLE_ORDER))} is keyed but never written"
    )


def test_a_converted_fact_keeps_the_number_its_calculator_reported() -> None:
    """A converted fact keeps the number its calculator reported.

    `reported_value`/`reported_unit` record what the calculator said before canonicalization, so the
    canonical column can be rebuilt if a conversion is found wrong. Writing the canonical number
    under the reported unit would make that unrecoverable. Asserted through the columns.
    """
    record = _record().model_copy(
        update={"properties": [_fact("reaction_delta_g", -0.02, "hartree")]}
    )
    row = rows_for(record, tenant_id="t", writer_version="w")["property_value"][0]

    assert row["value_canonical"] == pytest.approx(-12.5502, abs=1e-3)
    assert (row["reported_value"], row["reported_unit"]) == (-0.02, "hartree")


def test_a_fact_with_no_reported_value_still_records_one() -> None:
    """A `PropertyFact` with no reported value still records the canonical number, not NULL."""
    fact = PropertyFact(property="reaction_delta_g", value=-12.5, unit="kcal/mol")
    record = _record().model_copy(update={"properties": [fact]})
    row = rows_for(record, tenant_id="t", writer_version="w")["property_value"][0]

    assert (row["value_canonical"], row["reported_value"]) == (-12.5, -12.5)


def test_the_compound_row_carries_the_structure_its_own_id_was_derived_from() -> None:
    """The compound row carries the structure its own id was derived from.

    `compound_id` hashes the standardized SMILES, so `canonical_smiles` must be that structure, not
    whichever species was published last. A species' own SMILES lives on `subject_member.smiles` and
    `calculation_candidate.smiles`.
    """
    acid = _record().model_copy(
        update={
            "subject": Subject(
                kind="molecule",
                members=[
                    SubjectMember(
                        ordinal=0,
                        role="subject",
                        compound_id=compound_id("CC(=O)[O-]"),
                        smiles="CC(=O)[O-]",
                    )
                ],
                label="CC(=O)[O-]",
            )
        }
    )
    rows = rows_for(acid, tenant_id="t", writer_version="w")

    assert [row["compound_id"] for row in rows["compound"]] == [compound_id("CC(=O)O")]
    assert [row["canonical_smiles"] for row in rows["compound"]] == ["CC(=O)O"]
    # The species itself is still recorded, one table over.
    assert [row["smiles"] for row in rows["subject_member"]] == ["CC(=O)[O-]"]


def test_no_structure_column_is_blank_in_every_row_the_writer_can_build() -> None:
    """No `structure` column is blank in every row the writer can build.

    A column no writer can fill is a stored fact that is not one; `atom_count` and `geometry` were
    dropped from `rows_for` and the DDL. Coordinates travel in `calculation_payload`. Asserted over
    the union of a record's rows, since the two builders know different halves.
    """
    rows = rows_for(_anionic_ensemble(), tenant_id="t", writer_version="w")["structure"]
    blanks: tuple[Any, ...] = (None, "", 0, {})

    unfillable = sorted(
        column
        for column in {key for row in rows for key in row}
        if all(row.get(column) in blanks for row in rows)
    )

    assert not unfillable, (
        f"{unfillable} is written to `structure` on every row and is blank on every row — no "
        "producer can fill it, so the column records nothing while reading as a stored fact"
    )
