"""Every result shape this system produces is representable as a published record.

Each result model is projected and checked for the subject, conditions and facts it implies. The
coverage check tracks which payload keys each projector reads and pins the deliberately ignored
set, so a new result-model field fails as an unread key rather than being silently dropped.
"""

import ast
import copy
from pathlib import Path
from typing import Any

import pytest

from chemclaw.publish import project as projection
from chemclaw.publish.project import project
from chemclaw.publish.properties import REGISTRY, UNIT_CONVERSIONS
from chemclaw.publish.record import Conditions
from chemclaw.science.calc.models import (
    AtomCharge,
    BondDissociationSurvey,
    BondOrder,
    Conformer,
    ConformerEnsemble,
    DescriptorProfile,
    DissociatedBond,
    ElectronicProperties,
    EnsembleMember,
    EnsemblePayload,
    FailedBond,
    FailedMedium,
    FukuiSite,
    GlobalDescriptors,
    InteractionResult,
    LogdResult,
    MicrostatePka,
    OptimizationSummary,
    PkaResult,
    RankedSpecies,
    ReactionEnergyResult,
    Rotamer,
    RotationBarrier,
    RotationProfile,
    ScanPoint,
    ScanResult,
    SiteReactivityResult,
    SolubilityResult,
    SolventComparisonResult,
    SolventEffect,
    SpeciesDistribution,
    SpeciesEnergy,
    SpeciesSolventComparison,
    SpeciesSolventResponse,
    SpeciesStanding,
    Structure,
    ThermochemistryResult,
    VibrationalMode,
    XtbResult,
)
from chemclaw.science.calc.thermo import half_life_from_barrier

_PROJECT_MODULE = Path(projection.__file__)


def _structure(z: float = 1.0, smiles: str = "CCO") -> Structure:
    """A small valid geometry, enough to carry a `structure_id`."""
    return Structure(
        elements=[6, 1, 1, 1, 1],
        positions=[[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, z], [0, -1, 0]],
        smiles=smiles,
    )


def _with_structure_ids(payload: dict[str, Any], members: list[Any], key: str) -> dict[str, Any]:
    """Inject each member's `structure_id`, which is a property and so is not dumped.

    The composer does the same thing on the live path (`science/calc/geometry.py`), so this mirrors
    reality rather than working around it.
    """
    for dumped, member in zip(payload[key], members, strict=True):
        dumped["structure"]["structure_id"] = member.structure.structure_id
    return payload


def _reaction() -> ReactionEnergyResult:
    """A balanced reaction with a full per-species breakdown."""
    return ReactionEnergyResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        solvent="thf",
        temperature_k=298.15,
        level="standard",
        delta_e_kcal=-38.2,
        delta_h_kcal=-36.1,
        delta_g_kcal=-22.4,
        species=[
            SpeciesEnergy(
                smiles="C1CCCCC1",
                role="product",
                multiplicity=1,
                symmetry_number=12,
                electronic_energy_hartree=-38.7,
                enthalpy_hartree=-38.4,
                gibbs_free_energy_hartree=-38.5,
                is_minimum=True,
                was_cached=False,
                method="GFN2-xTB",
            ),
            SpeciesEnergy(
                smiles="C=C",
                role="reactant",
                multiplicity=1,
                symmetry_number=4,
                electronic_energy_hartree=-13.2,
                enthalpy_hartree=-13.1,
                gibbs_free_energy_hartree=-13.15,
                is_minimum=True,
                was_cached=True,
                method="GFN2-xTB",
            ),
        ],
        cache_hits=1,
        uncertainty_kcal=3.0,
        is_strongly_exothermic=True,
        exotherm_threshold_kcal=-20.0,
        conformer_treatment="single",
    )


def _distribution(species: list[tuple[str, str, float, float]]) -> SpeciesDistribution:
    """A ranked species set from `(smiles, label, relative_kcal, population)` tuples, so each test
    states which species are in it in one line.
    """
    return SpeciesDistribution(
        kind="microstates",
        method="GFN2-xTB",
        solvent="water",
        temperature_k=298.15,
        level="standard",
        species=[
            RankedSpecies(
                smiles=smiles,
                label=label,
                relative_kcal=relative,
                population=population,
                electronic_energy_hartree=-1.0,
            )
            for smiles, label, relative, population in species
        ],
        enumerated=len(species),
        uncertainty_kcal=1.5,
    )


def _microstate() -> MicrostatePka:
    """A macrostate pKa: two sampled ensembles reduced to one number.

    Its solvent is spelled the long way deliberately — this is the one projector of fifteen that
    stored the name as given, so the fixture has to carry an alias for that to be visible.
    """
    ensemble = ConformerEnsemble(
        smiles="Oc1ccccc1",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent="thf",
        temperature_k=298.15,
        conformers=[
            Conformer(relative_kcal=0.0, population=1.0, degeneracy=1, structure=_structure(1.0))
        ],
        total_found=1,
        conformational_entropy_cal_per_mol_k=0.0,
        ensemble_correction_kcal=0.0,
    )
    return MicrostatePka(
        smiles="Oc1ccccc1",
        branch="acid",
        pka=9.9,
        uncertainty=1.4,
        delta_g_kcal=21.6,
        site_smiles="[O-]c1ccccc1",
        method="CREST/GFN2-xTB",
        solvent="Tetrahydrofuran",
        temperature_k=298.15,
        neutral=ensemble,
        ionised=ensemble,
        microstates_found=4,
        microstates_within_rt=2,
        warnings=["two microstates carry population"],
    )


