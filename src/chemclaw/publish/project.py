"""Turning a calculator's own result model into the canonical published record.

**This is the only place that knows both vocabularies**, and keeping it in one module is what makes
"a stored payload becomes a queryable fact" a single reviewable act rather than a rule each
calculator half-remembers.

Three properties the projectors below are written to hold, each of which was a real failure mode
somewhere in this tree before it was a rule here:

- **A number is never guessed.** `ReactionEnergyResult.delta_g_kcal` is `None` at `quick` level
  *and* when any species' symmetry number was unstated — so a projector that fell back to
  `delta_e_kcal` would publish an electronic energy as a free energy. Absent stays absent; there is
  no fallback anywhere in this module.
- **A unit is stated, never assumed.** Every fact goes through `properties.to_canonical`, which
  refuses a unit it has no conversion for rather than passing it through. The model field names
  carry their units (`delta_g_kcal`, `energy_hartree`) and that is what is read.
- **The payload rides along untouched.** Whatever a projector fails to extract is still in
  `ResultRecord.payload`, so a projector bug is a re-projection rather than lost science.

**Geometries are not copied into the record.** A `Structure` reaches the published row as its
`structure_id` — the same rule `D-2026-08-21` established for what reaches the model's context, and
for the same reason: the coordinates are already held, addressably, and 3N floats in a fact table
are 3N floats nobody queries.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from chemclaw.core.chem import canonical_smiles, compound_id
from chemclaw.publish.properties import to_canonical
from chemclaw.publish.record import (
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

    A `ValueError`, so `durable/publish.py` marks it non-retryable by class name: a payload whose
    shape the projector cannot read will fail identically on every attempt, and the fix is code.
    """


def _identify(smiles: str | None) -> tuple[str, str]:
    """`(compound_id, canonical_smiles)` for a SMILES, or two empty strings.

    `core.chem.compound_id` is reused rather than an InChIKey minted here: it is already the join
    key between the knowledge graph, the fingerprint search and the QM notes, so a published result
    meets the note about the same compound with no second naming scheme.

    **A SMILES RDKit cannot parse is degraded to empty rather than raised on.** The calculation
    genuinely happened and its numbers are worth keeping; refusing to publish a finished result
    because a label will not canonicalize would lose the science to protect the join.
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


def _molecule(
    smiles: str | None, structure_id: str = "", *, role: str = "subject"
) -> SubjectMember:
    """The single member of a one-molecule or one-geometry subject."""
    identifier, canonical = _identify(smiles)
    return SubjectMember(
        ordinal=0,
        role=role,  # type: ignore[arg-type]
        compound_id=identifier,
        smiles=canonical,
        structure_id=structure_id,
    )


def _species_members(reactants: list[str], products: list[str]) -> list[SubjectMember]:
    """A reaction's members, one per stoichiometric equivalent.

    That is the tools' own convention — `compute_reaction_energy` documents listing a species once
    per equivalent (`["O", "O"]` for two waters) — so the faithful projection is N members at
    stoichiometry 1, not one member carrying a coefficient. The ordinals match the order
    `ReactionEnergyResult.species` uses, which is what lets a per-species fact address its member.
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

    Matched on `(role, molecule)` rather than on list position -- see the caller for the corruption
    that position-matching produced.

    **And the match is one-to-one**, which is what `claimed` is for. A species appearing twice in
    the equation is two members, because listing a species once per equivalent is the tools' own
    stoichiometric convention (`["O", "O"]` for two waters). Returning the *first* member with that
    identity for both copies looked harmless and was not: member 1 received no facts at all, and
    the two facts for member 0 collided on `value_id` -- which is a content hash over
    `(calc_ref, scope, ordinal, property)` -- so the far end's `DO UPDATE` silently kept one and
    discarded the other. Measured on `2 H2O`: 6 property rows carrying 5 distinct ids.

    Handing each copy the next unclaimed member of that role restores the invariant the ordinals
    exist for: one member, one set of per-species facts, one id.
    """
    identifier, canonical = _identify(species.get("smiles"))
    role = species.get("role")
    for member in members:
        if member.role != role or member.ordinal in claimed:
            continue
        if member.compound_id and identifier and member.compound_id == identifier:
            return member.ordinal
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
    """One numeric fact, canonicalized — or None when the calculator did not produce the value.

    Returning None for an absent number rather than substituting one is the whole discipline of
    this module: `delta_g_kcal is None` means the free energy was not established, and every caller
    below filters Nones out rather than defaulting them.
    """
    if value is None:
        return None
    return PropertyFact(
        property=name,
        value=to_canonical(name, value, unit),
        unit=unit,
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


def _warnings(messages: list[str]) -> list[FlagFact]:
    """A calculator's own warnings, as flags rather than as prose nobody queries."""
    return [
        FlagFact(ordinal=index, flag="calculator_warning", severity="warning", message=message)
        for index, message in enumerate(messages)
    ]


