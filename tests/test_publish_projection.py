"""Every result shape this system produces is representable as a published record.

**This is the test that answers the request "any calculation shall be stored".** It walks the
result models one by one and asserts that projecting each produces the subject, conditions and
facts that shape implies — so a model gaining a field, or a projector losing one, fails here rather
than silently publishing less than was computed.

The coverage check at the end is the one that matters most: it tracks which payload keys each
projector actually *reads*, and pins the set it deliberately ignores. A field added to a result
model tomorrow shows up as an unread key and fails, which is what stops this from drifting into a
projection that quietly drops the newest half of a calculator's output.
"""

from typing import Any

import pytest

from chemclaw.publish import project as projection
from chemclaw.publish.project import project
from chemclaw.publish.properties import REGISTRY
from chemclaw.science.calc.models import (
    AtomCharge,
    BondOrder,
    Conformer,
    ConformerEnsemble,
    DescriptorProfile,
    ElectronicProperties,
    EnsembleMember,
    EnsemblePayload,
    FukuiSite,
    InteractionResult,
    LogdResult,
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
                atom_charges=[AtomCharge(index=0, element="C", charge=-0.12)],
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
                sites=[FukuiSite(index=0, element="C", f_minus=0.11, f_plus=0.09, f_zero=0.10)],
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


def test_a_reaction_attaches_each_species_energy_to_the_right_member() -> None:
    """Per-species facts are matched by (role, molecule), never by list position.

    The fixture lists its species **product first**, while the equation lists reactants first — so
    a projector that zipped the two by index would attach cyclohexane's free energy to ethene.
    Both are plausible numbers in the same units, so nothing downstream would notice.
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
    """A `quick`-level reaction publishes no free energy — there is no fallback in the projector.

    `delta_g_kcal` is None at `quick` level and whenever a species' symmetry number was unstated.
    Falling back to `delta_e_kcal` would publish an electronic energy under the name of a free
    energy, which is the single most consequential thing this projector could get wrong.
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
    """The cached search and the weighted ensemble carry different halves; both must work.

    Measured against the models rather than assumed: `EnsembleMember` has `energy_hartree` and no
    population; `Conformer` has `relative_kcal` and `population` and no absolute energy. A
    projector requiring either one would make half the ensembles in this system unpublishable.
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


def test_a_solvent_screen_publishes_its_parts_as_well_as_its_aggregate() -> None:
    """Never store an aggregate whose parts are not also stored.

    A screen that published only its spread would leave "what was delta-G in DMSO" unanswerable
    even though the run computed it — and would make the cross-solvent question answer over
    screens only, missing every solvent run on its own.
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

    Measured, not argued: the payload records which keys the projector touched, and anything it
    ignored has to be listed above with a reason. This caught three real gaps when it was written —
    a missing descriptor, an exotherm threshold whose flag was published without it, and a scan
    whose coordinate said "dihedral" without saying which atoms.
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