def _cases() -> list[tuple[str, str, Any, dict[str, Any]]]:
    """Every shape, with the payload each needs. `(payload_kind, calc_type, model, payload)`."""
    ensemble_members = [
        Conformer(relative_kcal=0.0, population=0.7, degeneracy=1, structure=_structure(1.0)),
        Conformer(relative_kcal=0.9, population=0.3, degeneracy=2, structure=_structure(1.1)),
    ]
    ensemble = ConformerEnsemble(
        smiles="CCO",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent="thf",
        temperature_k=298.15,
        conformers=ensemble_members,
        total_found=12,
        conformational_entropy_cal_per_mol_k=1.4,
        ensemble_correction_kcal=-0.4,
    )
    cached_members = [
        EnsembleMember(energy_hartree=-154.1, degeneracy=1, structure=_structure(1.0)),
        EnsembleMember(energy_hartree=-154.0, degeneracy=2, structure=_structure(1.1)),
    ]
    cached = EnsemblePayload(
        structure_id=_structure().structure_id,
        method="GFN2-xTB",
        solvent="thf",
        search="conformers",
        effort="quick",
        members=cached_members,
        total_found=12,
    )
    scan = ScanResult(
        smiles="CCCC",
        input_structure_id=_structure().structure_id,
        method="GFN2-xTB",
        solvent=None,
        coordinate="dihedral",
        atoms=[0, 1, 2, 3],
        unit="degrees",
        points=[
            ScanPoint(value=0.0, energy_hartree=-158.0, relative_kcal=0.0),
            ScanPoint(value=60.0, energy_hartree=-157.99, relative_kcal=2.8),
        ],
        minimum_value=0.0,
        maximum_relative_kcal=2.8,
        minimum_structure=_structure(),
    )
    rotation = RotationProfile(
        smiles="CCCC",
        input_structure_id=_structure().structure_id,
        method="GFN2-xTB",
        solvent=None,
        temperature_k=298.15,
        level="quick",
        torsion_id="tor_6b25409b2bd410a6",
        atoms=[0, 1, 2, 3],
        label="the C1-C2 bond",
        symmetry_order=1,
        period_degrees=360.0,
        points=[
            ScanPoint(value=60.0, energy_hartree=-158.0, relative_kcal=0.75),
            ScanPoint(value=120.0, energy_hartree=-157.996, relative_kcal=2.76),
            ScanPoint(value=180.0, energy_hartree=-158.001, relative_kcal=0.0),
        ],
        rotamers=[
            Rotamer(
                dihedral_degrees=180.0,
                structure_id=_structure().structure_id,
                relative_kcal=0.0,
                population=0.59,
                degeneracy=1,
            ),
            Rotamer(
                dihedral_degrees=60.0,
                structure_id=_structure().structure_id,
                relative_kcal=0.75,
                population=0.41,
                degeneracy=1,
            ),
        ],
        barriers=[
            RotationBarrier(
                from_rotamer=0,
                to_rotamer=1,
                at_degrees=120.0,
                forward_kcal=2.76,
                reverse_kcal=2.01,
                basis="E",
                interconversion=half_life_from_barrier(2.76, 298.15),
            )
        ],
        highest_barrier_kcal=2.76,
        uncertainty_kcal=3.0,
        warnings=[],
    )
    scan_payload = scan.model_dump(mode="json")
    scan_payload["minimum_structure"]["structure_id"] = scan.minimum_structure.structure_id
    interaction = InteractionResult(
        smiles_a="CCO",
        smiles_b="O",
        method="GFN2-xTB",
        solvent="water",
        interaction_energy_kcal=-5.2,
        complex_energy_hartree=-30.0,
        monomer_energies_hartree=[-20.0, -10.0],
        binding_modes=3,
        structure=_structure(),
    )
    interaction_payload = interaction.model_dump(mode="json")
    interaction_payload["structure"]["structure_id"] = interaction.structure.structure_id

    ranked = [
        RankedSpecies(
            smiles="CC(=O)CC(C)=O",
            label="keto",
            relative_kcal=0.0,
            population=0.82,
            gibbs_free_energy_hartree=-267.2,
            electronic_energy_hartree=-267.3,
            structure_id=_structure().structure_id,
            conformers_found=4,
        ),
        RankedSpecies(
            smiles="CC(=O)C=C(C)O",
            label="enol",
            relative_kcal=0.9,
            population=0.18,
            gibbs_free_energy_hartree=-267.1,
            electronic_energy_hartree=-267.2,
            structure_id=_structure(1.1).structure_id,
            conformers_found=3,
        ),
    ]
    distribution = SpeciesDistribution(
        kind="tautomers",
        method="GFN2-xTB",
        solvent="thf",
        temperature_k=298.15,
        level="standard",
        species=ranked,
        enumerated=3,
        uncertainty_kcal=3.0,
        sampled=True,
    )
    gas_phase = distribution.model_copy(update={"solvent": None})
    simple: list[tuple[str, str, Any]] = [
        ("ReactionEnergyResult", "reaction.energy", _reaction()),
        ("SpeciesDistribution", "calc.rank_species", distribution),
        (
            "SpeciesSolventComparison",
            "calc.rank_species_across_solvents",
            SpeciesSolventComparison(
                kind="tautomers",
                method="GFN2-xTB",
                temperature_k=298.15,
                level="standard",
                distributions=[gas_phase, distribution],
                responses=[
                    SpeciesSolventResponse(
                        smiles="CC(=O)CC(C)=O",
                        label="keto",
                        standings=[
                            SpeciesStanding(solvent=None, relative_kcal=0.0, population=0.9),
                            SpeciesStanding(solvent="thf", relative_kcal=0.0, population=0.82),
                        ],
                        population_swing=0.08,
                        relative_swing_kcal=0.0,
                    )
                ],
                dominance_changes=False,
                largest_swing_kcal=0.4,
                uncertainty_kcal=3.0,
            ),
        ),
        (
            "BondDissociationSurvey",
            "calc.survey_bond_strengths",
            BondDissociationSurvey(
                smiles="CCc1ccccc1",
                method="GFN2-xTB",
                solvent=None,
                temperature_k=298.15,
                mode="homolytic",
                bonds=[
                    DissociatedBond(
                        atoms=[1, 2],
                        bond="C-C",
                        fragments=["[CH2]C", "[c]1ccccc1"],
                        dissociation_energy_kcal=101.0,
                        is_weakest=True,
                    )
                ],
                considered=2,
                uncertainty_kcal=5.0,
                failed=[
                    FailedBond(
                        atoms=[0, 1],
                        bond="C-C",
                        fragments=["[CH2]c1ccccc1", "[CH3]"],
                        reason="the optimisation did not converge",
                    )
                ],
            ),
        ),
        (
            "SolventComparisonResult",
            "reaction.solvent_screen",
            SolventComparisonResult(
                reactants=["C=C"],
                products=["CO"],
                method="GFN2-xTB",
                temperature_k=298.15,
                level="standard",
                effects=[
                    SolventEffect(
                        solvent="thf", delta_e_kcal=-1.0, delta_h_kcal=None, delta_g_kcal=-1.0
                    )
                ],
                best_solvent="thf",
                spread_kcal=0.5,
                uncertainty_kcal=3.0,
            ),
        ),
        (
            "ThermochemistryResult",
            "xtb.thermo",
            ThermochemistryResult(
                smiles="CCO",
                structure_id=_structure().structure_id,
                method="GFN2-xTB",
                solvent="thf",
                temperature_k=298.15,
                pressure_pa=101325.0,
                symmetry_number=1,
                is_minimum=True,
                imaginary_frequencies_cm=[],
                modes=[
                    VibrationalMode(wavenumber_cm=1200.0, ir_intensity_km_per_mol=15.0),
                    VibrationalMode(wavenumber_cm=2900.0, ir_intensity_km_per_mol=42.0),
                ],
                mode_count=2,
                lowest_wavenumbers_cm=[1200.0],
                electronic_energy_hartree=-154.0,
                zero_point_energy_kcal=50.1,
                thermal_enthalpy_correction_kcal=3.2,
                entropy_cal_per_mol_k=70.0,
                gibbs_correction_kcal=32.0,
                enthalpy_hartree=-153.9,
                gibbs_free_energy_hartree=-153.95,
                uncertainty_kcal=2.0,
            ),
        ),
        (
            "ElectronicProperties",
            "xtb.properties",
            ElectronicProperties(
                smiles="CCO",
                structure_id=_structure().structure_id,
                method="GFN2-xTB",
                solvent=None,
                total_energy_hartree=-154.0,
                homo_ev=-10.2,
                lumo_ev=1.1,
                gap_ev=11.3,
                dipole_debye=1.7,
                atom_charges=[
                    AtomCharge(
                        index=0,
                        element="C",
                        charge=-0.12,
                        wiberg_valence=3.94,
                        free_valence=0.06,
                    )
                ],
                bond_orders=[BondOrder(atom_i=0, atom_j=1, order=0.98)],
            ),
        ),
        (
            "SiteReactivityResult",
            "xtb.fukui",
            SiteReactivityResult(
                smiles="c1ccccc1",
                structure_id=_structure().structure_id,
                method="GFN2-xTB",
                solvent=None,
                mode="electrophilic",
                ranked_by="f_minus",
                total_atoms=12,
                descriptors=GlobalDescriptors(
                    ionization_potential_ev=13.5,
                    electron_affinity_ev=3.0,
                    chemical_potential_ev=-8.25,
                    hardness_ev=10.5,
                    softness_per_ev=0.0952,
                    electrophilicity_ev=3.24,
                ),
                sites=[
                    FukuiSite(
                        index=0,
                        element="C",
                        f_minus=0.11,
                        f_plus=0.09,
                        f_zero=0.10,
                        dual=-0.02,
                        local_softness_minus=0.010472,
                        local_softness_plus=0.008568,
                        local_electrophilicity_ev=0.2916,
                    )
                ],
            ),
        ),
        (
            "OptimizationSummary",
            "xtb.opt",
            OptimizationSummary(
                smiles="CCO",
                structure_id=_structure().structure_id,
                method="GFN2-xTB",
                engine="tblite",
                solvent="thf",
                energy_hartree=-154.0,
                relaxation_kcal=3.4,
                steps=12,
                max_gradient=0.0004,
                displacement_rms_angstrom=0.08,
            ),
        ),
        (
            "PkaResult",
            "pka",
            PkaResult(
                smiles="CC(=O)O",
                method="GFN2-xTB",
                pka=4.76,
                deprotonation_energy_kcal=340.0,
                uncertainty=1.2,
                site="acid",
            ),
        ),
        ("MicrostatePka", "calc.predict_microstate_pka", _microstate()),
        (
            "SolubilityResult",
            "solubility",
            SolubilityResult(
                smiles="CCO",
                model="esol-delaney@2004",
                log_s_mol_per_l=-0.24,
                uncertainty_log=0.6,
            ),
        ),
        (
            "LogdResult",
            "logd",
            LogdResult(smiles="CC(=O)O", ph=7.4, clogp=0.2, pka=4.76, log_d=-2.4, uncertainty=0.9),
        ),
        (
            "DescriptorProfile",
            "descriptors",
            DescriptorProfile(
                smiles="CCO",
                molecular_weight=46.07,
                clogp=-0.0014,
                tpsa=20.23,
                h_bond_donors=1,
                h_bond_acceptors=1,
                rotatable_bonds=0,
                aromatic_rings=0,
                fraction_csp3=1.0,
                qed=0.41,
                lipinski_violations=0,
                veber_pass=True,
            ),
        ),
        (
            "XtbResult",
            "xtb.energy",
            XtbResult(smiles="CCO", method="GFN2-xTB", charge=0, total_energy_hartree=-154.0),
        ),
    ]
    cases = [(kind, ctype, model, model.model_dump(mode="json")) for kind, ctype, model in simple]
    cases.append(
        (
            "ConformerEnsemble",
            "xtb.conformers",
            ensemble,
            _with_structure_ids(ensemble.model_dump(mode="json"), ensemble_members, "conformers"),
        )
    )
    cases.append(
        (
            "EnsemblePayload",
            "xtb.conformers",
            cached,
            _with_structure_ids(cached.model_dump(mode="json"), cached_members, "members"),
        )
    )
    cases.append(("ScanResult", "xtb.scan", scan, scan_payload))
    cases.append(
        ("RotationProfile", "calc.profile_rotation", rotation, rotation.model_dump(mode="json"))
    )
    cases.append(("InteractionResult", "xtb.complex", interaction, interaction_payload))
    return cases


