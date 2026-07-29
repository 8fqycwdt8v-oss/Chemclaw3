"""A concrete 3D structure — the value every xTB task consumes (xTB plan X1).

Why this exists. Before it, each calculator went SMILES → embed → compute in one
breath, so two tasks on "the same molecule" silently produced two different
geometries and nothing downstream could reuse one. `Structure` makes the geometry an
explicit, **content-addressed** value: `structure_id` is a stable hash of the chemical
content, so equal geometries collapse to one cache entry no matter how they were
produced, and `origin` records which calculation produced one (GxP lineage).

Two consequences that pay for the type immediately:

- the calculation cache key names the *geometry*, not the recipe that made it, so the
  embedding seed no longer has to appear in the key — its effect is already inside the
  coordinates (D-011 determinism, expressed one level more honestly);
- `multiplicity` generalizes the previous hard closed-shell rejection into a
  declared-and-validated electron count, which is what makes the Fukui ions
  (`chemclaw.science.calc.xtb_props`) a legitimate open-shell calculation rather than a silent one.

Coordinates are in **Angstrom** — the interchange unit of RDKit, XYZ files, and this
whole layer; `chemclaw.science.calc.xtb_engine` is the single boundary that converts to atomic
units.
"""

import numpy as np
from pydantic import BaseModel, Field, model_validator
from rdkit import Chem

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.xtb_engine import geometry, parse_molecule


