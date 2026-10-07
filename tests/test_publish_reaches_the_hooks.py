"""What the production call sites publish, not what the projectors can project.

Projector tests call `project()` with a hand-supplied `payload_kind`, which says nothing about
whether any production path reaches it. Every test here starts at a real hook (the envelope a
connector job returns, the row the backfill reads, the payload the cache writes, a real tool call)
and asserts what comes out the far end. Inputs are derived from what decides the answer:
`XtbJobResult`'s member fields, the connector manifests and the calculation server's key contract
fake. Anything that cannot route yet is named in `_NOT_YET_PUBLISHED` or
`_PRIMITIVES_NOT_PUBLISHED`, kept even when empty.
"""

import asyncio
import copy
import logging
from collections.abc import Callable
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.workflows import job_envelope
from chemclaw.connectors.registry import discovered
from chemclaw.durable.connector_job import ConnectorJobResult, job_record_for
from chemclaw.publish.project import PAYLOAD_PROJECTORS, projector_for, records_for
from chemclaw.science.calc.models import (
    BondDissociationSurvey,
    Conformer,
    ConformerEnsemble,
    DissociatedBond,
    EnsembleProperty,
    InteractionResult,
    MicrostatePka,
    RankedSpecies,
    ReactionEnergyResult,
    RefinedConformer,
    RefinedEnsemble,
    Rotamer,
    RotationBarrier,
    RotationProfile,
    ScanPoint,
    ScanResult,
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
    WeightedValue,
)
from chemclaw.science.calc.thermo import half_life_from_barrier
from tests.calc_server_fake import _KEYED

# The shapes a durable job can publish, derived from `XtbJobResult`'s member fields, so a new result
# shape appears here with no edit. Routes are not paired with shapes because a route never
# identifies a shape (`test_a_route_never_routes_on_its_own`).
_ENVELOPE_MEMBERS: tuple[str, ...] = tuple(
    sorted(
        annotation.__name__
        for field in XtbJobResult.model_fields.values()
        for annotation in get_args(field.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel)
    )
)

# Shapes that reach a hook and have no projector yet, declared rather than omitted. Empty; a shape
# not named here must route, so a new `XtbJobResult` member fails immediately.
_NOT_YET_PUBLISHED: frozenset[str] = frozenset()

# Every `calc_type` the calculation server stamps on a cache row, read off the fake that states its
# key contract, so a new cache type is parametrised with no edit.
_STAMPED_CALC_TYPES: tuple[str, ...] = tuple(
    sorted({calc_type for calc_type, _ in _KEYED.values()})
)

# Stamped types with no projector, declared for the same reason as `_NOT_YET_PUBLISHED`. Empty:
# `xtb.hess` projects its row (`_hessian`), and frequencies reach the store through the tool hook as
# `ThermochemistryResult`.
_PRIMITIVES_NOT_PUBLISHED: frozenset[str] = frozenset()

# The routes the hooks build, `<connector>.<job>`, read off the manifests so a new job cannot be
# added without this file seeing it.
_JOB_ROUTES: tuple[str, ...] = tuple(
    f"{name}.{job.name}"
    for name, (_directory, manifest) in sorted(discovered().items())
    for job in manifest.jobs
)


def _structure(z: float = 1.0) -> Structure:
    """A small valid geometry, enough to carry a `structure_id`."""
    return Structure(
        elements=[6, 1, 1, 1, 1],
        positions=[[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, z], [0, -1, 0]],
        smiles="CCO",
    )


def _reaction() -> ReactionEnergyResult:
    """A reaction result with a per-species breakdown, as `standard` level produces."""
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
            )
        ],
        cache_hits=0,
        uncertainty_kcal=3.0,
        is_strongly_exothermic=True,
        exotherm_threshold_kcal=-20.0,
        conformer_treatment="single",
    )


def _thermochemistry() -> ThermochemistryResult:
    """A thermochemistry result carrying vibrational modes.

    `_thermochemistry` reads list-element fields (`modes[].wavenumber_cm`), so the partial-payload
    sweep needs it.
    """
    return ThermochemistryResult(
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
            VibrationalMode(wavenumber_cm=412.0, ir_intensity_km_per_mol=1.2),
            VibrationalMode(wavenumber_cm=1050.0, ir_intensity_km_per_mol=8.4),
        ],
        mode_count=2,
        lowest_wavenumbers_cm=[412.0, 1050.0],
        electronic_energy_hartree=-154.2,
        zero_point_energy_kcal=31.2,
        thermal_enthalpy_correction_kcal=2.9,
        entropy_cal_per_mol_k=67.4,
        gibbs_correction_kcal=14.1,
        enthalpy_hartree=-154.1,
        gibbs_free_energy_hartree=-154.15,
        uncertainty_kcal=1.0,
    )


@pytest.mark.parametrize("route", _JOB_ROUTES)
def test_a_route_never_routes_on_its_own(route: str) -> None:
    """`<connector>.<job>` names where a result came from, never what shape it is.

    Asserted for every job in every manifest, so a prefix colliding with a connector name cannot
    make a composite project by accident.
    """
    assert projector_for(route) is None, (
        f"{route!r} resolved a projector from its route alone: a `_CALC_TYPE_PROJECTORS` prefix "
        "now collides with a connector name, so this job's results would be projected as the "
        "wrong shape rather than by their `payload_kind`"
    )


@pytest.mark.parametrize("payload_kind", _ENVELOPE_MEMBERS)
def test_every_shape_a_calc_job_can_return_routes_to_a_projector(payload_kind: str) -> None:
    """Every member `XtbJobResult` can carry routes to a projector, or is declared as not yet
    published.

    Many jobs share one workflow and envelope, so the envelope's members are the complete set of
    shapes this bundle can publish.
    """
    routed = projector_for("calc.any_job", payload_kind) is not None
    if payload_kind in _NOT_YET_PUBLISHED:
        assert not routed, (
            f"{payload_kind!r} now routes to a projector — delete it from `_NOT_YET_PUBLISHED`. "
            "That set is an exclusion with a deadline, and a stale entry is a claim that a shape "
            "is unpublished when it is not"
        )
        return
    assert routed, (
        f"a calc job returning {payload_kind!r} routes to no projector; its results would be "
        "silently dropped at the enqueue. Add one to `PAYLOAD_PROJECTORS`, or — if it genuinely "
        "cannot be published yet — name it in `_NOT_YET_PUBLISHED` so the gap is declared"
    )


