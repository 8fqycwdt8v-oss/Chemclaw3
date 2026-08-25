"""Turning a record into statements. Every value is bound; only fixed identifiers are written.

The rule this module holds, borrowed verbatim from `ingest/eln/warehouse/sql.py` because it earned
its place there: **the engine contributes structure, and everything else is a parameter.** The
difference is that the inbound engine takes its identifiers from a site's binding while this one
takes them from the schema *this repository ships* — so there is no path at all by which a value
reaches the statement text. Table and column names here are literals in this file.

**Upserts, because a redelivery must converge.** The outbox retries, and every primary key in the
shipped schema is a content hash, so writing the same record twice is a no-op rather than a
duplicate. Postgres spells that `ON CONFLICT ... DO UPDATE`; Snowflake and Oracle spell it `MERGE`.
The statements are built as text rather than through a query builder because there are a fixed
number of them, shaped by the schema and not by the caller — a builder would add a dependency and
an indirection to save nothing, and would make the exact string a test wants to assert harder to
see rather than easier.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from chemclaw.core.ids import stable_hash
from chemclaw.publish.properties import definition_for
from chemclaw.publish.record import ResultRecord
from chemclaw.publish.solvents import display_name

# One statement per table, in dependency order: a row is never written before the rows it
# references. Postgres and Snowflake both enforce that only if the site created the foreign keys,
# but the order is what makes the write correct where they did.
#
# `(table, columns, conflict_key)` — `conflict_key` is the primary key an upsert converges on, and
# an empty one means the table is append-only from this writer's side.
_Statement = tuple[str, tuple[str, ...], tuple[str, ...]]


def _value_id(calc_ref: str, scope: str, ordinal: int | None, prop: str) -> str:
    """The content address of one property fact — a hash, not a sequence.

    No sequences anywhere in this schema: they do not port to every target, and a derived key is
    what makes re-publishing the same record converge instead of appending.
    """
    return f"pv_{stable_hash([calc_ref, scope, ordinal, prop])}"


def rows_for(
    record: ResultRecord, *, tenant_id: str, writer_version: str
) -> dict[str, list[dict[str, Any]]]:
    """Every row one record contributes, keyed by table and in dependency order.

    Returned as plain dicts rather than tuples so a driver that speaks JSON (an HTTP endpoint) and
    one that speaks SQL can share the projection — the alternative is two row builders that agree
    today and diverge on the next column.
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
                    "canonical_smiles": member.smiles,
                    "first_seen_at": now,
                }
            )
        if member.structure_id:
            structures.append(
                {
                    "structure_id": member.structure_id,
                    "compound_id": member.compound_id or None,
                    "atom_count": 0,
                    "charge": member.charge or 0,
                    "multiplicity": member.multiplicity or 1,
                    "origin_calc_ref": "",
                    "geometry": {},
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
                "atom_count": 0,
                "charge": 0,
                "multiplicity": 1,
                "origin_calc_ref": record.calc_ref,
                "geometry": {},
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
                "reported_value": fact.value,
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
        "calculation_publication": [
            {
                "calc_ref": record.calc_ref,
                "tenant_id": publication.tenant_id or tenant_id,
                "session_id": publication.session_id,
                "job_id": publication.job_id,
                "actor": publication.actor,
                "correlation_id": publication.correlation_id,
                "rationale": publication.rationale,
                "published_at": now,
            }
            for publication in (record.publications or [])
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
        "calculation_artifact": [
            {
                "calc_ref": record.calc_ref,
                "name": artifact.name,
                "media_type": artifact.media_type,
                "byte_size": artifact.byte_size,
                "content_hash": artifact.content_hash,
            }
            for artifact in record.artifacts
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
    "calculation_artifact": ("calc_ref", "name"),
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
    "calculation_artifact",
)


def upsert_statement(table: str, columns: Sequence[str], placeholder: str = "%s") -> str:
    """The Postgres-flavoured upsert for one table over `columns`.

    Identifiers are literals from `TABLE_ORDER` and the row builder above — never anything a caller
    supplied — so the only thing interpolated is structure. Every value is bound.
    """
    keys = CONFLICT_KEYS[table]
    updatable = [column for column in columns if column not in keys]
    marks = ", ".join(placeholder for _ in columns)
    statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"
    if not updatable:
        # Every column is part of the key, so there is nothing an update could change: seeing the
        # row again is confirmation, not new information.
        return f"{statement} ON CONFLICT ({', '.join(keys)}) DO NOTHING"
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in updatable)
    return f"{statement} ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"