@pytest.mark.parametrize(
    ("kind", "calc_type", "payload"),
    [(kind, calc_type, payload) for kind, calc_type, _, payload in _cases()],
    ids=[kind for kind, _, _, _ in _cases()],
)
def test_every_result_shape_projects(kind: str, calc_type: str, payload: dict[str, Any]) -> None:
    """Every shape produces a valid record whose facts all name registered properties.

    The registry check is the important half: a fact under an unregistered name would be written
    to a column no query filters on, so it would look stored and be invisible.
    """
    record = project(
        calc_ref=f"{calc_type}@v1:aaa:bbb",
        calc_type=calc_type,
        payload=payload,
        payload_kind=kind,
    )
    assert record.subject.members, f"{kind} produced a subject with no members"
    facts = (
        [(f.property, f.scope) for f in record.properties]
        + [(f.property, "site") for f in record.sites]
        + [(f.property, "point") for f in record.points]
    )
    assert facts or record.conformers, f"{kind} produced no facts at all"
    for name, _ in facts:
        assert name in REGISTRY, f"{kind} published unregistered property {name!r}"
    # The payload rides along untouched, which is what makes a projector bug a re-projection
    # rather than lost science.
    assert record.payload == payload


#: The field that makes two copies of one fixture item distinct, per grown shape. Written out so a
#: renamed field fails rather than being silently skipped.
_DISTINGUISHING = {"sites": "index", "points": "value", "conformers": "relative_kcal"}


def _grown(kind: str, calc_type: str, key: str, count: int) -> dict[str, int]:
    """Project one shape with `count` items under `key`, and count the rows per result-store table.

    Copies of the fixture's own item, made distinguishable through `_DISTINGUISHING` (`project` does
    not dedupe, so distinctness only aids reading a failure). The assertion fails if a
    distinguishing field stops existing.
    """
    payload = next(p for k, c, _m, p in _cases() if k == kind and c == calc_type)
    items = payload[key]
    grown = [copy.deepcopy(items[index % len(items)]) for index in range(count)]
    field = _DISTINGUISHING[key]
    for index, item in enumerate(grown):
        assert isinstance(item, dict) and field in item, (
            f"a {key} item carries no `{field}`, so these copies are indistinguishable and this "
            "helper is measuring dedup rather than growth"
        )
        item[field] = index if isinstance(item[field], int) else float(index)
    assert len({item[field] for item in grown}) == count, "the copies did not come out distinct"
    scaled = copy.deepcopy(payload) | {key: grown}
    record = project(
        calc_ref=f"{calc_type}@v1:a:b", calc_type=calc_type, payload=scaled, payload_kind=kind
    )
    return {
        "property_value": len(record.properties),
        "calculation_site_value": len(record.sites),
        "calculation_point_value": len(record.points),
        "conformer": len(record.conformers),
    }


