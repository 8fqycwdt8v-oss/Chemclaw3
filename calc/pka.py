"""xTB-based pKa predictor (plan step 1c.4).

The user asked to "use xTB for pKa". This is the standard free-energy-difference
approach at semiempirical level: for the most acidic O-H/S-H site, compute the
GFN2-xTB solvated (ALPB water) deprotonation energy and map it to pKa with a
linear calibration (slope/intercept from config). Candidate sites are enumerated,
each conjugate base is evaluated, and the most stable anion defines the pKa.

Approximate by construction — the result carries the calibration's residual as an
uncertainty; never present the value as exact. v1 covers O-H/S-H acids (carboxylic
acids, phenols, alcohols, thiols); N-H and C-H acids are a later extension.
"""

from pydantic import BaseModel, Field
from rdkit import Chem

from calc.store import CalculationKey, ResultStore, cached_compute
from calc.xtb_engine import geometry, gfn2_energy, parse_molecule
from chemclaw.config import settings

CALC_TYPE = "pka"
_HARTREE_TO_KCAL = 627.509
# Heavy atoms whose O-H/S-H protons we treat as acidic sites in v1.
_ACIDIC_HEAVY = (8, 16)  # O, S


class PkaInput(BaseModel):
    """A pKa request: the neutral acid as SMILES."""

    smiles: str = Field(min_length=1)


class PkaResult(BaseModel):
    """A predicted pKa for the most acidic O-H/S-H site, with its uncertainty.

    `deprotonation_energy_kcal` is the solvated GFN2-xTB energy of the best
    conjugate base minus the neutral acid; `pka` is its linear calibration.
    """

    smiles: str
    method: str
    pka: float
    deprotonation_energy_kcal: float
    uncertainty: float


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


def predict_pka(job: PkaInput) -> PkaResult:
    """Predict the pKa of the most acidic O-H/S-H site of a molecule.

    Raises `ValueError` on an unparseable SMILES or a molecule with no acidic
    O-H/S-H site (nothing to deprotonate), rather than inventing a value (G4).
    """
    neutral = parse_molecule(job.smiles)
    anions = _conjugate_bases(neutral)
    if not anions:
        raise ValueError(f"no acidic O-H/S-H site to deprotonate in {job.smiles!r}")

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


async def run_cached_pka(store: ResultStore, job: PkaInput) -> tuple[PkaResult, bool]:
    """Return a pKa prediction for `job`, reusing the store on a repeat.

    The key is versioned by method, solvent, and calibration, so recalibrating or
    switching solvent recomputes rather than serving a stale pKa.
    """
    key = CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=(
            f"{settings.xtb_method}/alpb-{settings.pka_solvent}/"
            f"cal-{settings.pka_calibration_slope}:{settings.pka_calibration_intercept}"
        ),
        inputs={"smiles": job.smiles},
        params={"embed_seed": settings.xtb_embed_seed},
    )

    async def _compute() -> dict[str, object]:
        return predict_pka(job).model_dump()

    payload, was_cached = await cached_compute(store, key, _compute)
    return PkaResult.model_validate(payload), was_cached
