"""A stand-in for `Chemclaw3-mcp`'s `calc` server, so this suite proves wiring without physics.

It answers the same shapes and counts every call: "was this recomputed?" is a question about call
counts (D-011). Returned numbers are placeholders, never asserted as chemistry.

Keys are derived as the server derives them, including two properties a composite must handle: a
Fukui key does not name the mode (callers re-rank via `SiteReactivityResult.ranked_for`), and
`optimize_geometry` and `relax_structure` share one `xtb.opt` key while returning different
payloads (so only the full result is cached).
"""

import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolTransforms

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.structures import InMemoryStructureStore

# The version carries both key delimiters (`@` and `:`), as real ones do, so a client that splits
# the flat key form is caught. The conceptual-DFT panel is internally consistent (eta = IP - EA,
# S = 1/eta, omega = mu^2/2eta).
_FAKE_PANEL: dict[str, float] = {
    "ionization_potential_ev": 13.5,
    "electron_affinity_ev": 3.0,
    "chemical_potential_ev": -8.25,
    "hardness_ev": 10.5,
    "softness_per_ev": round(1 / 10.5, 6),
    "electrophilicity_ev": round(8.25**2 / (2 * 10.5), 4),
}

FAKE_VERSION = "GFN2-xTB+fake@1/cal-0.28733:-29.3116"

# Which cache type each compute tool answers under, and which arguments enter `params`. The subject
# (`structure` or `smiles`) is hashed into `input_hash`, never listed here; an argument absent from
# a tuple is one the answer does not depend on.
_KEYED: dict[str, tuple[str, tuple[str, ...]]] = {
    "compute_xtb_energy": ("xtb.sp", ("charge",)),
    "compute_electronic_properties": ("xtb.properties", ("solvent",)),
    # The same calculation at a named geometry: same `calc_type` and params, different subject, so a
    # relaxed conformer's properties land on the entry its own address names.
    "compute_properties_at": ("xtb.properties", ("solvent",)),
    "predict_site_reactivity": ("xtb.fukui", ()),
    # Same `calc_type` as the SMILES twin; `mode` and `top_n` stay out of the key because the server
    # computes all indices and sorts on the way out. `solvent` is keyed here (the server keys it)
    # but the SMILES twin takes no solvent.
    "compute_fukui_at": ("xtb.fukui", ("solvent",)),
    # SMILES-in tools proxied but not composed from; `tests/test_calc_fake_identity.py` checks these
    # rows against the real server's identity module.
    "compute_atomic_descriptors": ("xtb.atomic", ("solvent",)),
    "compute_surface_potential": ("xtb.surface", ("solvent",)),
    "optimize_geometry": ("xtb.opt", ("solvent",)),
    "predict_pka": ("pka", ()),
    "predict_solubility": ("solubility", ()),
    "predict_developability_profile": ("developability", ()),
    "relax_structure": ("xtb.opt", ("solvent", "frozen_atoms")),
    "compute_hessian": ("xtb.hess", ("solvent",)),
    "scan_point": ("xtb.opt", ("atoms", "value", "solvent")),
    "search_conformer_ensemble": ("xtb.conformers", ("search", "effort", "solvent")),
    "search_binding_modes": ("xtb.complex", ("effort", "solvent")),
}

# Tools with no cache row at all. `predict_logd` never had one — its expensive half is a cached pKa
# — and the two geometry builders are not compute tools, so the real server refuses them by name.
_UNKEYED = frozenset({"predict_logd", "embed_structure", "combine_structures"})


def embed(smiles: str, multiplicity: int = 1) -> dict[str, Any]:
    """A real ETKDG geometry for `smiles`, in the `Structure` shape the server returns.

    Real because a `structure_id` hashes coordinates: identical fake geometries would make two
    species share a relaxation entry.
    """
    canonical = require_canonical_smiles(smiles)
    mol = Chem.AddHs(Chem.MolFromSmiles(canonical))
    AllChem.EmbedMolecule(mol, randomSeed=7)
    conformer = mol.GetConformer()
    return {
        "elements": [atom.GetAtomicNum() for atom in mol.GetAtoms()],
        "positions": [list(conformer.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())],
        "charge": Chem.GetFormalCharge(mol),
        "multiplicity": multiplicity,
        "smiles": canonical,
    }