@pytest.mark.parametrize(
    ("kind", "calc_type", "key", "table", "per_item"),
    [
        ("SiteReactivityResult", "xtb.fukui", "sites", "calculation_site_value", 7),
        ("ScanResult", "xtb.scan", "points", "calculation_point_value", 2),
        ("ConformerEnsemble", "xtb.conformers", "conformers", "conformer", 1),
    ],
)
def test_a_result_projects_a_fixed_number_of_rows_per_item(
    kind: str, calc_type: str, key: str, table: str, per_item: int
) -> None:
    """How many rows one calculation becomes, as a per-item law
    (`D-2026-09-14-property-value-is-the-shallow-table`).

    The per-item slope is the cost that decides partitioning: one more fact per item multiplies the
    store by the item count.
    """
    one = _grown(kind, calc_type, key, 1)
    many = _grown(kind, calc_type, key, 47)
    assert (many[table] - one[table]) / 46 == per_item, (
        f"{calc_type} now projects {(many[table] - one[table]) / 46} rows per item into {table}, "
        f"not {per_item}. A corpus of these grows by that factor."
    )


@pytest.mark.parametrize(
    ("kind", "calc_type", "key"),
    [
        ("SiteReactivityResult", "xtb.fukui", "sites"),
        ("ScanResult", "xtb.scan", "points"),
        ("ConformerEnsemble", "xtb.conformers", "conformers"),
    ],
)
def test_property_value_does_not_grow_with_the_size_of_a_calculation(
    kind: str, calc_type: str, key: str
) -> None:
    """`property_value` does not grow with the size of a calculation.

    It is per result, not per item; per-item growth lands in tables such as
    `calculation_site_value`, so `property_value` is not the table to partition.
    """
    assert (
        _grown(kind, calc_type, key, 1)["property_value"]
        == (_grown(kind, calc_type, key, 47)["property_value"])
    )


def test_a_reaction_attaches_each_species_energy_to_the_right_member() -> None:
    """Per-species facts are matched by (role, molecule), never by list position.

    The fixture lists the product first while the equation lists reactants first, so index matching
    would attach plausible numbers to the wrong member.
    """
    reaction = _reaction()
    record = project(
        calc_ref="rxn",
        calc_type="reaction.energy",
        payload=reaction.model_dump(mode="json"),
        payload_kind="ReactionEnergyResult",
    )
    by_ordinal = {member.ordinal: member for member in record.subject.members}
    gibbs = {
        by_ordinal[f.member_ordinal].smiles: f.value
        for f in record.properties
        if f.property == "gibbs_free_energy" and f.member_ordinal is not None
    }
    assert gibbs == {"C=C": pytest.approx(-13.15), "C1CCCCC1": pytest.approx(-38.5)}
    # Butadiene has no species entry, so it correctly carries no per-species facts at all.
    assert "C=CC=C" not in gibbs


def test_an_absent_number_is_never_substituted() -> None:
    """An absent number is never substituted.

    `delta_g_kcal` is None at `quick` level; falling back to `delta_e_kcal` would publish an
    electronic energy as a free energy.
    """
    quick = _reaction().model_copy(update={"delta_g_kcal": None, "delta_h_kcal": None})
    record = project(
        calc_ref="rxn-quick",
        calc_type="reaction.energy",
        payload=quick.model_dump(mode="json"),
        payload_kind="ReactionEnergyResult",
    )
    published = {f.property for f in record.properties if f.scope == "calculation"}
    assert "reaction_delta_e" in published
    assert "reaction_delta_g" not in published
    assert "reaction_delta_h" not in published


def test_both_ensemble_shapes_project() -> None:
    """Both ensemble shapes project.

    `EnsembleMember` has `energy_hartree` and no population; `Conformer` has `relative_kcal` and
    `population` and no absolute energy. Requiring either would make half the ensembles
    unpublishable.
    """
    by_kind = {kind: (ctype, payload) for kind, ctype, _, payload in _cases()}
    weighted_type, weighted_payload = by_kind["ConformerEnsemble"]
    cached_type, cached_payload = by_kind["EnsemblePayload"]

    weighted = project(
        calc_ref="ens-w",
        calc_type=weighted_type,
        payload=weighted_payload,
        payload_kind="ConformerEnsemble",
    )
    assert [c.population for c in weighted.conformers] == [0.7, 0.3]
    assert all(c.energy_hartree is None for c in weighted.conformers)

    cached = project(
        calc_ref="ens-c",
        calc_type=cached_type,
        payload=cached_payload,
        payload_kind="EnsemblePayload",
    )
    assert [c.energy_hartree for c in cached.conformers] == [-154.1, -154.0]
    assert all(c.population is None for c in cached.conformers)
    # The cached shape names no molecule at all — only the seed geometry it searched from.
    assert cached.subject.members[0].structure_id.startswith("st_")


def test_a_solvent_name_is_canonicalized_at_projection() -> None:
    """Two accepted spellings of one solvent reach the record as one id.

    Not cosmetic: the calculation layer accepts both and passes the name through verbatim, so a
    record that stored the given name would make "every reaction in THF" answer with a subset.
    """
    long_form = _reaction().model_copy(update={"solvent": "tetrahydrofuran"})
    short_form = _reaction()
    both = {
        project(
            calc_ref=ref,
            calc_type="reaction.energy",
            payload=model.model_dump(mode="json"),
            payload_kind="ReactionEnergyResult",
        ).conditions.solvent
        for ref, model in (("a", long_form), ("b", short_form))
    }
    assert both == {"thf"}