@pytest.mark.parametrize("calc_type", _STAMPED_CALC_TYPES)
def test_every_calc_type_the_server_stamps_routes_to_a_projector(calc_type: str) -> None:
    """Every `calc_type` the server stamps routes to a projector.

    The cache hook (`science/calc/store.py::publish_stored_result`) has an untyped dict and no
    `payload_kind`, so a primitive routes by its server-stamped `calc_type` prefix alone.
    Parametrised over `calc_server_fake._KEYED`, this repository's statement of the server's key
    contract.
    """
    if calc_type in _PRIMITIVES_NOT_PUBLISHED:
        assert projector_for(calc_type) is None, (
            f"{calc_type!r} now routes — delete it from `_PRIMITIVES_NOT_PUBLISHED`"
        )
        return
    assert projector_for(calc_type) is not None, (
        f"the server stamps {calc_type!r} and no projector prefix matches it, so every one of "
        "those results is dropped at the enqueue with a debug line"
    )


def test_a_retired_calculators_rows_still_project() -> None:
    """A retired calculator's rows still project.

    `calculation_results` is never pruned, so `dft` rows from the removed QM bundle and older
    `xtb.scan` rows remain and resolve by `calc_type` prefix, all the backfill has.
    """
    assert projector_for("dft@nextflow-1.0.0:abc:def") is not None
    assert projector_for("xtb.scan@GFN2:abc:def") is not None


def test_what_the_calc_workflow_returns_projects_into_records() -> None:
    """What the calc workflow returns projects into records, through `job_envelope`.

    `job_envelope` is the function `CalcJobWorkflow.run` calls, so its `payload_kind` is the
    production one. It is pure (workflow code must replay identically), so it can be called without
    a worker.
    """
    envelope = job_envelope(
        XtbJobResult(kind="reaction", summary="ΔE = -38.2 kcal/mol", reaction=_reaction())
    )

    assert envelope.payload_kind == "ReactionEnergyResult", (
        "the workflow must name the shape it computed, not the envelope it wrapped it in"
    )
    assert "reaction" not in envelope.data, (
        "`data` must be the domain result itself; a wrapper key here means the science is a level "
        "down and `payload_kind` is naming the wrapper"
    )
    records = records_for(
        calc_ref="calc-job-1",
        calc_type="calc.compute_reaction_energy",
        payload=envelope.data,
        payload_kind=envelope.payload_kind,
    )
    assert records, "what the production hook returns projected nothing"
    assert records[0].subject.members, "the projected record names no species"


def _refined() -> RefinedEnsemble:
    """A free-energy-refined ensemble: two of forty-seven members re-scored."""
    return RefinedEnsemble(
        smiles="CCO",
        method="GFN2-xTB",
        solvent="thf",
        temperature_k=298.15,
        conformers=[
            RefinedConformer(
                structure=_structure(),
                relative_kcal=0.0,
                population=0.8,
                degeneracy=1,
                gibbs_free_energy_hartree=-154.15,
                electronic_energy_hartree=-154.2,
                is_minimum=True,
            ),
            RefinedConformer(
                structure=_structure(2.0),
                relative_kcal=0.9,
                population=0.2,
                degeneracy=2,
                gibbs_free_energy_hartree=-154.14,
                electronic_energy_hartree=-154.19,
                is_minimum=True,
            ),
        ],
        total_found=47,
        refined_count=2,
        refined_population_covered=0.62,
        refined_conformational_entropy_cal_per_mol_k=0.9,
        refined_ensemble_correction_kcal=-0.27,
    )


def _averaged() -> EnsembleProperty:
    """A Boltzmann-averaged scalar property over an ensemble."""
    return EnsembleProperty(
        smiles="CCO",
        property_name="dipole_debye",
        method="GFN2-xTB",
        solvent="thf",
        temperature_k=298.15,
        members_averaged=5,
        total_found=47,
        value=WeightedValue(mean=1.68, minimum=1.41, maximum=1.93, spread=0.52),
        population_covered=0.91,
    )


def _distribution() -> SpeciesDistribution:
    """A ranked tautomer population."""
    return SpeciesDistribution(
        kind="tautomers",
        method="GFN2-xTB",
        solvent="water",
        temperature_k=298.15,
        level="standard",
        species=[
            RankedSpecies(
                smiles="CC(=O)CC(=O)C",
                label="diketo",
                relative_kcal=0.0,
                population=0.93,
                gibbs_free_energy_hartree=-345.1,
                electronic_energy_hartree=-345.2,
                structure_id=_structure().structure_id,
                conformers_found=4,
            ),
            RankedSpecies(
                smiles="CC(O)=CC(=O)C",
                label="enol",
                relative_kcal=1.5,
                population=0.07,
                gibbs_free_energy_hartree=-345.09,
                electronic_energy_hartree=-345.18,
                conformers_found=3,
            ),
        ],
        enumerated=2,
        uncertainty_kcal=2.0,
    )


def _bond_survey_result() -> BondDissociationSurvey:
    """A homolytic bond dissociation survey with a named weakest bond."""
    return BondDissociationSurvey(
        smiles="CCO",
        method="GFN2-xTB",
        solvent=None,
        temperature_k=298.15,
        mode="homolytic",
        bonds=[
            DissociatedBond(
                atoms=[0, 1],
                bond="C-C",
                fragments=["[CH3]", "[CH2]O"],
                dissociation_energy_kcal=88.4,
            ),
            DissociatedBond(
                atoms=[1, 2],
                bond="C-O",
                fragments=["CC", "[OH]"],
                dissociation_energy_kcal=71.2,
                is_weakest=True,
            ),
        ],
        considered=2,
        uncertainty_kcal=4.0,
    )


def _ensemble_result(smiles: str = "CCO", search: str = "conformers") -> ConformerEnsemble:
    """A two-member conformer ensemble, as a CREST search returns one."""
    return ConformerEnsemble(
        smiles=smiles,
        method="GFN2-xTB",
        search=search,  # type: ignore[arg-type]
        effort="quick",
        solvent="thf",
        temperature_k=298.15,
        conformers=[
            Conformer(relative_kcal=0.0, population=0.7, degeneracy=1, structure=_structure(1.0)),
            Conformer(relative_kcal=0.9, population=0.3, degeneracy=2, structure=_structure(1.1)),
        ],
        total_found=12,
        conformational_entropy_cal_per_mol_k=1.4,
        ensemble_correction_kcal=-0.4,
    )


