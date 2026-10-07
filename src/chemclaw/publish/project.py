"""Turning a calculator's own result model into the canonical published record.

The only place that knows both vocabularies. Every projector holds three properties:

- **A number is never guessed.** Absent stays absent (e.g. `delta_g_kcal` is `None` at
  `quick` level and is never replaced by `delta_e_kcal`); there is no fallback here.
- **A unit is stated, never assumed.** Every fact goes through `properties.to_canonical`, which
  refuses a unit it cannot convert.
- **The payload rides along untouched**, so a projector bug is a re-projection rather than lost
  science. Only `_hessian` narrows it (see `project`).

Geometries are not copied into the record: a `Structure` appears as its `structure_id`.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from chemclaw.core.chem import canonical_smiles, compound_id
from chemclaw.publish.properties import definition_for, to_canonical
from chemclaw.publish.record import (
    CandidateFact,
    Conditions,
    ConformerFact,
    FlagFact,
    PointFact,
    PropertyFact,
    ResultRecord,
    SiteFact,
    Subject,
    SubjectMember,
    TheoryLevel,
)
from chemclaw.publish.solvents import canonical_solvent

logger = logging.getLogger(__name__)


class ProjectionError(ValueError):
    """A payload could not be projected into a record.

    A `ValueError`, so `durable/publish.py` marks it non-retryable: the fix is code.
    """


def _identify(smiles: str | None) -> tuple[str, str]:
    """`(compound_id, canonical_smiles)` for a SMILES, or two empty strings.

    `core.chem.compound_id` is the join key the knowledge graph and fingerprint search already use.
    An unparseable SMILES degrades to empty rather than raising: a label must not cost a finished
    calculation.
    """
    if not smiles:
        return "", ""
    try:
        return compound_id(smiles), canonical_smiles(smiles)
    except Exception:
        logger.warning(
            "publish: could not canonicalize %r; publishing without a compound id", smiles
        )
        return "", smiles


def _molecule(smiles: str | None, structure_id: str = "") -> SubjectMember:
    """The single member of a one-molecule or one-geometry subject.

    `role` is always `"subject"`; members with other roles come from `_species_members`.
    """
    identifier, canonical = _identify(smiles)
    return SubjectMember(
        ordinal=0,
        role="subject",
        compound_id=identifier,
        smiles=canonical,
        structure_id=structure_id,
    )


def _state(structure: dict[str, Any]) -> tuple[int | None, int | None]:
    """The electronic state a geometry payload states, `(charge, multiplicity)`, or two Nones.

    Absent stays absent: `0`/`1` is a real state a query matches, so a bare `structure_id` must
    not become a neutral singlet. A whole `Structure` dump carries both fields.
    """
    charge = structure.get("charge")
    multiplicity = structure.get("multiplicity")
    return (
        None if charge is None else int(charge),
        None if multiplicity is None else int(multiplicity),
    )


def _species_members(reactants: list[str], products: list[str]) -> list[SubjectMember]:
    """A reaction's members, one per stoichiometric equivalent.

    The tools list a species once per equivalent (`["O", "O"]` for two waters), so this is N
    members at stoichiometry 1. Ordinals match `ReactionEnergyResult.species`'s order.
    """
    members: list[SubjectMember] = []
    for smiles in reactants:
        identifier, canonical = _identify(smiles)
        members.append(
            SubjectMember(
                ordinal=len(members), role="reactant", compound_id=identifier, smiles=canonical
            )
        )
    for smiles in products:
        identifier, canonical = _identify(smiles)
        members.append(
            SubjectMember(
                ordinal=len(members), role="product", compound_id=identifier, smiles=canonical
            )
        )
    return members


def _member_for(
    members: list[SubjectMember], species: dict[str, Any], claimed: set[int]
) -> int | None:
    """The ordinal of the member a `SpeciesEnergy` describes, or None if it matches none.

    Matched on `(role, molecule)`, not list position, and one-to-one via `claimed`: a species
    appearing twice is two members, and handing both copies the same ordinal would collide their
    `value_id`s and silently drop a fact. The molecule is the member's exact SMILES, never
    `compound_id`, which standardizes and would conflate tautomers or an acid and its conjugate
    base. `_species_members` always sets the SMILES when it sets an id, so there is no id fallback.
    """
    canonical = _identify(species.get("smiles"))[1]
    role = species.get("role")
    for member in members:
        if member.role != role or member.ordinal in claimed:
            continue
        if member.smiles and member.smiles == canonical:
            return member.ordinal
    return None


def _reaction_label(reactants: list[str], products: list[str]) -> str:
    """A reaction SMILES, so a published row is legible without joining to its members."""
    return f"{'.'.join(reactants)}>>{'.'.join(products)}"


def _fact(
    name: str,
    value: float | None,
    unit: str,
    *,
    uncertainty: float | None = None,
    uncertainty_kind: str = "",
    member: int | None = None,
) -> PropertyFact | None:
    """One numeric fact, canonicalized, or None when the calculator did not produce the value.

    Returning None rather than substituting is this module's discipline; callers filter Nones out.
    `value` is canonical (the only unit conversion on the publish path, so `value_canonical`
    predicates are sound) and `reported_value` keeps what the calculator said, so a wrong conversion
    is recoverable. `tests/test_publish_projection.py` asserts the conversion is exercised on a live
    path.
    """
    if value is None:
        return None
    return PropertyFact(
        property=name,
        value=to_canonical(name, value, unit),
        unit=unit,
        reported_value=value,
        uncertainty=uncertainty,
        uncertainty_kind=uncertainty_kind,
        scope="member" if member is not None else "calculation",
        member_ordinal=member,
    )


def _text(name: str, value: str | None, *, member: int | None = None) -> PropertyFact | None:
    """One coded-text fact, or None when the calculator recorded nothing."""
    if not value:
        return None
    return PropertyFact(
        property=name,
        value_text=str(value),
        scope="member" if member is not None else "calculation",
        member_ordinal=member,
    )


def _flag(name: str, value: bool | None, *, member: int | None = None) -> PropertyFact | None:
    """One boolean fact, or None when the calculator did not decide it."""
    if value is None:
        return None
    return PropertyFact(
        property=name,
        value_bool=value,
        scope="member" if member is not None else "calculation",
        member_ordinal=member,
    )


def _kept(*facts: PropertyFact | None) -> list[PropertyFact]:
    """Drop the Nones — the one place absence is turned into an omitted row."""
    return [fact for fact in facts if fact is not None]


def _facts_belong_in_the_scalar_table(facts: list[PropertyFact]) -> None:
    """Refuse a projection that wrote a quantity the registry declares for another table.

    Compares each fact against `definition_for(name).scope_kind`, so a per-atom, per-point or
    per-conformer quantity cannot be stored as a scalar. Here rather than on `PropertyFact`, because
    a mismatch is a projection bug and the model also parses already-queued documents. `calculation`
    covers both of the scalar table's row scopes (calculation and member).
    """
    for fact in facts:
        declared = definition_for(fact.property).scope_kind
        if declared != "calculation":
            raise ProjectionError(
                f"property {fact.property!r} is registered at {declared!r} scope, so its values "
                f"belong in that table rather than in `property_value` — see "
                "`publish.properties.ScopeKind`"
            )


def _warnings(messages: list[str]) -> list[FlagFact]:
    """A calculator's own warnings, as flags rather than as prose nobody queries."""
    return [
        FlagFact(ordinal=index, flag="calculator_warning", severity="warning", message=message)
        for index, message in enumerate(messages)
    ]


def _not_computed(
    failed: list[dict[str, Any]], flag: str, label: Callable[[dict[str, Any]], str], start: int
) -> list[FlagFact]:
    """One flag per item a screen could not compute, so "which screens are partial" is a query.

    The message is the item's name; the server's reason rides in the JSONB `detail`, since a long
    reason in the bounded `message` column could fail the whole record at the sink.
    """
    return [
        FlagFact(
            ordinal=start + index,
            flag=flag,
            severity="warning",
            # A time-budget stop says so in the message too, because it is the one cause a reader
            # must not take as a property of the item: the same item may pass on an idle pod.
            message=(
                f"{label(entry)} was stopped by the calculation service's time budget"
                if entry.get("cause") == "time_budget"
                else f"{label(entry)} could not be computed"
            ),
            detail=dict(entry),
        )
        for index, entry in enumerate(failed)
    ]


