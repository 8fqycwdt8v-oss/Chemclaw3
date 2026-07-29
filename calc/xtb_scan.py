"""Relaxed scans along one internal coordinate (xTB plan X3).

Answers the shape questions a single optimization cannot: how high is the barrier to
rotating this bond, is this atropisomer configurationally stable at process
temperature, which torsion angles are actually populated, what does the strain look
like as this ring closes.

**How the constraint works, and what it costs.** RDKit's `rdMolTransforms` sets a bond
length, angle or dihedral by moving the whole attached fragment, so each scan point
starts from a chemically sensible geometry rather than one atom dragged out of place.
Those defining atoms are then frozen and everything else relaxes — pinning coordinates
with equal optimizer bounds, which makes each point an exact constrained minimization
over the free subspace. The approximation is that the frozen atoms' own local geometry
(the bond lengths and angles *between* them) cannot relax with the coordinate. For a
torsion profile, the case this is mostly used for, that is the standard treatment; for
a bond-breaking scan it is a real limitation, and the profile maximum is a sketch of a
barrier rather than a transition state — there is no saddle-point search here.
"""

import asyncio
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field, model_validator
from rdkit import Chem
from rdkit.Chem import rdMolTransforms
from rdkit.Geometry import Point3D

from calc.progress import Progress, no_progress
from calc.store import ResultStore, run_cached
from calc.structure import Structure
from calc.xtb_engine import parse_molecule
from calc.xtb_opt import OptSpec, optimize_structure
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.5094740631

# How many atoms define each internal coordinate, and the unit its value is in.
_COORDINATES: dict[int, tuple[str, str]] = {
    2: ("bond", "angstrom"),
    3: ("angle", "degree"),
    4: ("dihedral", "degree"),
}


