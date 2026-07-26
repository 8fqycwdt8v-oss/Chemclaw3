"""Non-covalent complexes: how two molecules associate, and how strongly (xTB plan X11).

The only question in this system about **two molecules together**. Everything else —
every energy, every descriptor, every reaction — describes one species at a time, and a
great deal of process chemistry does not: an API with an excipient, a substrate with a
catalyst, a solute with the solvent it will or will not crystallize from, a host with a
guest.

CREST's `--nci` mode is what makes it tractable. It wraps the pair in a logfermi wall
potential — otherwise a metadynamics search simply lets the two molecules drift apart —
and samples binding modes, so the answer is not "the geometry I happened to build" but
the arrangements the pair actually adopts.

**The interaction energy is a difference of relaxed species**, computed the only way that
means anything: the complex at its optimized binding mode, minus each monomer optimized
on its own. That deliberately includes the deformation cost of binding, which is part of
the interaction and is what a "rigid monomer" definition leaves out.

Three limits that belong with every number this produces:

- **It is an energy, not a free energy.** Association costs entropy — two molecules
  becoming one — and that term is absent here. A complex with a favourable interaction
  energy can be entirely unbound at room temperature, and for weak complexes the entropy
  term is comparable to the interaction itself.
- **The search is stochastic**, like every CREST search: a binding mode that was not
  sampled cannot be reported, and two runs may find different sets.
- **It is one pair, in a continuum.** No bulk, no competing solvent molecules, no
  stoichiometry beyond two.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from calc import crest_cli
from calc.crest_cli import CrestEffort
from calc.store import ResultStore, run_cached
from calc.structure import Structure, structure_from_smiles
from calc.xtb_opt import OptSpec, optimize_structure
from calc.xtb_spec import XtbSpec
from chemclaw.chem import require_canonical_smiles
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.5094740631


class ComplexSpec(XtbSpec):
    """Settings of one non-covalent complex search."""

    task: Literal["complex"] = "complex"
    effort: CrestEffort = Field(default_factory=lambda: settings.crest_effort)
    # Gap between the two monomers' bounding spheres in the starting arrangement, in
    # Angstrom. Only a starting point — the wall potential and the search decide where
    # they end up — but far enough apart that the pair does not begin fused.
    separation_angstrom: float = Field(default=3.5, gt=0)


class InteractionResult(BaseModel):
    """How two molecules associate, and how strongly.

    `interaction_energy_kcal` is negative for a bound complex. It is an **electronic**
    interaction energy: the association entropy that decides whether the complex exists
    at a given temperature is not in it.
    """

    smiles_a: str
    smiles_b: str
    method: str
    solvent: str | None
    interaction_energy_kcal: float
    complex_energy_hartree: float
    monomer_energies_hartree: list[float]
    # How many distinct binding modes the search found. One is a weak result, not a
    # confident one: it usually means the search was too quick rather than that the pair
    # has a single way to bind.
    binding_modes: int
    structure: Structure
    # A metadynamics search samples binding modes rather than enumerating them.
    sampled: Literal[True] = True


def _combine(first: Structure, second: Structure, separation: float) -> Structure:
    """Place `second` beside `first` and return the pair as one structure.

    Each monomer is centred and then offset along x by the sum of their radii plus a gap,
    so the two start apart regardless of their shapes. This is only a starting point:
    the wall potential holds the pair together and the search finds the binding modes, so
    the arrangement here decides nothing except that the pair does not begin overlapping.
    """
    left = np.array(first.positions)
    right = np.array(second.positions)
    left = left - left.mean(axis=0)
    right = right - right.mean(axis=0)
    offset = _radius(left) + _radius(right) + separation
    right = right + np.array([offset, 0.0, 0.0])
    return Structure(
        elements=[*first.elements, *second.elements],
        positions=[*left.tolist(), *right.tolist()],
        charge=first.charge + second.charge,
        # Two closed shells make a closed shell; an open-shell monomer is rejected by
        # `Structure` itself rather than silently mis-assigned here.
        multiplicity=first.multiplicity + second.multiplicity - 1,
        smiles=f"{first.smiles}.{second.smiles}",
    )


def _radius(positions: np.ndarray) -> float:
    """Distance from a centred molecule's centroid to its furthest atom."""
    return float(np.linalg.norm(positions, axis=1).max())


