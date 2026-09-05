"""Turning a record into statements. Every value is bound; only fixed identifiers are written.

The rule this module holds, borrowed verbatim from `ingest/eln/warehouse/sql.py` because it earned
its place there: **the engine contributes structure, and everything else is a parameter.** The
difference is that the inbound engine takes its identifiers from a site's binding while this one
takes them from the schema *this repository ships* — so there is no path at all by which a value
reaches the statement text. Table and column names here are literals in this file.

**Upserts, because a redelivery must converge.** The outbox retries, and every primary key in the
shipped schema is a content hash, so writing the same record twice is a no-op rather than a
duplicate.

**This emits Postgres, and only Postgres.** `ON CONFLICT ... DO UPDATE` is a Postgres spelling;
Snowflake and Oracle spell the same idea `MERGE`, and there is no `MERGE` emitter here. An earlier
version of this paragraph named all three, which read as though the SQL driver already spoke them —
it does not. What is portable today is the *schema*: `schema/result-store/` avoids arrays,
sequences and expression indexes precisely so a site can create it on another engine, and adding
that engine is then this module plus the driver's `information_schema` probe.
Until someone asks for one, saying so plainly is better than a sentence that has to be tested to
be disbelieved.

The statements are built as text rather than through a query builder because there are a fixed
number of them, shaped by the schema and not by the caller — a builder would add a dependency and
an indirection to save nothing, and would make the exact string a test wants to assert harder to
see rather than easier.
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
                    # **The structure the key was derived from, not the species that carried it.**
                    # `compound_id` is a hash over the *standardized* SMILES, so every tautomer,
                    # microstate and protonation state of one substance writes this same row —
                    # and with the member's own SMILES in the value column, the upsert's
                    # `DO UPDATE` left the row reading whichever species happened to publish last.
                    # The two columns of one row named two different molecules.
                    #
                    # `standard_smiles` rather than the strict form: a record built outside
                    # `project` (a backfill, a future producer) can carry a SMILES this cannot
                    # parse, and losing a finished calculation to normalize a label would be the
                    # wrong trade — the lenient helper returns the input unchanged, which is what
                    # the row would have said anyway.
                    "canonical_smiles": standard_smiles(member.smiles),
                    "first_seen_at": now,
                }
            )
        if member.structure_id:
            structures.append(
                {
                    "structure_id": member.structure_id,
                    "compound_id": member.compound_id or None,
                    "atom_count": 0,
                    # **Never fabricated.** These were `or 0` and `or 1` while no projector set
                    # either, so every anion and every radical this writer published was recorded
                    # as a neutral closed-shell singlet — and the values are not unknowable: they
                    # are inside the `structure_id` hash and are in the payload wherever the
                    # geometry itself is. Where the payload says nothing they stay `None`, which
                    # reads as "not recorded" rather than as a state nobody computed.
                    "charge": member.charge,
                    "multiplicity": member.multiplicity,
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
                "charge": conformer.charge,
                "multiplicity": conformer.multiplicity,
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
                # What the calculator said, in `reported_unit`. Falls back to the canonical value
                # for a fact built without one (a boolean, a coded string, or a `PropertyFact`
                # constructed outside `project._fact`), where the two are the same number.
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
        # **One row even when the record names no publication**, which is the *normal* case: the
        # cache hook and the backfill construct none, so every primitive — the overwhelming
        # majority of the corpus — used to publish no publication row at all. The shipped DDL says
        # a site's grants and row-level security attach to this table rather than to `calculation`,
        # so following that advice hid every primitive, and the manifest's `tenant_id` (there so
        # one shared results database can hold two deployments' output without their provenance
        # merging) was unreachable for them. The tenant is a property of the *writer* and is the
        # one field always knowable at write time; the actor is not, and stays empty rather than
        # being guessed — a calculation's identity excludes who asked for it.
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

# Columns a *blank* incoming value must never overwrite.
#
# `structure` is content-addressed: `structure_id` is a hash of the geometry, so two rows with one
# id agree about the science and can differ only in how much of the surrounding provenance the
# writer happened to know. Two builders below emit these rows -- a subject member, which knows the
# id and nothing else, and a conformer, which knows which calculation produced the geometry -- and
# the plain `DO UPDATE SET col = EXCLUDED.col` let whichever arrived second win. A member row
# landing after a conformer row therefore blanked a real `origin_calc_ref`.
#
# `DO NOTHING` would have been the wrong repair: it fixes that ordering and breaks the mirror one,
# where the member row lands first and the real origin never gets written. What is actually wanted
# is that a writer who does not know a fact cannot erase it from one who does, which is what this
# says. A member row is not given `record.calc_ref` instead, because the geometry a calculation
# *ran on* is its input -- claiming this calculation produced it would be a false provenance.
# `charge` and `multiplicity` are here for the same reason and are the case that makes the rule
# load-bearing rather than tidy: a subject member usually knows only the address, while a conformer
# knows the state its search computed at, so a member row landing second would blank a real charge —
# and a blanked charge is not a missing field but a *wrong* one, since 0/1 is a state a query
# matches. Their blank is `NULL` rather than `0`/`1`, because a neutral closed-shell singlet is a
# real answer that must be allowed to overwrite an unknown.
PRESERVE_ON_BLANK: dict[str, tuple[str, ...]] = {
    "structure": (
        "origin_calc_ref",
        "compound_id",
        "atom_count",
        "geometry",
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
# `NULLIF` compares values: an empty string is not a blank integer and neither is an empty object.
_BLANKS: dict[str, str] = {
    "origin_calc_ref": "''",
    "compound_id": "''",
    "atom_count": "0",
    "geometry": "'{}'::jsonb",
    # `NULLIF(x, NULL)` is `x` (the comparison is NULL, so the CASE takes its ELSE branch), and
    # `NULLIF(NULL, NULL)` is NULL — so the generated `COALESCE(NULLIF(EXCLUDED.charge, NULL),
    # structure.charge)` keeps a stated value and leaves the stored one alone when nothing was
    # stated. Spelled through the same generator as the other four rather than special-cased.
    "charge": "NULL",
    "multiplicity": "NULL",
}


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
    preserve = PRESERVE_ON_BLANK.get(table, ())
    assignments = ", ".join(
        # `NULLIF(…, '')` collapses the two ways "the writer did not know" arrives -- SQL NULL and
        # the empty string/zero/`{}` the row builders use -- so either one leaves the stored value
        # alone. Written per column rather than per table because only the content-addressed
        # tables want it; everywhere else a later write is genuinely newer information.
        f"{column} = COALESCE(NULLIF(EXCLUDED.{column}, {_BLANKS[column]}), {table}.{column})"
        if column in preserve
        else f"{column} = EXCLUDED.{column}"
        for column in updatable
    )
    return f"{statement} ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {assignments}"