class ScanSpec(OptSpec):
    """One relaxed scan: an optimization plus the coordinate being driven.

    Deliberately an `OptSpec` subclass rather than a peer. A scan point *is* a
    constrained optimization, so it inherits the convergence criterion and step cap —
    and, because `frozen_atoms` is filled from `atoms`, the very same object drives
    each point's optimization. No second settings model, no chance of the two drifting.
    """

    task: Literal["scan"] = "scan"  # type: ignore[assignment]
    # The atoms defining the coordinate: two for a bond, three for an angle, four for
    # a dihedral. They must be bonded in sequence — RDKit rejects the rest.
    atoms: tuple[int, ...] = Field(min_length=2, max_length=4)
    # The coordinate values to visit, in Angstrom (bond) or degrees (angle/dihedral).
    values: tuple[float, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _freeze_the_scanned_atoms(self) -> "ScanSpec":
        """Hold the coordinate by freezing the atoms that define it."""
        self.frozen_atoms = self.atoms
        return self

    @model_validator(mode="after")
    def _bound_the_point_count(self) -> "ScanSpec":
        """Refuse a scan longer than `xtb_scan_max_points`.

        Every point is a full constrained geometry optimization, so the length of `values` *is*
        the cost of the call — and the values come from the model. `xtb_scan_max_points` has
        documented itself as bounding that "the way `xtb_hessian_max_atoms` bounds a Hessian"
        since it was added, but unlike that one it was never enforced anywhere: the field carried
        `min_length=1` and no maximum, so a scan was an unbounded compute request the agent could
        issue by naming more values. Checked here rather than at the tool boundary because the
        spec is what every caller (tool, durable job, cache key) is built from.
        """
        limit = settings.xtb_scan_max_points
        if len(self.values) > limit:
            raise ValueError(
                f"a relaxed scan is capped at {limit} points "
                f"(xtb_scan_max_points); {len(self.values)} were requested"
            )
        return self

    @property
    def coordinate(self) -> str:
        """`"bond"`, `"angle"` or `"dihedral"`, from how many atoms were given."""
        return _COORDINATES[len(self.atoms)][0]

    @property
    def unit(self) -> str:
        """The unit of `values` for this coordinate."""
        return _COORDINATES[len(self.atoms)][1]


class ScanPoint(BaseModel):
    """One relaxed point of the profile."""

    value: float
    energy_hartree: float
    # Energy relative to the lowest point of this scan, in kcal/mol — the only form in
    # which a scan energy means anything.
    relative_kcal: float


class ScanResult(BaseModel):
    """A relaxed energy profile along one internal coordinate.

    `maximum_relative_kcal` is the highest point of the *profile*, not an optimized
    transition state. For a torsion it is a sound barrier estimate; for a bond being
    broken it is an upper-bound sketch. `minimum_structure` is the lowest point's
    geometry, so a scan that finds a better conformer hands it back usable.
    """

    smiles: str | None
    input_structure_id: str
    method: str
    solvent: str | None
    coordinate: str
    atoms: list[int]
    unit: str
    points: list[ScanPoint]
    minimum_value: float
    maximum_relative_kcal: float
    minimum_structure: Structure


def _mol_with_conformer(structure: Structure) -> Chem.Mol:
    """Rebuild the RDKit molecule for `structure`, carrying its geometry.

    A `Structure` holds elements and coordinates but no bonds, and setting an internal
    coordinate needs connectivity. Re-parsing the canonical SMILES reproduces the atom
    order the geometry was built in (`calc.structure.structure_from_smiles` embeds the
    same parse), and the element check turns that reliance into an assertion rather
    than an assumption.
    """
    if not structure.smiles:
        raise ValueError("a scan needs the molecule's SMILES to know its connectivity")
    mol = parse_molecule(structure.smiles)
    elements = [atom.GetAtomicNum() for atom in mol.GetAtoms()]
    if elements != structure.elements:
        raise ValueError("structure does not match its SMILES: atom order or composition differs")
    conformer = Chem.Conformer(len(elements))
    for index, (x, y, z) in enumerate(structure.positions):
        conformer.SetAtomPosition(index, Point3D(x, y, z))
    mol.AddConformer(conformer, assignId=True)
    return mol


def _set_coordinate(conformer: Chem.Conformer, atoms: tuple[int, ...], value: float) -> None:
    """Drive one internal coordinate to `value`, moving the attached fragment with it."""
    if len(atoms) == 2:
        rdMolTransforms.SetBondLength(conformer, *atoms, value)
    elif len(atoms) == 3:
        rdMolTransforms.SetAngleDeg(conformer, *atoms, value)
    else:
        rdMolTransforms.SetDihedralDeg(conformer, *atoms, value)


def run_scan(spec: ScanSpec, structure: Structure, progress: Progress = no_progress) -> ScanResult:
    """Relax `structure` at every value of the scanned coordinate.

    Each point is driven from the *input* geometry rather than from the previous
    point. That costs a little convergence speed and buys determinism: a sequential
    scan's result depends on the direction it was walked, which is exactly the kind of
    hidden input a content-addressed cache must not have (D-011).
    """
    if max(spec.atoms) >= len(structure.elements) or min(spec.atoms) < 0:
        raise ValueError(f"scan atom index out of range for {len(structure.elements)} atoms")
    mol = _mol_with_conformer(structure)

    points: list[ScanPoint] = []
    geometries: list[Structure] = []
    for index, value in enumerate(spec.values, start=1):
        progress(f"point {index}/{len(spec.values)}: {spec.coordinate} = {value:g} {spec.unit}")
        conformer = Chem.Conformer(mol.GetConformer())
        _set_coordinate(conformer, spec.atoms, value)
        driven = Structure(
            elements=structure.elements,
            positions=[list(conformer.GetAtomPosition(i)) for i in range(len(structure.elements))],
            charge=structure.charge,
            multiplicity=structure.multiplicity,
            smiles=structure.smiles,
        )
        relaxed = optimize_structure(spec, driven)
        # `relative_kcal` needs the whole profile; filled in once the lowest is known.
        points.append(
            ScanPoint(value=value, energy_hartree=relaxed.energy_hartree, relative_kcal=0.0)
        )
        geometries.append(relaxed.structure)

    energies = np.array([point.energy_hartree for point in points])
    lowest = int(np.argmin(energies))
    relative = (energies - energies[lowest]) * _HARTREE_TO_KCAL
    for point, value in zip(points, relative, strict=True):
        point.relative_kcal = round(float(value), 3)
    return ScanResult(
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        method=spec.method,
        solvent=spec.solvent,
        coordinate=spec.coordinate,
        atoms=list(spec.atoms),
        unit=spec.unit,
        points=points,
        minimum_value=spec.values[lowest],
        maximum_relative_kcal=round(float(relative.max()), 3),
        minimum_structure=geometries[lowest],
    )


async def run_cached_scan(
    store: ResultStore, structure: Structure, spec: ScanSpec, progress: Progress = no_progress
) -> tuple[ScanResult, bool]:
    """Return the scan profile for `structure`, reusing the store on a repeat.

    The whole profile is one cache entry. Its points are constrained optimizations
    whose constraint set is specific to this scan, so they are of no use to anyone
    else — caching them individually would multiply store round-trips for a reuse that
    cannot happen.
    """
    # Off the event loop: deriving the key calls `calc_version()`, whose first call in a
    # process shells out to `xtb --version` / `crest --version` (`calc.xtb_cli`), and the
    # hash walks every atom. Both are synchronous, and this runs inside the connector's
    # one-loop MCP server and inside Temporal activities that are coroutines.
    key = await asyncio.to_thread(spec.cache_key, structure)
    return await run_cached(store, key, lambda: run_scan(spec, structure, progress), ScanResult)
