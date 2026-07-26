"""Boltzmann-weighted GFN2-xTB conformer ensemble — research follow-up, D-092.

`calc.xtb` computes one seeded conformer's energy — a good *relative* comparator, but it
approximates each molecule as a single rigid structure. In solution a flexible molecule
populates many conformers, and process-relevant properties (an estimated solvation energy, a
conformer-averaged descriptor) are more honestly read from a Boltzmann-weighted ensemble than a
single geometry. This generates an RDKit ETKDG ensemble, prunes it with the cheap MMFF force
field (an expensive xTB pass on a near-duplicate or high-energy conformer buys nothing), then
runs GFN2-xTB on every surviving conformer and Boltzmann-weights the result — reusing the exact
same engine (`calc.xtb_engine`) as the single-conformer calculator, so it is zero new
dependencies.

An ensemble this size (tens of xTB single points) is materially heavier than the sub-second
budget the inline fast-calculator pattern assumes, so it is exposed as a durable Temporal
workflow (`workflows.conformer_job`) rather than an inline agent tool — the pure algorithm lives
here so it is unit-testable without Temporal, exactly as `bo.engine`'s pure functions are wrapped
by `workflows.bo_activities`.
"""

import math

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import AllChem

from calc.xtb_engine import (
    gfn2_energy,
    parse_molecule,
    positions_bohr,
    require_closed_shell,
)
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.509
# Gas constant in kcal/(mol*K), for the Boltzmann weight exp(-relative_energy / (R*T)).
_R_KCAL_PER_MOL_K = 1.987204e-3


class ConformerEnsembleInput(BaseModel):
    """A conformer-ensemble request: a molecule and its charge."""

    smiles: str = Field(min_length=1)
    charge: int = 0


class ConformerEnergy(BaseModel):
    """One surviving conformer's relative GFN2-xTB energy and Boltzmann population."""

    conformer_id: int
    relative_energy_kcal: float
    boltzmann_population: float


class ConformerEnsembleResult(BaseModel):
    """The ensemble's Boltzmann-weighted energy, plus every conformer that was actually scored.

    `n_conformers_generated` is the raw ETKDG count; `n_conformers_evaluated` is how many
    survived the MMFF energy-window prune before the expensive xTB pass — report both so a
    consumer can see how aggressive the pruning was, rather than only the final population table.
    """

    smiles: str
    method: str
    charge: int
    n_conformers_generated: int
    n_conformers_evaluated: int
    lowest_energy_hartree: float
    boltzmann_weighted_energy_hartree: float
    conformers: list[ConformerEnergy] = Field(default_factory=list)


def compute_conformer_ensemble(job: ConformerEnsembleInput) -> ConformerEnsembleResult:
    """Generate, prune, and Boltzmann-weight a GFN2-xTB conformer ensemble for one molecule.

    Raises `ValueError` on an unparseable SMILES, a declared charge that contradicts the SMILES
    formal charge, an open-shell electron count, or a molecule ETKDG cannot embed even one
    conformer for — the same gate-G4 discipline as `calc.xtb.run_xtb`, never a fabricated result.
    """
    mol = parse_molecule(job.smiles)
    formal_charge = Chem.GetFormalCharge(mol)
    if formal_charge != job.charge:
        raise ValueError(
            f"declared charge {job.charge} does not match the formal charge "
            f"{formal_charge} of {job.smiles!r}"
        )
    require_closed_shell(mol, job.charge)

    conf_ids = list(
        AllChem.EmbedMultipleConfs(
            mol,
            numConfs=settings.conformer_ensemble_size,
            randomSeed=settings.xtb_embed_seed,
            pruneRmsThresh=0.5,
        )
    )
    if not conf_ids:
        raise ValueError(f"could not embed any 3D conformer for {job.smiles!r}")

    # MMFF is orders of magnitude cheaper than xTB: optimize every conformer and use its energy
    # to prune the ensemble before spending xTB time on structures that would carry a negligible
    # Boltzmann population anyway. Skipped (no prune) when the force field lacks parameters for
    # this molecule (a valid, common case, e.g. some heteroaromatics) — every conformer proceeds.
    kept_ids = conf_ids
    if AllChem.MMFFHasAllMoleculeParams(mol):
        mmff_energies = dict(zip(conf_ids, AllChem.MMFFOptimizeMoleculeConfs(mol), strict=True))
        lowest_mmff = min(energy for _, energy in mmff_energies.values())
        kept_ids = [
            cid
            for cid, (_, energy) in mmff_energies.items()
            if (energy - lowest_mmff) <= settings.conformer_energy_window_kcal
        ]

    energies_hartree = {}
    for conf_id in kept_ids:
        numbers, positions = positions_bohr(mol, conf_id)
        energies_hartree[conf_id] = gfn2_energy(
            settings.xtb_method, numbers, positions, charge=job.charge
        )

    lowest = min(energies_hartree.values())
    rt = _R_KCAL_PER_MOL_K * settings.conformer_boltzmann_temperature_kelvin
    relative_kcal = {
        cid: (energy - lowest) * _HARTREE_TO_KCAL for cid, energy in energies_hartree.items()
    }
    weights = {cid: math.exp(-rel / rt) for cid, rel in relative_kcal.items()}
    total_weight = sum(weights.values())
    populations = {cid: weight / total_weight for cid, weight in weights.items()}
    weighted_energy = sum(populations[cid] * energy for cid, energy in energies_hartree.items())

    conformers = [
        ConformerEnergy(
            conformer_id=cid,
            relative_energy_kcal=relative_kcal[cid],
            boltzmann_population=populations[cid],
        )
        for cid in kept_ids
    ]
    return ConformerEnsembleResult(
        smiles=job.smiles,
        method=settings.xtb_method,
        charge=job.charge,
        n_conformers_generated=len(conf_ids),
        n_conformers_evaluated=len(kept_ids),
        lowest_energy_hartree=lowest,
        boltzmann_weighted_energy_hartree=weighted_energy,
        conformers=conformers,
    )
