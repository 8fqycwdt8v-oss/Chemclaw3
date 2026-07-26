"""GFN2-xTB semiempirical calculator (plan step 1c.2).

The first real calculator: fast, local, deterministic single-point energies via
`tblite` (latest GFN semiempirical parametrization, GFN2-xTB) on an RDKit-embedded
3D geometry. No HPC. Fast enough (sub-second) that it needs no durable workflow —
the calculation store (Phase 1b) gives the "never compute twice" guarantee, so
`run_cached_xtb` is the entry point that plugs xTB into the store. Geometry/energy
plumbing is shared with the pKa predictor via `calc.xtb_engine`.
"""

from pydantic import BaseModel, Field
from rdkit import Chem

from calc.store import CalculationKey, ResultStore, run_cached
from calc.xtb_engine import (
    engine_version,
    geometry,
    gfn2_energy,
    parse_molecule,
    require_closed_shell,
)
from chemclaw.chem import require_canonical_smiles
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

    Including the tblite and RDKit versions means an upgrade of either (which
    can shift energies via the parametrization or the seeded embedding) is a
    cache miss, not a silent stale hit.
    """
    return f"{settings.xtb_method}+{engine_version()}"


def run_xtb(job: XtbInput) -> XtbResult:
    """Compute a GFN2-xTB single-point energy for one molecule.

    Raises `ValueError` on an unparseable SMILES, a declared charge that
    contradicts the SMILES formal charge, an open-shell electron count, or a
    geometry that fails to embed, rather than returning a meaningless energy
    (G4): tblite silently converges a wrong-charge or odd-electron system to
    an energy that can be hundreds of kcal/mol off.
    """
    mol = parse_molecule(job.smiles)
    formal_charge = Chem.GetFormalCharge(mol)
    if formal_charge != job.charge:
        raise ValueError(
            f"declared charge {job.charge} does not match the formal charge "
            f"{formal_charge} of {job.smiles!r}"
        )
    require_closed_shell(mol, job.charge)
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
    energy. The computation runs on the same canonical SMILES the key is built
    from — atom order steers the seeded embedding, so computing on the raw
    spelling would make the stored value depend on which spelling arrived first
    (D-011 determinism).
    """
    canonical = job.model_copy(update={"smiles": require_canonical_smiles(job.smiles)})
    key = CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=_calc_version(),
        inputs={"smiles": canonical.smiles, "charge": canonical.charge},
        params={"embed_seed": settings.xtb_embed_seed},
    )
    return await run_cached(store, key, lambda: run_xtb(canonical), XtbResult)
