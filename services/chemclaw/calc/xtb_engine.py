"""Shared GFN2-xTB engine primitives: RDKit geometry + the tblite single point.

Used by every xTB-based calculator (`calc.xtb`, `calc.pka`, `calc.xtb_props`) so the
embed/SCF plumbing exists once (DRY). Geometry generation is deterministic via a
caller-supplied seed; single points optionally use ALPB implicit solvation.

This module is the **unit boundary**: everything above it works in Angstrom (the
interchange unit of RDKit, XYZ files, and `calc.structure.Structure`), and the
conversion to the atomic units tblite wants happens here and nowhere else.
"""

from importlib.metadata import version
from typing import Any

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from tblite.interface import Calculator

# Re-exported: `calc.xtb_opt` annotates the calculator it passes between its own helpers,
# and this module is the single place tblite is imported from (the unit boundary).
__all__ = [
    "COMMON_SOLVENTS",
    "Calculator",
    "engine_version",
    "evaluate_point",
    "geometry",
    "gfn2_energy",
    "make_calculator",
    "parse_molecule",
    "require_closed_shell",
    "run_singlepoint",
]

# tblite works in atomic units; everything above this module is in Angstrom.
_ANGSTROM_TO_BOHR = 1.8897259886

# ALPB solvents named in the error for an unrecognized one. Not the full parameter
# set — a curated list of the solvents process chemistry actually asks about, which is
# what a caller who mistyped one needs to see.
COMMON_SOLVENTS = (
    "water",
    "methanol",
    "ethanol",
    "acetonitrile",
    "acetone",
    "thf",
    "dmso",
    "toluene",
    "chcl3",
    "ch2cl2",
    "hexane",
    "ether",
    "ethylacetate",
)

# The tblite result properties any calculator here reads. Named explicitly rather than
# taking the whole result: it also carries the density matrix and orbital coefficients,
# which nothing consumes and which scale as the square of the basis size.
_CONSUMED_PROPERTIES = (
    "energy",
    "charges",
    "bond-orders",
    "dipole",
    "orbital-energies",
    "orbital-occupations",
)


# Revision of the *Hamiltonian settings* this engine applies, independent of the tblite
# build. Bumped when a change to how a calculation is set up moves numbers — the
# spin-polarization contribution added for open-shell systems is revision 2. Without a
# tag like this, such a change is invisible to the cache key and old entries would be
# served for a physics the current code would not reproduce (D-011).
_HAMILTONIAN_REVISION = "h2"


def engine_version() -> str:
    """The installed tblite and RDKit builds, for embedding in calculation cache keys.

    Every cache key of a calculator that runs this engine (xTB energy, pKa) must
    include both so an upgrade of either — tblite shifts energies, RDKit shifts
    the seeded ETKDG embedding and MMFF geometries — is a cache miss, not a
    silent stale hit (D-011). Widening the version string invalidates existing
    entries; that is correct, as those did not record the geometry stack that
    produced them.
    """
    return f"tblite-{version('tblite')}/rdkit-{version('rdkit')}/{_HAMILTONIAN_REVISION}"


