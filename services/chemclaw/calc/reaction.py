"""Reaction energies and solvent comparisons (xTB plan X4).

The composite the whole ladder is in service of. Everything below it answers a question
about *one* structure; this answers the question a chemist actually asks — does this
reaction go, and which way does the solvent push it — by treating every species
identically and doing the bookkeeping that is otherwise done by hand and got wrong.

Three disciplines are enforced here rather than trusted:

**Balance.** Reactant and product atoms and total charge must match. An unbalanced
equation produces a difference that includes whatever atoms the two sides do not share
— a number that is meaningless rather than merely imprecise, and one that looks
entirely ordinary. It is rejected.

**Same treatment on both sides.** Every species is optimized with the same spec, in
the same solvent, at the same level. Mixing an optimized product with an unoptimized
reactant is the other way a reaction energy silently becomes fiction.

**Every geometry is a real minimum.** A species whose Hessian carries an imaginary
frequency is not a minimum, so its free energy is not a free energy. It is reported in
`warnings` rather than quietly folded into the total.

There is deliberately **no reaction-level cache entry**. The expensive parts — one
optimization and one Hessian per species — are cached individually by
`calc.xtb_opt`/`calc.xtb_thermo`, so a second reaction sharing a species reuses it, and
a reaction is then a subtraction over values already held. Caching the subtraction too
would add an entry that can never be hit by anything the per-species entries miss.
"""

from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field
from rdkit import Chem

from calc.conformers import ConformerSpec, run_cached_ensemble
from calc.progress import Progress, no_progress
from calc.store import ResultStore
from calc.structure import structure_from_smiles
from calc.xtb_engine import parse_molecule
from calc.xtb_opt import OptSpec, run_cached_optimization
from calc.xtb_thermo import ThermoSpec, relax_to_minimum
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.5094740631

# How far the ladder is climbed per species. `quick` optimizes and differences electronic
# energies; `standard` adds a Hessian and gives enthalpies and free energies; `thorough`
# first searches conformational space, works from the lowest member, and adds the
# conformational entropy that a single-conformer free energy is missing (plan X6).
ReactionLevel = Literal["quick", "standard", "thorough"]


class SpeciesEnergy(BaseModel):
    """One species of the equation, and what was computed for it.

    `enthalpy_hartree` and `gibbs_free_energy_hartree` are None at `quick` level. Both
    are absolute values, in Hartree, because their only use is being differenced.
    """

    smiles: str
    role: Literal["reactant", "product"]
    multiplicity: int
    electronic_energy_hartree: float
    enthalpy_hartree: float | None
    gibbs_free_energy_hartree: float | None
    is_minimum: bool | None
    # The -T*S_conf term the ensemble contributed, present only at `thorough`. Positive
    # flexibility lowers a free energy, so this is negative when it is present at all.
    conformational_entropy_kcal: float | None = None
    was_cached: bool


class ReactionEnergyResult(BaseModel):
    """The energetics of one balanced reaction, with its per-species breakdown.

    Deltas are products minus reactants in kcal/mol: negative is downhill. Report the
    uncertainty with the number — a semiempirical reaction free energy is a screening
    quantity, good for comparing related reactions and poor as an absolute.
    """

    reactants: list[str]
    products: list[str]
    method: str
    solvent: str | None
    temperature_k: float
    level: ReactionLevel
    delta_e_kcal: float
    delta_h_kcal: float | None
    delta_g_kcal: float | None
    species: list[SpeciesEnergy]
    cache_hits: int
    uncertainty_kcal: float
    # Which conformational treatment produced the deltas. Was hard-coded to "single" and
    # therefore wrong at `thorough`, where an ensemble is searched and its entropy folded
    # into every ΔG — the one level where a reader most needs to know it was not single.
    conformer_treatment: Literal["single", "lowest-plus-conformational-entropy"]
    warnings: list[str] = Field(default_factory=list)


class SolventEffect(BaseModel):
    """One solvent's effect on the same reaction. `solvent=None` is the gas phase."""

    solvent: str | None
    delta_e_kcal: float
    delta_h_kcal: float | None
    delta_g_kcal: float | None


class SolventComparisonResult(BaseModel):
    """The same reaction across several solvents, most favourable first.

    `spread_kcal` is the range of the ranking quantity across the solvents tried. When
    it is not larger than `uncertainty_kcal`, the calculation has **not** distinguished
    them, and `warnings` says so — an implicit continuum model resolving 0.4 kcal/mol
    between two solvents is reading its own noise.
    """

    reactants: list[str]
    products: list[str]
    method: str
    temperature_k: float
    level: ReactionLevel
    effects: list[SolventEffect]
    best_solvent: str | None
    spread_kcal: float
    uncertainty_kcal: float
    warnings: list[str] = Field(default_factory=list)


def _composition(smiles: str) -> tuple[Counter[str], int]:
    """Element counts (hydrogens explicit) and formal charge of one species."""
    mol = parse_molecule(smiles)
    counts: Counter[str] = Counter(atom.GetSymbol() for atom in mol.GetAtoms())
    return counts, Chem.GetFormalCharge(mol)