def _solvent_screen() -> SolventComparisonResult:
    """A reaction compared across two media — the shape that decomposes into parts."""
    return SolventComparisonResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=[
            SolventEffect(
                solvent="thf", delta_e_kcal=-38.0, delta_h_kcal=-36.0, delta_g_kcal=-22.0
            ),
            SolventEffect(
                solvent="toluene", delta_e_kcal=-37.5, delta_h_kcal=-35.5, delta_g_kcal=-24.8
            ),
        ],
        best_solvent="toluene",
        spread_kcal=2.8,
        uncertainty_kcal=3.0,
    )


def _species_solvent_screen() -> SpeciesSolventComparison:
    """A ranked species set fanned out over two media."""
    gas = _distribution().model_copy(update={"solvent": None})
    return SpeciesSolventComparison(
        kind="tautomers",
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        distributions=[gas, _distribution()],
        responses=[
            SpeciesSolventResponse(
                smiles="CC(=O)CC(=O)C",
                label="diketo",
                standings=[
                    SpeciesStanding(solvent=None, relative_kcal=0.0, population=0.95),
                    SpeciesStanding(solvent="water", relative_kcal=0.0, population=0.93),
                ],
                population_swing=0.02,
                relative_swing_kcal=0.0,
            )
        ],
        dominance_changes=False,
        largest_swing_kcal=0.4,
        uncertainty_kcal=2.0,
    )


def _scan() -> ScanResult:
    """A relaxed scan along one dihedral."""
    return ScanResult(
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


def _rotation() -> RotationProfile:
    """A rotational profile about one named torsion."""
    return RotationProfile(
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
            ScanPoint(value=180.0, energy_hartree=-158.001, relative_kcal=0.0),
        ],
        rotamers=[
            Rotamer(
                dihedral_degrees=180.0,
                structure_id=_structure().structure_id,
                relative_kcal=0.0,
                population=0.59,
                degeneracy=1,
            )
        ],
        barriers=[
            RotationBarrier(
                from_rotamer=0,
                to_rotamer=0,
                at_degrees=120.0,
                forward_kcal=2.76,
                reverse_kcal=2.76,
                basis="E",
                interconversion=half_life_from_barrier(2.76, 298.15),
            )
        ],
        highest_barrier_kcal=2.76,
        uncertainty_kcal=3.0,
    )


