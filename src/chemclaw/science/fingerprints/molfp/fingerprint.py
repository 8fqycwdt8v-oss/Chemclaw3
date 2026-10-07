"""ECFP4 fingerprints — the molecule-specific "SMILES → bits" step.

Morgan fingerprints via RDKit as fixed-width bitstrings for a Postgres `bit(N)` column; radius and
width come from config.
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from chemclaw.core.chem import STANDARDIZATION_VERSION, standardize
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintInputError

# Stereochemistry is part of the bits (not RDKit's default), so enantiomers do not tie at 1.0
# while citing different compounds. Not a setting; recorded in `molecule_definition`. Isotopes
# remain invisible to ECFP4; `compound_id` tells isotopologues apart.
_INCLUDE_CHIRALITY = True


@lru_cache(maxsize=8)
def _generator(
    radius: int, n_bits: int, include_chirality: bool
) -> rdFingerprintGenerator.FingerprintGenerator64:
    """Cache the Morgan generator per (radius, bits, chirality) — constructing it is not free."""
    return rdFingerprintGenerator.GetMorganGenerator(
        radius=radius, fpSize=n_bits, includeChirality=include_chirality
    )


def _parse(smiles: str) -> Chem.Mol:
    """Parse a SMILES into an RDKit molecule, raising `FingerprintInputError` on failure.

    RDKit parses "" to a zero-atom Mol, which would fingerprint to all zeros, so it is rejected too.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise FingerprintInputError(f"unparseable SMILES: {smiles!r}")
    if mol.GetNumAtoms() == 0:
        raise FingerprintInputError(f"empty SMILES (no atoms): {smiles!r}")
    return mol


def ecfp_bitstring(smiles: str) -> str:
    """Return the ECFP4 fingerprint of `smiles` as a `settings.ecfp_bits`-long bitstring.

    One char per bit, sized to insert into the `bit(ecfp_bits)` column. Fingerprinted after
    standardization, so a salt and its free base are the same compound rather than "similar".
    """
    mol = standardize(_parse(smiles))
    generator = _generator(settings.ecfp_radius, settings.ecfp_bits, _INCLUDE_CHIRALITY)
    return str(generator.GetFingerprint(mol).ToBitString())


def molecule_definition() -> str:
    """The current ECFP definition signature stored on each molecule row.

    Rows of different definitions are incomparable, so the store refuses to rank across them. Covers
    radius, width, the standardization version and chirality; changing any retires old rows until
    re-indexed.
    """
    chirality = "chiral" if _INCLUDE_CHIRALITY else "flat"
    return (
        f"ecfp:r{settings.ecfp_radius}:b{settings.ecfp_bits}:{chirality}:{STANDARDIZATION_VERSION}"
    )
