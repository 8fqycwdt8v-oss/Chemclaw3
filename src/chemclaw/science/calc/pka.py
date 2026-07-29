"""xTB-based pKa predictor (plan step 1c.4).

The user asked to "use xTB for pKa". This is the standard free-energy-difference
approach at semiempirical level: for the most acidic O-H/S-H site, compute the
GFN2-xTB solvated (ALPB water) deprotonation energy and map it to pKa with a
linear calibration (slope/intercept from config). Candidate sites are enumerated,
each conjugate base is evaluated, and the most stable anion defines the pKa.

Approximate by construction — the result carries the calibration's residual as an
uncertainty; never present the value as exact. Covers **net-neutral** O-H/S-H acids
(carboxylic acids, phenols, alcohols, thiols); the calibration was fitted over neutral
reference acids through this exact acid(0)/anion(-1) path, so charged inputs are rejected
rather than mapped through an out-of-domain calibration (G4). C-H acids are out of scope.

**Bases (X11).** The same construction runs in reverse for a base: enumerate its
protonated forms, take the most stable, and calibrate the energy of BH+ -> B + H+ to the
conjugate-acid pKa. Fitted over 20 experimental amines, and the fit split the class in
two so sharply that only one half ships:

- **Aromatic and aryl nitrogen** — pyridines, imidazoles, azoles, anilines. Spearman
  **1.000** over seven compounds spanning pKa 1.0-6.95, R^2 0.993, worst error -0.37.
  Better than the acid calibration, and shipped.
- **Aliphatic amines** — refused. Spearman **-0.17**: the method does not merely predict
  them imprecisely, it has no ranking ability at all, and a number would be worse than a
  refusal because it would look like an answer.

The failure is diagnosed rather than assumed, and the diagnosis is why no amount of
recalibration fixes it. In the **gas phase** GFN2 reproduces the experimental proton
affinities exactly (NH3 < MeNH2 < Me2NH < Me3N), so the Hamiltonian is fine. Switching on
ALPB **reverses** that order completely. And the true aqueous order is neither: it is
non-monotonic (Me3N < NH3 < MeNH2 < Me2NH), because aqueous aliphatic amine basicity is
set by how many hydrogen bonds the ammonium ion can donate to water — which falls with
substitution and which a continuum model, having no explicit solvent, cannot represent.
A different linear map cannot recover a non-monotonic relationship.

That is the honest state: this predictor covers aromatic nitrogen well and aliphatic
amines not at all. Explicit-solvent or cluster-continuum treatment is what would change
it, and neither is in this system.
"""

from typing import Literal

from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.science.calc.store import CalculationKey, ResultStore, run_cached
from chemclaw.science.calc.structure import Structure
from chemclaw.science.calc.xtb_engine import (
    engine_version,
    geometry,
    gfn2_energy,
    parse_molecule,
    require_closed_shell,
)
from chemclaw.science.calc.xtb_opt import OptSpec, optimize_structure

CALC_TYPE = "pka"
_HARTREE_TO_KCAL = 627.509
# Heavy atoms whose O-H/S-H protons we treat as acidic sites.
_ACIDIC_HEAVY = (8, 16)  # O, S
# Nitrogen valence at which there is no lone pair left to protonate.
_SATURATED_NITROGEN = 4


class PkaInput(BaseModel):
    """A pKa request: the neutral acid as SMILES."""

    smiles: str = Field(min_length=1)


class PkaResult(BaseModel):
    """A predicted pKa with its uncertainty, and which calibration produced it.

    `deprotonation_energy_kcal` is always the solvated GFN2-xTB energy of the
    **deprotonated** species minus the protonated one — for an acid that is anion minus
    neutral, for a base neutral minus cation. `site` says which, because the number a
    chemist needs is different: an acid's own pKa, or a base's *conjugate acid* pKa.
    """

    smiles: str
    method: str
    pka: float
    deprotonation_energy_kcal: float
    uncertainty: float
    # "acid" = an O-H/S-H proton came off; "base" = the pKa of the protonated form
    # (pKaH), which is what is tabulated for amines and what an extraction pH is set
    # against. Each has its own calibration, fitted separately.
    site: Literal["acid", "base"] = "acid"


def _conjugate_bases(mol: Chem.Mol) -> list[Chem.Mol]:
    """Enumerate deprotonated anions at each acidic O-H/S-H site.

    For every hydrogen bonded to O or S, remove it and place the -1 charge on the
    heavy atom (with implicit H disabled so the anion is not silently re-protonated
    on sanitize). Returns one sanitized anion molecule per candidate site.
    """
    sites = [
        (atom.GetIdx(), atom.GetNeighbors()[0].GetIdx())
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 1
        and atom.GetDegree() == 1
        and atom.GetNeighbors()[0].GetAtomicNum() in _ACIDIC_HEAVY
    ]
    anions: list[Chem.Mol] = []
    for h_idx, heavy_idx in sites:
        editable = Chem.RWMol(mol)
        heavy = editable.GetAtomWithIdx(heavy_idx)
        heavy.SetFormalCharge(-1)
        heavy.SetNoImplicit(True)
        editable.RemoveAtom(h_idx)
        anion = editable.GetMol()
        Chem.SanitizeMol(anion)
        anions.append(anion)
    return anions