def _interaction() -> InteractionResult:
    """A non-covalent complex and its interaction energy."""
    return InteractionResult(
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


def _microstate_pka() -> MicrostatePka:
    """A macrostate pKa from two sampled ensembles — the most expensive result in the tier."""
    return MicrostatePka(
        smiles="Oc1ccccc1",
        branch="acid",
        pka=9.9,
        uncertainty=1.4,
        delta_g_kcal=21.6,
        site_smiles="[O-]c1ccccc1",
        method="CREST/GFN2-xTB",
        solvent="water",
        temperature_k=298.15,
        neutral=_ensemble_result("Oc1ccccc1"),
        ionised=_ensemble_result("[O-]c1ccccc1", search="deprotomers"),
        microstates_found=4,
        microstates_within_rt=2,
        warnings=["two microstates within RT"],
    )


# One minimal-valid instance per shape the envelope can carry, keyed by the model's own name (the
# `payload_kind` key) and checked against `_ENVELOPE_MEMBERS`. Used to prove each shape actually
# projects, not only routes: a projector that raises on every payload (an unregistered property in a
# required field) still routes. Typed `Any` because the envelope field is a specific optional type.
_SHAPES: dict[str, Callable[[], Any]] = {
    "ReactionEnergyResult": _reaction,
    "SolventComparisonResult": _solvent_screen,
    "ScanResult": _scan,
    "RotationProfile": _rotation,
    "ConformerEnsemble": _ensemble_result,
    "InteractionResult": _interaction,
    "MicrostatePka": _microstate_pka,
    "RefinedEnsemble": _refined,
    "EnsembleProperty": _averaged,
    "SpeciesDistribution": _distribution,
    "SpeciesSolventComparison": _species_solvent_screen,
    "BondDissociationSurvey": _bond_survey_result,
}

# Model name -> the envelope field carrying it, derived from `XtbJobResult` for the same reason
# `_ENVELOPE_MEMBERS` is: a tenth result shape must reach these tests with no edit here.
_MEMBER_FIELDS: dict[str, str] = {
    annotation.__name__: name
    for name, field in XtbJobResult.model_fields.items()
    for annotation in get_args(field.annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel)
}


def test_a_specimen_exists_for_every_shape_the_envelope_can_carry() -> None:
    """The completeness half: a new shape cannot be added without a specimen to project.

    Without this the parametrisation below would silently shrink to the shapes someone remembered,
    which is the same failure one level up that `_ENVELOPE_MEMBERS` was derived to end.
    """
    assert set(_SHAPES) == set(_ENVELOPE_MEMBERS), (
        "every shape `XtbJobResult` can carry needs a specimen in `_SHAPES`; missing "
        f"{sorted(set(_ENVELOPE_MEMBERS) - set(_SHAPES))}, stale "
        f"{sorted(set(_SHAPES) - set(_ENVELOPE_MEMBERS))}"
    )


@pytest.mark.parametrize("payload_kind", _ENVELOPE_MEMBERS)
def test_every_shape_a_calc_job_can_return_actually_projects(payload_kind: str) -> None:
    """Every shape a calc job can return actually projects, not merely routes.

    `_fact` refuses property names the registry does not define, so one unregistered property in a
    required field makes a projector raise on every payload. Driven through `job_envelope`.
    """
    field = _MEMBER_FIELDS[payload_kind]
    envelope = job_envelope(
        XtbJobResult(kind=field, summary="s", **{field: _SHAPES[payload_kind]()})
    )
    assert envelope.payload_kind == payload_kind

    records = records_for(
        calc_ref=f"calc-job-{field}",
        calc_type=f"calc.{field}",
        payload=envelope.data,
        payload_kind=envelope.payload_kind,
    )

    assert records, f"{payload_kind} projected no record"
    record = records[0]
    assert record.subject.members, "the projected record names nothing it is about"
    # Something quantitative survived: a record with a subject and no facts says a calculation
    # happened and nothing about what it found.
    assert (
        record.properties
        or record.sites
        or record.points
        or record.conformers
        or (record.candidates)
    ), f"{payload_kind} projected a record carrying no facts at all"


def test_a_refined_ensemble_publishes_electronic_energies_and_free_energy_populations() -> None:
    """A refined ensemble publishes electronic energies and free-energy populations.

    `energy_hartree` carries the electronic energy even though ranking is by G, so the two ensemble
    kinds stay comparable on one column.
    """
    envelope = job_envelope(XtbJobResult(kind="refined", summary="s", refined=_refined()))
    record = records_for(
        calc_ref="calc-job-refined",
        calc_type="calc.refine_ensemble",
        payload=envelope.data,
        payload_kind=envelope.payload_kind,
    )[0]

    assert [c.energy_hartree for c in record.conformers] == [-154.2, -154.19]
    assert [c.relative_kcal for c in record.conformers] == [0.0, 0.9]
    assert [c.population for c in record.conformers] == [0.8, 0.2]
    assert record.level.treatment == "free-energy-weighted-top-n", (
        "the treatment is what disambiguates the relative energies; without it the electronic "
        "absolutes and the free-energy relatives read as one scale"
    )
    named = {fact.property for fact in record.properties}
    assert "refined_conformational_entropy" in named and "conformational_entropy" not in named, (
        "the refined subset's entropy must not be published under the ensemble-wide name"
    )


def test_a_refined_ensemble_stored_before_the_rename_still_publishes_both_headline_numbers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refined ensemble stored before the entropy-field rename still publishes both headline
    numbers.

    `_refined_ensemble` reads the old and new names, since the rename changed only the label, not
    the arithmetic; the values asserted confirm that. The warning is asserted too, since an operator
    backfilling a legacy corpus wants to see it.
    """
    legacy = _refined().model_dump(mode="json")
    entropy = legacy.pop("refined_conformational_entropy_cal_per_mol_k")
    correction = legacy.pop("refined_ensemble_correction_kcal")
    legacy["conformational_entropy_cal_per_mol_k"] = entropy
    legacy["ensemble_correction_kcal"] = correction

    with caplog.at_level(logging.WARNING, logger="chemclaw.publish.project"):
        record = records_for(
            calc_ref="calc-job-refined-legacy",
            calc_type="calc.refine_ensemble",
            payload=legacy,
            payload_kind="RefinedEnsemble",
        )[0]

    facts = {fact.property: fact.value for fact in record.properties}
    assert facts.get("refined_conformational_entropy") == entropy, (
        "an ensemble stored under the pre-rename field names published without its entropy, "
        f"indistinguishable from one that had none: {sorted(facts)}"
    )
    assert facts.get("refined_ensemble_correction") == correction
    assert "conformational_entropy" not in facts and "ensemble_correction" not in facts, (
        "the refined subset's numbers must still land under the refined names — the rename is the "
        "reason the fallback is allowed at all"
    )
    assert any("legacy field" in message for message in caplog.messages), (
        f"reading a legacy field name must say so: {caplog.messages}"
    )


def test_a_current_refined_ensemble_reads_no_legacy_field_and_says_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fallback must be a fallback: a current payload must not trip it.

    Without this, `_renamed` reading the legacy name *first* — or a payload carrying both — would
    pass the test above while warning on every ensemble this system writes today.
    """
    with caplog.at_level(logging.WARNING, logger="chemclaw.publish.project"):
        record = records_for(
            calc_ref="calc-job-refined-current",
            calc_type="calc.refine_ensemble",
            payload=_refined().model_dump(mode="json"),
            payload_kind="RefinedEnsemble",
        )[0]

    facts = {fact.property: fact.value for fact in record.properties}
    assert facts["refined_conformational_entropy"] == 0.9
    assert not [m for m in caplog.messages if "legacy field" in m], caplog.messages


def test_a_bond_survey_publishes_pairs_and_hoists_the_weakest() -> None:
    """A bond survey publishes atom pairs and hoists the weakest bond to a scalar.

    A projector emitting one site per bond with `atom_j = -1` would still route, so this is checked
    directly.
    """
    envelope = job_envelope(XtbJobResult(kind="bonds", summary="s", bonds=_bond_survey_result()))
    record = records_for(
        calc_ref="calc-job-bonds",
        calc_type="calc.survey_bond_strengths",
        payload=envelope.data,
        payload_kind=envelope.payload_kind,
    )[0]

    assert [(s.atom_i, s.atom_j) for s in record.sites] == [(0, 1), (1, 2)]
    assert all(site.property == "bond_dissociation_energy" for site in record.sites)
    scalars = {fact.property: fact for fact in record.properties}
    assert scalars["weakest_bond"].value_text == "C-O"
    assert scalars["weakest_bond_dissociation_energy"].value == 71.2
    assert scalars["weakest_bond_dissociation_energy"].uncertainty == 4.0


def test_a_species_distribution_publishes_candidates_not_subject_members() -> None:
    """A species distribution publishes candidates, not subject members.

    A ranked set is what a calculation produced, not what it was about; this keeps a compound's
    tautomer set from colliding with the compound.
    """
    envelope = job_envelope(
        XtbJobResult(kind="distribution", summary="s", distribution=_distribution())
    )
    record = records_for(
        calc_ref="calc-job-dist",
        calc_type="calc.rank_species",
        payload=envelope.data,
        payload_kind=envelope.payload_kind,
    )[0]

    assert record.subject.kind == "system"
    assert [c.score for c in record.candidates] == [0.93, 0.07]
    # `species_population`, not `population`: the latter is registered at conformer scope, and a
    # candidate row scored with it contradicts the table `scope_kind` places it in.
    assert all(c.score_property == "species_population" for c in record.candidates)
    assert record.candidates[0].detail["label"] == "diketo"


def test_an_envelope_carrying_no_result_is_a_loud_failure() -> None:
    """A job that produced nothing must not report success with a `kind` describing an absence.

    The alternative — returning the bookkeeping fields alone — is what the publish path used to
    receive, and it is indistinguishable at the far end from a result this release cannot read.
    """
    with pytest.raises(ValueError, match="carried 0"):
        XtbJobResult(kind="reaction", summary="nothing ran").outcome()


def test_the_envelope_carries_the_shape_its_data_came_from() -> None:
    """The envelope carries `payload_kind`, defaulting to "not said" and surviving validation.

    `data` is a plain dict across the Temporal wire, so the model identity must travel separately.
    """
    assert ConnectorJobResult(summary="x").payload_kind == "", (
        "payload_kind must default empty — every history in flight decodes without it"
    )
    result = _reaction()
    envelope = ConnectorJobResult(
        summary="done",
        data=result.model_dump(mode="json"),
        payload_kind=type(result).__name__,
    )
    assert envelope.payload_kind == "ReactionEnergyResult"
    assert ConnectorJobResult.model_validate(envelope.model_dump()).payload_kind == (
        "ReactionEnergyResult"
    )


def test_the_durable_record_keeps_the_shape_for_the_backfill() -> None:
    """The durable record keeps the shape for the backfill.

    The backfill reads `job_records`, not the envelope; without it every composite row would be
    skipped as unprojectable.
    """
    from chemclaw.durable.connector_job import ConnectorJobInput

    job = ConnectorJobInput(
        connector="calc",
        job="compute_reaction_energy",
        workflow="ReactionEnergyWorkflow",
        task_queue="connector-calc",
        rationale="checking the Diels-Alder driving force",
        requested_by="chemist@example.com",
    )
    result = _reaction()
    envelope = ConnectorJobResult(
        summary="done",
        data=result.model_dump(mode="json"),
        payload_kind=type(result).__name__,
    )
    record = job_record_for("job-1", job, envelope)
    assert record.payload_kind == "ReactionEnergyResult"
    assert projector_for(f"{record.connector}.{record.job}", record.payload_kind) is not None


def test_a_solvent_screen_publishes_its_parts_and_not_only_its_verdict() -> None:
    """A solvent screen publishes its parts and not only its verdict.

    `records_for` puts the decomposition on the live path, so ΔG in each solvent is queryable, not
    only `best_solvent`.
    """
    screen = SolventComparisonResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=[
            SolventEffect(
                solvent="dmso", delta_e_kcal=-38.0, delta_h_kcal=-36.0, delta_g_kcal=-24.0
            ),
            SolventEffect(
                solvent="toluene", delta_e_kcal=-40.0, delta_h_kcal=-38.0, delta_g_kcal=-28.9
            ),
        ],
        best_solvent="toluene",
        spread_kcal=4.9,
        uncertainty_kcal=3.0,
    )
    records = records_for(
        calc_ref="screen-1",
        calc_type="calc.compare_solvents",
        payload=screen.model_dump(mode="json"),
        payload_kind="SolventComparisonResult",
    )
    assert len(records) == 3, "the comparison plus one record per solvent it compared"
    parts = records[1:]
    assert [record.conditions.solvent for record in parts] == ["dmso", "toluene"]
    assert all(record.depends_on == ["screen-1"] for record in parts), (
        "every part must edge back to the aggregate, or the verdict cannot be traced to its numbers"
    )
    # And each part is answerable on its own, which is what makes the cross-solvent question work
    # over solvents that were never compared in one call.
    for part in parts:
        assert any(fact.property == "reaction_delta_g" for fact in part.properties)