def ionised(structure: dict[str, Any], search: str) -> dict[str, Any]:
    """What a protonation search returns: a *different species* from the one it was given.

    `--deprotonate` returns one atom fewer at charge -1 and `--protonate` one more at charge +1; the
    electron count is unchanged, so the multiplicity carries over. The label is derived from the
    input SMILES here, where the real server perceives it from the geometry.
    """
    if search not in ("protomers", "deprotomers"):
        return structure
    elements = list(structure["elements"])
    positions = [list(row) for row in structure["positions"]]
    if search == "deprotomers":
        index = len(elements) - 1 - elements[::-1].index(1)
        elements.pop(index)
        positions.pop(index)
        shift = -1
    else:
        elements.append(1)
        positions.append([value + 1.0 for value in positions[0]])
        shift = 1
    return {
        **structure,
        "elements": elements,
        "positions": positions,
        "charge": structure["charge"] + shift,
        "smiles": _ionised_smiles(structure.get("smiles"), shift),
    }


def _ionised_smiles(smiles: str | None, shift: int) -> str | None:
    """The SMILES of the ionised form, or None when this molecule has no obvious site."""
    if smiles is None:
        return None
    mol = Chem.RWMol(Chem.MolFromSmiles(smiles))
    for atom in mol.GetAtoms():
        if shift < 0 and atom.GetAtomicNum() in (7, 8, 16) and atom.GetTotalNumHs() > 0:
            atom.SetNumExplicitHs(atom.GetTotalNumHs() - 1)
            atom.SetNoImplicit(True)
            atom.SetFormalCharge(-1)
            return str(Chem.MolToSmiles(mol))
        if shift > 0 and atom.GetAtomicNum() == 7 and atom.GetTotalNumHs() + atom.GetDegree() < 4:
            atom.SetNumExplicitHs(atom.GetTotalNumHs() + 1)
            atom.SetNoImplicit(True)
            atom.SetFormalCharge(1)
            return str(Chem.MolToSmiles(mol))
    return None


def harmonic_hessian(
    structure: dict[str, Any], *, imaginary: bool = False, max_gradient: float | None = 1e-4
) -> dict[str, Any]:
    """A well-formed Hessian payload for `structure`, optionally carrying one negative eigenvalue.

    A diagonal matrix with a chosen spectrum, base64-encoded as the server encodes one.
    `imaginary=True` drives the saddle-point escape loop. `max_gradient` defaults to a converged
    value; a large one drives the non-stationary-geometry check, and `None` reproduces the `xtb`
    binary backend, which reports no gradient.
    """
    import base64
    import io

    size = 3 * len(structure["elements"])
    diagonal = np.full(size, 0.5)
    if imaginary:
        # About -64 cm^-1: above `xtb_imaginary_threshold_cm`, so a real imaginary mode, with a
        # zero-point term small enough not to invert a barrier by itself.
        diagonal[0] = -0.5
    matrix = np.diag(diagonal)

    def pack(array: np.ndarray) -> str:
        buffer = io.BytesIO()
        np.save(buffer, array, allow_pickle=False)
        return base64.b64encode(buffer.getvalue()).decode()

    return {
        "calc_version": FAKE_VERSION,
        "calc_key": None,
        "structure_id": _structure_id(structure),
        "method": "GFN2-xTB",
        "solvent": structure.get("solvent"),
        "atom_count": len(structure["elements"]),
        "electronic_energy_hartree": -1.0 * len(structure["elements"]),
        "max_gradient_hartree_per_angstrom": max_gradient,
        "hessian_npy": pack(matrix),
        "dipole_derivatives_npy": pack(np.zeros((size, 3))),
        "ir_intensities": None,
    }