def _basic_nitrogens(mol: Chem.Mol) -> list[int]:
    """Indices of nitrogens with a lone pair available to accept a proton."""
    return [
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 7
        and atom.GetFormalCharge() == 0
        and atom.GetTotalNumHs() + atom.GetDegree() < _SATURATED_NITROGEN
    ]


def _is_aryl_nitrogen(atom: Chem.Atom) -> bool:
    """Whether a nitrogen is aromatic or attached to an aromatic system.

    The class boundary the calibration is fitted on, and it is a real one rather than a
    convenience: aryl and aromatic nitrogen delocalize into the ring, so their basicity
    is dominated by that electronic effect — which GFN2 with a continuum captures well.
    An aliphatic amine's aqueous basicity is dominated by how its ammonium ion hydrogen
    bonds to water, which the same model cannot see at all (see the module docstring).
    """
    return atom.GetIsAromatic() or any(n.GetIsAromatic() for n in atom.GetNeighbors())


def _protonated_forms(mol: Chem.Mol, sites: list[int]) -> list[tuple[Chem.Mol, bool]]:
    """Build one cation per basic nitrogen, paired with whether that site is aryl.

    Returns `(cation, is_aryl)` so the caller can both pick the most stable protomer —
    the one that defines the conjugate acid — and know which calibration that site is in.
    """
    forms: list[tuple[Chem.Mol, bool]] = []
    for index in sites:
        editable = Chem.RWMol(mol)
        nitrogen = editable.GetAtomWithIdx(index)
        aryl = _is_aryl_nitrogen(nitrogen)
        nitrogen.SetFormalCharge(1)
        nitrogen.SetNumExplicitHs(nitrogen.GetNumExplicitHs() + 1)
        nitrogen.SetNoImplicit(True)
        cation = editable.GetMol()
        try:
            Chem.SanitizeMol(cation)
        except Chem.KekulizeException:  # a protonation that breaks aromaticity is not one
            continue
        forms.append((Chem.AddHs(cation), aryl))
    return forms


def _relaxed_energy(mol: Chem.Mol, charge: int) -> float:
    """Solvated energy of `mol` at a GFN2-optimized geometry.

    The base path optimizes where the acid path stops at a force-field geometry, and the
    difference was measured rather than assumed: on the same seven references, MMFF
    geometries give Spearman 0.893 and GFN2-optimized ones give **1.000**. Protonation
    changes a nitrogen's geometry substantially — pyramidalization, ring puckering — so
    the relaxation is doing real work rather than polishing.

    The acid calibration keeps its own force-field policy because it was fitted through
    that path and validated there (U3); refitting it on optimized geometries is a
    separate, deliberate change, not a side effect of this one.
    """
    numbers, positions = geometry(mol, settings.xtb_embed_seed, optimize=True)
    structure = Structure(
        elements=[int(number) for number in numbers],
        positions=[[float(value) for value in row] for row in positions],
        charge=charge,
    )
    relaxed = optimize_structure(OptSpec(solvent=settings.pka_solvent), structure)
    return relaxed.energy_hartree


def _predict_base_pka(smiles: str, base: Chem.Mol, sites: list[int]) -> PkaResult:
    """Predict the conjugate-acid pKa (pKaH) of a base, for aromatic/aryl nitrogen only.

    Raises for an aliphatic amine rather than returning a number. That is not caution —
    it is what the measurement requires: over 13 aliphatic amines the computed energy
    correlates with the experimental pKa at Spearman **-0.17**, so a prediction would
    carry no information while looking exactly like one that did (gate G4).
    """
    forms = _protonated_forms(base, sites)
    if not forms:
        raise ValueError(f"no protonatable nitrogen in {smiles!r}")
    energy_base = _relaxed_energy(base, charge=0)
    # The conjugate acid is the *most stable* protomer, so it is the lowest energy that
    # defines the equilibrium — and its site decides which calibration applies.
    energy_cation, aryl = min(
        ((_relaxed_energy(cation, charge=1), aryl) for cation, aryl in forms),
        key=lambda pair: pair[0],
    )
    if not aryl:
        raise ValueError(
            f"{smiles!r} protonates on an aliphatic nitrogen, which this predictor does "
            "not cover: over 13 reference amines its computed basicity correlates with "
            "the measured pKa at Spearman -0.17 (no ranking ability). The cause is the "
            "implicit solvent — aqueous aliphatic amine basicity is set by the ammonium "
            "ion's hydrogen bonding to water, which a continuum model cannot represent"
        )
    delta_e_kcal = (energy_base - energy_cation) * _HARTREE_TO_KCAL
    return PkaResult(
        smiles=smiles,
        method=f"{settings.xtb_method}/ALPB-{settings.pka_solvent}",
        pka=settings.pka_base_calibration_slope * delta_e_kcal
        + settings.pka_base_calibration_intercept,
        deprotonation_energy_kcal=delta_e_kcal,
        uncertainty=settings.pka_base_uncertainty,
        site="base",
    )