def test_a_shape_that_does_not_decompose_still_yields_exactly_one_record() -> None:
    """`records_for` is the only entry point, so the ordinary case must go through it unchanged."""
    records = records_for(
        calc_ref="rxn-1",
        calc_type="calc.compute_reaction_energy",
        payload=_reaction().model_dump(mode="json"),
        payload_kind="ReactionEnergyResult",
    )
    assert len(records) == 1


def test_a_repeated_species_gets_its_own_member_and_its_own_row_id() -> None:
    """A repeated species gets its own member and its own row id.

    Tools list a species once per equivalent; matching each energy to the first matching member
    would leave the second without facts and collide two facts on `value_id`. The two energies
    differ so a collision is detectable.
    """
    from chemclaw.publish.dialect import rows_for

    payload: dict[str, Any] = {
        "reactants": ["O", "O"],
        "products": ["OO"],
        "method": "gfn2",
        "temperature_k": 298.15,
        "level": "full",
        "solvent": "water",
        "delta_e_kcal": -5.0,
        "delta_h_kcal": -5.0,
        "delta_g_kcal": -4.0,
        "species": [
            {"smiles": "O", "role": "reactant", "gibbs_free_energy_hartree": -76.4},
            {"smiles": "O", "role": "reactant", "gibbs_free_energy_hartree": -76.5},
        ],
        "warnings": [],
    }
    record = records_for(
        calc_ref="c1",
        calc_type="calc.compute_reaction_energy",
        payload=payload,
        payload_kind="ReactionEnergyResult",
    )[0]
    per_member = [
        (fact.member_ordinal, fact.value)
        for fact in record.properties
        if fact.property == "gibbs_free_energy"
    ]
    assert sorted(per_member) == [(0, -76.4), (1, -76.5)], (
        "each stoichiometric equivalent must claim its own member; both values must survive"
    )
    rows = rows_for(record, tenant_id="t", writer_version="w")["property_value"]
    ids = [row["value_id"] for row in rows]
    assert len(ids) == len(set(ids)), (
        "two facts sharing a value_id means the far end's upsert silently keeps one of them"
    )


def test_an_ensemble_publishes_populations_through_the_same_entry_point() -> None:
    """The conformer case, driven through `records_for` rather than through `project`."""
    members = [
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
        conformers=members,
        total_found=12,
        conformational_entropy_cal_per_mol_k=1.4,
        ensemble_correction_kcal=-0.4,
    )
    payload = ensemble.model_dump(mode="json")
    # `structure_id` is a derived property, so it is not dumped — the live path injects it in
    # `science/calc/geometry.py` and this mirrors that.
    for dumped, member in zip(payload["conformers"], members, strict=True):
        dumped["structure_id"] = member.structure.structure_id
    records = records_for(
        calc_ref="ens-1",
        calc_type="xtb.conformers",
        payload=payload,
        payload_kind="ConformerEnsemble",
    )
    assert len(records) == 1
    populations = [conformer.population for conformer in records[0].conformers]
    assert populations == [0.7, 0.3], (
        "the populations are the whole reason an ensemble is published"
    )


def test_every_payload_projector_is_reachable_by_some_declared_kind() -> None:
    """Every payload projector is reachable by some declared kind.

    The table's keys must be exactly what `projector_for` honours, or a projector is dead code that
    reads like coverage.
    """
    for kind in PAYLOAD_PROJECTORS:
        assert projector_for("nothing.matches.this.prefix", kind) is not None, (
            f"{kind!r} is registered but does not route"
        )