def _medium_label(entry: dict[str, Any]) -> str:
    """A failed medium's name: its solvent, or the gas phase (`solvent` is absent on the wire)."""
    return str(entry.get("solvent") or "gas phase")


def _bond_label(entry: dict[str, Any]) -> str:
    """A failed bond's name with its atoms: "C-H" alone is ambiguous in most molecules."""
    return f"{entry.get('bond') or 'bond'} {list(entry.get('atoms') or [])}"


def _renamed(payload: dict[str, Any], current: str, legacy: str, what: str) -> Any:
    """A payload field read under its current name, falling back to the name it used to have.

    Only for a pure rename, where the same expression was assigned to a new name:
    `RefinedEnsemble`'s entropy and ensemble correction gained a `refined_` prefix with no change to
    the arithmetic. A rename where the quantity also moved must refuse the row instead. Reachable
    only through `payload_kind="RefinedEnsemble"`, so the legacy name here is always the refined
    subset's own value.
    """
    value = payload.get(current)
    if value is not None:
        return value
    value = payload.get(legacy)
    if value is not None:
        # A log line rather than a flag: the published value is identical, so only an operator
        # running a backfill over a legacy corpus needs to know.
        logger.warning(
            "publish: %s read from the legacy field %r (now %r) for %r; the arithmetic is "
            "unchanged, only the name",
            what,
            legacy,
            current,
            payload.get("smiles") or payload.get("structure_id") or "<unlabelled>",
        )
    return value


# --- the projectors, one per result model -------------------------------------------------------
#
# Each takes `model_dump(mode="json")` rather than the model, so the backfill path (dicts from
# `calculation_results` and `job_records`) and the live path run the same projector, and this
# module does not depend on shapes that cross a Temporal wire. A field read only under its new
# name silently drops on older payloads; `_renamed` is the one place a legacy name is read.