def _nudged(structure: dict[str, Any], index: int) -> dict[str, Any]:
    """The same molecule at a slightly different geometry — one ensemble member.

    Displaces every atom along x by `index/100` Angstrom, above `Structure`'s rounding, so each
    member has its own `structure_id`. Index 0 is the input unchanged.
    """
    if index == 0:
        return structure
    offset = index / 100.0
    return {
        **structure,
        "positions": [[x + offset, y, z] for x, y, z in structure["positions"]],
    }


# A three-well torsional potential in Hartree, shaped like n-butane's: minima at 60, 180 and 300
# degrees, anti about 1 kcal/mol below gauche, barriers about 2.5 kcal/mol.
_BARRIER_HARTREE = 0.004
_GAUCHE_HARTREE = 0.0016
_WELLS = (60.0, 180.0, 300.0)

# What releasing a constrained geometry buys, in Hartree — about 0.13 kcal/mol, the order the live
# server gives on n-butane. Small on purpose: a barrier measured from the wrong zero is wrong by
# exactly this, and a test that needed a large number to see it would not be testing the real case.
_RELAXATION_HARTREE = 2.0e-4


def torsional_energy(degrees: float) -> float:
    """The synthetic torsional potential at one dihedral, in Hartree above its own minimum."""
    radians = math.radians(degrees)
    threefold = _BARRIER_HARTREE * (1.0 + math.cos(3.0 * radians)) / 2.0
    onefold = _GAUCHE_HARTREE * (1.0 + math.cos(radians)) / 2.0
    return threefold + onefold


def torsional_surface_energy(
    structure: dict[str, Any], atoms: tuple[int, int, int, int], shift: float = 0.0
) -> float:
    """This surface's energy for a geometry, in Hartree — the one definition all three tools use.

    `scan_point`, `relax_structure` and `compute_hessian` must agree, or a composite comparing two
    of them compares different surfaces. `shift` is the caller's `solvent_shifts` entry, so the
    agreement holds in every medium.
    """
    angle = dihedral_of(structure, atoms)
    relaxed = _RELAXATION_HARTREE if _near_a_well(angle) else 0.0
    return -1.0 * len(structure["elements"]) + shift + torsional_energy(angle) - relaxed


def _near_a_well(degrees: float, tolerance: float = 20.0) -> bool:
    """Is this geometry close enough to a minimum of the synthetic potential to be one?"""
    return any(
        min(abs(degrees - well), 360.0 - abs(degrees - well)) <= tolerance for well in _WELLS
    )


def _nearest_well(degrees: float) -> float:
    """Which of the three minima an unconstrained relaxation from `degrees` would settle into."""
    return min(_WELLS, key=lambda well: min(abs(degrees - well), 360.0 - abs(degrees - well)))


def dihedral_of(structure: dict[str, Any], atoms: tuple[int, int, int, int]) -> float:
    """The dihedral these four atoms span, in degrees on [0, 360) — RDKit's own measurement."""
    return float(rdMolTransforms.GetDihedralDeg(_conformer(structure), *atoms)) % 360.0


def with_dihedral(
    structure: dict[str, Any], atoms: tuple[int, int, int, int], degrees: float
) -> dict[str, Any]:
    """`structure` with that dihedral driven to `degrees`, moving the attached fragment with it.

    Real geometry manipulation, because the composite reads the angle back off the coordinates.
    """
    conformer = _conformer(structure)
    rdMolTransforms.SetDihedralDeg(conformer, *atoms, degrees)
    return {
        **structure,
        "positions": [
            list(conformer.GetAtomPosition(index)) for index in range(conformer.GetNumAtoms())
        ],
    }