def test_a_partial_payload_never_escapes_the_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial payload never escapes the enqueue.

    `enqueue_payload` never raises, yet projectors reading list-element fields raise a bare
    `KeyError` on a missing field. Live payloads come from pydantic, but `backfill_cached` reads
    rows other calculator versions wrote. Every single-field deletion must be absorbed.
    """
    from chemclaw.publish import outbox

    # Enabled, but with the write stubbed: this test is about the guard, not the queue.
    monkeypatch.setattr(outbox, "publishing_enabled", lambda: True)
    monkeypatch.setattr(outbox, "enqueue", _never_written)

    shapes = {
        "ReactionEnergyResult": _reaction().model_dump(mode="json"),
        "ThermochemistryResult": _thermochemistry().model_dump(mode="json"),
    }
    mutations: list[tuple[str, str, dict[str, Any]]] = []
    for kind, full in shapes.items():
        for key, value in full.items():
            partial = copy.deepcopy(full)
            del partial[key]
            mutations.append((kind, f"{key} removed", partial))
            # Nested removal is the case that mattered: a top-level key vanishing raised
            # `ValueError`, which the old guard caught. A field missing from a *list element* did
            # not, and that is what an older calculator version's rows look like.
            if isinstance(value, list) and value and isinstance(value[0], dict):
                for nested in list(value[0]):
                    deep = copy.deepcopy(full)
                    for item in deep[key]:
                        item.pop(nested, None)
                    mutations.append((kind, f"{key}[].{nested} removed", deep))

    async def _run() -> None:
        for kind, label, partial in mutations:
            written = await outbox.enqueue_payload(
                calc_ref="c1",
                calc_type="calc.compute_thermochemistry",
                payload=partial,
                payload_kind=kind,
            )
            assert written in (0, 1), f"{kind} [{label}]: unexpected write count {written}"

    assert len(mutations) > 30, "the sweep must actually exercise the nested-field case"
    asyncio.run(_run())


async def _never_written(records: Any) -> int:
    """Stand-in for the queue write, so the mutation sweep touches no database."""
    return len(records)


def test_the_shipped_driver_satisfies_the_shipped_sink() -> None:
    """The shipped driver satisfies the shipped sink's runtime check.

    `Warehouse` is `@runtime_checkable`, which requires every member, so a driver missing one fails
    every delivery at connect. Asserted with `isinstance`, which is what production runs.
    """
    from chemclaw.ingest.eln.warehouse.driver import Warehouse
    from chemclaw.publish.drivers.postgres import PostgresWarehouse

    driver = PostgresWarehouse(dsn="postgresql://unused/never-connected")
    assert isinstance(driver, Warehouse), (
        "the shipped Postgres driver fails the shipped sink's own runtime check; "
        f"missing: {sorted(set(dir(Warehouse)) - set(dir(driver)) - {'_is_runtime_protocol'})}"
    )


# --- the third hook: a tool composite -----------------------------------------------------------
#
# `D-2026-08-27-a-composite-needs-a-hook-not-a-projector` added a hook for tool composites. These
# tests start at a real tool call through the MCP tool manager, with the hook installed as
# `connector_app` installs it, not at `publish_tool_result`.


def _publishing(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Turn publishing on the way a deployment does, and capture what reaches the queue.

    `settings.result_sinks` is set rather than patching `publishing_enabled`, which two modules
    read. Only the queue write is stubbed.
    """
    from chemclaw.core.config import settings
    from chemclaw.publish import outbox

    queued: list[Any] = []

    async def _enqueue(records: list[Any]) -> int:
        queued.extend(records)
        return len(records)

    monkeypatch.setattr(settings, "result_sinks", "test-sink")
    monkeypatch.setattr(outbox, "enqueue", _enqueue)
    return queued


def _calc_stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The `calc` tool surface with its publish hook installed, over a fake calculation server.

    Importing the bundle's `app` module is what runs `connector_app`, which is what installs the
    hook — so this asserts the wiring a pod actually gets rather than calling the installer here.
    """
    import chemclaw.connectors.calc.server.app  # noqa: F401  runs `connector_app`, installing it
    import chemclaw.connectors.calc.server.tools as calc_tools
    from chemclaw.connectors.calc import compose
    from chemclaw.science.calc.artifacts import InMemoryArtifactStore
    from chemclaw.science.calc.store import InMemoryStore
    from tests.calc_server_fake import FakeCalcServer, install

    install(monkeypatch, FakeCalcServer())
    monkeypatch.setattr(calc_tools, "default_store", lambda: InMemoryStore())
    monkeypatch.setattr(compose, "default_artifact_store", InMemoryArtifactStore)
    return calc_tools


def test_a_hessian_cache_miss_publishes_what_its_row_actually_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Hessian cache miss publishes what its row actually holds, through `cached_compute`.

    Runs the composite's own `hessian()`: remote key, store miss, remote compute, `store.put`,
    `publish_stored_result`. A wavenumber needs the mass-weighted matrix and the row carries no
    elements, so no frequency is asserted; that is why the third hook exists.
    """
    from chemclaw.connectors.calc import compose
    from chemclaw.science.calc.artifacts import InMemoryArtifactStore
    from chemclaw.science.calc.store import InMemoryStore
    from tests.calc_server_fake import FakeCalcServer, install

    install(monkeypatch, FakeCalcServer())
    queued = _publishing(monkeypatch)

    async def _take_one() -> Any:
        store = InMemoryStore()
        structure = await compose.embed("CCO")
        return await compose.hessian(store, structure, None, artifacts=InMemoryArtifactStore())

    payload, cached = asyncio.run(_take_one())

    assert cached is False, "the publish hook is on the miss path; this run has to be a miss"
    records = [record for record in queued if record.calc_type.startswith("xtb.hess")]
    assert len(records) == 1, "a computed Hessian reached no results store"
    facts = {fact.property: fact for fact in records[0].properties}
    assert facts["electronic_energy"].value == payload.electronic_energy_hartree
    assert facts["atom_count"].value == payload.atom_count
    assert "wavenumber" not in facts and not records[0].points, (
        "a Hessian row cannot yield a frequency — it carries no masses. If this passes, the "
        "projector is inventing one"
    )
    # The packed arrays are dropped from the published payload: `result_publications` is never
    # pruned, the matrix is already content-addressed in the artifact store, and the arrays grow as
    # (3N)^2 doubles while everything else is a few scalars.
    assert not {"hessian_npy", "dipole_derivatives_npy"} & set(records[0].payload), (
        "the packed arrays rode into the outbox document"
    )
    assert records[0].payload["structure_id"] == payload.structure_id, (
        "everything that is not an array still rides along untouched"
    )