def _ordered(smiles_a: str, smiles_b: str) -> tuple[str, str]:
    """The pair in a canonical order, so A-with-B and B-with-A are one calculation.

    The interaction of two molecules is one physical quantity, but `_combine` is not
    symmetric in its arguments: it holds the first monomer at the origin and offsets the
    second along +x, so swapping them negates the intermolecular vector while leaving each
    monomer's own orientation alone. That is a *different* starting arrangement, and it
    would key to a different cache entry — paying twice, at minutes per search, for the
    same answer.

    Ordering the pair here rather than making `_combine` symmetric keeps one rule in one
    place: the result then reports the canonical order too, so a caller never sees a
    number labelled with an order that is not the one it was computed in.
    """
    first, second = require_canonical_smiles(smiles_a), require_canonical_smiles(smiles_b)
    return (first, second) if first <= second else (second, first)


def compute_interaction(spec: ComplexSpec, smiles_a: str, smiles_b: str) -> InteractionResult:
    """Search the binding modes of two molecules and return their interaction energy.

    The pair is canonicalized (see `_ordered`), so the two molecules may be given in
    either order and the result is identical — including which is reported as `smiles_a`.

    Raises `ValueError` if either molecule is open-shell (the pair's multiplicity would
    then be a guess) or if CREST is not installed.
    """
    smiles_a, smiles_b = _ordered(smiles_a, smiles_b)
    monomers = [
        optimize_structure(
            OptSpec(method=spec.method, solvent=spec.solvent),
            structure_from_smiles(smiles, optimize=True),
        )
        for smiles in (smiles_a, smiles_b)
    ]
    start = _combine(monomers[0].structure, monomers[1].structure, spec.separation_angstrom)
    modes = crest_cli.run(
        start,
        search="complex",
        method=spec.method,
        effort=spec.effort,
        solvent=spec.solvent,
    )
    if not modes:
        raise ValueError("the complex search returned no binding modes")
    bound = optimize_structure(
        OptSpec(method=spec.method, solvent=spec.solvent),
        min(modes, key=lambda member: member.energy_hartree).structure,
    )
    separated = sum(monomer.energy_hartree for monomer in monomers)
    return InteractionResult(
        smiles_a=smiles_a,
        smiles_b=smiles_b,
        method=spec.method,
        solvent=spec.solvent,
        interaction_energy_kcal=round((bound.energy_hartree - separated) * _HARTREE_TO_KCAL, 2),
        complex_energy_hartree=bound.energy_hartree,
        monomer_energies_hartree=[monomer.energy_hartree for monomer in monomers],
        binding_modes=len(modes),
        structure=bound.structure,
    )


async def run_cached_interaction(
    store: ResultStore, smiles_a: str, smiles_b: str, spec: ComplexSpec | None = None
) -> tuple[InteractionResult, bool]:
    """Return the interaction of two molecules, reusing the store on a repeat.

    Keyed on the *combined* starting structure, so the pair is the calculation's subject
    exactly as a single molecule is elsewhere — and, because the pair is canonicalized
    first (`_ordered`), so A-with-B and B-with-A share one entry rather than running the
    same minutes-long search twice.
    """
    spec = spec or ComplexSpec()
    smiles_a, smiles_b = _ordered(smiles_a, smiles_b)
    monomers = [structure_from_smiles(smiles, optimize=True) for smiles in (smiles_a, smiles_b)]
    subject = _combine(monomers[0], monomers[1], spec.separation_angstrom)
    return await run_cached(
        store,
        spec.cache_key(subject),
        lambda: compute_interaction(spec, smiles_a, smiles_b),
        InteractionResult,
    )