def check_balance(reactants: list[str], products: list[str]) -> None:
    """Raise unless the equation conserves atoms and charge (gate G4).

    Named the failure, not just detected: the message says which element is short and
    by how much, because the usual cause is a forgotten water or proton and that is
    immediately fixable once stated.
    """
    if not reactants or not products:
        raise ValueError("a reaction needs at least one reactant and one product")
    left: Counter[str] = Counter()
    right: Counter[str] = Counter()
    left_charge = right_charge = 0
    for smiles in reactants:
        counts, charge = _composition(smiles)
        left += counts
        left_charge += charge
    for smiles in products:
        counts, charge = _composition(smiles)
        right += counts
        right_charge += charge
    if left != right:
        difference = {
            element: left[element] - right[element]
            for element in sorted(set(left) | set(right))
            if left[element] != right[element]
        }
        raise ValueError(
            "reaction is not atom-balanced (reactants minus products): "
            + ", ".join(f"{element} {count:+d}" for element, count in difference.items())
        )
    if left_charge != right_charge:
        raise ValueError(
            f"reaction is not charge-balanced: reactants {left_charge:+d}, "
            f"products {right_charge:+d}"
        )


async def _species_energy(
    store: ResultStore,
    smiles: str,
    role: Literal["reactant", "product"],
    opt_spec: OptSpec,
    thermo_spec: ThermoSpec | None,
    conformer_spec: ConformerSpec | None = None,
) -> SpeciesEnergy:
    """Optimize one species and, above `quick`, run its Hessian.

    Multiplicity comes from the SMILES' own radical electrons, so a homolysis — the
    reaction whose whole point is that one side is open-shell — needs no extra
    argument to be computable.
    """
    structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
    ensemble_correction = 0.0
    if conformer_spec is not None:
        ensemble, _ = await run_cached_ensemble(store, structure, conformer_spec)
        structure = ensemble.lowest
        ensemble_correction = ensemble.ensemble_correction_kcal
    if thermo_spec is None:
        optimization, opt_cached = await run_cached_optimization(store, structure, opt_spec)
        return SpeciesEnergy(
            smiles=smiles,
            role=role,
            multiplicity=structure.multiplicity,
            electronic_energy_hartree=optimization.energy_hartree,
            enthalpy_hartree=None,
            gibbs_free_energy_hartree=None,
            is_minimum=None,
            was_cached=opt_cached,
        )
    minimum, thermo, cached = await relax_to_minimum(store, structure, opt_spec, thermo_spec)
    # The conformational entropy is a free-energy term only: it changes G, never H.
    gibbs = thermo.gibbs_free_energy_hartree + ensemble_correction / _HARTREE_TO_KCAL
    return SpeciesEnergy(
        smiles=smiles,
        role=role,
        multiplicity=structure.multiplicity,
        electronic_energy_hartree=minimum.energy_hartree,
        enthalpy_hartree=thermo.enthalpy_hartree,
        gibbs_free_energy_hartree=gibbs,
        # `is not None`, not truthiness: a rigid species has a genuine 0.000 correction,
        # and `0.0 or None` reported that as "not computed at this level".
        conformational_entropy_kcal=(
            round(ensemble_correction, 3) if conformer_spec is not None else None
        ),
        is_minimum=thermo.is_minimum,
        was_cached=cached,
    )


def _difference(species: list[SpeciesEnergy], attribute: str) -> float | None:
    """Products minus reactants of one energy attribute, in kcal/mol."""
    total = 0.0
    for entry in species:
        value = getattr(entry, attribute)
        if value is None:
            return None
        total += value if entry.role == "product" else -value
    return total * _HARTREE_TO_KCAL