def parse_molecule(smiles: str) -> Chem.Mol:
    """Parse a SMILES into a molecule with explicit hydrogens, or raise (G4)."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    return Chem.AddHs(mol)


def require_closed_shell(mol: Chem.Mol, charge: int) -> None:
    """Reject odd-electron (open-shell) species with a `ValueError` (G4).

    tblite converges odd-electron systems via fractional occupation without any
    error, returning an energy for an ill-defined electronic state, and a SMILES
    does not encode the true spin multiplicity — so a caller who has only a SMILES
    has nothing honest to pass as `uhf` and failing fast is the right contract.
    Expects explicit hydrogens (`parse_molecule` output) so the electron count is
    complete.

    Kept for `calc.pka`, whose v1 calibration is defined over neutral closed-shell
    acids. Callers that *can* state a multiplicity use `calc.structure.Structure`
    instead, which validates the electron count against it rather than refusing
    every open shell — that is what makes the Fukui ions computable.
    """
    electrons = sum(atom.GetAtomicNum() for atom in mol.GetAtoms()) - charge
    if electrons % 2:
        raise ValueError(
            f"open-shell species ({electrons} electrons at charge {charge}) is not "
            "supported: GFN2-xTB here is closed-shell only"
        )


def conformer_positions(mol: Chem.Mol, conf_id: int = -1) -> tuple[np.ndarray, np.ndarray]:
    """Extract (atomic numbers, positions in **Angstrom**) from an embedded conformer on `mol`.

    Shared by the single-conformer `geometry` (below) and the multi-conformer ensemble
    (`calc.conformer_ensemble`), which embeds many conformers up front via
    `AllChem.EmbedMultipleConfs` and then reads each one out by id rather than re-embedding.

    Arrived from `main` as `positions_bohr`, returning atomic units — correct there, where
    `geometry` also returned Bohr and `gfn2_energy` consumed it. It is renamed rather than
    adapted at the call site because this branch made `calc.xtb_engine` the **single unit
    boundary** (X1): everything above it is Angstrom and the conversion happens inside
    `make_calculator`/`evaluate_point`. Two functions here disagreeing about their unit is
    precisely the bug that boundary exists to prevent, and a name that states the unit is
    what stops the next person reintroducing it.
    """
    conformer = mol.GetConformer(conf_id)
    numbers = np.array([atom.GetAtomicNum() for atom in mol.GetAtoms()])
    positions = np.array([list(conformer.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())])
    return numbers, positions


def geometry(mol: Chem.Mol, seed: int, optimize: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Embed a deterministic 3D geometry and return (atomic numbers, positions in Angstrom).

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
    return conformer_positions(work)


def run_singlepoint(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
) -> dict[str, Any]:
    """Run one GFN single point and return every property the SCF produced.

    The same SCF that yields the total energy also yields Mulliken charges, Wiberg
    bond orders, the dipole, and the orbital energies — reading them out costs
    nothing, so this is the one entry point every xTB task uses and the energy-only
    `gfn2_energy` is a thin wrapper over it (DRY).

    `positions` is in **Angstrom** (see the module docstring); the conversion to
    atomic units happens here. `uhf` is the number of unpaired electrons, which the
    caller must state explicitly — tblite converges an odd-electron system silently
    at `uhf=0`, so an honest open-shell calculation depends on it being set.

    Args:
        method: GFN parametrization name, e.g. "GFN2-xTB".
        numbers: Atomic numbers, one per atom.
        positions: Cartesian coordinates in Angstrom, shape (natoms, 3).
        charge: Net molecular charge.
        uhf: Number of unpaired electrons (0 = closed shell).
        solvent: ALPB implicit solvent name, or None for gas phase.

    Returns:
        The consumed subset of the tblite result, as numpy arrays and scalars, in
        atomic units. Deliberately a subset: the full result also carries the density
        matrix and orbital coefficients, which nothing here reads and which are large.
    """
    calc = make_calculator(method, numbers, positions, charge=charge, uhf=uhf, solvent=solvent)
    result = calc.singlepoint()
    return {key: result.get(key) for key in _CONSUMED_PROPERTIES}


def make_calculator(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    uhf: int = 0,
    solvent: str | None = None,
) -> Calculator:
    """Build a configured tblite calculator for one system; `positions` in Angstrom.

    Exists so the tasks that evaluate the *same* system at many geometries — geometry
    optimization, the finite-difference Hessian, a relaxed scan — set the Hamiltonian
    up once and then call `energy_and_gradient` per step, instead of reconstructing a
    calculator per single point. `run_singlepoint` goes through it too, so the
    verbosity and solvation setup exist once (DRY).
    """
    calc = Calculator(method, numbers, positions * _ANGSTROM_TO_BOHR, charge=charge, uhf=uhf)
    # tblite prints an SCF iteration table to stdout at its default verbosity, which
    # would pollute every worker log and test run. It affects no numbers.
    calc.set("verbosity", 0)
    if uhf:
        # Without this, `uhf` only changes the *occupation*: the energy expression has
        # no spin-dependent term, so an open-shell state is not stabilized at all.
        # Measured, and the measurement is decisive — triplet O2 comes out 1.7 kcal/mol
        # *above* singlet O2 without it (qualitatively wrong; the triplet is the ground
        # state) and 15.8 kcal/mol below it with (experimental gap ~22). Enabled
        # wherever there are unpaired electrons, with no scaling, which is what
        # `xtb --spinpol` does.
        calc.add("spin-polarization", 1.0)
    if solvent is not None:
        try:
            calc.add("alpb-solvation", solvent)
        except RuntimeError as error:
            # tblite's own message ("String value for epsilon was not found among
            # database of solvents") names an implementation detail rather than the
            # mistake, and an unrecognized solvent is the easy typo on every solvated
            # call. Name the argument and give the common process solvents (G4).
            raise ValueError(
                f"unknown ALPB solvent {solvent!r}; common valid names are "
                f"{', '.join(COMMON_SOLVENTS)}"
            ) from error
    return calc


def evaluate_point(calc: Calculator, positions: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """Move `calc`'s system to `positions` (Angstrom) and evaluate it there.

    Returns `(energy, gradient, dipole)`: the energy in Hartree, the **analytic**
    gradient in Hartree/Angstrom, and the dipole in atomic units. tblite returns the
    gradient in Hartree/Bohr; the chain-rule factor for the conversion is the same
    constant that converts the coordinates, applied in the opposite direction.

    The dipole rides along because the SCF produced it anyway. Optimization discards
    it; the Hessian loop, which displaces every Cartesian and would otherwise need a
    second pass for dipole derivatives, gets IR intensities for free (the same move
    `calc.xtb_props` makes for charges and bond orders).

    An analytic gradient is what makes optimization cheap and puts the
    finite-difference Hessian at 6N single points rather than 6N^2.
    """
    calc.update(positions=positions * _ANGSTROM_TO_BOHR)
    result = calc.singlepoint()
    energy = float(result.get("energy"))
    gradient = np.asarray(result.get("gradient"), dtype=float) * _ANGSTROM_TO_BOHR
    dipole = np.asarray(result.get("dipole"), dtype=float)
    return energy, gradient, dipole


def gfn2_energy(
    method: str,
    numbers: np.ndarray,
    positions: np.ndarray,
    charge: int = 0,
    solvent: str | None = None,
) -> float:
    """Return the GFN2-xTB total energy (Hartree) for a closed-shell system.

    Positions are in Angstrom. Closed-shell only by signature: callers needing an
    open-shell energy go through `run_singlepoint` and state `uhf` themselves.
    """
    result = run_singlepoint(method, numbers, positions, charge=charge, solvent=solvent)
    return float(result["energy"])