async def test_a_published_gradient_is_converted_into_the_unit_the_registry_keeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published gradient is converted into the unit the registry keeps.

    Both calculators report gradients per Angstrom while the registry keeps `max_gradient` in
    Hartree/bohr. Asserted over both projectors from the real cache hook, against an independently
    written constant.
    """
    from chemclaw.connectors.calc import compose
    from chemclaw.science.calc.artifacts import InMemoryArtifactStore
    from chemclaw.science.calc.store import InMemoryStore
    from tests.calc_server_fake import FakeCalcServer, install

    bohr_radius_angstrom = 0.529177210903

    install(monkeypatch, FakeCalcServer())
    queued = _publishing(monkeypatch)

    store = InMemoryStore()
    structure = await compose.embed("CCO")
    relaxed, _ = await compose.relax(store, structure, None)
    await compose.hessian(store, relaxed.structure, None, artifacts=InMemoryArtifactStore())

    gradients = [
        fact for record in queued for fact in record.properties if fact.property == "max_gradient"
    ]
    assert len(gradients) == 2, "both the optimization and the Hessian report a gradient"
    for fact in gradients:
        assert fact.unit == "hartree/angstrom", "the reported unit must be the calculator's own"
        assert fact.value == pytest.approx(fact.reported_value * bohr_radius_angstrom), (
            "`value` is the column a chemist writes a convergence predicate against, so it is in "
            "the registry's Hartree/bohr; `reported_value` is what the calculator said"
        )


def test_a_thermochemistry_tool_call_publishes_the_frequencies_it_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thermochemistry tool call publishes the frequencies it computed.

    `compute_thermochemistry` is a tool composite with no cache row or job envelope, so only the
    tool hook sees it. Driven through the MCP tool manager with the hook `connector_app` installs;
    the numbers asserted are the ones the tool returned.
    """
    calc_tools = _calc_stack(monkeypatch)
    queued = _publishing(monkeypatch)

    result = asyncio.run(
        calc_tools.server._tool_manager.call_tool("compute_thermochemistry", {"smiles": "CCO"})
    )

    records = [record for record in queued if record.payload_kind == "ThermochemistryResult"]
    assert len(records) == 1, "a thermochemistry the agent asked for reached no results store"
    record = records[0]
    assert record.calc_type == "calc.compute_thermochemistry", (
        "the route says where it came from; the shape is carried beside it"
    )
    facts = {fact.property: fact for fact in record.properties}
    assert facts["gibbs_free_energy"].value == result.gibbs_free_energy_hartree
    assert facts["zero_point_energy"].value == result.zero_point_energy_kcal
    published = [point.value for point in record.points if point.property == "wavenumber"]
    assert published == [mode.wavenumber_cm for mode in result.modes], (
        "the frequencies published must be the frequencies returned, in order"
    )
    assert published, "the whole point of this hook is that a frequency reaches a results store"


# The tool that produces each declared tool composite, and its arguments. Paired with
# `TOOL_COMPOSITES` below so a shape is declared publishable only if some tool publishes it.
_TOOL_COMPOSITE_CALLS: dict[str, tuple[str, dict[str, Any]]] = {
    "ThermochemistryResult": ("compute_thermochemistry", {"smiles": "CCO"}),
    "LogdResult": ("predict_logd", {"smiles": "CC(=O)Nc1ccc(O)cc1"}),
}


