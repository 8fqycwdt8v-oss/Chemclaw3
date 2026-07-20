"""Shared GFN2-xTB engine primitives: RDKit geometry + tblite energy.

Used by both the xTB energy calculator (`calc.xtb`) and the xTB-based pKa
predictor (`calc.pka`) so the embed/energy plumbing exists once (DRY). Geometry
generation is deterministic via a caller-supplied seed; energies optionally use
ALPB implicit solvation.
"""

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from tblite.interface import Calculator

# tblite works in atomic units; RDKit geometries are in Angstrom.
_ANGSTROM_TO_BOHR = 1.8897259886


def parse_molecule(smiles: str) -> Chem.Mol:
    """Parse a SMILES into a molecule with explicit hydrogens, or raise (G4)."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    return Chem.AddHs(mol)


def geometry(mol: Chem.Mol, seed: int, optimize: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Embed a deterministic 3D geometry and return (atomic numbers, positions in Bohr).

    Falls back to random-coordinate embedding if the default fails, then raises if
    that also fails. Optional MMFF pre-optimization is skipped when the force field
    lacks parameters for the molecule (a valid, common case) rather than erroring.
    """
    work = Chem.Mol(mol)  # copy so the caller's molecule gets no conformer
    if AllChem.EmbedMolecule(work, randomSeed=seed) != 0:
        if AllChem.EmbedMolecule(work, randomSeed=seed, useRandomCoords=True) != 0:
            raise ValueError("could not embed a 3D geometry")
    if optimize and AllChem.MMFFHasAllMoleculeParams(work):
        AllChem.MMFFOptimizeMolecule(work)
    conformer = work.GetConformer()
    numbers = np.array([atom.GetAtomicNum() for atom in work.GetAtoms()])
    positions = (
        np.array([list(conformer.GetAtomPosition(i)) for i in range(work.GetNumAtoms())])
        * _ANGSTROM_TO_BOHR
    )
    return numbers, positions


def gfn2_energy(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    solvent: str | None = None,
) -> float:
    """Return the GFN2-xTB total energy (Hartree), optionally with ALPB solvation."""
    calc = Calculator(method, numbers, positions, charge=charge)
    if solvent is not None:
        calc.add("alpb-solvation", solvent)
    return float(calc.singlepoint().get("energy"))