async def compute_reaction_energy(
    store: ResultStore,
    reactants: list[str],
    products: list[str],
    solvent: str | None = None,
    temperature_k: float | None = None,
    level: ReactionLevel = "standard",
    progress: Progress = no_progress,
) -> ReactionEnergyResult:
    """Compute the energetics of a balanced reaction, one entry per equivalent.

    Args:
        store: The calculation store; every species is computed once, ever.
        reactants: SMILES of every reactant, repeated per stoichiometric equivalent.
        products: SMILES of every product, repeated per stoichiometric equivalent.
        solvent: ALPB implicit solvent name, or None for gas phase.
        temperature_k: Temperature for the thermal corrections; None takes the config
            default (298.15 K).
        level: `quick` optimizes and gives ΔE only; `standard` adds ΔH and ΔG.
        progress: Called with a human-readable line as each species completes. Minute-
            scale runs on drug-sized molecules are the normal case, so a caller that
            needs liveness (the durable activity's heartbeat) passes it here.

    Returns:
        ΔE and (above `quick`) ΔH/ΔG in kcal/mol, the per-species breakdown, how many
        species came from the cache, and the method uncertainty to report with them.
    """
    check_balance(reactants, products)
    temperature = temperature_k or settings.xtb_thermo_temperature_k
    opt_spec = OptSpec(solvent=solvent)
    thermo_spec = (
        ThermoSpec(solvent=solvent, temperature_k=temperature) if level != "quick" else None
    )
    conformer_spec = (
        ConformerSpec(solvent=solvent, temperature_k=temperature) if level == "thorough" else None
    )

    roles: tuple[tuple[Literal["reactant", "product"], list[str]], ...] = (
        ("reactant", reactants),
        ("product", products),
    )
    queue = [(role, smiles) for role, group in roles for smiles in group]
    species = []
    for index, (role, smiles) in enumerate(queue, start=1):
        progress(f"species {index}/{len(queue)}: {smiles}")
        species.append(
            await _species_energy(store, smiles, role, opt_spec, thermo_spec, conformer_spec)
        )
    warnings = [
        f"{entry.smiles} is not a minimum (imaginary frequency): its free energy is not "
        "a free energy"
        for entry in species
        if entry.is_minimum is False
    ]
    # Every level, not just `standard`: the caveat is about the *energies*, which every
    # level differences, so gating it on one level dropped it from exactly the `thorough`
    # homolysis a user paid the most for.
    if any(entry.multiplicity > 1 for entry in species):
        warnings.append(
            "open-shell species present: unrestricted GFN2 energies are less reliable "
            "than closed-shell ones, so treat a homolysis energy as an ordering"
        )
    # Electronic energies are always present, so this delta is never optional.
    delta_e = _HARTREE_TO_KCAL * sum(
        entry.electronic_energy_hartree * (1 if entry.role == "product" else -1)
        for entry in species
    )
    return ReactionEnergyResult(
        reactants=reactants,
        products=products,
        method=opt_spec.method,
        solvent=solvent,
        temperature_k=temperature,
        level=level,
        delta_e_kcal=round(delta_e, 2),
        delta_h_kcal=_round(_difference(species, "enthalpy_hartree")),
        delta_g_kcal=_round(_difference(species, "gibbs_free_energy_hartree")),
        species=species,
        cache_hits=sum(entry.was_cached for entry in species),
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        conformer_treatment=(
            "lowest-plus-conformational-entropy" if conformer_spec is not None else "single"
        ),
        warnings=warnings,
    )


def _round(value: float | None) -> float | None:
    """Round a kcal/mol delta, passing None through."""
    return None if value is None else round(value, 2)


async def compare_solvent_effects(
    store: ResultStore,
    reactants: list[str],
    products: list[str],
    solvents: list[str],
    temperature_k: float | None = None,
    level: ReactionLevel = "standard",
    progress: Progress = no_progress,
) -> SolventComparisonResult:
    """Rank solvents by how far they push the same reaction toward products.

    Includes the gas phase as a reference point, because "the solvent barely matters
    here" is a real and useful answer and it is invisible without one.

    Args:
        store: The calculation store.
        reactants: SMILES of every reactant, repeated per stoichiometric equivalent.
        products: SMILES of every product, repeated per stoichiometric equivalent.
        solvents: ALPB solvent names to compare.
        temperature_k: Temperature for the thermal corrections; None uses the default.
        level: As `compute_reaction_energy`.
        progress: As `compute_reaction_energy`; a screen is one reaction per solvent, so
            this is the call most likely to run for minutes.

    Returns:
        One entry per solvent plus the gas phase, most favourable (most negative ΔG,
        or ΔE at `quick`) first, with the spread and a warning when that spread is
        inside the method's uncertainty.
    """
    if not solvents:
        raise ValueError("give at least one solvent to compare")
    results = []
    for solvent in [None, *solvents]:
        label = solvent or "gas phase"

        def relay(line: str, label: str = label) -> None:
            """Prefix the inner reaction's progress with which medium it is running in."""
            progress(f"{label}: {line}")

        results.append(
            await compute_reaction_energy(
                store, reactants, products, solvent, temperature_k, level, progress=relay
            )
        )
    effects = [
        SolventEffect(
            solvent=result.solvent,
            delta_e_kcal=result.delta_e_kcal,
            delta_h_kcal=result.delta_h_kcal,
            delta_g_kcal=result.delta_g_kcal,
        )
        for result in results
    ]

    def ranking(effect: SolventEffect) -> float:
        return effect.delta_g_kcal if effect.delta_g_kcal is not None else effect.delta_e_kcal

    effects.sort(key=ranking)
    spread = ranking(effects[-1]) - ranking(effects[0])
    uncertainty = settings.xtb_reaction_uncertainty_kcal
    warnings = list(dict.fromkeys(warning for result in results for warning in result.warnings))
    if spread <= uncertainty:
        warnings.append(
            f"the solvents span {spread:.1f} kcal/mol, within the method's "
            f"±{uncertainty:.1f}: this calculation does not distinguish them"
        )
    return SolventComparisonResult(
        reactants=reactants,
        products=products,
        method=results[0].method,
        temperature_k=results[0].temperature_k,
        level=level,
        effects=effects,
        best_solvent=effects[0].solvent,
        spread_kcal=round(spread, 2),
        uncertainty_kcal=uncertainty,
        warnings=warnings,
    )
