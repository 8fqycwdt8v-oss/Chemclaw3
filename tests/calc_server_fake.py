"""A stand-in for `Chemclaw3-mcp`'s `calc` server, so this suite proves wiring without physics.

`D-2026-08-16-the-physics-leaves-the-cache-stays` moved every engine out of this repository. What
is left here is the cache, the composition and the ledger, and all three are testable *without* a
quantum chemistry program — but not without something on the other end of `calc_session`. This is
that something.

**It answers the same shapes and counts every call**, which is what the surviving tests are about:
"was this recomputed?" is a question about call counts and nothing else (D-011), and "did the
composite ask for the right parts?" is a question about which tools were called with what. The
numbers it returns are arithmetic placeholders and are never asserted on as chemistry — the one
place real physics is asserted is `tests/test_calc_thermo.py`, which runs the RRHO arithmetic over
Hessians *recorded from the live server* and checks them against measured entropies.

**Keys are derived the way the server derives them**, from the same inputs: `structure_id` (or the
canonical SMILES) plus the parameters that move the answer. Two properties that were measured
against the running server are reproduced deliberately, because a fake that got them wrong would
make the tests pass on a design that fails in production:

- **A Fukui key does not name the mode.** All three modes on phenol derive one key, so a cache hit
  can serve the wrong ranking unless the caller re-ranks (`SiteReactivityResult.ranked_for`).
- **A relaxation key does not name who asked.** `optimize_geometry` and `relax_structure` derive
  the same `xtb.opt` key while returning different payloads, which is why this repository only ever
  caches the full result.
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

# A version string carrying **both** key delimiters, because a real one does: `esol-delaney@2004`
# carries the `@` and `cal-0.28733:-29.3116` carries the `:`. A client that split the flat
# `type@version:input:params` form would reassemble a key that matches nothing, forever.
FAKE_VERSION = "GFN2-xTB+fake@1/cal-0.28733:-29.3116"

# Which cache type each compute tool answers under, and which of its arguments enter `params`. The
# *subject* — a `structure` or a `smiles` — is never in this tuple: it is hashed into `input_hash`,
# and a molecule's canonical form is what makes two spellings one row. An argument missing from a
# tuple is one the answer does not depend on, which is exactly the property the composites rely on:
# `predict_site_reactivity` has an empty tuple because a Fukui calculation does not depend on the
# mode, and `scan_point` carries `atoms` and `value` because a constrained point does.
_KEYED: dict[str, tuple[str, tuple[str, ...]]] = {
    "compute_xtb_energy": ("xtb.sp", ("charge",)),
    "compute_electronic_properties": ("xtb.properties", ("solvent",)),
    # The same calculation asked at a *named* geometry rather than at one embedded from a SMILES,
    # so it answers under the same `calc_type` with the same params — the subject is what differs,
    # and the subject is `input_hash`. Reproduced here because it is the property the
    # cheap-search-then-careful-optimization chain rests on: relaxing a conformer and then asking
    # for its properties must reach the entry that conformer's own address names.
    "compute_properties_at": ("xtb.properties", ("solvent",)),
    "predict_site_reactivity": ("xtb.fukui", ()),
    # The geometry-taking twin, under the same `calc_type` — one row serves a Fukui computed from
    # a SMILES and one computed at the identical geometry. `mode` and `top_n` stay out of the key
    # on both: the server computes all three indices from three single points and sorts on the way
    # out, so keying on `mode` would make a cache *hit* authoritative about an ordering it never
    # chose, which is what `ranked_for` exists to prevent.
    #
    # **`solvent` is in the key here and absent from the twin, and that is not an inconsistency.**
    # `predict_site_reactivity(smiles, mode, top_n)` takes no solvent at all, while
    # `compute_fukui_at(structure, mode, solvent, top_n)` does, and the server keys it —
    # `identity._fukui_at` builds `XtbSpec(task="fukui", solvent=_solvent(arguments))`. The two
    # tools shared one entry here and only one of them fitted it, so a Fukui set computed in water
    # and one in the gas phase collided in tests while production correctly recomputed.
    "compute_fukui_at": ("xtb.fukui", ("solvent",)),
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

    Real rather than synthetic because a `structure_id` is a hash of coordinates and half of every
    downstream key: a fake that returned the same three atoms for every molecule would make two
    different species share a relaxation entry, which is the one failure a cache test must be able
    to see.
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


def harmonic_hessian(structure: dict[str, Any], *, imaginary: bool = False) -> dict[str, Any]:
    """A well-formed Hessian payload for `structure`, optionally carrying one negative eigenvalue.

    Not physics: a diagonal matrix whose spectrum is chosen, base64-encoded exactly as the server
    encodes a real one. `imaginary=True` is how the saddle-point refinement loop is driven — the
    escape it performs is a property of this repository (the key of a thermochemistry would name
    the geometry the loop settles on, which is why it was never shipped), so it has to be
    exercisable without a real saddle point.
    """
    import base64
    import io

    size = 3 * len(structure["elements"])
    diagonal = np.full(size, 0.5)
    if imaginary:
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
        "hessian_npy": pack(matrix),
        "dipole_derivatives_npy": pack(np.zeros((size, 3))),
        "ir_intensities": None,
    }


def _nudged(structure: dict[str, Any], index: int) -> dict[str, Any]:
    """The same molecule at a slightly different geometry — one ensemble member.

    Displaces every atom along x by `index/100` Angstrom, which is two orders of magnitude above the
    rounding `Structure` applies, so each member has its own `structure_id` and therefore its own
    cache entry. Index 0 is returned unchanged, so the lowest member is still the input geometry and
    the existing tests that follow it through a composite are unaffected.
    """
    if index == 0:
        return structure
    offset = index / 100.0
    return {
        **structure,
        "positions": [[x + offset, y, z] for x, y, z in structure["positions"]],
    }


# A three-well torsional potential in Hartree, shaped like n-butane's: minima at 60, 180 and 300
# degrees, the anti well (180) about 1 kcal/mol below the two gauche wells, and a barrier of about
# 2.5 kcal/mol between them. The numbers are placeholders; the *shape* is what the profile
# composite is tested against, because a flat or monotonic surface has no rotamers to find.
_BARRIER_HARTREE = 0.004
_GAUCHE_HARTREE = 0.0016
_WELLS = (60.0, 180.0, 300.0)


def torsional_energy(degrees: float) -> float:
    """The synthetic torsional potential at one dihedral, in Hartree above its own minimum."""
    radians = math.radians(degrees)
    threefold = _BARRIER_HARTREE * (1.0 + math.cos(3.0 * radians)) / 2.0
    onefold = _GAUCHE_HARTREE * (1.0 + math.cos(radians)) / 2.0
    return threefold + onefold


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

    Real geometry manipulation rather than a recorded number, because the composite reads the angle
    back off the coordinates: a fake that reported a dihedral it had not actually set would let a
    released well keep the constrained geometry and still look correct.
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
        self, *, saddle_first: bool = False, torsion: tuple[int, int, int, int] | None = None
    ) -> None:
        """Start with no calls recorded.

        `saddle_first` makes the first Hessian carry an imaginary frequency and every later one a
        minimum, which is the sequence `relax_to_minimum`'s escape needs to be visible.

        `torsion` turns on a **one-dimensional torsional potential** over those four atoms: a
        constrained point actually sets the dihedral and reports `_TORSIONAL` at it, and an
        unconstrained relaxation actually settles into the nearest of its three wells. Nothing else
        here models a potential energy surface at all, and this one does for a reason rather than
        for realism — the rotational profile's whole claim is that releasing a scan point's
        constraint moves it to a different geometry with a different energy. A fake whose relaxation
        returns its input unchanged cannot express that claim being false, and a fake that cannot
        express the failure is not evidence
        (`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`).
        """
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._saddle_first = saddle_first
        self._torsion = torsion
        self.overrides: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        # Where the composites' geometries land once `install` has wired it in. On the fake rather
        # than built by `install`, so a test can read it back — resolving an id a result reported
        # is how "the handle the agent is shown is the handle it can pass back" gets checked
        # against the real code path instead of against a stub.
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
            return _Result({"detail": str(error)}, is_error=True)

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
        # `structure_id` for the calculations that run on a geometry, exactly as the real server
        # reports it — it is the server's authoritative answer to "which geometry is this about",
        # and it is what `calculation_results.structure_id` records so a stored row can be found by
        # the conformer a chemist picked (D-2026-08-21). Absent for a molecule-keyed calculator,
        # which is about a compound and not about any particular geometry of it.
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
            result["energy_hartree"] += torsional_energy(settled)
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
        return {
            "calc_version": FAKE_VERSION,
            "calc_key": f"xtb.opt@{FAKE_VERSION}:{_structure_id(structure)}:0",
            "smiles": structure.get("smiles"),
            "input_structure_id": _structure_id(structure),
            "structure": structure,
            "method": "GFN2-xTB",
            "engine": "tblite",
            "solvent": solvent,
            "initial_energy_hartree": -1.0 * len(structure["elements"]) + 0.01,
            "energy_hartree": -1.0 * len(structure["elements"]),
            "relaxation_kcal": 6.3,
            "steps": 4,
            "max_gradient": 1e-5,
            "displacement_rms_angstrom": 0.02,
            "frozen_atoms": [],
        }

    def _compute_hessian(self, arguments: dict[str, Any]) -> dict[str, Any]:
        saddle = self._saddle_first and self.count("compute_hessian") == 1
        payload = harmonic_hessian(arguments["structure"], imaginary=saddle)
        payload["solvent"] = arguments.get("solvent")
        return payload

    def _search_conformer_ensemble(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._ensemble(arguments, search=arguments.get("search", "conformers"))

    def _search_binding_modes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._ensemble(arguments, search="complex")

    def _ensemble(self, arguments: dict[str, Any], *, search: str) -> dict[str, Any]:
        structure = arguments["structure"]
        return {
            "calc_version": FAKE_VERSION,
            "calc_key": None,
            "structure_id": _structure_id(structure),
            "method": "GFN2-xTB",
            "solvent": arguments.get("solvent"),
            "search": search,
            "effort": arguments.get("effort", "quick"),
            # Three members, degeneracies 1/2/1: enough for a degeneracy-weighted population to
            # differ visibly from an unweighted one, which is the arithmetic that stayed here.
            #
            # **Each member is a distinct geometry**, nudged along x by a hundredth of an Angstrom.
            # They shared one structure until a refinement composite needed them not to: refining
            # an ensemble is one optimization and one Hessian *per member*, and three members at one
            # address collapse to a single cache entry — so a fake with identical members reports
            # three refinements as one call and every fan-out test passes on work that never
            # happened. The displacement is above `_GEOMETRY_DECIMALS`, so the three addresses
            # genuinely differ.
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
        # f_minus descends with the index and f_plus ascends, so the two modes rank the atoms in
        # opposite orders — which makes a mis-served ranking visible rather than coincidental.
        #
        # **`f_zero` varies per atom and `nudge` moves it per geometry**, and both matter. It is
        # the field an ensemble average actually reports (`compose._DEFAULT_FUKUI_MODE` is
        # "radical"), and it used to be the constant 0.5 for every atom of every conformer — so
        # `test_an_averaged_fukui_ranking_reaches_the_geometry_taking_tool` was comparing 0.5 to
        # 0.5 and could not have failed. `nudge` is what makes two conformers rank their atoms
        # differently, which is the whole premise of averaging over an ensemble.
        sites = [
            {
                "index": atom.GetIdx(),
                "element": atom.GetSymbol(),
                "f_minus": round(1.0 - atom.GetIdx() / len(atoms), 4),
                "f_plus": round(atom.GetIdx() / len(atoms), 4),
                "f_zero": round(0.5 + nudge * (1 if atom.GetIdx() % 2 else -1), 4),
            }
            for atom in atoms
        ]
        # **Ranked most-susceptible first and truncated, because that is the contract the real
        # server keeps** (`SiteReactivityResult`: "ordered most-susceptible first by the index named
        # in `ranked_by`, and truncated to the most susceptible `len(sites)` of `total_atoms`").
        # The fake used to return them in atom-index order and whole, which is the shape in which
        # pairing conformers by list position happens to be correct — so the fake could not express
        # the defect that shipped.
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
            "sites": sites,
        }

    def _compute_fukui_at(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The geometry-taking twin of the Fukui ranking, answering about *this* geometry.

        **Geometry-dependent, which is the entire point of the tool.** This used to delegate on the
        SMILES alone, so every conformer of one molecule came back byte-identical — and an ensemble
        average over identical members cannot show a mispairing, a reordering or a truncation. The
        `DEFERRED.md` row this tool closed was written to ask how often the top-ranked site *moves*
        between geometries; a fake that holds it still answers "never" by construction.
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

        **The dipole depends on the geometry**, which the SMILES-in twin cannot express and which a
        Boltzmann average is entirely about. A fake whose property is the same at every conformer
        makes an ensemble average equal to its own mean by construction, so the spread is zero and a
        test over it passes whatever the weighting does. The dependence is the crudest thing that
        works — the first atom's x coordinate — because the number is never asserted as chemistry.
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
            # Varied *per molecule*, not just per atom: BoFire refuses a descriptor column with
            # no variation across categories, which is a real property of a featurized campaign —
            # a descriptor that is the same for every option tells the surrogate nothing.
            "atom_charges": [
                {
                    "index": atom.GetIdx(),
                    "element": atom.GetSymbol(),
                    "charge": round((0.1 + len(atoms) / 1000) * (-1) ** i, 4),
                }
                for i, atom in enumerate(atoms)
            ],
            "bond_orders": [{"atom_i": 0, "atom_j": 1, "order": 1.0}],
        }

    def _predict_logd(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("logD is composed on the client side; this tool should never be called")


class _Result:
    """The `CallToolResult` shape the client reads: `isError` plus text content."""

    def __init__(self, payload: dict[str, Any], is_error: bool = False) -> None:
        import json

        self.isError = is_error
        self.content = [_Text(json.dumps(payload))]


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

    Patched at `connectors.calc.remote`, the one module that opens a session — so the tool path,
    the composites, the durable activities and the BO calculator bindings all reach this fake
    through their real call chains rather than through a stub of their own.

    **The geometry store is part of "no socket is opened anywhere".** Every composite persists the
    geometries it receives (D-2026-08-21-a-geometry-is-an-address-not-a-payload), so without this
    an offline composite test reaches Postgres — which is exactly the shape of dependency this
    fake exists to remove, and the failure a sandbox with no database would see. Installing one
    in-memory store shared by all three callers also makes the round trip testable: a test can
    relax a molecule and then resolve the id the result reported, through the real code path.
    """

    @asynccontextmanager
    async def _session() -> AsyncIterator[FakeCalcServer]:
        yield server

    monkeypatch.setattr("chemclaw.connectors.calc.remote.calc_session", _session)
    for module in _STRUCTURE_STORE_CALLERS:
        monkeypatch.setattr(f"{module}.default_structure_store", lambda: server.structures)
    return server
