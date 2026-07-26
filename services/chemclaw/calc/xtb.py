"""GFN2-xTB semiempirical single-point energies (plan step 1c.2).

The first real calculator: fast, local, deterministic single-point energies via
`tblite` (latest GFN semiempirical parametrization, GFN2-xTB) on an RDKit-embedded
3D geometry. No HPC. Fast enough (sub-second) that it needs no durable workflow —
the calculation store (Phase 1b) gives the "never compute twice" guarantee, so
`run_cached_xtb` is the entry point that plugs xTB into the store.

Since the xTB capability layer (plan X1) this module is thin: `calc.structure` owns
the geometry and its validation, `calc.xtb_spec` owns the cache key, and
`calc.xtb_engine` owns the SCF. What is left here is the one thing specific to a
single point — its input and result shape — which is exactly how much code a task
should be.
"""

from pydantic import BaseModel, Field

from calc.store import ResultStore, run_cached
from calc.structure import Structure, structure_from_smiles
from calc.xtb_engine import gfn2_energy
from calc.xtb_spec import XtbSpec


class XtbInput(BaseModel):
    """A single-point xTB request: a molecule and its charge."""

    smiles: str = Field(min_length=1)
    charge: int = 0


class XtbResult(BaseModel):
    """The parsed result of a GFN2-xTB single point."""

    smiles: str
    method: str
    charge: int
    total_energy_hartree: float


def _energy(spec: XtbSpec, structure: Structure) -> XtbResult:
    """Compute one single-point energy for an already-validated structure."""
    numbers, positions = structure.arrays()
    return XtbResult(
        smiles=structure.smiles or "",
        method=spec.method,
        charge=structure.charge,
        total_energy_hartree=gfn2_energy(
            spec.method, numbers, positions, charge=structure.charge, solvent=spec.solvent
        ),
    )


def run_xtb(job: XtbInput) -> XtbResult:
    """Compute a GFN2-xTB single-point energy for one molecule.

    Raises `ValueError` on an unparseable SMILES, a declared charge that
    contradicts the SMILES formal charge, an open-shell electron count, or a
    geometry that fails to embed, rather than returning a meaningless energy
    (G4): tblite silently converges a wrong-charge or odd-electron system to
    an energy that can be hundreds of kcal/mol off. Those checks live in
    `calc.structure.Structure`, so every xTB task inherits them identically.
    """
    return _energy(XtbSpec(task="sp"), structure_from_smiles(job.smiles, charge=job.charge))


async def run_cached_xtb(store: ResultStore, job: XtbInput) -> tuple[XtbResult, bool]:
    """Return a GFN2-xTB result for `job`, reusing the store on a repeat (Phase 1b).

    Returns `(result, was_cached)`. The key names the *geometry* rather than the
    recipe that produced it (`XtbSpec.cache_key`), so an identical structure reaching
    this calculator by any route hits the same entry, and an engine upgrade — which
    can shift both the embedding and the energy — recomputes rather than serving a
    stale value. `structure_from_smiles` canonicalizes before embedding, so two
    spellings of one molecule cannot produce two different energies (D-011).
    """
    structure = structure_from_smiles(job.smiles, charge=job.charge)
    spec = XtbSpec(task="sp")
    return await run_cached(
        store, spec.cache_key(structure), lambda: _energy(spec, structure), XtbResult
    )