def _reaction(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """The subject, conditions, level and facts of a reaction energy."""
    reactants = list(payload.get("reactants") or [])
    products = list(payload.get("products") or [])
    if not reactants or not products:
        raise ProjectionError("a reaction payload must name reactants and products")
    members = _species_members(reactants, products)
    subject = Subject(kind="reaction", members=members, label=_reaction_label(reactants, products))
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment=payload.get("conformer_treatment") or "",
    )
    uncertainty = payload.get("uncertainty_kcal")
    facts = _kept(
        _fact(
            "reaction_delta_e",
            payload.get("delta_e_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported",
        ),
        # None at `quick` level and whenever a symmetry number was unstated; never substituted with
        # delta_e.
        _fact(
            "reaction_delta_h",
            payload.get("delta_h_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported",
        ),
        _fact(
            "reaction_delta_g",
            payload.get("delta_g_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported",
        ),
        _fact("cache_hits", payload.get("cache_hits"), ""),
        _flag("is_strongly_exothermic", payload.get("is_strongly_exothermic")),
        # The threshold beside the flag it produced: `exotherm_threshold_kcal` is a deployment
        # setting, so a stored boolean with no threshold cannot be re-read after someone changes it.
        _fact("exotherm_threshold", payload.get("exotherm_threshold_kcal"), "kcal/mol"),
        _text("reaction_level", payload.get("level")),
        # Beside `reaction_delta_g`: a free energy is unusable without its reference state (1 mol/L
        # vs 1 atm differ by 1.894*dn kcal/mol).
        _text("standard_state", payload.get("standard_state")),
        _text("conformer_treatment", payload.get("conformer_treatment")),
    )
    # The per-species breakdown, matched by (role, molecule), never by list position: the two lists
    # are produced independently, and zipping by index would attach a product's energy to a
    # reactant.
    claimed: set[int] = set()
    for species in payload.get("species") or []:
        index = _member_for(subject.members, species, claimed)
        if index is None:
            logger.warning(
                "publish: species %r (%s) matches no member of %s; energies not published",
                species.get("smiles"),
                species.get("role"),
                subject.label,
            )
            continue
        claimed.add(index)
        facts.extend(
            _kept(
                _fact(
                    "electronic_energy",
                    species.get("electronic_energy_hartree"),
                    "hartree",
                    member=index,
                ),
                _fact("enthalpy", species.get("enthalpy_hartree"), "hartree", member=index),
                _fact(
                    "gibbs_free_energy",
                    species.get("gibbs_free_energy_hartree"),
                    "hartree",
                    member=index,
                ),
                _fact("symmetry_number", species.get("symmetry_number"), "", member=index),
                _fact(
                    "conformational_entropy_correction",
                    species.get("conformational_entropy_kcal"),
                    "kcal/mol",
                    member=index,
                ),
                _flag("is_minimum", species.get("is_minimum"), member=index),
            )
        )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "flags": _warnings(list(payload.get("warnings") or [])),
        },
    )


def _solvent_screen(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A solvent comparison: the aggregate only; its parts publish as their own records.

    Never store an aggregate whose parts are not also stored. The per-solvent energies publish as
    reaction records at their own `Conditions` (see `records_from_solvent_screen`), so cross-solvent
    questions answer over every solvent run, screened together or not.
    """
    reactants = list(payload.get("reactants") or [])
    products = list(payload.get("products") or [])
    subject = Subject(
        kind="reaction",
        members=_species_members(reactants, products),
        label=_reaction_label(reactants, products),
    )
    # No solvent on the comparison itself: it is *about* the solvents rather than run in one.
    conditions = Conditions(temperature_k=payload.get("temperature_k"))
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment=payload.get("level") or "",
    )
    # A spread and a winner describe a comparison: over one medium the spread is zero by
    # construction, and over a partial screen they are only bounds (a failed medium may be the
    # best). Both are read either way so the field guard sees them consumed.
    spread, best = payload.get("spread_kcal"), payload.get("best_solvent")
    compared = len(payload.get("effects") or []) >= 2 and not payload.get("failed")
    facts = _kept(
        _fact(
            "solvent_spread",
            spread if compared else None,
            "kcal/mol",
            uncertainty=payload.get("uncertainty_kcal"),
            uncertainty_kind="reported",
        ),
        _text("best_solvent", canonical_solvent(best) if compared else None),
        _text("reaction_level", payload.get("level")),
    )
    flags = _warnings(list(payload.get("warnings") or []))
    flags += _not_computed(
        list(payload.get("failed") or []), "medium_not_computed", _medium_label, len(flags)
    )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "flags": flags,
        },
    )


def _species_solvent_screen(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """One species set ranked across media: the aggregate; each medium publishes its own record.

    Same rule as `_solvent_screen`. This record carries only the comparison: the largest swing and
    whether the dominant form reordered. `dominance_changes` is a warning flag, not a property: it
    tells a reader that "the compound" means a different species per medium. Subject kind and
    vocabulary match `_species_distribution` so the aggregate joins its parts.
    """
    distributions = list(payload.get("distributions") or [])
    first = distributions[0] if distributions else {}
    species = list(first.get("species") or [])
    subject = Subject(
        kind="system",
        members=[
            SubjectMember(
                ordinal=index,
                role="subject",
                compound_id=_identify(entry.get("smiles"))[0],
                smiles=_identify(entry.get("smiles"))[1],
            )
            for index, entry in enumerate(species)
        ],
        label=f"{payload.get('kind') or 'custom'} across {len(distributions)} media",
    )
    # No solvent on the comparison itself: it is *about* the media rather than run in one — the
    # same reason `_solvent_screen` leaves it off.
    conditions = Conditions(temperature_k=payload.get("temperature_k"))
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment=payload.get("level") or "",
    )
    # The swing is a comparison's finding, so one medium publishes none, and a partial screen none
    # either — over the media computed it is a lower bound: `_solvent_screen`'s two reasons.
    swing = payload.get("largest_swing_kcal")
    compared = len(distributions) >= 2 and not payload.get("failed")
    facts = _kept(
        _fact(
            "solvent_swing",
            swing if compared else None,
            "kcal/mol",
            uncertainty=payload.get("uncertainty_kcal"),
            uncertainty_kind="reported",
        ),
        _fact("media_compared", float(len(distributions)) if distributions else None, ""),
        _text("distribution_kind", payload.get("kind")),
        _text("reaction_level", payload.get("level")),
    )
    flags = _warnings(list(payload.get("warnings") or []))
    flags += _not_computed(
        list(payload.get("failed") or []), "medium_not_computed", _medium_label, len(flags)
    )
    if payload.get("dominance_changes"):
        flags.append(
            FlagFact(
                ordinal=len(flags),
                flag="dominance_changes_with_medium",
                severity="warning",
                message=(
                    "the most populated species is not the same in every medium, so any property "
                    "computed for 'the compound' describes a different form depending on solvent"
                ),
            )
        )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "flags": flags,
        },
    )


def _ensemble(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A conformer ensemble: one subject, N conformer rows.

    The subject is the molecule searched; members are outputs, each published as a `structure_id`,
    never coordinates. `population` is carried when present, with its `temperature_k` on the
    conditions.
    """
    # The subject is the search seed: `ConformerEnsemble` carries `smiles`, the cached
    # `EnsemblePayload` only `structure_id`; both are read so one projector serves both shapes.
    smiles = payload.get("smiles")
    seed_structure_id = payload.get("structure_id") or ""
    subject = Subject(
        kind="ensemble",
        members=[_molecule(smiles, seed_structure_id)],
        label=smiles or seed_structure_id,
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="crest",
        treatment=payload.get("treatment") or "",
    )
    conformers: list[ConformerFact] = []
    for index, member in enumerate(payload.get("conformers") or payload.get("members") or []):
        structure = member.get("structure") or {}
        structure_id = structure.get("structure_id") or member.get("structure_id") or ""
        if not structure_id:
            # A member with no address cannot be referred to later, which is the whole point of
            # publishing an ensemble. Skipped loudly rather than stored unreachable.
            logger.warning("publish: ensemble member %d has no structure_id; skipped", index)
            continue
        # The cached shape carries `energy_hartree`; the returned shape carries `relative_kcal` and
        # `population`. Both are read optionally; `ConformerFact` refuses a member with neither.
        energy = member.get("energy_hartree")
        relative = member.get("relative_kcal")
        if energy is None and relative is None:
            logger.warning("publish: ensemble member %d has no energy; skipped", index)
            continue
        charge, multiplicity = _state(structure)
        conformers.append(
            ConformerFact(
                ordinal=index,
                structure_id=structure_id,
                charge=charge,
                multiplicity=multiplicity,
                energy_hartree=None if energy is None else float(energy),
                relative_kcal=None if relative is None else float(relative),
                population=member.get("population"),
                degeneracy=int(member.get("degeneracy", 1)),
            )
        )
    facts = _kept(
        _fact("total_conformers", payload.get("total_found"), ""),
        _fact(
            "conformational_entropy",
            payload.get("conformational_entropy_cal_per_mol_k"),
            "cal/(mol*K)",
        ),
        _fact("ensemble_correction", payload.get("ensemble_correction_kcal"), "kcal/mol"),
        _text("search_kind", payload.get("search")),
        _text("search_effort", payload.get("effort")),
        _text("conformer_treatment", payload.get("treatment")),
    )
    return subject, conditions, level, {"properties": facts, "conformers": conformers}


def _refined_ensemble(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A conformer ensemble re-weighted by free energy over its top N members.

    Shares `_ensemble`'s subject and conformer rows but publishes `refined_*` property names: these
    are computed over the refined subset, and sharing the ensemble-wide names would put two meanings
    in one column. Legacy payloads are read through `_renamed`.

    `energy_hartree` is the electronic energy (comparable across both ensemble shapes); the
    G-weighting is expressed by `relative_kcal`, `population` and `TheoryLevel.treatment`. The
    per-member absolute G has no column and is not published.
    """
    smiles = payload.get("smiles")
    subject = Subject(kind="ensemble", members=[_molecule(smiles)], label=smiles or "")
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="crest",
        treatment=payload.get("treatment") or "",
    )
    conformers: list[ConformerFact] = []
    for index, member in enumerate(payload.get("conformers") or []):
        structure = member.get("structure") or {}
        structure_id = structure.get("structure_id") or ""
        if not structure_id:
            logger.warning("publish: refined member %d has no structure_id; skipped", index)
            continue
        charge, multiplicity = _state(structure)
        conformers.append(
            ConformerFact(
                ordinal=index,
                structure_id=structure_id,
                charge=charge,
                multiplicity=multiplicity,
                energy_hartree=member.get("electronic_energy_hartree"),
                relative_kcal=member.get("relative_kcal"),
                population=member.get("population"),
                degeneracy=int(member.get("degeneracy", 1)),
            )
        )
    facts = _kept(
        _fact("total_conformers", payload.get("total_found"), ""),
        _fact("refined_conformers", payload.get("refined_count"), ""),
        _fact("refined_population_covered", payload.get("refined_population_covered"), ""),
        _fact(
            "refined_conformational_entropy",
            _renamed(
                payload,
                "refined_conformational_entropy_cal_per_mol_k",
                "conformational_entropy_cal_per_mol_k",
                "the refined ensemble's conformational entropy",
            ),
            "cal/(mol*K)",
        ),
        _fact(
            "refined_ensemble_correction",
            _renamed(
                payload,
                "refined_ensemble_correction_kcal",
                "ensemble_correction_kcal",
                "the refined ensemble's correction",
            ),
            "kcal/mol",
        ),
        _text("conformer_treatment", payload.get("treatment")),
    )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "conformers": conformers,
            "flags": _warnings(list(payload.get("warnings") or [])),
        },
    )


# Which registered property an ensemble average is *of*, by the job's own property name, so an
# averaged value lands on the same name a single-point calculation does. Per-atom entries map to a
# site property, hence the scope in each entry.
_AVERAGED_PROPERTIES: dict[str, tuple[str, str, str]] = {
    # asked-for name -> (registered property, unit, scope)
    "dipole_debye": ("dipole", "debye", "calculation"),
    "homo_ev": ("homo", "ev", "calculation"),
    "lumo_ev": ("lumo", "ev", "calculation"),
    "gap_ev": ("homo_lumo_gap", "ev", "calculation"),
    "charges": ("partial_charge", "e", "site"),
    "fukui": ("fukui_zero", "", "site"),
}


def _ensemble_property(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """One property, Boltzmann-averaged over a conformer ensemble.

    Lands on the same registered name as a single-point value, with `TheoryLevel.treatment` and
    `members_averaged` saying an average was taken. The spread is not published: it is in each
    property's own unit, so one registered `property_spread` would have no canonical unit.
    `population_covered` says how much of the ensemble backs the mean.
    """
    smiles = payload.get("smiles")
    subject = Subject(kind="ensemble", members=[_molecule(smiles)], label=smiles or "")
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment="boltzmann-averaged",
    )
    asked = str(payload.get("property_name") or "")
    mapped = _AVERAGED_PROPERTIES.get(asked)
    if mapped is None:
        # An unregistered property is refused, not stored under the tool's vocabulary where nobody
        # could find it.
        raise ProjectionError(
            f"ensemble average of {asked!r} has no registered property; add it to "
            "`_AVERAGED_PROPERTIES` and to `publish.properties`"
        )
    name, unit, scope = mapped
    facts = _kept(
        _fact("members_averaged", payload.get("members_averaged"), ""),
        _fact("total_conformers", payload.get("total_found"), ""),
        _fact("population_covered", payload.get("population_covered"), ""),
    )
    sites: list[SiteFact] = []
    value = payload.get("value") or {}
    if scope == "calculation" and value.get("mean") is not None:
        fact = _fact(name, value["mean"], unit)
        if fact is not None:
            facts.append(fact)
    for atom in payload.get("per_atom") or []:
        mean = (atom.get("value") or {}).get("mean")
        if mean is None:
            continue
        sites.append(
            SiteFact(
                atom_i=int(atom.get("index", 0)),
                element=str(atom.get("element") or ""),
                property=name,
                value=to_canonical(name, float(mean), unit),
            )
        )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "sites": sites,
            "flags": _warnings(list(payload.get("warnings") or [])),
        },
    )


def _species_distribution(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A ranked population over related species: tautomers, microstates, stereoisomers.

    The species are `CandidateFact` outputs (an open-ended ranked set), not subject members. The
    subject is the enumeration itself, as a `system`, so a compound's tautomer set never collides
    with the compound.
    """
    species = list(payload.get("species") or [])
    if not species:
        raise ProjectionError("a species distribution with no species has nothing to publish")
    members = [
        SubjectMember(
            ordinal=index,
            role="subject",
            compound_id=_identify(item.get("smiles"))[0],
            smiles=_identify(item.get("smiles"))[1],
            structure_id=str(item.get("structure_id") or ""),
        )
        for index, item in enumerate(species)
    ]
    subject = Subject(kind="system", members=members, label=str(payload.get("kind") or ""))
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment="boltzmann-populated",
    )
    uncertainty = payload.get("uncertainty_kcal")
    candidates = [
        CandidateFact(
            ordinal=index,
            kind="compound",
            smiles=_identify(item.get("smiles"))[1],
            compound_id=_identify(item.get("smiles"))[0],
            score=item.get("population"),
            # A *species* population: `population` is conformer-scoped and belongs to the
            # `conformer` table.
            score_property="species_population",
            # The tool's extra fields, verbatim and never a predicate: relative energy, label,
            # conformer count.
            detail={
                "relative_kcal": item.get("relative_kcal"),
                "label": item.get("label") or "",
                "structure_id": item.get("structure_id") or "",
                "conformers_found": item.get("conformers_found", 0),
                "electronic_energy_hartree": item.get("electronic_energy_hartree"),
                "gibbs_free_energy_hartree": item.get("gibbs_free_energy_hartree"),
            },
        )
        for index, item in enumerate(species)
    ]
    facts = _kept(
        _fact("species_enumerated", payload.get("enumerated"), ""),
        _text("distribution_kind", payload.get("kind")),
        _text("reaction_level", payload.get("level")),
        # The gap to the runner-up, not the winner's own relative energy (always 0.0 by
        # construction). That gap is what the ranking rests on. Absent for a set of one: nothing was
        # ranked.
        _fact(
            "species_gap",
            species[1].get("relative_kcal") if len(species) > 1 else None,
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported" if uncertainty is not None else "",
        ),
    )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "candidates": candidates,
            "flags": _warnings(list(payload.get("warnings") or [])),
        },
    )