# --- the projectors, one per result model -------------------------------------------------------
#
# Each takes the model's `model_dump(mode="json")` rather than the model itself, deliberately. Two
# reasons, and the second is the load-bearing one: a dict is what comes back out of
# `calculation_results.result` and out of `job_records.result`, so the backfill path and the live
# path run the *same* projector rather than two that can disagree; and importing the models here
# would make this module depend on shapes that cross a Temporal wire, where an older history can
# carry a field this release has renamed.


def _reaction(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """The subject, conditions, level and facts of a reaction energy."""
    reactants = list(payload.get("reactants") or [])
    products = list(payload.get("products") or [])
    if not reactants or not products:
        raise ProjectionError("a reaction payload must name reactants and products")
    members = _species_members(reactants, products)
    subject = Subject(kind="reaction", members=members, label=_reaction_label(reactants, products))
    conditions = Conditions(
        solvent=canonical_solvent(payload.get("solvent")),
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
        # `delta_h_kcal` and `delta_g_kcal` are None at `quick` level and whenever a species'
        # symmetry number was unstated. Absent stays absent: substituting delta_e here would
        # publish an electronic energy under the name of a free energy.
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
        _text("conformer_treatment", payload.get("conformer_treatment")),
    )
    # The per-species breakdown, which is what `job_records.result` holds today and nothing can
    # query.
    #
    # **Matched by (role, molecule), never by list position.** `species` and the equation's own
    # lists are two independently produced sequences: a `quick`-level run returns no species at
    # all, and nothing in the result model promises the two orders agree. Zipping them by index
    # attaches a product's free energy to a reactant -- silently, since both are plausible numbers
    # in the same units -- which is a data corruption rather than a missing row. Measured: a
    # two-species breakdown over a three-member equation put cyclohexane's energy on butadiene.
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
    """A solvent comparison — the aggregate only; its parts publish as their own records.

    **The rule this follows: never store an aggregate whose parts are not also stored.** A screen's
    per-solvent energies are published as ordinary reaction records at their own `Conditions`, and
    this record carries only what is genuinely about the comparison — the spread and the winner.
    `records_from_solvent_screen` below is what emits both halves. The payoff is that "compare
    this reaction across solvents" then answers over solvents that were never screened in one call,
    which is the more common case.
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
    facts = _kept(
        _fact(
            "solvent_spread",
            payload.get("spread_kcal"),
            "kcal/mol",
            uncertainty=payload.get("uncertainty_kcal"),
            uncertainty_kind="reported",
        ),
        _text("best_solvent", canonical_solvent(payload.get("best_solvent"))),
        _text("reaction_level", payload.get("level")),
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


def _ensemble(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A conformer ensemble: one subject, N conformer rows.

    The members the search found are **outputs, not subjects** — the subject is the molecule that
    was searched. Each conformer reaches the record as its `structure_id`, never its coordinates:
    one measured search envelope was 29,086 characters of Cartesians, and a fact table full of them
    is a fact table nobody queries.

    `population` is carried when the payload has it, and its `temperature_k` goes on the conditions
    so the number is never read without the temperature that produced it.
    """
    # **The subject is the seed the search started from, and the two upstream shapes name it
    # differently.** `ConformerEnsemble` (returned) carries `smiles`; `EnsemblePayload` (cached)
    # carries only `structure_id`, because it is keyed on a geometry rather than a molecule. Taking
    # both is what lets one projector serve the cached row and the returned envelope — and it is
    # why `SubjectMember` accepts a structure with no SMILES.
    smiles = payload.get("smiles")
    seed_structure_id = payload.get("structure_id") or ""
    subject = Subject(
        kind="ensemble",
        members=[_molecule(smiles, seed_structure_id)],
        label=smiles or seed_structure_id,
    )
    conditions = Conditions(
        solvent=canonical_solvent(payload.get("solvent")),
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
        # **The two ensemble shapes carry different halves, and neither carries both.** Measured
        # against the models: `EnsembleMember` (the cached search) has `energy_hartree` and no
        # relative energy or population; `Conformer` (what the tool returns after Boltzmann
        # weighting) has `relative_kcal` and `population` and no absolute energy. Reading both keys
        # optionally is what lets one projector serve `xtb.conformers` rows and returned ensembles
        # alike — and `ConformerFact` refuses a member that has neither.
        energy = member.get("energy_hartree")
        relative = member.get("relative_kcal")
        if energy is None and relative is None:
            logger.warning("publish: ensemble member %d has no energy; skipped", index)
            continue
        conformers.append(
            ConformerFact(
                ordinal=index,
                structure_id=structure_id,
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
        ),
    ]
    subject = Subject(kind="complex", members=members, label=f"{can_a} + {can_b}")
    conditions = Conditions(
        solvent=canonical_solvent(payload.get("solvent")),
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
        solvent=canonical_solvent(payload.get("solvent")),
        solvent_model="alpb" if payload.get("solvent") else "",
    )
    level = TheoryLevel(
        method=payload.get("method") or "unknown", family="semiempirical", engine="xtb"
    )
    unit = payload.get("unit") or ""
    # The coordinate names *which* internal coordinate, and the atom indices are what make that
    # unambiguous: "dihedral" alone does not say which dihedral. Folded into the label so the series
    # is self-describing without a join back to the payload.
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
    # The relaxed geometry at the minimum, as an address — the scan's output, in the same shape an
    # optimization's is. A `produced_structure` fact rather than a field on the record: the
    # record's own `structure_id` means the geometry the calculation ran ON, and overloading it
    # would answer a different question than the one a chemist holding a conformer address asks.
    produced = (payload.get("minimum_structure") or {}).get("structure_id") or ""
    facts = [*facts, *_kept(_text("produced_structure", produced))]
    extra: dict[str, Any] = {"properties": facts, "points": points}
    return subject, conditions, level, extra


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
        solvent=canonical_solvent(payload.get("solvent")),
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
        _flag("is_minimum", payload.get("is_minimum")),
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
    # An imaginary mode is a fact about the geometry, not a warning about the run: it says the
    # structure is a saddle point. Reported as negative wavenumbers, which is the convention the
    # result model itself uses.
    facts += [
        PropertyFact(property="imaginary_frequency", value=float(frequency), unit="cm^-1")
        for frequency in (payload.get("imaginary_frequencies_cm") or [])[:1]
    ]
    return subject, conditions, level, {"properties": facts, "points": points}


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
        solvent=canonical_solvent(payload.get("solvent")),
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
    """Fukui indices: three numbers per atom, ranked.

    All three are published per atom rather than only the ranked one, because the ranking is a
    presentation choice and the indices are the measurement.
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
    for site in payload.get("sites") or []:
        index, element = int(site["index"]), site.get("element", "")
        for key, name in (
            ("f_minus", "fukui_minus"),
            ("f_plus", "fukui_plus"),
            ("f_zero", "fukui_zero"),
        ):
            if site.get(key) is not None:
                sites.append(
                    SiteFact(atom_i=index, element=element, property=name, value=float(site[key]))
                )
    facts = _kept(
        _fact("atom_count", payload.get("total_atoms"), ""),
        _text("fukui_mode", payload.get("mode")),
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
        solvent=canonical_solvent(payload.get("solvent")),
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
        # None under GFN-FF, which reports no gradient. Absent stays absent.
        _fact("max_gradient", payload.get("max_gradient"), "hartree/bohr"),
        _fact("displacement_rms", payload.get("displacement_rms_angstrom"), "angstrom"),
    )
    # The geometry the optimization *produced*, as an address. Not a subject member: the subject is
    # what was asked about, and the relaxed structure is the answer. Published as a fact for the
    # same reason the scan's is — see `_scan`.
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


def _solubility(payload: dict[str, Any]) -> tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]:
    """A predicted aqueous solubility, carrying its applicability-domain flag.

    `estimate.in_domain` is the field `CALCULATION_EPOCH` was bumped for: a pre-change row validates
    back with `estimate=None`, and an out-of-domain salt then degrades silently to "not assessed".
    Publishing it as `in_domain` on the fact keeps that distinction visible — None is *unknown*,
    never *yes*.
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
    """A DFT energy from the HPC tier. The basis set is part of the level, not a condition."""
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


# What each projector is keyed by. Two vocabularies reach this module and they are deliberately
# kept apart:
#
# - `PAYLOAD_PROJECTORS` is keyed by the **pydantic model name**, which is what a durable job's
#   envelope and a caller holding a typed result can state exactly.
# - `_CALC_TYPE_PROJECTORS` is keyed by the **`calc_type` prefix** of a stored cache row, which is
#   all the backfill path has: `calculation_results` holds an untyped JSONB whose only clue to its
#   shape is the key it was stored under.
#
# Both resolve to the same functions, so the live path and the backfill path cannot disagree about
# what a payload means.
_Projector = Callable[[dict[str, Any]], tuple[Subject, Conditions, TheoryLevel, dict[str, Any]]]

PAYLOAD_PROJECTORS: dict[str, _Projector] = {
    "ReactionEnergyResult": _reaction,
    "SolventComparisonResult": _solvent_screen,
    "ConformerEnsemble": _ensemble,
    "EnsemblePayload": _ensemble,
    "InteractionResult": _interaction,
    "ScanResult": _scan,
    "ThermochemistryResult": _thermochemistry,
    "ElectronicProperties": _electronic_properties,
    "SiteReactivityResult": _site_reactivity,
    "OptimizationResult": _optimization,
    "OptimizationSummary": _optimization,
    "PkaResult": _pka,
    "SolubilityResult": _solubility,
    "LogdResult": _logd,
    "DescriptorProfile": _descriptors,
    "XtbResult": _single_point,
    "QMJobResult": _dft,
}

# Longest prefix wins, so `xtb.properties` reaches `_electronic_properties` rather than being
# swallowed by a shorter `xtb.` entry.
#
# **An entry here is a `calc_type` something has actually stamped** — today's server, or a release
# whose rows a deployment still holds. Four entries met neither test and were deleted:
# `descriptors`, `logd`, `xtb.thermo` and `xtb.energy` name spellings that no version of this
# system ever wrote (checked against `XtbTask` and each engine's `CALC_TYPE` before and after
# `D-2026-08-16-the-physics-leaves-the-cache-stays`; the descriptor panel has always stamped
# `developability`, and logD has never had a cache row at all). The cost was not the dead rows —
# it was that `descriptors` *looked* like the descriptor panel's route, so every
# `predict_developability_profile` result was dropped by `enqueue_payload` with a debug line while
# `test_publish_projection.py` exercised the dead spelling and never the live one.
#
# `xtb.scan` stays and is the reason the rule is not simply "what the server stamps now": `scan`
# was an `XtbTask` before the move and is not one today, and `calculation_results` is never
# pruned, so those rows are still there for the backfill to find. A retired calculator keeps its
# projector; a spelling that never existed does not get one.
_CALC_TYPE_PROJECTORS: tuple[tuple[str, _Projector], ...] = (
    ("xtb.properties", _electronic_properties),
    ("xtb.conformers", _ensemble),
    ("xtb.complex", _interaction),
    ("xtb.fukui", _site_reactivity),
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

    `payload_kind` wins when it is given, because a model name is exact while a `calc_type` prefix
    is an inference. Returning None rather than raising is deliberate: a deployment may hold rows
    from a calculator this release no longer ships (`calculation_results` is never pruned), and a
    backfill must skip those rather than abort on the first one.
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

    Raises `ProjectionError` when nothing here can read the payload — which is a code gap, and is
    reported as one rather than silently producing a record with no facts in it. A caller walking a
    whole corpus asks `projector_for` first and skips what it cannot read.
    """
    projector = projector_for(calc_type, payload_kind)
    if projector is None:
        raise ProjectionError(
            f"no projector for calc_type {calc_type!r} "
            f"(payload kind {payload_kind or 'unknown'!r}); "
            "add one to `PAYLOAD_PROJECTORS` and `_CALC_TYPE_PROJECTORS`"
        )
    subject, conditions, level, extra = projector(payload)
    # A geometry-keyed calculation records the structure it ran *on*. The server's own answer wins
    # when the caller has one; otherwise the subject's member carries it, which is the same value by
    # construction for every projector above.
    ran_on = structure_id or next(
        (member.structure_id for member in subject.members if member.structure_id), ""
    )
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
        properties=extra.get("properties", []),
        sites=extra.get("sites", []),
        points=extra.get("points", []),
        conformers=extra.get("conformers", []),
        candidates=extra.get("candidates", []),
        flags=extra.get("flags", []),
        provenance=provenance,
        compute_seconds=compute_seconds,
        computed_at=computed_at,
        depends_on=list(depends_on or []),
        payload=payload,
        payload_kind=payload_kind,
    )


def records_from_solvent_screen(
    *, calc_ref: str, payload: dict[str, Any], depends_on: list[str] | None = None, **common: Any
) -> list[ResultRecord]:
    """A solvent screen as its comparison **plus** one record per solvent it compared.

    **Never store an aggregate whose parts are not also stored.** A screen that published only its
    spread and its winner would leave "what was ΔG in acetonitrile" unanswerable even though the
    run computed it — and, worse, would make "compare this reaction across solvents" answer only
    over screens, missing every solvent run on its own. Emitting the parts as ordinary reaction
    records at their own conditions is what makes the cross-solvent question answer over the union.

    Each part is edged back to the comparison through `depends_on`, so the aggregate can be traced
    to the numbers behind it.
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


# The payload kinds whose projection is more than one record. Keyed by model name rather than by
# `calc_type` for the same reason the projector table is: a model name is exact, and a composite's
# `calc_type` is a route (`<connector>.<job>`) that names no shape.
# A multi-record emitter: same call shape as `project`, but returning the aggregate *and* its parts.
_MultiProjector = Callable[..., list[ResultRecord]]

_MULTI_RECORD_PROJECTORS: dict[str, _MultiProjector] = {
    "SolventComparisonResult": records_from_solvent_screen,
}


def records_for(
    *, calc_ref: str, calc_type: str, payload: dict[str, Any], payload_kind: str = "", **common: Any
) -> list[ResultRecord]:
    """Every record one stored payload becomes — usually one, sometimes an aggregate and its parts.

    **The one place that decides one-versus-many**, so no caller has to know which shapes decompose.
    Before this existed, `records_from_solvent_screen` was reachable only from tests: the three
    production hooks all called `project()` directly and got the comparison alone, which meant the
    rule that function's docstring states — never store an aggregate whose parts are not also
    stored — held nowhere a chemist could observe it.
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
