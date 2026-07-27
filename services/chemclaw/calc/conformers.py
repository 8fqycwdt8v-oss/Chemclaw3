"""Conformer ensembles and their populations (xTB plan X6).

The phase that retires this system's most pervasive caveat. Every number below it — an
energy, a pKa, a dipole, a Fukui ranking, a reaction free energy — describes one
conformer, and for a flexible molecule that is a shape rather than the compound. An
ensemble replaces "the free energy of a geometry" with "the free energy of a compound",
and it does one more thing that matters at drug size: it supplies the **conformational
entropy**, a term missing from every single-conformer free energy and worth several
kcal/mol on a molecule with a dozen rotatable bonds.

**What `level="thorough"` actually does, stated precisely.** It runs the search, takes
the lowest member for the full optimization and Hessian, and adds the ensemble's
conformational entropy. It does *not* Boltzmann-average free energies over every
conformer: that would be one Hessian per member, which on a 76-atom substrate is roughly
half an hour each. The approximation is the standard one and its error is second-order —
the populations weight energies that are already close — but it is an approximation, and
`ConformerEnsemble.treatment` says which one was used rather than leaving a reader to
assume the better one.

**This is the system's first non-deterministic calculator, and that must be said out
loud.** CREST samples by metadynamics from a random seed, so two runs on the same
molecule find slightly different ensembles — measured on n-butane, the same two
conformers but rotamer counts of 17 and 18 on consecutive runs, moving the lowest
population by ~1.4 points. Everything else in `calc/` satisfies "same key, same value";
this does not. Two consequences, both deliberate:

- the calculation store is what makes it *stable*: the first run's ensemble is the one
  every later question about that molecule sees, so a report and the number behind it
  cannot drift apart even though the underlying search would;
- a population is a sampled quantity and should be read as one. A 60/40 split is a real
  finding; a 58/42 versus 60/40 difference between two molecules is not.

The degeneracy-weighted populations reproduce CREST's own reported figures to within that
sampling scatter (n-butane's anti at 57.8-59.2% against its reported 59.1%), which is the
check that the two are counting the same thing.
"""

import math
from typing import Literal

from pydantic import BaseModel, Field

from calc import crest_cli
from calc.crest_cli import CrestEffort, EnsembleSearch
from calc.store import ResultStore, run_cached
from calc.structure import Structure
from calc.xtb_spec import CrestSpec
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.5094740631
_GAS_CONSTANT_CAL = 1.987204258640832  # cal/(mol K)


class ConformerSpec(CrestSpec):
    """Settings of one ensemble search.

    Every field enters the cache key through `model_dump()`, as with the other task
    specs — including `effort`, because a quick search and an extensive one are different
    calculations that must not share an entry.

    `CrestSpec` rather than `XtbSpec` because crest, not `engine`, is what runs one of
    these — so crest's build is what the key is versioned on.
    """

    task: Literal["conformers"] = "conformers"
    search: EnsembleSearch = "conformers"
    effort: CrestEffort = Field(default_factory=lambda: settings.crest_effort)
    temperature_k: float = Field(default_factory=lambda: settings.xtb_thermo_temperature_k, gt=0)
    # How many members to report. The search commonly finds dozens; the ones that matter
    # are the populated ones, and a hundred geometries is not something a reader uses.
    max_members: int = Field(default_factory=lambda: settings.crest_max_members, gt=0)


class Conformer(BaseModel):
    """One member of an ensemble, with what it contributes.

    `degeneracy` is how many rotamers this conformer stands for, and it multiplies the
    population — a detail that changes n-butane's anti fraction from 73% to the correct
    59%, so it is load-bearing rather than descriptive.
    """

    relative_kcal: float
    population: float
    degeneracy: int
    structure: Structure


