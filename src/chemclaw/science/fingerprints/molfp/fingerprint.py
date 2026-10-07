"""ECFP4 fingerprints — the molecule-specific "SMILES → bits" step.

A SMILES becomes an ECFP4 (Morgan radius 2, 2048-bit) fingerprint via RDKit, stored as a fixed-width
bitstring that maps onto a Postgres `bit(2048)` column. Radius and width come from config. Ranking
and storage are in the domain-neutral `chemclaw.science.fingerprints.store`.
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from chemclaw.core.chem import STANDARDIZATION_VERSION, standardize
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintInputError

# Stereochemistry is part of the bits (RDKit's default leaves it out). Otherwise enantiomers and
# E/Z pairs fingerprint identically and tie at Tanimoto 1.0 while citing different compounds,
# since `standardize` keeps them apart. Not a setting: it decides whether the index agrees with
# the identity function feeding it; it is named in `molecule_definition`. Isotopes stay invisible,
# as ECFP4's invariant has no isotope; `compound_id` distinguishes isotopologues.
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

    Fingerprints of equal width but different definition are incomparable, so the store records this
    per row and refuses to rank across signatures. It covers radius, width, the standardization
    version and the `chiral` token (derived from `_INCLUDE_CHIRALITY`); changing any retires old
    rows until a re-index rebuilds them.
    """
    chirality = "chiral" if _INCLUDE_CHIRALITY else "flat"
    return (
        f"ecfp:r{settings.ecfp_radius}:b{settings.ecfp_bits}:{chirality}:{STANDARDIZATION_VERSION}"
    )