def test_a_microstate_pka_publishes_the_free_energy_the_number_is_a_map_of() -> None:
    """A microstate pKa publishes the free energy the number is a map of.

    Every property the projector emits must be registered, or `_fact` raises on every payload.
    `branch` is not registered separately: it is `PkaResult.site` under another name, and two names
    would split one property. The winning microstate's constitution is a distinct fact with its own
    name.
    """
    record = project(
        calc_ref="microstate@v1:a:b",
        calc_type="calc.predict_microstate_pka",
        payload=_microstate().model_dump(mode="json"),
        payload_kind="MicrostatePka",
    )

    facts = {fact.property: fact for fact in record.properties}
    assert facts["pka"].value == 9.9
    assert facts["pka"].uncertainty == 1.4, "a semiempirical pKa without its error bar is a claim"
    assert facts["deprotonation_free_energy"].value == 21.6, (
        "the pKa is a linear map of this number, and a refit changes one without changing the other"
    )
    assert facts["microstates_within_rt"].value == 2, (
        "more than one microstate within RT is why this is a macrostate pKa rather than a "
        "site-resolved one — the caveat has to travel with the number"
    )
    assert facts["species_enumerated"].value == 4
    assert facts["pka_site"].value_text == "acid", (
        "which equilibrium was computed is the same fact `predict_pka` publishes under this name; "
        "a second name for it would make 'every base pKa we computed' answer over one pipeline"
    )
    assert facts["ionised_microstate"].value_text == "[O-]c1ccccc1", (
        "which proton came off is the half of a pKa a bare number does not carry"
    )
    assert [flag.message for flag in record.flags] == ["two microstates carry population"]

    # F5: the one projector of fifteen that stored the solvent as given. `Tetrahydrofuran` and
    # `thf` are one solvent, and the store's `solvent_id` is minted straight from this field.
    assert record.conditions.solvent == "thf"
    assert record.conditions.temperature_k == 298.15
    assert record.subject.kind == "molecule", "the ensembles are how it was computed, not what "
    "it is about"


def test_a_condition_set_canonicalizes_its_own_solvent() -> None:
    """A condition set canonicalizes its own solvent, structurally.

    Canonicalization happens in `Conditions` at write time, not per projector, so one solvent under
    two spellings cannot land under two `condition_id`s.
    """
    assert Conditions(solvent=" Tetrahydrofuran ").solvent == "thf"
    assert Conditions(solvent="H2O").solvent == "water"
    # Gas phase is a real state, not a missing value, and stays distinguishable from an empty name.
    assert Conditions(solvent=None).solvent is None
    assert Conditions(solvent="  ").solvent is None
    # An unrecognised solvent is still a fact about the run: normalized, never rejected.
    assert Conditions(solvent="Cyclopentyl methyl ether").solvent == "cyclopentyl methyl ether"