def _bond_survey(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """Bond dissociation energies across one molecule's breakable bonds.

    Each bond is a `SiteFact` pair (`atom_j >= 0`), not a scalar per bond, so bond energies stay out
    of the scalar table's index. The weakest bond is also a calculation-scope fact, since "which
    bond breaks first" is the question the survey answers.
    """
    smiles = payload.get("smiles")
    if not smiles:
        raise ProjectionError("a bond survey with no subject SMILES has nothing to publish")
    subject = Subject(kind="molecule", members=[_molecule(smiles)], label=str(smiles))
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment=str(payload.get("mode") or ""),
    )
    sites: list[SiteFact] = []
    weakest: dict[str, Any] | None = None
    for bond in payload.get("bonds") or []:
        atoms = list(bond.get("atoms") or [])
        energy = bond.get("dissociation_energy_kcal")
        if len(atoms) != 2 or energy is None:
            # A bond that names anything other than its two atoms, or carries no energy, cannot be
            # addressed or ranked. Skipped loudly rather than stored unusable.
            logger.warning("publish: bond %r has no atom pair or no energy; skipped", atoms)
            continue
        sites.append(
            SiteFact(
                atom_i=int(atoms[0]),
                atom_j=int(atoms[1]),
                element=str(bond.get("bond") or ""),
                property="bond_dissociation_energy",
                value=to_canonical("bond_dissociation_energy", float(energy), "kcal/mol"),
            )
        )
        if bond.get("is_weakest"):
            weakest = bond
    failed = list(payload.get("failed") or [])
    if failed:
        # A partial survey publishes no weakest bond: a refused bond may be weaker, and
        # `bond_not_computed` flags name what is missing.
        weakest = None
    uncertainty = payload.get("uncertainty_kcal")
    facts = _kept(
        _fact("bonds_considered", payload.get("considered"), ""),
        _text("dissociation_mode", payload.get("mode")),
        _text("weakest_bond", None if weakest is None else str(weakest.get("bond") or "")),
        _fact(
            "weakest_bond_dissociation_energy",
            None if weakest is None else weakest.get("dissociation_energy_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported" if uncertainty is not None else "",
        ),
    )
    flags = _warnings(list(payload.get("warnings") or []))
    flags += _not_computed(failed, "bond_not_computed", _bond_label, len(flags))
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "sites": sites,
            "flags": flags,
        },
    )