class ConformerEnsemble(BaseModel):
    """A sampled ensemble with its Boltzmann populations and conformational entropy.

    `conformational_entropy_cal_per_mol_k` is the term a single-conformer free energy is
    missing: -R * sum(p ln p) over the populations. It is always positive, so ignoring it
    systematically *over*-estimates the free energy of a flexible species — and does so
    unequally when a reaction changes flexibility, which is exactly when it matters.
    """

    smiles: str | None
    method: str
    search: EnsembleSearch
    effort: CrestEffort
    solvent: str | None
    temperature_k: float
    conformers: list[Conformer]
    total_found: int
    conformational_entropy_cal_per_mol_k: float
    # A metadynamics search is stochastic: this ensemble is a *sample* of conformational
    # space, not an enumeration of it. Carried on the result so a reader treats the
    # populations as sampled rather than exact (see the module docstring).
    sampled: Literal[True] = True
    # Free energy of the ensemble relative to its lowest member, in kcal/mol: the
    # -T*S_conf correction to add to a single-conformer free energy.
    ensemble_correction_kcal: float
    treatment: Literal["lowest-plus-conformational-entropy"] = "lowest-plus-conformational-entropy"

    @property
    def lowest(self) -> Structure:
        """The lowest-energy member — what a downstream single-structure task should use."""
        return self.conformers[0].structure


def _populations(energies: list[float], degeneracies: list[int], temperature: float) -> list[float]:
    """Degeneracy-weighted Boltzmann populations from relative energies in kcal/mol."""
    rt = _GAS_CONSTANT_CAL * temperature / 1000.0  # kcal/mol
    weights = [
        degeneracy * math.exp(-value / rt)
        for value, degeneracy in zip(energies, degeneracies, strict=True)
    ]
    total = sum(weights)
    return [weight / total for weight in weights]


def _conformational_entropy(populations: list[float], degeneracies: list[int]) -> float:
    """Ensemble entropy in cal/(mol K), degeneracies included.

    Each conformer stands for `g` equally populated rotamers, so the sum runs over states
    rather than over conformers: S = -R * sum p ln(p/g). Reproduces CREST's own reported
    ensemble entropy for n-butane to three figures, which is the check that the two are
    counting the same thing.
    """
    return -_GAS_CONSTANT_CAL * sum(
        population * math.log(population / degeneracy)
        for population, degeneracy in zip(populations, degeneracies, strict=True)
        if population > 0
    )


def compute_ensemble(spec: ConformerSpec, structure: Structure) -> ConformerEnsemble:
    """Search conformational space around `structure` and weight what was found."""
    members = crest_cli.run(
        structure,
        search=spec.search,
        method=spec.method,
        effort=spec.effort,
        solvent=spec.solvent,
        temperature_k=spec.temperature_k,
    )
    if not members:
        raise ValueError("the conformer search returned no structures")
    lowest = min(member.energy_hartree for member in members)
    relative = [(member.energy_hartree - lowest) * _HARTREE_TO_KCAL for member in members]
    degeneracies = [member.degeneracy for member in members]
    populations = _populations(relative, degeneracies, spec.temperature_k)
    entropy = _conformational_entropy(populations, degeneracies)
    return ConformerEnsemble(
        smiles=structure.smiles,
        method=spec.method,
        search=spec.search,
        effort=spec.effort,
        solvent=spec.solvent,
        temperature_k=spec.temperature_k,
        conformers=[
            Conformer(
                relative_kcal=round(energy, 3),
                population=round(population, 4),
                degeneracy=member.degeneracy,
                structure=member.structure,
            )
            for energy, population, member in zip(relative, populations, members, strict=True)
        ][: spec.max_members],
        total_found=len(members),
        conformational_entropy_cal_per_mol_k=round(entropy, 3),
        ensemble_correction_kcal=round(-spec.temperature_k * entropy / 1000.0, 3),
    )


async def run_cached_ensemble(
    store: ResultStore, structure: Structure, spec: ConformerSpec | None = None
) -> tuple[ConformerEnsemble, bool]:
    """Return the ensemble for `structure`, reusing the store on a repeat.

    Worth caching more than anything else here: a CREST search is the most expensive
    single calculation in the system, and a molecule's ensemble is reused by every
    question anyone later asks about it.
    """
    spec = spec or ConformerSpec()
    return await run_cached(
        store,
        spec.cache_key(structure),
        lambda: compute_ensemble(spec, structure),
        ConformerEnsemble,
    )