class Structure(BaseModel):
    """One 3D molecular structure, addressed by the hash of its chemical content.

    `elements` and `positions` are parallel: atom `i` has atomic number
    `elements[i]` at `positions[i]` (Angstrom). Positions are normalized on
    construction (rounded to `settings.xtb_geometry_decimals`) so that float noise
    from a re-run cannot fork the cache while the stored coordinates still *are* the
    ones that were hashed.
    """

    elements: list[int] = Field(min_length=1)
    positions: list[list[float]] = Field(min_length=1)
    charge: int = 0
    # Spin multiplicity 2S+1: 1 = closed-shell singlet, 2 = doublet, 3 = triplet.
    multiplicity: int = Field(default=1, ge=1)
    # The canonical SMILES this structure represents, when it came from (or maps to)
    # one. Carried for reporting and for the atom-index mapping in `symbols`.
    smiles: str | None = None
    # `CalculationKey.as_str()` of the calculation that produced this geometry, for
    # structures that are a calculation's *output* rather than an embedding.
    origin: str | None = None

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> "Structure":
        """Round coordinates, then reject a structure that is not physically consistent.

        Three ways a structure can be wrong are caught here rather than by tblite
        converging something meaningless (gate G4): mismatched array lengths, a
        coordinate row that is not 3D, and an electron count that cannot produce the
        declared multiplicity.
        """
        if len(self.positions) != len(self.elements):
            raise ValueError(f"{len(self.positions)} positions for {len(self.elements)} elements")
        if any(len(row) != 3 for row in self.positions):
            raise ValueError("every position must have exactly three coordinates")
        decimals = settings.xtb_geometry_decimals
        # `+ 0.0` normalizes the negative zero that rounding can produce, so two
        # geometrically identical structures cannot differ in their hash by a sign bit.
        self.positions = [[round(value, decimals) + 0.0 for value in row] for row in self.positions]
        unpaired = self.multiplicity - 1
        electrons = sum(self.elements) - self.charge
        if electrons < unpaired or (electrons - unpaired) % 2:
            # The default (closed-shell) case gets the specific message, because it is
            # the one a caller hits by accident — from a radical SMILES or a wrong
            # charge — and the fix is to declare the multiplicity, not to fix the atoms.
            if self.multiplicity == 1:
                raise ValueError(
                    f"open-shell species ({electrons} electrons at charge {self.charge}) "
                    "cannot be a closed-shell singlet: declare its multiplicity explicitly"
                )
            raise ValueError(
                f"{electrons} electrons at charge {self.charge} cannot form multiplicity "
                f"{self.multiplicity} ({unpaired} unpaired)"
            )
        return self

    @property
    def structure_id(self) -> str:
        """Content address: `st_` + a stable hash of the chemistry, not the provenance.

        Deliberately excludes `smiles` and `origin`: two identical geometries are the
        same structure whether one was embedded from a SMILES and the other optimized,
        and that is exactly the identity that lets a downstream task hit the cache
        regardless of which route produced its input.
        """
        payload = {
            "elements": self.elements,
            "positions": self.positions,
            "charge": self.charge,
            "multiplicity": self.multiplicity,
        }
        return f"st_{stable_hash(payload)}"

    @property
    def uhf(self) -> int:
        """Number of unpaired electrons, the form tblite wants."""
        return self.multiplicity - 1

    @property
    def symbols(self) -> list[str]:
        """Element symbols, one per atom, for human-readable per-atom results."""
        table = Chem.GetPeriodicTable()
        return [table.GetElementSymbol(number) for number in self.elements]

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (atomic numbers, positions in Angstrom) for the engine."""
        return np.array(self.elements), np.array(self.positions)


def structure_from_mol(
    mol: Chem.Mol,
    *,
    charge: int,
    multiplicity: int = 1,
    smiles: str | None = None,
    optimize: bool = False,
) -> Structure:
    """Embed a deterministic geometry for `mol` and wrap it as a `Structure`.

    Expects explicit hydrogens (`chemclaw.science.calc.xtb_engine.parse_molecule` output) so the
    electron count validated by `Structure` is complete. The embedding seed comes
    from config, so the geometry — and therefore the structure id — is reproducible.
    """
    numbers, positions = geometry(mol, settings.xtb_embed_seed, optimize=optimize)
    return Structure(
        elements=[int(number) for number in numbers],
        positions=[[float(value) for value in row] for row in positions],
        charge=charge,
        multiplicity=multiplicity,
        smiles=smiles,
    )


def radical_multiplicity(mol: Chem.Mol) -> int:
    """The spin multiplicity a SMILES' explicit radical electrons imply.

    A SMILES *can* state its open shell: `[CH3]` carries one radical electron, `[O][O]`
    two. Where it does, the ground-state multiplicity follows (2S+1 with every radical
    electron unpaired), and there is nothing to guess — which is what makes a homolysis
    energy computable from two SMILES rather than from a hand-declared spin state.
    Silent on the cases a SMILES genuinely does not encode: a closed-shell formula whose
    ground state is a triplet still needs `multiplicity` stated explicitly.
    """
    return 1 + sum(int(atom.GetNumRadicalElectrons()) for atom in mol.GetAtoms())


def structure_from_smiles(
    smiles: str,
    *,
    charge: int | None = None,
    multiplicity: int | None = 1,
    optimize: bool = False,
) -> Structure:
    """Build a `Structure` from a SMILES, canonicalizing first (D-011 determinism).

    Atom order steers the seeded embedding, so canonicalizing *before* embedding is
    what makes two spellings of one molecule produce the same geometry — and thus the
    same structure id and the same cache entry.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net charge. `None` takes the SMILES' own formal charge; an explicit
            value that contradicts it is rejected rather than computed at the wrong
            electron count (gate G4).
        multiplicity: Spin multiplicity 2S+1; validated against the electron count.
            `None` derives it from the SMILES' explicit radical electrons, which is
            what a caller wants for a set of species that may include radicals; the
            default of 1 keeps every existing caller closed-shell-or-error.
        optimize: Pre-optimize with MMFF where the force field has parameters.

    Returns:
        The embedded structure, carrying the canonical SMILES.
    """
    canonical = require_canonical_smiles(smiles)
    mol = parse_molecule(canonical)
    formal_charge = Chem.GetFormalCharge(mol)
    if charge is None:
        charge = formal_charge
    elif charge != formal_charge:
        raise ValueError(
            f"declared charge {charge} does not match the formal charge "
            f"{formal_charge} of {smiles!r}"
        )
    return structure_from_mol(
        mol,
        charge=charge,
        multiplicity=radical_multiplicity(mol) if multiplicity is None else multiplicity,
        smiles=canonical,
        optimize=optimize,
    )