def _interaction(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A two-molecule complex: three members — two monomers and the complex they form."""
    a, b = payload.get("smiles_a"), payload.get("smiles_b")
    id_a, can_a = _identify(a)
    id_b, can_b = _identify(b)
    structure = payload.get("structure") or {}
    members = [
        SubjectMember(ordinal=0, role="monomer", compound_id=id_a, smiles=can_a),
        SubjectMember(ordinal=1, role="monomer", compound_id=id_b, smiles=can_b),
        SubjectMember(
            ordinal=2,
            role="complex",
            smiles=f"{can_a}.{can_b}",
            structure_id=structure.get("structure_id") or "",
            charge=_state(structure)[0],
            multiplicity=_state(structure)[1],
        ),
    ]
    subject = Subject(kind="complex", members=members, label=f"{can_a} + {can_b}")
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="crest"
    )
    facts = _kept(
        _fact("interaction_energy", payload.get("interaction_energy_kcal"), "kcal/mol"),
        _fact("complex_energy", payload.get("complex_energy_hartree"), "hartree", member=2),
        _fact("binding_modes", payload.get("binding_modes"), ""),
    )
    # Each monomer's own absolute energy, addressed to the member it belongs to.
    for index, energy in enumerate(payload.get("monomer_energies_hartree") or []):
        if index < 2:
            facts.extend(_kept(_fact("total_energy", energy, "hartree", member=index)))
    return subject, conditions, level, {"properties": facts}


def _scan(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A relaxed scan: one subject, an ordered series of points along one coordinate."""
    smiles = payload.get("smiles")
    subject = Subject(
        kind="geometry",
        members=[_molecule(smiles, payload.get("input_structure_id") or "")],
        label=smiles or "",
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    unit = payload.get("unit") or ""
    # The atom indices name *which* coordinate ("dihedral" alone does not), folded into the label so
    # the series is self-describing.
    atoms = payload.get("atoms") or []
    coordinate = payload.get("coordinate") or ""
    x_label = f"{coordinate}({','.join(str(a) for a in atoms)})" if atoms else coordinate
    points: list[PointFact] = []
    for index, point in enumerate(payload.get("points") or []):
        points.append(
            PointFact(
                series="scan",
                ordinal=index,
                property="point_energy",
                value=float(point["energy_hartree"]),
                x_value=point.get("value"),
                x_unit=unit,
                x_label=x_label,
            )
        )
        if point.get("relative_kcal") is not None:
            points.append(
                PointFact(
                    series="scan",
                    ordinal=index,
                    property="point_relative_energy",
                    value=float(point["relative_kcal"]),
                    x_value=point.get("value"),
                    x_unit=unit,
                    x_label=x_label,
                )
            )
    facts = _kept(
        # An upper bound on the ground-state profile, and deliberately not called a barrier: there
        # is no transition state in a relaxed scan.
        _fact("max_relative_energy", payload.get("maximum_relative_kcal"), "kcal/mol"),
        _fact("scan_minimum_coordinate", payload.get("minimum_value"), ""),
        _text("scan_coordinate", x_label),
    )
    # The relaxed minimum geometry as a `produced_structure` fact: the record's own `structure_id`
    # is the geometry the calculation ran on.
    produced = (payload.get("minimum_structure") or {}).get("structure_id") or ""
    facts = [*facts, *_kept(_text("produced_structure", produced))]
    extra: dict[str, Any] = {"properties": facts, "points": points}
    return subject, conditions, level, extra


def _rotation(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A rotational profile: points as a series, rotamers as conformers, the barrier as a fact.

    Three shapes because the result has three: the profile is a `PointFact` series like a scan's, a
    rotamer is a geometry with a degeneracy (`ConformerFact`), and the barrier and implied lifetime
    are per-compound numbers a site will query.
    """
    smiles = payload.get("smiles")
    subject = Subject(
        kind="geometry",
        members=[_molecule(smiles, payload.get("input_structure_id") or "")],
        label=smiles or "",
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    atoms = payload.get("atoms") or []
    x_label = f"dihedral({','.join(str(atom) for atom in atoms)})"
    points = [
        PointFact(
            series="rotation",
            ordinal=index,
            property="point_relative_energy",
            value=float(point["relative_kcal"]),
            x_value=point.get("value"),
            x_unit="degree",
            x_label=x_label,
        )
        for index, point in enumerate(payload.get("points") or [])
    ]
    conformers = [
        ConformerFact(
            ordinal=index,
            structure_id=rotamer.get("structure_id") or "",
            relative_kcal=float(rotamer["relative_kcal"]),
            population=rotamer.get("population"),
            degeneracy=int(rotamer.get("degeneracy", 1)),
        )
        for index, rotamer in enumerate(payload.get("rotamers") or [])
        if rotamer.get("relative_kcal") is not None
    ]
    barriers = payload.get("barriers") or []
    # The barrier out of the most populated well (ordinal 0), which decides configurational
    # stability, not the profile's highest point.
    leaving = [barrier for barrier in barriers if barrier.get("from_rotamer") == 0]
    highest = max(leaving or barriers, key=lambda barrier: barrier["forward_kcal"], default=None)
    lifetime = (highest or {}).get("interconversion") or {}
    uncertainty = payload.get("uncertainty_kcal")
    facts = _kept(
        # The method's uncertainty rides on the barrier, as on a reaction energy; the half-life
        # below is exponential in it.
        _fact(
            "rotational_barrier",
            (highest or {}).get("forward_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported",
        ),
        _fact("interconversion_half_life", lifetime.get("half_life_seconds"), "s"),
        _fact("rotamer_count", len(conformers), ""),
        _fact("torsion_symmetry_order", payload.get("symmetry_order"), ""),
        _fact("torsion_period", payload.get("period_degrees"), "degree"),
        _text("torsion_label", payload.get("label") or ""),
        _text("torsion_id", payload.get("torsion_id") or ""),
        _text("reaction_level", payload.get("level")),
        # Which energy the barrier is: electronic, or a free energy from a Hessian at the pass. A
        # reader must never have to infer this from the level.
        _text("barrier_basis", (highest or {}).get("basis") or ""),
    )
    return (
        subject,
        conditions,
        level,
        {
            "properties": facts,
            "points": points,
            "conformers": conformers,
        },
    )


# Packed `.npy` fields of a Hessian payload, named here rather than imported because this module
# reads payloads, which may come from an older calculator.
_PACKED_ARRAYS: tuple[str, ...] = ("hessian_npy", "dipole_derivatives_npy")


def _hessian(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """The second derivatives at one geometry: everything about them except the matrix.

    No frequencies: they need the mass-weighted Hessian, and the payload carries no elements (the
    geometry is an address this pure projector may not read). Frequencies are published from
    `ThermochemistryResult`. Published here: the SCF energy and `max_gradient`, the evidence the
    geometry was a stationary point.

    The packed arrays do not ride along: they are megabytes, the outbox is never pruned, and no
    re-projection could derive a fact from them. The matrix is kept in the content-addressed
    artifact store.
    """
    structure_id = payload.get("structure_id") or ""
    subject = Subject(kind="geometry", members=[_molecule(None, structure_id)], label=structure_id)
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    facts = _kept(
        _fact("electronic_energy", payload.get("electronic_energy_hartree"), "hartree"),
        # `None` from the `xtb` binary backend, which reports no gradient beside its Hessian.
        # Absent stays absent: "not reported" is a different claim from "zero".
        _fact("max_gradient", payload.get("max_gradient_hartree_per_angstrom"), "hartree/angstrom"),
        _fact("atom_count", payload.get("atom_count"), ""),
    )
    carried = {key: value for key, value in payload.items() if key not in _PACKED_ARRAYS}
    return subject, conditions, level, {"properties": facts, "payload": carried}


def _thermochemistry(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A frequency calculation: the thermochemistry, plus the vibrational modes as a series."""
    smiles = payload.get("smiles")
    structure_id = payload.get("structure_id") or ""
    subject = Subject(
        kind="geometry", members=[_molecule(smiles, structure_id)], label=smiles or ""
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
        temperature_k=payload.get("temperature_k"),
        pressure_pa=payload.get("pressure_pa"),
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine="xtb",
        treatment=payload.get("conformer_treatment") or "",
    )
    uncertainty = payload.get("uncertainty_kcal")
    facts = _kept(
        _fact("electronic_energy", payload.get("electronic_energy_hartree"), "hartree"),
        _fact("enthalpy", payload.get("enthalpy_hartree"), "hartree"),
        _fact("gibbs_free_energy", payload.get("gibbs_free_energy_hartree"), "hartree"),
        _fact(
            "zero_point_energy",
            payload.get("zero_point_energy_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported",
        ),
        _fact(
            "thermal_enthalpy_correction",
            payload.get("thermal_enthalpy_correction_kcal"),
            "kcal/mol",
        ),
        _fact(
            "gibbs_correction",
            payload.get("gibbs_correction_kcal"),
            "kcal/mol",
            uncertainty=uncertainty,
            uncertainty_kind="reported",
        ),
        _fact("entropy", payload.get("entropy_cal_per_mol_k"), "cal/(mol*K)"),
        _fact("symmetry_number", payload.get("symmetry_number"), ""),
        _fact("mode_count", payload.get("mode_count"), ""),
        # A non-stationary geometry often shows no imaginary mode, and its ZPE is quietly too small,
        # so the gradient is the evidence `is_minimum` cannot carry. Reported in Hartree/Angstrom;
        # `None` when the backend reported none.
        _fact("max_gradient", payload.get("max_gradient_hartree_per_angstrom"), "hartree/angstrom"),
        _flag("is_minimum", payload.get("is_minimum")),
        # The reference state the entropy and Gibbs terms are quoted at (1 atm gas, 1 mol/L
        # solution).
        _text("standard_state", payload.get("standard_state")),
        _text("conformer_treatment", payload.get("conformer_treatment")),
    )
    points = [
        PointFact(
            series="modes",
            ordinal=index,
            property="wavenumber",
            value=float(mode["wavenumber_cm"]),
            x_value=float(mode["wavenumber_cm"]),
            x_unit="cm^-1",
            x_label="wavenumber",
        )
        for index, mode in enumerate(payload.get("modes") or [])
    ]
    points += [
        PointFact(
            series="modes",
            ordinal=index,
            property="ir_intensity",
            value=float(mode["ir_intensity_km_per_mol"]),
            x_value=float(mode["wavenumber_cm"]),
            x_unit="cm^-1",
            x_label="wavenumber",
        )
        for index, mode in enumerate(payload.get("modes") or [])
        if mode.get("ir_intensity_km_per_mol") is not None
    ]
    # An imaginary mode is a fact about the geometry (a saddle point), reported as a negative
    # wavenumber like the result model.
    facts += [
        PropertyFact(property="imaginary_frequency", value=float(frequency), unit="cm^-1")
        for frequency in (payload.get("imaginary_frequencies_cm") or [])[:1]
    ]
    # Says why the `ir_intensity` series is short: intensities that could not be paired with modes
    # are dropped, and a consumer should see the reason.
    unpaired = payload.get("spectrum_unavailable")
    flags = (
        [
            FlagFact(
                ordinal=0,
                flag="spectrum_unavailable",
                severity="warning",
                message=str(unpaired),
            )
        ]
        if unpaired
        else []
    )
    return subject, conditions, level, {"properties": facts, "points": points, "flags": flags}


def _electronic_properties(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """Orbital energies, a dipole, and the per-atom/per-bond breakdown behind them."""
    smiles = payload.get("smiles")
    structure_id = payload.get("structure_id") or ""
    subject = Subject(
        kind="geometry", members=[_molecule(smiles, structure_id)], label=smiles or ""
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    facts = _kept(
        _fact("total_energy", payload.get("total_energy_hartree"), "hartree"),
        _fact("homo", payload.get("homo_ev"), "ev"),
        _fact("lumo", payload.get("lumo_ev"), "ev"),
        _fact("homo_lumo_gap", payload.get("gap_ev"), "ev"),
        _fact("dipole", payload.get("dipole_debye"), "debye"),
    )
    sites = [
        SiteFact(
            atom_i=int(charge["index"]),
            element=charge.get("element", ""),
            property="partial_charge",
            value=float(charge["charge"]),
        )
        for charge in payload.get("atom_charges") or []
    ]
    sites += [
        SiteFact(
            atom_i=int(bond["atom_i"]),
            atom_j=int(bond["atom_j"]),
            property="bond_order",
            value=float(bond["order"]),
        )
        for bond in payload.get("bond_orders") or []
    ]
    return subject, conditions, level, {"properties": facts, "sites": sites}


def _site_reactivity(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """Fukui indices and the conceptual-DFT panel: per-atom facts plus per-molecule ones.

    Every per-atom index is published (the ranking is presentation); the global panel describes the
    molecule and is published once. The cache cannot be queried by payload, so this is what makes
    these questions answerable at all.
    """
    smiles = payload.get("smiles")
    subject = Subject(
        kind="geometry",
        members=[_molecule(smiles, payload.get("structure_id") or "")],
        label=smiles or "",
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    sites: list[SiteFact] = []
    for site in payload.get("sites") or []:
        index, element = int(site["index"]), site.get("element", "")
        for key, name in (
            ("f_minus", "fukui_minus"),
            ("f_plus", "fukui_plus"),
            ("f_zero", "fukui_zero"),
            ("dual", "fukui_dual"),
            ("local_softness_minus", "local_softness_minus"),
            ("local_softness_plus", "local_softness_plus"),
            ("local_electrophilicity_ev", "local_electrophilicity"),
        ):
            if site.get(key) is not None:
                sites.append(
                    SiteFact(atom_i=index, element=element, property=name, value=float(site[key]))
                )
    panel = payload.get("descriptors") or {}
    facts = _kept(
        _fact("atom_count", payload.get("total_atoms"), ""),
        _text("fukui_mode", payload.get("mode")),
        # Units on every value. `softness_per_ev` is a reciprocal hardness, so its unit is 1/ev.
        # Lower case, matching the registry's spelling (units are matched, not parsed).
        _fact("ionization_potential", panel.get("ionization_potential_ev"), "ev"),
        _fact("electron_affinity", panel.get("electron_affinity_ev"), "ev"),
        _fact("chemical_potential", panel.get("chemical_potential_ev"), "ev"),
        _fact("chemical_hardness", panel.get("hardness_ev"), "ev"),
        _fact("chemical_softness", panel.get("softness_per_ev"), "1/ev"),
        _fact("electrophilicity_index", panel.get("electrophilicity_ev"), "ev"),
    )
    return subject, conditions, level, {"properties": facts, "sites": sites}


def _optimization(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A geometry optimization. The subject is the geometry it started **from**."""
    smiles = payload.get("smiles")
    structure = payload.get("structure") or {}
    started_from = payload.get("input_structure_id") or ""
    subject = Subject(
        kind="geometry", members=[_molecule(smiles, started_from)], label=smiles or ""
    )
    conditions = Conditions(
        solvent=payload.get("solvent"),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown",
        family="semiempirical",
        engine=payload.get("engine") or "xtb",
    )
    facts = _kept(
        _fact("total_energy", payload.get("energy_hartree"), "hartree"),
        _fact("initial_energy", payload.get("initial_energy_hartree"), "hartree"),
        _fact("relaxation", payload.get("relaxation_kcal"), "kcal/mol"),
        _fact("optimization_steps", payload.get("steps"), ""),
        # None under GFN-FF, which reports no gradient. Reported in Hartree/Angstrom (as
        # `OptimizationResult.max_gradient` holds it), not the canonical Hartree/bohr, so
        # `to_canonical` converts it.
        _fact("max_gradient", payload.get("max_gradient"), "hartree/angstrom"),
        _fact("displacement_rms", payload.get("displacement_rms_angstrom"), "angstrom"),
    )
    # The geometry the optimization produced, as an address fact, not a subject member (see
    # `_scan`).
    produced = structure.get("structure_id") or payload.get("structure_id") or ""
    facts = [*facts, *_kept(_text("produced_structure", produced))]
    extra: dict[str, Any] = {"properties": facts}
    return subject, conditions, level, extra


def _molecule_property(
    payload: dict[str, Any],
    *,
    facts: list[PropertyFact],
    method_key: str = "method",
    family: str = "empirical",
    engine: str = "rdkit",
    ph: float | None = None,
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """The shared shape of every molecule-keyed predictor: one subject, a handful of scalars."""
    smiles = payload.get("smiles")
    subject = Subject(kind="molecule", members=[_molecule(smiles)], label=smiles or "")
    return (
        subject,
        Conditions(ph=ph),
        TheoryLevel(method=payload.get(method_key) or "unknown", family=family, engine=engine),
        {"properties": facts},
    )


def _pka(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A predicted pKa, with the site it describes and the energy behind it."""
    uncertainty = payload.get("uncertainty")
    return _molecule_property(
        payload,
        facts=_kept(
            _fact(
                "pka", payload.get("pka"), "", uncertainty=uncertainty, uncertainty_kind="reported"
            ),
            _fact("deprotonation_energy", payload.get("deprotonation_energy_kcal"), "kcal/mol"),
            _text("pka_site", payload.get("site")),
        ),
        family="semiempirical",
        engine="xtb",
    )


def _microstate_pka(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A pKa computed from two sampled macrostates: the same property as `_pka`, differently made.

    Separate from `_pka` because the two pipelines carry separate calibrations; `method` names the
    sampler so they are never averaged together. Four facts beyond the number:

    - `pka_site`: which equilibrium (`acid` HA -> A- + H+, or `base` BH+ -> B + H+), the same
      fact and name `_pka` publishes.
    - `ionised_microstate`: the winning microstate's perceived constitution (which proton); absent
      when perception declined.
    - `microstates_within_rt`: more than one means no single conjugate base;
      `species_enumerated` is how many the search found.
    - `deprotonation_free_energy`: the computed quantity, which a recalibration leaves unchanged.

    Solvent and temperature are conditions, not properties.
    """
    return (
        Subject(
            kind="molecule",
            members=[_molecule(payload.get("smiles"))],
            label=payload.get("smiles") or "",
        ),
        Conditions(
            solvent=payload.get("solvent"),
            temperature_k=payload.get("temperature_k"),
        ),
        TheoryLevel(
            method=payload.get("method") or "unknown",
            family="semiempirical",
            engine="crest",
        ),
        {
            "properties": _kept(
                _fact(
                    "pka",
                    payload.get("pka"),
                    "",
                    uncertainty=payload.get("uncertainty"),
                    uncertainty_kind="reported",
                ),
                _fact("deprotonation_free_energy", payload.get("delta_g_kcal"), "kcal/mol"),
                _fact("microstates_within_rt", payload.get("microstates_within_rt"), ""),
                _fact("species_enumerated", payload.get("microstates_found"), ""),
                _text("pka_site", payload.get("branch")),
                _text("ionised_microstate", payload.get("site_smiles")),
            ),
            # Published as flags, as every projector publishes a calculator's warnings.
            "flags": _warnings(list(payload.get("warnings") or [])),
        },
    )


def _solubility(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A predicted aqueous solubility, carrying its applicability-domain flag.

    `in_domain` is published so None (unknown, from an older row) is never read as yes.
    """
    estimate = payload.get("estimate") or {}
    fact = _fact(
        "log_s",
        payload.get("log_s_mol_per_l"),
        "",
        uncertainty=payload.get("uncertainty_log"),
        uncertainty_kind=estimate.get("method") or "reported",
    )
    facts = []
    if fact is not None:
        facts.append(fact.model_copy(update={"in_domain": estimate.get("in_domain")}))
    return _molecule_property(payload, facts=facts, method_key="model", family="empirical")


def _logd(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A predicted logD at a stated pH — the pH is a condition, not a property."""
    return _molecule_property(
        payload,
        facts=_kept(
            _fact(
                "log_d",
                payload.get("log_d"),
                "",
                uncertainty=payload.get("uncertainty"),
                uncertainty_kind="propagated",
            ),
            _fact("clogp", payload.get("clogp"), ""),
            _fact("pka", payload.get("pka"), ""),
        ),
        method_key="method",
        ph=payload.get("ph"),
    )


def _descriptors(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A developability profile: cheap RDKit descriptors, all calculation-scope scalars."""
    return _molecule_property(
        payload,
        facts=_kept(
            _fact("molecular_weight", payload.get("molecular_weight"), "g/mol"),
            _fact("clogp", payload.get("clogp"), ""),
            _fact("tpsa", payload.get("tpsa"), "angstrom^2"),
            _fact("hydrogen_bond_donors", payload.get("h_bond_donors"), ""),
            _fact("hydrogen_bond_acceptors", payload.get("h_bond_acceptors"), ""),
            _fact("rotatable_bonds", payload.get("rotatable_bonds"), ""),
            _fact("aromatic_rings", payload.get("aromatic_rings"), ""),
            _fact("fraction_csp3", payload.get("fraction_csp3"), ""),
            _fact("qed", payload.get("qed"), ""),
            _fact("lipinski_violations", payload.get("lipinski_violations"), ""),
            _flag("veber_pass", payload.get("veber_pass")),
        ),
    )


def _single_point(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A single-point energy — the smallest thing this system caches."""
    smiles = payload.get("smiles")
    subject = Subject(kind="molecule", members=[_molecule(smiles)], label=smiles or "")
    return (
        subject,
        Conditions(charge=payload.get("charge")),
        TheoryLevel(
            method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
        ),
        {
            "properties": _kept(
                _fact("total_energy", payload.get("total_energy_hartree"), "hartree")
            )
        },
    )


def _dft(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A stored DFT energy. The basis set is part of the level, not a condition.

    Backfill-only: nothing writes `dft` rows any more, but `calculation_results` is never pruned,
    and a retired calculator keeps its `calc_type` projector.
    """
    smiles = payload.get("molecule_smiles")
    subject = Subject(kind="molecule", members=[_molecule(smiles)], label=smiles or "")
    return (
        subject,
        Conditions(),
        TheoryLevel(
            method=payload.get("method") or "unknown",
            family="dft",
            basis_set=payload.get("basis_set") or "",
        ),
        {
            "properties": _kept(
                _fact("total_energy", payload.get("total_energy_hartree"), "hartree"),
                _flag("converged", payload.get("converged")),
            )
        },
    )


def _atomic_descriptors(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """The binary-only per-atom panel: polarisability, dispersion, coordination and multipoles.

    Per-atom and not normalised per molecule, so they compare across compounds, which is what makes
    them worth a queryable store.
    """
    smiles = payload.get("smiles")
    subject = Subject(
        kind="geometry",
        members=[_molecule(smiles, payload.get("structure_id") or "")],
        label=smiles or "",
    )
    conditions = Conditions(
        solvent=canonical_solvent(payload.get("solvent")),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    sites: list[SiteFact] = []
    for atom in payload.get("atoms") or []:
        index, element = int(atom["index"]), atom.get("element", "")
        # No unit beside each name: a `SiteFact` carries none, because the property registry is
        # the one place a unit is stated — the same shape `_site_reactivity` uses for its indices.
        for key, name in (
            ("polarisability_au", "atomic_polarisability"),
            ("c6_au", "atomic_c6"),
            ("coordination_number", "coordination_number"),
            ("charge", "partial_charge"),
            ("dipole_norm_au", "atomic_dipole"),
            ("quadrupole_norm_au", "atomic_quadrupole"),
        ):
            if atom.get(key) is not None:
                sites.append(
                    SiteFact(atom_i=index, element=element, property=name, value=float(atom[key]))
                )
    facts = _kept(_fact("total_energy", payload.get("total_energy_hartree"), "hartree"))
    return subject, conditions, level, {"properties": facts, "sites": sites}


def _surface_potential(
    payload: dict[str, Any],
) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """The electrostatic-potential extrema on a molecular surface.

    Two molecule-level numbers rather than the grid, which nothing downstream reads.
    """
    smiles = payload.get("smiles")
    subject = Subject(
        kind="geometry",
        members=[_molecule(smiles, payload.get("structure_id") or "")],
        label=smiles or "",
    )
    conditions = Conditions(
        solvent=canonical_solvent(payload.get("solvent")),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    surface = payload.get("surface") or {}
    facts = _kept(
        _fact("surface_potential_min", surface.get("minimum_kcal_per_mol"), "kcal/mol"),
        _fact("surface_potential_max", surface.get("maximum_kcal_per_mol"), "kcal/mol"),
        _fact("surface_grid_points", surface.get("grid_points"), ""),
    )
    return subject, conditions, level, {"properties": facts, "sites": []}


# What each projector is keyed by. Two vocabularies, kept apart:
#
# - `PAYLOAD_PROJECTORS` is keyed by the **pydantic model name**, which a job envelope or typed
#   caller can state exactly.
# - `_CALC_TYPE_PROJECTORS` is keyed by the **`calc_type` prefix** of a stored cache row, which is
#   all the backfill path has.
#
# Both resolve to the same functions, so the live and backfill paths cannot disagree.
_Projector = Callable[[dict[str, Any]], tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]]


PAYLOAD_PROJECTORS: dict[str, _Projector] = {
    "ReactionEnergyResult": _reaction,
    "SolventComparisonResult": _solvent_screen,
    # The aggregate over `SpeciesDistribution`, which is registered below with its siblings.
    "SpeciesSolventComparison": _species_solvent_screen,
    "ConformerEnsemble": _ensemble,
    "RefinedEnsemble": _refined_ensemble,
    "EnsembleProperty": _ensemble_property,
    "SpeciesDistribution": _species_distribution,
    "BondDissociationSurvey": _bond_survey,
    "EnsemblePayload": _ensemble,
    "InteractionResult": _interaction,
    "ScanResult": _scan,
    "RotationProfile": _rotation,
    "HessianPayload": _hessian,
    "ThermochemistryResult": _thermochemistry,
    "ElectronicProperties": _electronic_properties,
    "SiteReactivityResult": _site_reactivity,
    "AtomicDescriptorResult": _atomic_descriptors,
    "SurfacePotentialResult": _surface_potential,
    "OptimizationResult": _optimization,
    "OptimizationSummary": _optimization,
    "PkaResult": _pka,
    "MicrostatePka": _microstate_pka,
    "SolubilityResult": _solubility,
    "LogdResult": _logd,
    "DescriptorProfile": _descriptors,
    "XtbResult": _single_point,
}

# Longest prefix wins, so `xtb.properties` is not swallowed by a shorter `xtb.` entry.
#
# An entry must name a `calc_type` something has actually stamped, now or in a release whose rows a
# deployment still holds (`xtb.scan` stays for that reason). A spelling nothing ever wrote gets no
# entry: it would only hide a missing route behind a test of the dead one.
_CALC_TYPE_PROJECTORS: tuple[tuple[str, _Projector], ...] = (
    ("xtb.atomic", _atomic_descriptors),
    ("xtb.surface", _surface_potential),
    ("xtb.properties", _electronic_properties),
    ("xtb.conformers", _ensemble),
    ("xtb.complex", _interaction),
    ("xtb.fukui", _site_reactivity),
    ("xtb.hess", _hessian),
    ("xtb.scan", _scan),
    ("xtb.opt", _optimization),
    ("xtb.sp", _single_point),
    ("solubility", _solubility),
    ("developability", _descriptors),
    ("pka", _pka),
    ("dft", _dft),
)


def projector_for(calc_type: str, payload_kind: str = "") -> _Projector | None:
    """The projector for a stored row, or None when nothing here can read it.

    `payload_kind` wins when given (exact, where a prefix is an inference). None rather than
    raising, so a backfill skips rows from retired calculators.
    """
    if payload_kind and payload_kind in PAYLOAD_PROJECTORS:
        return PAYLOAD_PROJECTORS[payload_kind]
    for prefix, projector in _CALC_TYPE_PROJECTORS:
        if calc_type.startswith(prefix):
            return projector
    return None


def project(
    *,
    calc_ref: str,
    calc_type: str,
    payload: dict[str, Any],
    payload_kind: str = "",
    calc_version: str = "",
    input_hash: str = "",
    params_hash: str = "",
    structure_id: str = "",
    provenance: str = "computed",
    compute_seconds: float | None = None,
    computed_at: datetime | None = None,
    depends_on: list[str] | None = None,
) -> ResultRecord:
    """Project one stored calculation into its canonical published record.

    Raises `ProjectionError` when nothing here can read the payload (a code gap), rather than
    producing a record with no facts. A corpus walker asks `projector_for` first.
    """
    projector = projector_for(calc_type, payload_kind)
    if projector is None:
        raise ProjectionError(
            f"no projector for calc_type {calc_type!r} "
            f"(payload kind {payload_kind or 'unknown'!r}); "
            "add one to `PAYLOAD_PROJECTORS` and `_CALC_TYPE_PROJECTORS`"
        )
    subject, conditions, level, extra = projector(payload)
    # The payload rides along unless a projector narrows it; only `_hessian` does, dropping packed
    # arrays that are bytes, not science. Anything not removed is carried unmodified.
    carried = extra.get("payload", payload)
    # The structure a geometry-keyed calculation ran *on*: the server's answer if given, else the
    # subject member's, which is the same value for every projector above.
    ran_on = structure_id or next(
        (member.structure_id for member in subject.members if member.structure_id), ""
    )
    properties = list(extra.get("properties", []))
    # Checked here, on the one funnel every projector and every multi-record emitter goes through,
    # so a projector cannot avoid it by being written after this line.
    _facts_belong_in_the_scalar_table(properties)
    return ResultRecord(
        calc_ref=calc_ref,
        calc_type=calc_type,
        calc_version=calc_version,
        input_hash=input_hash,
        params_hash=params_hash,
        subject=subject,
        conditions=conditions,
        level=level,
        structure_id=ran_on,
        properties=properties,
        sites=extra.get("sites", []),
        points=extra.get("points", []),
        conformers=extra.get("conformers", []),
        candidates=extra.get("candidates", []),
        flags=extra.get("flags", []),
        provenance=provenance,
        compute_seconds=compute_seconds,
        computed_at=computed_at,
        depends_on=list(depends_on or []),
        payload=carried,
        payload_kind=payload_kind,
    )


def records_from_solvent_screen(
    *, calc_ref: str, payload: dict[str, Any], depends_on: list[str] | None = None, **common: Any
) -> list[ResultRecord]:
    """A solvent screen as its comparison **plus** one record per solvent it compared.

    Never store an aggregate whose parts are not also stored: the parts are ordinary reaction
    records at their own conditions, so cross-solvent questions answer over the union of screened
    and unscreened runs. Each part links to the comparison through `depends_on`.
    """
    comparison = project(
        calc_ref=calc_ref,
        payload=payload,
        payload_kind="SolventComparisonResult",
        depends_on=list(depends_on or []),
        **common,
    )
    records = [comparison]
    for index, effect in enumerate(payload.get("effects") or []):
        part = {
            "reactants": payload.get("reactants"),
            "products": payload.get("products"),
            "method": payload.get("method"),
            "temperature_k": payload.get("temperature_k"),
            "level": payload.get("level"),
            "solvent": effect.get("solvent"),
            # Each row's own reference state: the gas entry is 1 atm, solution entries 1 mol/L.
            "standard_state": effect.get("standard_state"),
            "delta_e_kcal": effect.get("delta_e_kcal"),
            "delta_h_kcal": effect.get("delta_h_kcal"),
            "delta_g_kcal": effect.get("delta_g_kcal"),
            "uncertainty_kcal": payload.get("uncertainty_kcal"),
            "species": [],
            "warnings": [],
        }
        # A derived ref, so the part is addressable and idempotent without colliding with a
        # standalone run of the same reaction in the same solvent.
        part_common = {key: value for key, value in common.items() if key != "calc_type"}
        part_common["calc_type"] = "reaction.solvent_screen_part"
        records.append(
            project(
                calc_ref=f"{calc_ref}#solvent{index}",
                payload=part,
                payload_kind="ReactionEnergyResult",
                depends_on=[calc_ref],
                **part_common,
            )
        )
    return records


def records_from_species_solvent_screen(
    *, calc_ref: str, payload: dict[str, Any], depends_on: list[str] | None = None, **common: Any
) -> list[ResultRecord]:
    """A species screen as its comparison **plus** one distribution record per medium.

    Same rule as `records_from_solvent_screen`. Each part is a full `SpeciesDistribution`, the same
    shape the single-solvent job publishes, taken verbatim from the composite.
    """
    comparison = project(
        calc_ref=calc_ref,
        payload=payload,
        payload_kind="SpeciesSolventComparison",
        depends_on=list(depends_on or []),
        **common,
    )
    records = [comparison]
    part_common = {key: value for key, value in common.items() if key != "calc_type"}
    part_common["calc_type"] = "species_ranking.solvent_screen_part"
    for index, distribution in enumerate(payload.get("distributions") or []):
        records.append(
            project(
                # A derived ref, so a part is addressable and idempotent without colliding with a
                # standalone `rank_species` run of the same set in the same medium.
                calc_ref=f"{calc_ref}#medium{index}",
                payload=distribution,
                payload_kind="SpeciesDistribution",
                depends_on=[calc_ref],
                **part_common,
            )
        )
    return records


# The payload kinds whose projection is more than one record, keyed by model name (a composite's
# `calc_type` is a route that names no shape). A multi-record emitter: same call shape as
# `project`, but returning the aggregate *and* its parts.
_MultiProjector = Callable[..., list[ResultRecord]]

_MULTI_RECORD_PROJECTORS: dict[str, _MultiProjector] = {
    "SolventComparisonResult": records_from_solvent_screen,
    "SpeciesSolventComparison": records_from_species_solvent_screen,
}


def records_for(
    *, calc_ref: str, calc_type: str, payload: dict[str, Any], payload_kind: str = "", **common: Any
) -> list[ResultRecord]:
    """Every record one stored payload becomes: usually one, sometimes an aggregate and its parts.

    The one place that decides one-versus-many, so no hook needs to know which shapes decompose.
    """
    emitter = _MULTI_RECORD_PROJECTORS.get(payload_kind)
    if emitter is not None:
        return emitter(calc_ref=calc_ref, calc_type=calc_type, payload=payload, **common)
    return [
        project(
            calc_ref=calc_ref,
            calc_type=calc_type,
            payload=payload,
            payload_kind=payload_kind,
            **common,
        )
    ]