def _conformer(structure: dict[str, Any]) -> Chem.Conformer:
    """An RDKit conformer over a structure payload, with the bonds needed to drive a dihedral.

    Built from the SMILES rather than from the elements alone: `SetDihedralDeg` moves everything
    bonded beyond the axis, so it needs the connectivity, and a bare point cloud has none.
    """
    mol = Chem.AddHs(Chem.MolFromSmiles(require_canonical_smiles(structure["smiles"])))
    conformer = Chem.Conformer(len(structure["positions"]))
    for index, position in enumerate(structure["positions"]):
        conformer.SetAtomPosition(index, [float(value) for value in position])
    mol.AddConformer(conformer, assignId=True)
    return mol.GetConformer()


def _structure_id(structure: dict[str, Any]) -> str:
    """The content address of a structure dict, by the same rule `Structure.structure_id` uses."""
    return "st_" + stable_hash(
        {
            "elements": structure["elements"],
            "positions": structure["positions"],
            "charge": structure.get("charge", 0),
            "multiplicity": structure.get("multiplicity", 1),
        }
    )


class FakeCalcServer:
    """One MCP session's worth of calculation server, counting every tool call it answers."""

    def __init__(
        self,
        *,
        saddle_first: bool = False,
        torsion: tuple[int, int, int, int] | None = None,
        solvent_shifts: dict[tuple[str, str], float] | None = None,
    ) -> None:
        """Start with no calls recorded.

        `saddle_first` makes the first Hessian carry an imaginary frequency and later ones minima,
        which `relax_to_minimum`'s escape needs.

        `torsion` enables a one-dimensional torsional potential over four atoms: a constrained point
        sets the dihedral, and an unconstrained relaxation settles into the nearest well, so a
        rotational profile's release step is observable.

        `solvent_shifts` maps `(smiles, solvent)` to a Hartree shift on that species' relaxed
        energy, so a solvent fan-out can reorder species and `dominance_changes` can be observed.
        """
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # The read bound each session was opened with, in order — `None` where the caller took the
        # default. See `install`.
        self.timeouts: list[float | None] = []
        self._saddle_first = saddle_first
        self._torsion = torsion
        self._solvent_shifts = dict(solvent_shifts or {})
        self.overrides: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        # Where composites' geometries land once `install` wires it in; a test resolves reported ids
        # here through the real code path.
        self.structures = InMemoryStructureStore()

    def count(self, tool: str) -> int:
        """How many times `tool` was called."""
        return sum(1 for name, _ in self.calls if name == tool)

    def arguments(self, tool: str) -> list[dict[str, Any]]:
        """The arguments every call to `tool` was made with, in order."""
        return [args for name, args in self.calls if name == tool]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Answer one tool call in the `CallToolResult` shape the client reads."""
        self.calls.append((name, arguments))
        try:
            return _Result(self._answer(name, arguments))
        except ValueError as error:
            # The wire's shape for a refused call: one plain text block with FastMCP's prefix, which
            # the client reads to tell a domain refusal from a full pod or broken server
            # (`core/mcp_session.server_marked`).
            return _Result(f"Error executing tool {name}: {error}", is_error=True)

    def _answer(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one call, honouring an override the test installed."""
        if name == "calculation_key":
            return self._identity(arguments["tool"], arguments["arguments"])
        override = self.overrides.get(name)
        if override is not None:
            return override(arguments)
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            raise ValueError(f"{name!r} is not a tool on this server")
        result: dict[str, Any] = handler(arguments)
        return result

    def _identity(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """What `calculation_key` answers: the version always, the key only when there is one."""
        if tool in _UNKEYED:
            return {"tool": tool, "calc_version": FAKE_VERSION, "key": None, "calc_key": None}
        if tool not in _KEYED:
            raise ValueError(f"{tool!r} is not a compute tool on this server")
        calc_type, keyed = _KEYED[tool]
        subject = arguments.get("structure")
        inputs: Any = (
            _structure_id(subject)
            if subject is not None
            else require_canonical_smiles(arguments["smiles"])
        )
        params = {field: arguments.get(field) for field in keyed}
        key = {
            "calc_type": calc_type,
            "calc_version": FAKE_VERSION,
            "input_hash": stable_hash(inputs),
            "params_hash": stable_hash(params),
        }
        # `structure_id` for geometry-based calculations, as the real server reports it; absent for
        # a molecule-keyed calculator.
        return {
            "tool": tool,
            "calc_version": FAKE_VERSION,
            "key": key,
            "calc_key": None,
            "structure_id": _structure_id(subject) if subject is not None else None,
        }

    # --- the tools themselves ---------------------------------------------------------------

    def _embed_structure(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return embed(arguments["smiles"], arguments.get("multiplicity") or 1)

    def _combine_structures(self, arguments: dict[str, Any]) -> dict[str, Any]:
        first, second = arguments["first"], arguments["second"]
        offset = [10.0, 0.0, 0.0]
        return {
            "elements": [*first["elements"], *second["elements"]],
            "positions": [
                *first["positions"],
                *[[x + offset[0], y, z] for x, y, z in second["positions"]],
            ],
            "charge": first["charge"] + second["charge"],
            "multiplicity": first["multiplicity"] + second["multiplicity"] - 1,
            "smiles": f"{first['smiles']}.{second['smiles']}",
        }

    def _relax_structure(self, arguments: dict[str, Any]) -> dict[str, Any]:
        structure = arguments["structure"]
        if self._torsion is not None:
            # Settle into the nearest well, geometry and energy together — which is what makes a
            # released rotamer a different thing from the constrained point it came from.
            settled = _nearest_well(dihedral_of(structure, self._torsion))
            structure = with_dihedral(structure, self._torsion, settled)
            result = self._optimization(structure, arguments.get("solvent"))
            # A released well sits below its constrained point by more than the torsional term,
            # because the other coordinates relax too; this keeps constrained and released energy
            # zeros distinguishable.
            result["energy_hartree"] += torsional_energy(settled) - _RELAXATION_HARTREE
            return result
        return self._optimization(structure, arguments.get("solvent"))

    def _scan_point(self, arguments: dict[str, Any]) -> dict[str, Any]:
        structure = arguments["structure"]
        value = float(arguments["value"])
        if self._torsion is not None and len(arguments["atoms"]) == 4:
            structure = with_dihedral(structure, self._torsion, value)
            result = self._optimization(structure, arguments.get("solvent"))
            result["energy_hartree"] += torsional_energy(value)
        else:
            # The driven coordinate shifts the energy, so a profile has a minimum somewhere to find.
            result = self._optimization(structure, arguments.get("solvent"))
            result["energy_hartree"] += (value - 1.5) ** 2
        result["frozen_atoms"] = list(arguments["atoms"])
        return result

    def _optimization(self, structure: dict[str, Any], solvent: str | None) -> dict[str, Any]:
        shift = self._solvent_shifts.get((structure.get("smiles") or "", solvent or ""), 0.0)
        return {
            "calc_version": FAKE_VERSION,
            "calc_key": f"xtb.opt@{FAKE_VERSION}:{_structure_id(structure)}:0",
            "smiles": structure.get("smiles"),
            "input_structure_id": _structure_id(structure),
            "structure": structure,
            "method": "GFN2-xTB",
            "engine": "tblite",
            "solvent": solvent,
            "initial_energy_hartree": -1.0 * len(structure["elements"]) + shift + 0.01,
            "energy_hartree": -1.0 * len(structure["elements"]) + shift,
            "relaxation_kcal": 6.3,
            "steps": 4,
            "max_gradient": 1e-5,
            "displacement_rms_angstrom": 0.02,
            "frozen_atoms": [],
        }

    def _compute_hessian(self, arguments: dict[str, Any]) -> dict[str, Any]:
        saddle = self._saddle_first and self.count("compute_hessian") == 1
        if self._torsion is not None and not saddle:
            # A torsional maximum is a first-order saddle, so the pass Hessian reports one imaginary
            # mode and the free-energy barrier path is reachable.
            saddle = not _near_a_well(dihedral_of(arguments["structure"], self._torsion))
            payload = harmonic_hessian(arguments["structure"], imaginary=saddle)
            # The energy *this surface* gives that geometry, so a free energy computed from this
            # Hessian is comparable with the scan point and the relaxation beside it.
            payload["electronic_energy_hartree"] = torsional_surface_energy(
                arguments["structure"],
                self._torsion,
                self._solvent_shifts.get(
                    (arguments["structure"].get("smiles") or "", arguments.get("solvent") or ""),
                    0.0,
                ),
            )
            payload["solvent"] = arguments.get("solvent")
            return payload
        payload = harmonic_hessian(arguments["structure"], imaginary=saddle)
        payload["solvent"] = arguments.get("solvent")
        return payload

    def _search_conformer_ensemble(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._ensemble(arguments, search=arguments.get("search", "conformers"))

    def _search_binding_modes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._ensemble(arguments, search="complex")

    def _ensemble(self, arguments: dict[str, Any], *, search: str) -> dict[str, Any]:
        structure = ionised(arguments["structure"], search)
        return {
            "calc_version": FAKE_VERSION,
            "calc_key": None,
            "structure_id": _structure_id(structure),
            "method": "GFN2-xTB",
            "solvent": arguments.get("solvent"),
            "search": search,
            "effort": arguments.get("effort", "quick"),
            # Three members, degeneracies 1/2/1, so weighted and unweighted populations differ. Each
            # member is a distinct geometry (above `_GEOMETRY_DECIMALS`), so a per-member refinement
            # is three cache entries, not one.
            "members": [
                {
                    "energy_hartree": -1.0 * len(structure["elements"]) - shift,
                    "degeneracy": degeneracy,
                    "structure": _nudged(structure, index),
                }
                for index, (shift, degeneracy) in enumerate(((0.0, 1), (-0.001, 2), (-0.002, 1)))
            ],
            "total_found": 3,
        }

    def _compute_xtb_energy(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "calc_version": FAKE_VERSION,
            "smiles": require_canonical_smiles(arguments["smiles"]),
            "method": "GFN2-xTB",
            "charge": arguments.get("charge", 0),
            "total_energy_hartree": -5.07,
        }

    def _predict_solubility(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "calc_version": FAKE_VERSION,
            "smiles": require_canonical_smiles(arguments["smiles"]),
            "model": "esol-delaney@2004",
            "log_s_mol_per_l": -2.13,
            "uncertainty_log": 0.75,
            "estimate": {"value": -2.13, "unit": "log S", "uncertainty": 0.75},
        }

    def _predict_pka(self, arguments: dict[str, Any]) -> dict[str, Any]:
        canonical = require_canonical_smiles(arguments["smiles"])
        # An aromatic nitrogen with no acidic proton is a base; everything else here is an acid.
        molecule = Chem.MolFromSmiles(canonical)
        acidic = any(
            atom.GetAtomicNum() in (8, 16) and atom.GetTotalNumHs() for atom in molecule.GetAtoms()
        )
        return {
            "calc_version": FAKE_VERSION,
            "smiles": canonical,
            "method": "GFN2-xTB/alpb-water",
            "pka": 4.2 if acidic else 5.2,
            "deprotonation_energy_kcal": 320.0,
            "uncertainty": 1.6 if acidic else 1.0,
            "site": "acid" if acidic else "base",
        }

    def _predict_developability_profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "calc_version": FAKE_VERSION,
            "smiles": require_canonical_smiles(arguments["smiles"]),
            "molecular_weight": 180.16,
            "clogp": 1.31,
            "tpsa": 63.6,
            "h_bond_donors": 1,
            "h_bond_acceptors": 3,
            "rotatable_bonds": 2,
            "aromatic_rings": 1,
            "fraction_csp3": 0.11,
            "qed": 0.55,
            "lipinski_violations": 0,
            "veber_pass": True,
        }

    def _predict_site_reactivity(
        self, arguments: dict[str, Any], nudge: float = 0.0
    ) -> dict[str, Any]:
        canonical = require_canonical_smiles(arguments["smiles"])
        molecule = Chem.AddHs(Chem.MolFromSmiles(canonical))
        atoms = list(molecule.GetAtoms())
        # f_minus descends and f_plus ascends with the index, so the two modes rank atoms in
        # opposite orders. `f_zero` varies per atom and `nudge` per geometry, so conformers rank
        # differently and an ensemble average is meaningful.
        sites = [
            {
                "index": atom.GetIdx(),
                "element": atom.GetSymbol(),
                "f_minus": round(1.0 - atom.GetIdx() / len(atoms), 4),
                "f_plus": round(atom.GetIdx() / len(atoms), 4),
                "f_zero": round(0.5 + nudge * (1 if atom.GetIdx() % 2 else -1), 4),
                # Derived exactly as the real server derives them, from the rounded indices and the
                # panel below — so a reader that recomputes one of these from the two it is given
                # gets the number the payload also carries.
                "dual": round(
                    round(atom.GetIdx() / len(atoms), 4)
                    - round(1.0 - atom.GetIdx() / len(atoms), 4),
                    4,
                ),
                "local_softness_minus": round(
                    _FAKE_PANEL["softness_per_ev"] * round(1.0 - atom.GetIdx() / len(atoms), 4), 6
                ),
                "local_softness_plus": round(
                    _FAKE_PANEL["softness_per_ev"] * round(atom.GetIdx() / len(atoms), 4), 6
                ),
                "local_electrophilicity_ev": round(
                    _FAKE_PANEL["electrophilicity_ev"] * round(atom.GetIdx() / len(atoms), 4), 4
                ),
            }
            for atom in atoms
        ]
        # Ranked most-susceptible first and truncated, the real server's `SiteReactivityResult`
        # contract, so pairing conformers by list position is wrong here as it is in production.
        sites.sort(key=lambda site: -float(site["f_minus"]))
        sites = sites[: int(arguments.get("top_n") or len(sites))]
        return {
            "calc_version": FAKE_VERSION,
            "smiles": canonical,
            "structure_id": "st_fake",
            "method": "GFN2-xTB",
            "solvent": arguments.get("solvent"),
            # Whatever the server's own default is — the caller must not depend on it.
            "mode": "electrophilic",
            "ranked_by": "f_minus",
            "total_atoms": len(atoms),
            "descriptors": dict(_FAKE_PANEL),
            "sites": sites,
        }

    def _compute_fukui_at(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The geometry-taking twin of the Fukui ranking, answering about *this* geometry.

        Geometry-dependent, so conformers of one molecule differ and an ensemble average can show a
        mispairing, reordering or truncation.
        """
        structure = arguments["structure"]
        identifier = _structure_id(structure)
        # Small, deterministic, and derived from the address — enough to reorder the ranking
        # between conformers without pretending to be physics.
        nudge = (int(identifier[-4:], 16) % 17) / 100.0
        answer = self._predict_site_reactivity(
            {"smiles": structure["smiles"], "top_n": arguments.get("top_n")}, nudge=nudge
        )
        answer["structure_id"] = identifier
        return answer

    def _compute_properties_at(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The geometry-taking twin, answering about the structure's own molecule.

        The dipole depends on the geometry (the first atom's x coordinate), so a Boltzmann average
        has a non-zero spread and its weighting is testable.
        """
        structure = arguments["structure"]
        answer = self._compute_electronic_properties({"smiles": structure["smiles"], **arguments})
        answer["structure_id"] = _structure_id(structure)
        answer["dipole_debye"] = round(answer["dipole_debye"] + structure["positions"][0][0], 4)
        return answer

    def _compute_electronic_properties(self, arguments: dict[str, Any]) -> dict[str, Any]:
        canonical = require_canonical_smiles(arguments["smiles"])
        molecule = Chem.AddHs(Chem.MolFromSmiles(canonical))
        atoms = list(molecule.GetAtoms())
        return {
            "calc_version": FAKE_VERSION,
            "calc_key": f"xtb.properties@{FAKE_VERSION}:{stable_hash(canonical)}:0",
            "smiles": canonical,
            "structure_id": "st_fake",
            "method": "GFN2-xTB",
            "solvent": arguments.get("solvent"),
            "total_energy_hartree": -1.0 * len(atoms),
            # Derived from the molecule so two categories of a BO parameter get different
            # descriptors, which is what a featurized surrogate is supposed to be able to see.
            "homo_ev": -10.0 - len(atoms) / 100,
            "lumo_ev": 1.0 + len(atoms) / 100,
            "gap_ev": 11.0 + len(atoms) / 50,
            "dipole_debye": 1.5 + len(atoms) / 100,
            # Varied per molecule and per atom: BoFire refuses a descriptor column with no
            # variation, and a constant field cannot reveal a reader that mixes atoms up.
            # `free_valence` is None for sulfur, as the real server reports for multi-valent
            # elements.
            "atom_charges": [
                {
                    "index": atom.GetIdx(),
                    "element": atom.GetSymbol(),
                    "charge": round((0.1 + len(atoms) / 1000) * (-1) ** i, 4),
                    "wiberg_valence": round(1.0 + atom.GetDegree(), 3),
                    "free_valence": (
                        None if atom.GetSymbol() in ("S", "P") else round(0.05 * (i + 1), 3)
                    ),
                }
                for i, atom in enumerate(atoms)
            ],
            "bond_orders": [{"atom_i": 0, "atom_j": 1, "order": 1.0}],
        }

    def _predict_logd(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("logD is composed on the client side; this tool should never be called")


class _Result:
    """The `CallToolResult` shape the client reads: `isError` plus text content.

    A failed call carries plain text rather than JSON — see `call_tool` above, which builds it the
    way the transport does.
    """

    def __init__(self, payload: dict[str, Any] | str, is_error: bool = False) -> None:
        import json

        self.isError = is_error
        self.content = [_Text(payload if isinstance(payload, str) else json.dumps(payload))]


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


# The modules that bound `default_structure_store` at import time, so a patch has to reach each of
# them rather than the definition site. Three, and the list is short because the geometry store has
# exactly three callers: the composites that write to it and the two paths that resolve a handle.
_STRUCTURE_STORE_CALLERS = (
    "chemclaw.connectors.calc.compose",
    "chemclaw.connectors.calc.activities",
    "chemclaw.connectors.calc.server.tools",
)


def install(monkeypatch: pytest.MonkeyPatch, server: FakeCalcServer) -> FakeCalcServer:
    """Make every `calc_session()` yield `server` and every geometry go to `server.structures`.

    Patched at `connectors.calc.remote`, the one module that opens a session, so tools, composites,
    activities and BO bindings reach the fake through their real call chains. The in-memory geometry
    store keeps composite tests off Postgres and lets a test resolve a reported id.
    """

    @asynccontextmanager
    async def _session(timeout_seconds: float | None = None) -> AsyncIterator[FakeCalcServer]:
        # Recorded rather than ignored: the read bound is a *property of the call*, and a sampling
        # call that inherits a Hessian's bound is abandoned by the client while the server runs it
        # to completion. A fake that dropped the argument could not see that.
        server.timeouts.append(timeout_seconds)
        yield server

    monkeypatch.setattr("chemclaw.connectors.calc.remote.calc_session", _session)
    for module in _STRUCTURE_STORE_CALLERS:
        monkeypatch.setattr(f"{module}.default_structure_store", lambda: server.structures)
    return server
