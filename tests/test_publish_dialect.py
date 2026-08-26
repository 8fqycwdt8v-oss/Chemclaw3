"""What the row builder writes beside the science: provenance, identity and electronic state.

`test_publish_projection.py` asserts what a projector extracts and `test_publish_sql.py` asks the
six chemistry questions of a loaded database. Between them sits `dialect.rows_for`, which turns one
record into the rows every table gets — and three of its columns were being filled with values no
producer ever supplied:

- `calculation_publication` was built **only** from `record.publications`, and the cache hook (the
  path every primitive takes, i.e. most of the corpus) constructs none — so the tenant the manifest
  declares, and any row-level security a site attaches to that table, covered the composites only.
- `structure.charge` and `structure.multiplicity` were `member.charge or 0` and
  `member.multiplicity or 1`, while no projector ever set either — so every anion and every radical
  this system published was recorded as a neutral closed-shell singlet.
- `calculation.writer_version` came from a driver parameter with no default and no manifest key,
  so it was the empty string on every row a deployment writes: "recorded, and blank".

Each of those reads as a stored fact and is not one, which is what makes this file's assertions
about *columns* rather than about chemistry.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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
    """A deprotonated phenolate-shaped geometry: charge -1, and a real one.

    Element list chosen so the electron count is even at charge -1 — `Structure` refuses an
    open-shell species declared as a closed-shell singlet, which is exactly the check that makes
    the charge on this fixture meaningful rather than decorative.
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
    """The tenant is a property of the *writer*, and is the one field always knowable at write time.

    `publish_stored_result` and `backfill_cached` — the paths every primitive takes — construct no
    `Publication`, so this list was empty for the overwhelming majority of rows and no publication
    row was written at all. The shipped DDL says of that table "a site's grants and row-level
    security attach here rather than to `calculation`", so a site following that advice saw zero
    primitives, and the manifest's `tenant_id` (whose stated purpose is keeping two deployments'
    output from merging in one shared database) was unreachable for them.
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
    """Charge and multiplicity are in the `structure_id` hash; they must reach the columns too.

    Both were hardcoded — `or 0` / `or 1` on the member rows, literal `0` / `1` on the conformer
    rows — and no projector set either, so *every* geometry this writer emitted was recorded as a
    neutral closed-shell singlet. "Show me every anionic geometry we have optimised" then returns
    nothing, and a consumer re-running a calculation from a published geometry re-runs it on the
    wrong species.
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
    """A geometry named only by its address says nothing about its charge — and must not claim to.

    `0` and `1` are real values a query filters on, so fabricating them where the payload states
    nothing is worse than a null: it makes "we did not record this" indistinguishable from "we
    recorded a neutral singlet", and — because a later writer that *does* know is an ordinary
    upsert — it overwrites the real value with the fabricated one.
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
    """The argument `PRESERVE_ON_BLANK` already makes for `origin_calc_ref`, applied to the state.

    Two builders emit `structure` rows for one content-addressed id — a subject member, which may
    know only the address, and a conformer, which knows the geometry's charge. Whichever landed
    second used to win, so a member row arriving after a conformer row blanked a real charge.
    """
    assert "charge" in PRESERVE_ON_BLANK["structure"]
    assert "multiplicity" in PRESERVE_ON_BLANK["structure"]
    statement = upsert_statement("structure", ("structure_id", "charge", "multiplicity"))
    assert "COALESCE(NULLIF(EXCLUDED.charge, NULL), structure.charge)" in statement
    assert "COALESCE(NULLIF(EXCLUDED.multiplicity, NULL), structure.multiplicity)" in statement


def test_no_table_is_required_for_a_fact_nothing_can_write() -> None:
    """`calculation_artifact` had no producer at any of the three layers that shipped it.

    No projector returned an `artifacts` key, `project()` never read one, and `record.artifacts`
    was therefore `[]` on every record this system can build — while the table stayed in
    `TABLE_ORDER`, so `_known_columns` refused to deliver *anything* to a site that had not created
    it. A requirement on the site, for a table guaranteed to stay empty.

    An absence test rather than a comment, exactly as
    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` left behind for
    `audit_events.agent`: re-adding the claim without a producer fails here.
    """
    assert "calculation_artifact" not in TABLE_ORDER, (
        "a table in `TABLE_ORDER` is a table the site must create for delivery to work; do not "
        "put this back without a projector that emits artifacts and a `project()` that reads them"
    )
    assert "artifacts" not in ResultRecord.model_fields
    assert not hasattr(record_module, "ArtifactFact")
    assert "calculation_artifact" not in _DDL.read_text(encoding="utf-8")


def test_every_table_the_row_builder_emits_is_ordered_and_keyed() -> None:
    """The three declarations that must agree, checked against each other rather than by eye.

    Deleting `calculation_artifact` had to touch the row builder, `TABLE_ORDER` and `CONFLICT_KEYS`
    together; leaving it in any one of them would have kept the requirement on the site (the probe
    reads `TABLE_ORDER`) or crashed the upsert (which reads `CONFLICT_KEYS`).
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
    """`reported_value`/`reported_unit` are a *pair*, and one of them was the other's value.

    The shipped DDL says of these two columns: "what the calculator actually said, before
    canonicalization. Kept for audit and for the day a conversion is found wrong - at which point
    the canonical column can be rebuilt from this." `PropertyFact` had only the canonical number to
    offer, so this row builder wrote *that* under the reported unit — a number in kcal/mol labelled
    `hartree`, which is not merely wrong but unrecoverable, and rebuilding from it would convert a
    second time.

    Invisible today because every projector reports the canonical unit already, which is the same
    reason the conversion in `project._fact` is untested (`test_publish_projection.py` pins that
    half). Asserted here through the columns, because that is where the pair is written.
    """
    record = _record().model_copy(
        update={"properties": [_fact("reaction_delta_g", -0.02, "hartree")]}
    )
    row = rows_for(record, tenant_id="t", writer_version="w")["property_value"][0]

    assert row["value_canonical"] == pytest.approx(-12.5502, abs=1e-3)
    assert (row["reported_value"], row["reported_unit"]) == (-0.02, "hartree")


def test_a_fact_with_no_reported_value_still_records_one() -> None:
    """A `PropertyFact` built outside `_fact` reports the canonical number, not NULL.

    `reported_value` is optional on the model — `_text` and `_flag` produce no number at all, and
    one projector constructs a `PropertyFact` directly — so the fallback is what keeps this column
    populated for the rows that have always populated it.
    """
    fact = PropertyFact(property="reaction_delta_g", value=-12.5, unit="kcal/mol")
    record = _record().model_copy(update={"properties": [fact]})
    row = rows_for(record, tenant_id="t", writer_version="w")["property_value"][0]

    assert (row["value_canonical"], row["reported_value"]) == (-12.5, -12.5)