@pytest.mark.parametrize("payload_kind", sorted(_TOOL_COMPOSITE_CALLS))
def test_every_declared_tool_composite_is_published_by_a_real_tool_call(
    payload_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every declared tool composite is published by a real tool call.

    The derivation test proves nothing is missing from `TOOL_COMPOSITES`; this proves nothing in it
    is something no tool emits.
    """
    from chemclaw.publish.hooks import TOOL_COMPOSITES

    assert set(_TOOL_COMPOSITE_CALLS) == set(TOOL_COMPOSITES), (
        "every declared tool composite needs a call that produces it; missing "
        f"{sorted(set(TOOL_COMPOSITES) - set(_TOOL_COMPOSITE_CALLS))}"
    )
    calc_tools = _calc_stack(monkeypatch)
    queued = _publishing(monkeypatch)
    tool, arguments = _TOOL_COMPOSITE_CALLS[payload_kind]

    asyncio.run(calc_tools.server._tool_manager.call_tool(tool, arguments))

    published = [record for record in queued if record.payload_kind == payload_kind]
    assert len(published) == 1, f"{tool} published no {payload_kind}"
    assert published[0].calc_type == f"calc.{tool}"
    assert published[0].properties, "a record with no facts says nothing was found"


async def test_asking_the_same_composite_twice_is_one_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking the same composite twice is one record.

    A tool composite has no cache key, so its identity is the route plus a hash of the result:
    identical questions collapse on `ON CONFLICT DO NOTHING`, while a second temperature is a second
    measurement.
    """
    calc_tools = _calc_stack(monkeypatch)
    queued = _publishing(monkeypatch)

    manager = calc_tools.server._tool_manager
    await manager.call_tool("compute_thermochemistry", {"smiles": "CCO"})
    await manager.call_tool("compute_thermochemistry", {"smiles": "CCO"})
    await manager.call_tool("compute_thermochemistry", {"smiles": "CCO", "temperature_k": 310.0})

    refs = [record.calc_ref for record in queued if record.payload_kind == "ThermochemistryResult"]
    assert len(refs) == 3
    assert refs[0] == refs[1], "the same question twice must address one record"
    assert refs[2] != refs[0], "a second temperature is a second measurement, not a duplicate"


async def test_an_unstated_default_and_the_value_it_resolves_to_are_one_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unstated default and the value it resolves to are one record.

    `ph=None` and `temperature_k=0.0` are sentinels resolved from settings, so identity is taken
    from the parameter the result restates. Both composites are exercised because the sentinel types
    differ.
    """
    from chemclaw.core.config import settings

    calc_tools = _calc_stack(monkeypatch)
    queued = _publishing(monkeypatch)

    manager = calc_tools.server._tool_manager
    await manager.call_tool("predict_logd", {"smiles": "CC(=O)Nc1ccc(O)cc1"})
    await manager.call_tool(
        "predict_logd", {"smiles": "CC(=O)Nc1ccc(O)cc1", "ph": settings.logd_default_ph}
    )
    await manager.call_tool("compute_thermochemistry", {"smiles": "CCO"})
    await manager.call_tool(
        "compute_thermochemistry",
        {"smiles": "CCO", "temperature_k": settings.xtb_thermo_temperature_k},
    )

    for kind in ("LogdResult", "ThermochemistryResult"):
        refs = [record.calc_ref for record in queued if record.payload_kind == kind]
        assert len(refs) == 2, f"both {kind} calls must reach the hook"
        assert refs[0] == refs[1], (
            f"{kind}: omitting the default and passing it are one measurement, so one record"
        )


async def test_a_presentational_argument_does_not_fork_a_composite_s_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A presentational argument does not fork a composite's identity.

    `top_bands` only truncates `modes` for the caller's context budget; every measurement is
    identical, so it must not create a second permanent row. A real second temperature still does.
    """
    calc_tools = _calc_stack(monkeypatch)
    queued = _publishing(monkeypatch)

    manager = calc_tools.server._tool_manager
    await manager.call_tool("compute_thermochemistry", {"smiles": "CCO"})
    await manager.call_tool("compute_thermochemistry", {"smiles": "CCO", "top_bands": 200})

    records = [record for record in queued if record.payload_kind == "ThermochemistryResult"]
    assert len(records) == 2, "both calls must reach the hook"
    assert records[0].payload["modes"] != records[1].payload["modes"], (
        "the two calls returned the same `modes` list, so this test would pass without proving "
        "anything — pick a `top_bands` that actually truncates against this stack"
    )
    assert records[0].calc_ref == records[1].calc_ref, (
        "how many IR bands the caller asked to see forked the permanent identity of the "
        "measurement: same molecule, same temperature, same free energy, two rows kept forever"
    )


def test_a_composite_recomputed_after_the_calculator_moved_is_not_dropped_as_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A composite recomputed after the calculator moved is not dropped as a duplicate.

    The outbox identity is `(sink, calc_ref, schema_version)` and delivered rows are kept, so the
    ref must move when the numbers do. Composites carry no version (`publish` may not import
    `science`), so the result's numbers provide it.
    """
    from tests.calc_server_fake import FakeCalcServer, install

    class _MovedCalculator(FakeCalcServer):
        """The same server after its pKa model was refit — one number, everything else identical."""

        def _predict_pka(self, arguments: dict[str, Any]) -> dict[str, Any]:
            payload = super()._predict_pka(arguments)
            return {**payload, "pka": payload["pka"] + 0.7}

    calc_tools = _calc_stack(monkeypatch)
    queued = _publishing(monkeypatch)
    arguments = {"smiles": "CC(=O)Nc1ccc(O)cc1"}

    async def _before() -> None:
        await calc_tools.server._tool_manager.call_tool("predict_logd", arguments)

    asyncio.run(_before())
    install(monkeypatch, _MovedCalculator())

    async def _after() -> None:
        await calc_tools.server._tool_manager.call_tool("predict_logd", arguments)

    asyncio.run(_after())

    records = [record for record in queued if record.payload_kind == "LogdResult"]
    assert len(records) == 2, "both calls must reach the hook"
    assert records[0].payload["pka"] != records[1].payload["pka"], (
        "the fixture has to actually change the science, or this proves nothing"
    )
    assert records[0].calc_ref != records[1].calc_ref, (
        "the re-run's different result would be dropped as a duplicate of the first computation"
    )
    assert records[0].input_hash == records[1].input_hash, (
        "the request did not change, and `input_hash` is the request"
    )


def test_a_results_store_that_cannot_be_reached_fails_neither_the_tool_nor_the_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable results store fails neither the tool nor the calculation.

    Driven from both new paths with the queue write raising beneath `enqueue` (which swallows its
    own failures), at `enqueue_payload`, to prove the guard itself.
    """
    from chemclaw.publish import hooks
    from chemclaw.science.calc.store import InMemoryStore

    calc_tools = _calc_stack(monkeypatch)
    _publishing(monkeypatch)

    refused: list[str] = []

    async def _refuse(**kwargs: Any) -> int:
        refused.append(str(kwargs.get("payload_kind")))
        raise ConnectionError("the results store is not there")

    monkeypatch.setattr(hooks, "enqueue_payload", _refuse)
    monkeypatch.setattr(calc_tools, "default_store", lambda: InMemoryStore())

    result = asyncio.run(
        calc_tools.server._tool_manager.call_tool("compute_thermochemistry", {"smiles": "CCO"})
    )
    assert result.gibbs_free_energy_hartree, "the tool must return its science regardless"
    # Without this the assertion above is vacuous: a hook that never fired also cannot fail a tool,
    # which is exactly the state this whole change was fixing.
    assert refused == ["ThermochemistryResult"], "the failing publish was never attempted"


def test_every_projector_is_claimed_by_exactly_one_hook() -> None:
    """Every projector is claimed by exactly one hook.

    `TOOL_COMPOSITES` is declared in `publish/hooks.py`; this test derives it: a shape no
    `_CALC_TYPE_PROJECTORS` prefix reaches and no job envelope carries is a tool composite and must
    be declared. Derived from `_CALC_TYPE_PROJECTORS` rather than the fake's `_KEYED`, which omits
    two cached primitives.
    """
    from chemclaw.publish.hooks import TOOL_COMPOSITES
    from chemclaw.publish.project import _CALC_TYPE_PROJECTORS

    cached = {projector for _prefix, projector in _CALC_TYPE_PROJECTORS}
    derived = {
        kind
        for kind, projector in PAYLOAD_PROJECTORS.items()
        if projector not in cached and kind not in _ENVELOPE_MEMBERS
    }
    assert derived == set(TOOL_COMPOSITES), (
        "a shape with a projector, no cache prefix and no job envelope reaches a results store "
        "only through the tool hook. Undeclared: "
        f"{sorted(derived - set(TOOL_COMPOSITES))}; stale: "
        f"{sorted(set(TOOL_COMPOSITES) - derived)}"
    )
