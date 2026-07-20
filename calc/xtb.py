"""GFN2-xTB semiempirical calculator (plan step 1c.2).

The first real calculator: fast, local, deterministic single-point energies via
`tblite` (latest GFN semiempirical parametrization, GFN2-xTB) on an RDKit-embedded
3D geometry. No HPC. Fast enough (sub-second) that it needs no durable workflow —
the calculation store (Phase 1b) gives the "never compute twice" guarantee, so
`run_cached_xtb` is the entry point that plugs xTB into the store. Geometry/energy
plumbing is shared with the pKa predictor via `calc.xtb_engine`.
"""

import asyncio
from importlib.metadata import version

from pydantic import BaseModel, Field

from calc.store import CalculationKey, ResultStore, cached_compute
from calc.xtb_engine import geometry, gfn2_energy, parse_molecule
from chemclaw.config import settings

CALC_TYPE = "xtb"


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


def _calc_version() -> str:
    """Cache-key version tying results to method + engine build.

    Including the tblite version means an engine upgrade (which can shift
    energies) is a cache miss, not a silent stale hit.
    """
    return f"{settings.xtb_method}+tblite-{version('tblite')}"


def run_xtb(job: XtbInput) -> XtbResult:
    """Compute a GFN2-xTB single-point energy for one molecule.

    Raises `ValueError` on an unparseable SMILES or a geometry that fails to
    embed, rather than returning a meaningless energy (G4).
    """
    mol = parse_molecule(job.smiles)
    numbers, positions = geometry(mol, settings.xtb_embed_seed)
    energy = gfn2_energy(settings.xtb_method, numbers, positions, charge=job.charge)
    return XtbResult(
        smiles=job.smiles,
        method=settings.xtb_method,
        charge=job.charge,
        total_energy_hartree=energy,
    )


async def run_cached_xtb(store: ResultStore, job: XtbInput) -> tuple[XtbResult, bool]:
    """Return a GFN2-xTB result for `job`, reusing the store on a repeat (Phase 1b).

    Returns `(result, was_cached)`. The key is versioned by method + engine, so the
    same molecule under an upgraded engine recomputes rather than returning a stale
    energy.
    """
    key = CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=_calc_version(),
        inputs={"smiles": job.smiles, "charge": job.charge},
        params={"embed_seed": settings.xtb_embed_seed},
    )

    async def _compute() -> dict[str, object]:
        # Offload the blocking RDKit+tblite work so the event loop stays free.
        result = await asyncio.to_thread(run_xtb, job)
        return result.model_dump()

    payload, was_cached = await cached_compute(store, key, _compute)
    return XtbResult.model_validate(payload), was_cached