def predict_pka(job: PkaInput) -> PkaResult:
    """Predict the pKa of the most acidic O-H/S-H site of a neutral molecule.

    Raises `ValueError` on an unparseable SMILES, a net-charged or open-shell
    input, or a molecule with no acidic O-H/S-H site (nothing to deprotonate),
    rather than inventing a value (G4). Charged acids are outside the v1
    calibration domain (fitted on neutral acids at charge 0 with -1 anions);
    computing them here would silently run both species at wrong electron
    counts and can even invert real acidity orderings.
    """
    neutral = parse_molecule(job.smiles)
    formal_charge = Chem.GetFormalCharge(neutral)
    if formal_charge != 0:
        raise ValueError(
            f"pKa v1 requires a neutral acid; {job.smiles!r} has net formal charge {formal_charge}"
        )
    require_closed_shell(neutral, 0)
    anions = _conjugate_bases(neutral)
    if not anions:
        # No proton to lose — but it may have a lone pair to gain one on, which is the
        # question a chemist asks about an amine. Acid first when both are present: a
        # molecule with an O-H has a pKa in the ordinary sense, and that is the number
        # meant by "the pKa" of, say, an aminophenol.
        basic = _basic_nitrogens(neutral)
        if basic:
            return _predict_base_pka(job.smiles, neutral, basic)
        raise ValueError(
            f"no acidic O-H/S-H site and no basic nitrogen in {job.smiles!r}: nothing to "
            "protonate or deprotonate"
        )

    # Acid and anions share one geometry policy (MMFF where parametrized, else the
    # embedded geometry). The calibration was fitted through this exact code path,
    # so any systematic geometry effect is absorbed into slope/intercept.
    numbers, positions = geometry(neutral, settings.xtb_embed_seed, optimize=True)
    energy_acid = gfn2_energy(settings.xtb_method, numbers, positions, solvent=settings.pka_solvent)

    # The most acidic site gives the most stable (lowest-energy) conjugate base.
    best_anion_energy = min(
        gfn2_energy(
            settings.xtb_method,
            *geometry(anion, settings.xtb_embed_seed, optimize=True),
            charge=-1,
            solvent=settings.pka_solvent,
        )
        for anion in anions
    )

    delta_e_kcal = (best_anion_energy - energy_acid) * _HARTREE_TO_KCAL
    pka = settings.pka_calibration_slope * delta_e_kcal + settings.pka_calibration_intercept
    return PkaResult(
        smiles=job.smiles,
        method=f"{settings.xtb_method}/ALPB-{settings.pka_solvent}",
        pka=pka,
        deprotonation_energy_kcal=delta_e_kcal,
        uncertainty=settings.pka_uncertainty,
    )


def _calc_version() -> str:
    """Cache-key version tying pKa results to method, engine, solvent, calibration, uncertainty.

    The engine build is included (see `chemclaw.science.calc.xtb_engine.engine_version`) so a tblite
    or RDKit upgrade recomputes, exactly as the xTB energy key does. The reported
    `uncertainty` is part of the stored result, so it is keyed too — otherwise
    re-tuning `pka_uncertainty` would serve the old value from cache.
    """
    return (
        f"{settings.xtb_method}+{engine_version()}/alpb-{settings.pka_solvent}/"
        f"cal-{settings.pka_calibration_slope}:{settings.pka_calibration_intercept}/"
        f"base-{settings.pka_base_calibration_slope}:{settings.pka_base_calibration_intercept}/"
        f"u-{settings.pka_uncertainty}:{settings.pka_base_uncertainty}"
    )


async def run_cached_pka(store: ResultStore, job: PkaInput) -> tuple[PkaResult, bool]:
    """Return a pKa prediction for `job`, reusing the store on a repeat.

    The key is versioned by method, engine build, solvent, and calibration, so an
    engine upgrade, a recalibration, or a solvent switch recomputes rather than
    serving a stale pKa. The computation runs on the same canonical SMILES the
    key is built from — atom order steers the seeded embedding, so computing on
    the raw spelling would make the stored value depend on which spelling
    arrived first (D-011 determinism).
    """
    canonical = job.model_copy(update={"smiles": require_canonical_smiles(job.smiles)})
    key = CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=_calc_version(),
        inputs={"smiles": canonical.smiles},
        params={"embed_seed": settings.xtb_embed_seed},
    )
    return await run_cached(store, key, lambda: predict_pka(canonical), PkaResult)
