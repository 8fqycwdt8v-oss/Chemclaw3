"""GFN2-xTB semiempirical calculator (plan step 1c.2).

The first *real* calculator: fast, local, deterministic single-point energies via
`tblite` (latest GFN semiempirical parametrization, GFN2-xTB) on an RDKit-embedded
3D geometry. No HPC. Fast enough (sub-second) that it needs no durable workflow —
the calculation store (Phase 1b) provides the "never compute twice" guarantee, so
`run_cached_xtb` is the entry point that plugs xTB into the store.
"""

from importlib.metadata import version

import numpy as np
from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import AllChem
from tblite.interface import Calculator as TbliteCalculator

from calc.store import CalculationKey, ResultStore, cached_compute
from chemclaw.config import settings

CALC_TYPE = "xtb"
# Angstrom → Bohr; tblite works in atomic units, RDKit geometries are in Angstrom.
_ANGSTROM_TO_BOHR = 1.8897259886


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

    Embeds a 3D geometry (deterministic via the configured seed), then runs the
    configured GFN method. Raises `ValueError` on an unparseable SMILES or a
    geometry that fails to embed, rather than returning a meaningless energy (G4).
    """
    mol = Chem.MolFromSmiles(job.smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {job.smiles!r}")
    mol = Chem.AddHs(mol)
    if AllChem.EmbedMolecule(mol, randomSeed=settings.xtb_embed_seed) != 0:
        raise ValueError(f"could not embed a 3D geometry for {job.smiles!r}")

    conformer = mol.GetConformer()
    numbers = np.array([atom.GetAtomicNum() for atom in mol.GetAtoms()])
    positions = (
        np.array([list(conformer.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())])
        * _ANGSTROM_TO_BOHR
    )

    calculator = TbliteCalculator(settings.xtb_method, numbers, positions, charge=job.charge)
    result = calculator.singlepoint()
    return XtbResult(
        smiles=job.smiles,
        method=settings.xtb_method,
        charge=job.charge,
        total_energy_hartree=float(result.get("energy")),
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
        return run_xtb(job).model_dump()

    payload, was_cached = await cached_compute(store, key, _compute)
    return XtbResult.model_validate(payload), was_cached