def test_a_solvent_screen_publishes_its_parts_as_well_as_its_aggregate() -> None:
    """A solvent screen publishes its parts as well as its aggregate.

    Otherwise "what was delta-G in DMSO" would be unanswerable and cross-solvent queries would miss
    every part.
    """
    from chemclaw.publish.project import records_from_solvent_screen

    screen = SolventComparisonResult(
        reactants=["C=C"],
        products=["CO"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=[
            SolventEffect(
                solvent="dmso", delta_e_kcal=-38.0, delta_h_kcal=None, delta_g_kcal=-19.9
            ),
            SolventEffect(
                solvent="toluene", delta_e_kcal=-37.5, delta_h_kcal=None, delta_g_kcal=-24.8
            ),
        ],
        best_solvent="toluene",
        spread_kcal=4.9,
        uncertainty_kcal=3.0,
    )
    records = records_from_solvent_screen(
        calc_ref="screen-1",
        payload=screen.model_dump(mode="json"),
        calc_type="reaction.solvent_screen",
    )
    assert len(records) == 3, "the comparison plus one record per solvent compared"
    parts = records[1:]
    assert [r.conditions.solvent for r in parts] == ["dmso", "toluene"]
    # Every part is edged back to the comparison, so the aggregate is traceable to its numbers.
    assert all(r.depends_on == ["screen-1"] for r in parts)
    # And each part carries a real free energy, which is what makes it answerable on its own.
    for part in parts:
        assert any(f.property == "reaction_delta_g" for f in part.properties)


def test_a_species_solvent_screen_publishes_each_medium_as_its_own_distribution() -> None:
    """The same rule as the reaction screen: never store an aggregate whose parts are not stored.

    Here the parts are the distributions verbatim, so "which tautomer dominates in DMSO" answers
    over media screened together *and* over the medium computed on its own — one shape, both routes.
    """
    from chemclaw.publish.project import records_from_species_solvent_screen

    ranked = [
        RankedSpecies(
            smiles="CC(=O)CC(C)=O",
            label="keto",
            relative_kcal=0.0,
            population=0.8,
            electronic_energy_hartree=-267.3,
        ),
        RankedSpecies(
            smiles="CC(=O)C=C(C)O",
            label="enol",
            relative_kcal=1.0,
            population=0.2,
            electronic_energy_hartree=-267.2,
        ),
    ]

    def _in(solvent: str | None) -> SpeciesDistribution:
        return SpeciesDistribution(
            kind="tautomers",
            method="GFN2-xTB",
            solvent=solvent,
            temperature_k=298.15,
            level="standard",
            species=ranked,
            enumerated=2,
            uncertainty_kcal=3.0,
        )

    screen = SpeciesSolventComparison(
        kind="tautomers",
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        distributions=[_in(None), _in("water"), _in("toluene")],
        responses=[],
        dominance_changes=True,
        largest_swing_kcal=4.2,
        uncertainty_kcal=3.0,
    )
    records = records_from_species_solvent_screen(
        calc_ref="screen-9",
        payload=screen.model_dump(mode="json"),
        calc_type="calc.rank_species_across_solvents",
    )

    assert len(records) == 4, "the comparison plus one distribution per medium"
    parts = records[1:]
    # `solvent=None` is the gas phase — a real state, per `Conditions` — not a missing value.
    assert [record.conditions.solvent for record in parts] == [None, "water", "toluene"]
    assert all(record.depends_on == ["screen-9"] for record in parts)
    # Each part stands on its own: the populations are what a distribution is for, and they reach
    # the record as ranked candidates rather than as property facts.
    for part in parts:
        assert [candidate.score for candidate in part.candidates] == [0.8, 0.2]
        assert {candidate.detail["label"] for candidate in part.candidates} == {"keto", "enol"}
    # And the aggregate carries the finding, as a flag rather than a number.
    assert any(flag.flag == "dominance_changes_with_medium" for flag in records[0].flags)
    # The comparison itself carries no solvent, the same as a reaction screen's aggregate: it is
    # *about* the media rather than run in one.
    assert records[0].conditions.solvent is None


class _TrackingDict(dict[str, Any]):
    """A payload that records which keys a projector read.

    The mechanism behind the coverage test below: rather than eyeballing a model's field list
    against a projector, this measures it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        """Record the read, then behave as a dict."""
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        """Record the read, then behave as a dict."""
        self.read.add(key)
        return super().__getitem__(key)


# Fields a projector deliberately does not publish, with the reason. **Anything not listed here
# that a projector fails to read is a coverage gap**, and the test below fails on it — which is how
# a result model gaining a field is caught rather than silently dropped.
_DELIBERATELY_UNREAD: dict[str, dict[str, str]] = {
    "ReactionEnergyResult": {},
    "SolventComparisonResult": {
        "effects": "read by `records_from_solvent_screen`, which publishes each as its own record"
    },
    "ThermochemistryResult": {
        "imaginary_displacement": "a 3N vector of refinement machinery; the tool itself nulls it",
        "lowest_wavenumbers_cm": "a derived view of `modes`, all of which are published as points",
        "is_stationary": (
            "the second half of `is_minimum`, which *is* published: a geometry that is not a "
            "stationary point is not a minimum, and the flag now says so. Publishing both would "
            "put two booleans about one finding into the record, and the one a reader acts on is "
            "the one that already answers 'may I use this free energy?'"
        ),
        "max_gradient_hartree_per_angstrom": (
            "the evidence behind that flag, in Hartree/Angstrom — deliberately not published under "
            "the existing `max_gradient` property, which is registered in hartree/bohr and belongs "
            "to the optimization that produced the geometry. One property name for two units is "
            "the silent-wrong-number shape this whole registry exists to prevent"
        ),
    },
    "ElectronicProperties": {},
    "SiteReactivityResult": {
        "ranked_by": "restates `mode`, which is published as `fukui_mode`",
    },
    "OptimizationSummary": {},
    "ScanResult": {},
    "RotationProfile": {
        "warnings": (
            "advice to a reader about this profile's own resolution, not a property of the "
            "molecule — the same treatment every other result's warnings get here"
        ),
        "input_structure_id": "read as the subject member's structure_id, like every other shape",
        "uncertainty_kcal": "published as the barrier fact's own uncertainty, not as a fact",
        "highest_barrier_kcal": (
            "a summary of `barriers` for a reader, and deliberately not what is published: the "
            "`rotational_barrier` fact is the barrier *out of the most populated well*, which is "
            "what decides configurational stability, and on n-butane that is a different pass "
            "from the profile's highest"
        ),
        "atoms": "folded into the point series' x_label, exactly as a scan's are",
    },
    "InteractionResult": {
        "sampled": "a Literal[True] marker; constant, so it carries no information",
    },
    "PkaResult": {},
    "MicrostatePka": {
        "neutral": (
            "the sampled evidence on the protonated side: a full `ConformerEnsemble` that the "
            "CREST search publishes under its own `xtb.conformers` key, so reading it here would "
            "store one ensemble twice"
        ),
        "ionised": "the same, on the deprotonated side",
    },
    "SolubilityResult": {},
    "LogdResult": {},
    "DescriptorProfile": {},
    "XtbResult": {},
    "ConformerEnsemble": {
        "sampled": "a Literal[True] marker; constant, so it carries no information",
        "lowest_structure_id": (
            "a computed view of conformers[0], and every member is published with its ordinal — "
            "ordinal 0 is the lowest, so storing it again would be a second copy of one fact"
        ),
    },
    "EnsemblePayload": {},
    "SpeciesDistribution": {
        "sampled": (
            "whether a conformer search ran under each species, which `reaction_level` already "
            "says — it is true exactly at level='thorough', so publishing both would store one "
            "fact twice"
        )
    },
    "BondDissociationSurvey": {},
    "SpeciesSolventComparison": {
        "responses": (
            "the transpose of `distributions`, each of which publishes as its own record — "
            "reading it too would store every relative energy and population twice"
        )
    },
}


@pytest.mark.parametrize(
    ("kind", "payload"),
    [(kind, payload) for kind, _, _, payload in _cases() if kind in _DELIBERATELY_UNREAD],
    ids=[kind for kind, _, _, _ in _cases() if kind in _DELIBERATELY_UNREAD],
)
def test_every_model_field_is_read_or_deliberately_ignored(
    kind: str, payload: dict[str, Any]
) -> None:
    """No result-model field is silently dropped on the way into the published record.

    The payload records which keys the projector touched; anything ignored must be listed above with
    a reason.
    """
    tracked = _TrackingDict(payload)
    projection.PAYLOAD_PROJECTORS[kind](tracked)
    unread = set(tracked) - tracked.read
    allowed = set(_DELIBERATELY_UNREAD[kind])
    assert unread <= allowed, (
        f"{kind} has field(s) no projector reads: {sorted(unread - allowed)}. "
        "Either publish them, or add them to `_DELIBERATELY_UNREAD` with the reason."
    )
    assert allowed <= set(payload), (
        f"{kind} lists {sorted(allowed - set(payload))} as deliberately unread, but the model has "
        "no such field — the exemption has outlived its reason and should be deleted."
    )


def test_the_conversion_guard_is_load_bearing_on_a_live_path() -> None:
    """The unit-conversion guard is load-bearing on a live path.

    Scans `project.py` for `_fact` call sites whose literal unit is not the property's canonical
    unit and asserts the set is non-empty, without pinning a count
    (D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose).
    """
    source = ast.parse(_PROJECT_MODULE.read_text(encoding="utf-8"))
    converting: set[tuple[str, str]] = set()
    for call in ast.walk(source):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_fact"
            and len(call.args) >= 3
        ):
            continue
        name_node, unit_node = call.args[0], call.args[2]
        if not (isinstance(name_node, ast.Constant) and isinstance(unit_node, ast.Constant)):
            continue  # a computed property name or unit; this scan reads literals only
        name, unit = name_node.value, unit_node.value
        if not (isinstance(name, str) and isinstance(unit, str)):
            continue
        definition = REGISTRY.get(name)
        if definition is not None and unit != definition.canonical_unit:
            converting.add((name, unit))
    assert converting, (
        "no `_fact` call site reports a non-canonical unit, so `to_canonical` is an identity on "
        "every live path again. That is not a failure in itself — but the guard's justification "
        "now rests on a future caller rather than a present one, and this test should be rewritten "
        "to say so rather than deleted."
    )
    for name, unit in sorted(converting):
        assert (unit, REGISTRY[name].canonical_unit) in UNIT_CONVERSIONS, (
            f"`{name}` is projected in `{unit}` but `UNIT_CONVERSIONS` has no path to "
            f"`{REGISTRY[name].canonical_unit}`, so `to_canonical` raises on a live path"
        )


def test_a_fact_reported_in_a_non_canonical_unit_is_converted_before_it_is_published() -> None:
    """A fact reported in a non-canonical unit is converted before it is published.

    `value` is the predicate column, so it must be canonical: an energy in hartree or kJ/mol would
    be off by 627.5 or 4.184 while passing range filters. The reported value and unit are asserted
    too, so the original stays recoverable.
    """
    fact = projection._fact("reaction_delta_g", -0.02, "hartree")
    assert fact is not None
    assert fact.value == pytest.approx(-12.5502, abs=1e-3), (
        "a hartree reached `value_canonical` unconverted; every kcal/mol predicate over this "
        "property now silently excludes or includes the row"
    )
    assert (fact.reported_value, fact.unit) == (-0.02, "hartree"), (
        "the number the calculator reported is unrecoverable, so a conversion found wrong later "
        "cannot be rebuilt from the row"
    )

    # The identity path, so the assertion above cannot be satisfied by converting everything.
    same = projection._fact("reaction_delta_g", -12.5, "kcal/mol")
    assert same is not None and same.value == -12.5 and same.reported_value == -12.5


def test_two_tautomers_are_two_members_and_two_subjects() -> None:
    """Two tautomers are two members and two subjects.

    `core.chem.compound_id` deliberately collapses tautomers (standardized SMILES), but a
    `SpeciesDistribution` exists to tell such species apart, so `Subject.subject_id` hashes each
    member's own SMILES first. Otherwise two enumerations share a `subject_id` and the second
    overwrites the first's members.
    """
    ph4 = _distribution([("OC(=O)CC(=O)O", "H2A", 0.0, 0.6), ("[O-]C(=O)CC(=O)O", "HA-", 0.4, 0.4)])
    ph9 = _distribution(
        [("OC(=O)CC(=O)O", "H2A", 0.0, 0.7), ("[O-]C(=O)CC(=O)[O-]", "A2-", 0.9, 0.3)]
    )
    records = [
        project(
            calc_ref=ref,
            calc_type="calc.microstates",
            payload=payload.model_dump(mode="json"),
            payload_kind="SpeciesDistribution",
        )
        for ref, payload in (("job-ph4", ph4), ("job-ph9", ph9))
    ]

    assert records[0].subject_id != records[1].subject_id
    # And the collapse the old hash rested on is still real, which is why this needed a fix at all.
    from chemclaw.core.chem import compound_id

    assert compound_id("[O-]C(=O)CC(=O)O") == compound_id("[O-]C(=O)CC(=O)[O-]")


def test_a_reaction_between_tautomers_attaches_each_energy_to_its_own_member() -> None:
    """A reaction between tautomers attaches each energy to its own member.

    Tautomers share a `compound_id`, so `_member_for` matches the exact SMILES before the coarse id.
    """
    record = project(
        calc_ref="rxn-tautomers",
        calc_type="reaction.energy",
        payload={
            "reactants": ["CC(=O)CC(C)=O", "CC(=O)C=C(C)O"],
            "products": ["O"],
            "method": "GFN2-xTB",
            "delta_e_kcal": -1.0,
            "species": [
                {
                    "smiles": "CC(=O)C=C(C)O",
                    "role": "reactant",
                    "electronic_energy_hartree": -111.0,
                },
                {
                    "smiles": "CC(=O)CC(C)=O",
                    "role": "reactant",
                    "electronic_energy_hartree": -222.0,
                },
            ],
        },
        payload_kind="ReactionEnergyResult",
    )
    by_ordinal = {member.ordinal: member for member in record.subject.members}
    energies = {
        by_ordinal[f.member_ordinal].smiles: f.value
        for f in record.properties
        if f.property == "electronic_energy" and f.member_ordinal is not None
    }
    assert energies == {"CC(=O)C=C(C)O": -111.0, "CC(=O)CC(C)=O": -222.0}


def test_a_species_distribution_publishes_the_gap_and_not_a_constant_zero() -> None:
    """A species distribution publishes the gap, not a constant zero.

    The ranking is sorted by relative energy, so `species[0].relative_kcal` is always 0.0. The
    useful number is how far the runner-up sits above the winner, compared against method
    uncertainty, and it has its own property name (`relative_energy` is per-conformer).
    """
    record = project(
        calc_ref="tautomers",
        calc_type="calc.rank_species",
        payload=_distribution(
            [("CC(=O)CC(C)=O", "keto", 0.0, 0.9), ("CC(=O)C=C(C)O", "enol", 3.7, 0.1)]
        ).model_dump(mode="json"),
        payload_kind="SpeciesDistribution",
    )
    facts = {f.property: f for f in record.properties}

    assert "relative_energy" not in facts
    assert facts["species_gap"].value == pytest.approx(3.7)
    assert facts["species_gap"].unit == "kcal/mol"
    # The uncertainty is meaningful here, which is the whole point: 3.7 against 1.5 is a decision.
    assert facts["species_gap"].uncertainty == pytest.approx(1.5)


def test_a_single_species_distribution_publishes_no_gap() -> None:
    """Absent stays absent: with nothing to rank against, there is no discrimination to state."""
    record = project(
        calc_ref="one-species",
        calc_type="calc.rank_species",
        payload=_distribution([("CC(=O)CC(C)=O", "keto", 0.0, 1.0)]).model_dump(mode="json"),
        payload_kind="SpeciesDistribution",
    )

    assert "species_gap" not in {f.property for f in record.properties}


def test_an_unpairable_spectrum_publishes_the_reason_beside_the_missing_intensities() -> None:
    """An unpairable spectrum publishes the reason beside the missing intensities.

    Intensity points are emitted only where present; a `FlagFact` says why they are all absent,
    without a property column on every row for the exceptional case.
    """
    payload: dict[str, Any] = {
        "smiles": "O=C=O",
        "structure_id": "st_co2",
        "method": "GFN2-xTB",
        "mode_count": 4,
        "modes": [
            {"wavenumber_cm": 667.0, "ir_intensity_km_per_mol": None},
            {"wavenumber_cm": 2593.0, "ir_intensity_km_per_mol": None},
        ],
        "spectrum_unavailable": "the server projected out 6 external mode(s) leaving 3",
    }

    _, _, _, extra = projection.PAYLOAD_PROJECTORS["ThermochemistryResult"](payload)

    assert [point.property for point in extra["points"]] == ["wavenumber", "wavenumber"], (
        "the premise: no intensity point is published when there is no intensity"
    )
    flags = extra["flags"]
    assert [(flag.flag, flag.severity) for flag in flags] == [("spectrum_unavailable", "warning")]
    assert "projected out 6" in flags[0].message

    payload["spectrum_unavailable"] = None
    payload["modes"] = [{"wavenumber_cm": 667.0, "ir_intensity_km_per_mol": 68.71}]
    _, _, _, paired = projection.PAYLOAD_PROJECTORS["ThermochemistryResult"](payload)
    assert paired["flags"] == [], "and nothing is flagged when the spectrum is there"
    assert "ir_intensity" in {point.property for point in paired["points"]}


# --- a screen that could not compute every item -----------------------------------------------


def _screen(**update: Any) -> dict[str, Any]:
    """A two-medium solvent screen in its wire shape, with `update` applied to the model first."""
    effects = [
        SolventEffect(solvent=None, delta_e_kcal=-1.0, delta_h_kcal=None, delta_g_kcal=-1.0),
        SolventEffect(solvent="thf", delta_e_kcal=-2.0, delta_h_kcal=None, delta_g_kcal=-2.0),
    ]
    screen = SolventComparisonResult(
        reactants=["C=C"],
        products=["CO"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=effects,
        best_solvent="thf",
        spread_kcal=1.0,
        uncertainty_kcal=3.0,
    ).model_copy(update=update)
    # `exclude_none`, because that is what the job wire does (`connectors/calc/workflows.py`):
    # a failed gas phase arrives with no `solvent` key at all.
    return screen.model_dump(mode="json", exclude_none=True)


def test_each_medium_a_screen_could_not_compute_is_a_flag_with_its_reason_in_detail() -> None:
    """Which screens are partial is a query over `medium_not_computed`, not a text search.

    The reason rides in `detail`, which is JSONB, because `message` is `VARCHAR(2000)` at the sink:
    a reason there could fail the whole record and dead-letter the media that *were* computed.
    """
    reason = "the optimisation did not converge " * 200
    payload = _screen(failed=[FailedMedium(solvent=None, reason=reason)])

    _, _, _, extra = projection.PAYLOAD_PROJECTORS["SolventComparisonResult"](payload)

    (flag,) = [f for f in extra["flags"] if f.flag == "medium_not_computed"]
    assert flag.message == "gas phase could not be computed", "named even with `solvent` dropped"
    assert flag.detail["reason"] == reason
    assert len(flag.message) < 2000


def test_a_screen_that_compared_one_medium_publishes_no_spread_and_no_winner() -> None:
    """A spread over one row is zero by construction, and "no solvent effect" is a query."""
    payload = _screen(
        effects=[
            SolventEffect(solvent="thf", delta_e_kcal=-2.0, delta_h_kcal=None, delta_g_kcal=-2.0)
        ],
        spread_kcal=0.0,
        failed=[FailedMedium(solvent=None, reason="refused")],
    )

    _, _, _, extra = projection.PAYLOAD_PROJECTORS["SolventComparisonResult"](payload)

    published = {fact.property for fact in extra["properties"]}
    assert not published & {"solvent_spread", "best_solvent"}


def test_a_partial_solvent_screen_publishes_no_spread_and_no_winner() -> None:
    """Two media computed and one stopped: the spread is a lower bound and the winner may be wrong.

    `weakest_bond`'s rule, held for the screens it was not applied to: a query for "best solvent =
    thf" reads the fact without the `medium_not_computed` flag beside it.
    """
    stopped = FailedMedium(solvent="dmso", reason="stopped", cause="time_budget")
    _, _, _, extra = projection.PAYLOAD_PROJECTORS["SolventComparisonResult"](
        _screen(failed=[stopped])
    )
    published = {fact.property for fact in extra["properties"]}
    assert not published & {"solvent_spread", "best_solvent"}
    assert any(flag.flag == "medium_not_computed" for flag in extra["flags"])

    _, _, _, whole = projection.PAYLOAD_PROJECTORS["SolventComparisonResult"](_screen())
    assert {"solvent_spread", "best_solvent"} <= {fact.property for fact in whole["properties"]}


def test_a_partial_species_screen_publishes_no_swing() -> None:
    """The largest swing over the media computed is a lower bound on the screen's."""
    from chemclaw.publish.project import records_from_species_solvent_screen

    ranked = [
        RankedSpecies(
            smiles="CC(=O)CC(C)=O",
            label="keto",
            relative_kcal=0.0,
            population=0.8,
            electronic_energy_hartree=-267.3,
        ),
        RankedSpecies(
            smiles="CC(=O)C=C(C)O",
            label="enol",
            relative_kcal=1.0,
            population=0.2,
            electronic_energy_hartree=-267.2,
        ),
    ]

    def _screen_of(failed: list[FailedMedium]) -> list[Any]:
        distributions = [
            SpeciesDistribution(
                kind="tautomers",
                method="GFN2-xTB",
                solvent=solvent,
                temperature_k=298.15,
                level="standard",
                species=ranked,
                enumerated=2,
                uncertainty_kcal=3.0,
            )
            for solvent in (None, "water")
        ]
        screen = SpeciesSolventComparison(
            kind="tautomers",
            method="GFN2-xTB",
            temperature_k=298.15,
            level="standard",
            distributions=distributions,
            responses=[],
            dominance_changes=False,
            largest_swing_kcal=0.4,
            uncertainty_kcal=3.0,
            failed=failed,
        )
        return records_from_species_solvent_screen(
            calc_ref="screen-10",
            payload=screen.model_dump(mode="json"),
            calc_type="calc.rank_species_across_solvents",
        )

    def _swing(records: list[Any]) -> bool:
        return any(fact.property == "solvent_swing" for fact in records[0].properties)

    assert _swing(_screen_of([])), "the probe is vacuous: a whole screen publishes its swing"
    assert not _swing(_screen_of([FailedMedium(solvent="toluene", reason="refused")]))


def test_a_partial_bond_survey_publishes_its_bonds_but_no_weakest_bond() -> None:
    """The weakest computed bond is not the molecule's weakest, and a query reads the fact bare."""
    survey = BondDissociationSurvey(
        smiles="CCc1ccccc1",
        method="GFN2-xTB",
        solvent=None,
        temperature_k=298.15,
        mode="homolytic",
        bonds=[
            DissociatedBond(
                atoms=[1, 2],
                bond="C-C",
                fragments=["[CH2]C", "[c]1ccccc1"],
                dissociation_energy_kcal=101.0,
                is_weakest=True,
            )
        ],
        considered=2,
        uncertainty_kcal=5.0,
        failed=[FailedBond(atoms=[0, 1], bond="C-C", fragments=["a", "b"], reason="refused")],
    )

    _, _, _, extra = projection.PAYLOAD_PROJECTORS["BondDissociationSurvey"](
        survey.model_dump(mode="json", exclude_none=True)
    )

    published = {fact.property for fact in extra["properties"]}
    assert not published & {"weakest_bond", "weakest_bond_dissociation_energy"}
    assert len(extra["sites"]) == 1, "the computed bond is still published"
    (flag,) = [f for f in extra["flags"] if f.flag == "bond_not_computed"]
    assert flag.message == "C-C [0, 1] could not be computed"


def test_a_medium_the_clock_stopped_is_published_as_a_stop_not_a_failure_of_the_item() -> None:
    """The one cause a reader must not take as a property of the item says so in the flag itself."""
    payload = _screen(
        failed=[FailedMedium(solvent="toluene", reason="inline budget", cause="time_budget")]
    )

    _, _, _, extra = projection.PAYLOAD_PROJECTORS["SolventComparisonResult"](payload)

    (flag,) = [f for f in extra["flags"] if f.flag == "medium_not_computed"]
    assert flag.message == "toluene was stopped by the calculation service's time budget"
    assert flag.detail["cause"] == "time_budget"
