"""Turning a record into statements. Every value is bound; only fixed identifiers are written.

Table and column names are literals from the schema this repository ships, so no value can reach
statement text. Writes are upserts keyed on content hashes, so a redelivery converges.

This emits Postgres only (`ON CONFLICT ... DO UPDATE`); there is no `MERGE` emitter. The schema
in `schema/result-store/` is portable (no arrays, sequences or expression indexes), so another
engine is this module plus the driver's `information_schema` probe. Statements are plain text
because there are a fixed few, shaped by the schema.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from chemclaw.core.chem import standard_smiles
from chemclaw.core.ids import stable_hash
from chemclaw.publish.properties import definition_for
from chemclaw.publish.record import Publication, ResultRecord
from chemclaw.publish.solvents import display_name

# One statement per table, in dependency order: a row is never written before the rows it
# references. `(table, columns, conflict_key)`; an empty `conflict_key` means append-only.
_Statement = tuple[str, tuple[str, ...], tuple[str, ...]]


def _value_id(calc_ref: str, scope: str, ordinal: int | None, prop: str) -> str:
    """The content address of one property fact: a hash, not a sequence.

    No sequences in this schema: they do not port, and a derived key makes re-publishing converge.
    """
    return f"pv_{stable_hash([calc_ref, scope, ordinal, prop])}"


def rows_for(
    record: ResultRecord, *, tenant_id: str, writer_version: str
) -> dict[str, list[dict[str, Any]]]:
    """Every row one record contributes, keyed by table and in dependency order.

    Plain dicts so the JSON (HTTP) and SQL drivers share one row builder.
    """
    now = datetime.now(UTC)
    subject_id = record.subject_id
    conditions, level = record.conditions, record.level
    solvent_id = conditions.solvent
    method = level.method

    compounds: list[dict[str, Any]] = []
    structures: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    for member in record.subject.members:
        if member.compound_id:
            compounds.append(
                {
                    "compound_id": member.compound_id,
                    # The standardized structure the key was derived from, not the species that
                    # carried it, so every tautomer or protonation state upserting this row writes
                    # the same value. `standard_smiles` is lenient (returns the input on parse
                    # failure) so a label never costs a finished calculation.
                    "canonical_smiles": standard_smiles(member.smiles),
                    "first_seen_at": now,
                }
            )
        if member.structure_id:
            # The address, its compound and its electronic state; the coordinates ride whole in
            # `calculation_payload`.
            structures.append(
                {
                    "structure_id": member.structure_id,
                    "compound_id": member.compound_id or None,
                    # Never fabricated: where the payload says nothing these stay `None` ("not
                    # recorded"), never a neutral singlet.
                    "charge": member.charge,
                    "multiplicity": member.multiplicity,
                    "origin_calc_ref": "",
                    "created_at": now,
                }
            )
        members.append(
            {
                "subject_id": subject_id,
                "ordinal": member.ordinal,
                "role": member.role,
                "compound_id": member.compound_id or None,
                "structure_id": member.structure_id or None,
                "smiles": member.smiles,
                "stoichiometry": member.stoichiometry,
                "charge": member.charge,
                "multiplicity": member.multiplicity,
            }
        )

    # A conformer's geometry is referenced by `conformer.structure_id`, so its structure row has to
    # exist too — otherwise every ensemble write violates a foreign key the site did create.
    for conformer in record.conformers:
        structures.append(
            {
                "structure_id": conformer.structure_id,
                "compound_id": (record.subject.members[0].compound_id or None),
                "charge": conformer.charge,
                "multiplicity": conformer.multiplicity,
                "origin_calc_ref": record.calc_ref,
                "created_at": now,
            }
        )

    properties = []
    for fact in record.properties:
        # Canonicalization already happened in the projector; the registry lookup here is what
        # refuses an unregistered name before it reaches a foreign key that may not be enforced.
        unit = definition_for(fact.property).canonical_unit
        properties.append(
            {
                "value_id": _value_id(
                    record.calc_ref, fact.scope, fact.member_ordinal, fact.property
                ),
                "calc_ref": record.calc_ref,
                "property": fact.property,
                "scope_kind": fact.scope,
                "member_ordinal": fact.member_ordinal,
                "value_canonical": fact.value,
                "value_bool": fact.value_bool,
                "value_text": fact.value_text or None,
                # What the calculator said, in `reported_unit`; falls back to the canonical value
                # for a fact built without one, where the two are the same number.
                "reported_value": (
                    fact.value if fact.reported_value is None else fact.reported_value
                ),
                "reported_unit": fact.unit or unit,
                "uncertainty": fact.uncertainty,
                "uncertainty_kind": fact.uncertainty_kind,
                "in_domain": fact.in_domain,
                "subject_id": subject_id,
                "calc_type": record.calc_type,
                "method": method,
                "solvent_id": solvent_id,
                "temperature_k": conditions.temperature_k,
                "computed_at": record.computed_at,
            }
        )

    return {
        # Dimensions first: everything below references them.
        "solvent": (
            [{"solvent_id": solvent_id, "display_name": display_name(solvent_id), "smiles": ""}]
            if solvent_id
            else []
        ),
        "theory_level": [
            {
                "level_id": level.level_id,
                "method": method,
                "family": level.family,
                "basis_set": level.basis_set,
                "engine": level.engine,
                "treatment": level.treatment,
            }
        ],
        "condition_set": [
            {
                "condition_id": conditions.condition_id,
                "solvent_id": solvent_id,
                "solvent_model": conditions.solvent_model,
                "temperature_k": conditions.temperature_k,
                "pressure_pa": conditions.pressure_pa,
                "ph": conditions.ph,
                "charge": conditions.charge,
                "multiplicity": conditions.multiplicity,
            }
        ],
        "compound": compounds,
        "structure": structures,
        "subject": [
            {
                "subject_id": subject_id,
                "kind": record.subject.kind,
                "member_count": len(record.subject.members),
                "label": record.subject.label,
            }
        ],
        "subject_member": members,
        "calculation": [
            {
                "calc_ref": record.calc_ref,
                "calc_type": record.calc_type,
                "calc_version": record.calc_version,
                "input_hash": record.input_hash,
                "params_hash": record.params_hash,
                "subject_id": subject_id,
                "condition_id": conditions.condition_id,
                "level_id": level.level_id,
                "structure_id": record.structure_id,
                "provenance": record.provenance,
                "status": "valid",
                "compute_seconds": record.compute_seconds,
                "computed_at": record.computed_at,
                "writer_version": writer_version,
                "contract_version": record.contract_version,
                "ingested_at": now,
            }
        ],
        "calculation_payload": [
            {
                "calc_ref": record.calc_ref,
                "payload_kind": record.payload_kind,
                "payload": record.payload,
            }
        ],
        # One row even when the record names no publication (the normal case for primitives),
        # because a site's grants and row-level security attach to this table. The tenant is a
        # property of the writer and always known; the actor is not part of a calculation's identity
        # and stays empty.
        "calculation_publication": [
            {
                "calc_ref": record.calc_ref,
                "tenant_id": publication.tenant_id or tenant_id,
                "session_id": publication.session_id,
                "job_id": publication.job_id,
                "actor": publication.actor,
                "correlation_id": publication.correlation_id,
                "rationale": publication.rationale,
                "note_id": publication.note_id,
                "published_at": now,
            }
            for publication in (record.publications or [Publication()])
        ],
        "calculation_input": [
            {"calc_ref": record.calc_ref, "depends_on_calc_ref": parent, "role": ""}
            for parent in record.depends_on
        ],
        "property_value": properties,
        "calculation_site_value": [
            {
                "calc_ref": record.calc_ref,
                "atom_i": site.atom_i,
                "atom_j": site.atom_j,
                "property": site.property,
                "element": site.element,
                "value": site.value,
            }
            for site in record.sites
        ],
        "calculation_point_value": [
            {
                "calc_ref": record.calc_ref,
                "series": point.series,
                "ordinal": point.ordinal,
                "property": point.property,
                "value": point.value,
                "x_value": point.x_value,
                "x_unit": point.x_unit,
                "x_label": point.x_label,
                "structure_id": point.structure_id or None,
            }
            for point in record.points
        ],
        "conformer": [
            {
                "calc_ref": record.calc_ref,
                "ordinal": conformer.ordinal,
                "structure_id": conformer.structure_id,
                "energy_hartree": conformer.energy_hartree,
                "relative_kcal": conformer.relative_kcal,
                "population": conformer.population,
                "degeneracy": conformer.degeneracy,
            }
            for conformer in record.conformers
        ],
        "calculation_candidate": [
            {
                "calc_ref": record.calc_ref,
                "ordinal": candidate.ordinal,
                "candidate_kind": candidate.kind,
                "compound_id": candidate.compound_id or None,
                "smiles": candidate.smiles,
                "score": candidate.score,
                "score_property": candidate.score_property or None,
                "detail": candidate.detail,
            }
            for candidate in record.candidates
        ],
        "calculation_flag": [
            {
                "calc_ref": record.calc_ref,
                "ordinal": flag.ordinal,
                "flag": flag.flag,
                "severity": flag.severity,
                "message": flag.message,
                "detail": flag.detail,
            }
            for flag in record.flags
        ],
    }


# The primary key each table converges on. Written here rather than inferred, so an upsert cannot
# silently target the wrong columns after a schema change.
CONFLICT_KEYS: dict[str, tuple[str, ...]] = {
    "solvent": ("solvent_id",),
    "theory_level": ("level_id",),
    "condition_set": ("condition_id",),
    "compound": ("compound_id",),
    "structure": ("structure_id",),
    "subject": ("subject_id",),
    "subject_member": ("subject_id", "ordinal"),
    "calculation": ("calc_ref",),
    "calculation_payload": ("calc_ref",),
    "calculation_publication": ("calc_ref", "tenant_id", "session_id", "job_id"),
    "calculation_input": ("calc_ref", "depends_on_calc_ref", "role"),
    "property_value": ("value_id",),
    "calculation_site_value": ("calc_ref", "atom_i", "atom_j", "property"),
    "calculation_point_value": ("calc_ref", "series", "ordinal", "property"),
    "conformer": ("calc_ref", "ordinal"),
    "calculation_candidate": ("calc_ref", "ordinal"),
    "calculation_flag": ("calc_ref", "ordinal"),
}

# The columns whose *absence* changes what a row asserts, per table, as opposed to those whose
# absence merely records less.
#
# The store may lag this image's schema, so missing metadata columns are written down to. But a
# missing value, uncertainty or conflict-key column would produce a row that claims something
# false, so it is refused like a missing table (`SinkRejectedError` naming `sink_schema`). Only
# the four fact tables carry measurements.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "property_value": frozenset(
        {
            *CONFLICT_KEYS["property_value"],
            "calc_ref",
            "property",
            "scope_kind",
            # The value, in all three of the shapes a fact may take, plus what the calculator
            # actually said and the unit it said it in.
            "value_canonical",
            "value_bool",
            "value_text",
            "reported_value",
            "reported_unit",
            # The error bar and its kind travel together: `uncertainty_kind` without `uncertainty`
            # is a claim about a number the row does not hold.
            "uncertainty",
            "uncertainty_kind",
        }
    ),
    "calculation_site_value": frozenset({*CONFLICT_KEYS["calculation_site_value"], "value"}),
    "calculation_point_value": frozenset(
        {*CONFLICT_KEYS["calculation_point_value"], "value", "x_value", "x_unit"}
    ),
    "conformer": frozenset(
        {*CONFLICT_KEYS["conformer"], "energy_hartree", "relative_kcal", "population"}
    ),
}


# Columns a *blank* incoming value must never overwrite.
#
# `structure` is content-addressed, so rows sharing an id agree on the science and differ only in
# how much provenance the writer knew. A subject-member row knows only the address; a conformer row
# knows its origin calculation and electronic state. Whichever arrives second must not erase what
# the other knew, so a blank leaves the stored value alone. The blank for `charge` and
# `multiplicity` is `NULL`, because 0/1 is a real state that must be allowed to overwrite unknown.
PRESERVE_ON_BLANK: dict[str, tuple[str, ...]] = {
    "structure": (
        "origin_calc_ref",
        "compound_id",
        "charge",
        "multiplicity",
    ),
}

# Dependency order. A row is never written before what it references, which is what makes the write
# correct against a site that actually created the foreign keys.
TABLE_ORDER: tuple[str, ...] = (
    "solvent",
    "theory_level",
    "condition_set",
    "compound",
    "structure",
    "subject",
    "subject_member",
    "calculation",
    "calculation_payload",
    "calculation_publication",
    "calculation_input",
    "property_value",
    "calculation_site_value",
    "calculation_point_value",
    "conformer",
    "calculation_candidate",
    "calculation_flag",
)


# What "the writer did not know" looks like per column, for `PRESERVE_ON_BLANK`. Typed, because
# `NULLIF` compares values: an empty string is not a blank integer.
_BLANKS: dict[str, str] = {
    "origin_calc_ref": "''",
    "compound_id": "''",
    # With a `NULL` blank, `COALESCE(NULLIF(EXCLUDED.charge, NULL), structure.charge)` keeps a
    # stated value and leaves the stored one when nothing was stated.
    "charge": "NULL",
    "multiplicity": "NULL",
}


def upsert_statement(table: str, columns: Sequence[str], placeholder: str = "%s") -> str:
    """The Postgres-flavoured upsert for one table over `columns`.

    Identifiers are literals from `TABLE_ORDER` and the row builder, never caller-supplied; every
    value is bound.
    """
    keys = CONFLICT_KEYS[table]
    updatable = [column for column in columns if column not in keys]
    marks = ", ".join(placeholder for _ in columns)
    statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"
    if not updatable:
        # Every column is part of the key, so there is nothing an update could change: seeing the
        # row again is confirmation, not new information.
        return f"{statement} ON CONFLICT ({', '.join(keys)}) DO NOTHING"
    preserve = PRESERVE_ON_BLANK.get(table, ())
    assignments = ", ".join(
        # `NULLIF` collapses both forms of "the writer did not know" (SQL NULL and the builder's
        # blank), so either leaves the stored value alone. Only the content-addressed tables want
        # this.
        f"{column} = COALESCE(NULLIF(EXCLUDED.{column}, {_BLANKS[column]}), {table}.{column})"
        if column in preserve
        else f"{column} = EXCLUDED.{column}"
        for column in updatable
    )
    return f"{statement} ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"
