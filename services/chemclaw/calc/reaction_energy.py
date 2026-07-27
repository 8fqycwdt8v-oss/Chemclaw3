"""Reaction electronic energy / exotherm screen — research follow-up, D-092.

Process development and thermal-hazard assessment both start from the same question: is this
transformation strongly exothermic? This composes the existing cached GFN2-xTB single-point
calculator (`calc.xtb`) over each reactant/product, weighted by the caller's stoichiometric
coefficients, into a reaction electronic energy — the same "sum of product energies minus sum of
reactant energies" a chemist would do by hand from individual xTB runs, done once and cached per
species. No new dependency, no new science.

Advisory only, exactly like the structural hazard screen (`safety/`, D-080): a negative (favorable)
electronic energy at the semiempirical level is a *screening* signal, not a validated heat of
reaction — it omits entropy, solvation beyond xTB's implicit model, and phase changes. It flags
attention, it never certifies a reaction is safe to scale.
"""

from pydantic import BaseModel, Field

from calc.store import ResultStore
from calc.xtb import XtbInput, run_cached_xtb
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.509


class ReactionSpecies(BaseModel):
    """One species in a reaction: its SMILES, net charge, and stoichiometric coefficient."""

    smiles: str = Field(min_length=1)
    charge: int = 0
    coefficient: float = Field(gt=0)


class ReactionEnergyInput(BaseModel):
    """A reaction energy request: the balanced reactant and product sides."""

    reactants: list[ReactionSpecies] = Field(min_length=1)
    products: list[ReactionSpecies] = Field(min_length=1)


class ReactionEnergyResult(BaseModel):
    """The reaction's GFN2-xTB electronic energy, with the exotherm flag it triggers."""

    reaction_energy_kcal: float
    is_strongly_exothermic: bool
    exotherm_threshold_kcal: float


async def estimate_reaction_energy(
    store: ResultStore, job: ReactionEnergyInput
) -> ReactionEnergyResult:
    """Estimate a reaction's electronic energy from cached per-species GFN2-xTB single points.

    Each species is looked up (or computed and cached) independently via `run_cached_xtb`, so
    re-scoring a reaction that shares species with an earlier one is mostly free. Raises
    `ValueError` on any unparseable species or charge mismatch — `calc.xtb`'s own validation,
    propagated unchanged (gate G4) — rather than silently dropping a bad species from the sum.
    """

    async def _weighted_energy(species: list[ReactionSpecies]) -> float:
        total = 0.0
        for entry in species:
            result, _ = await run_cached_xtb(
                store, XtbInput(smiles=entry.smiles, charge=entry.charge)
            )
            total += entry.coefficient * result.total_energy_hartree
        return total

    reactant_energy = await _weighted_energy(job.reactants)
    product_energy = await _weighted_energy(job.products)
    delta_kcal = (product_energy - reactant_energy) * _HARTREE_TO_KCAL
    threshold = settings.reaction_energy_exotherm_threshold_kcal
    return ReactionEnergyResult(
        reaction_energy_kcal=delta_kcal,
        is_strongly_exothermic=delta_kcal <= threshold,
        exotherm_threshold_kcal=threshold,
    )
