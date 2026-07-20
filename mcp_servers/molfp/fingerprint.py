"""ECFP4 fingerprints + Tanimoto — the deterministic capability core (plan step 3.1).

Pure, GPU-free, model-free: a SMILES becomes an ECFP4 (Morgan radius 2, 2048-bit)
fingerprint via RDKit, stored as a fixed-width bitstring so it maps directly onto a
Postgres `bit(2048)` column. Radius and width come from config, so the fingerprint
definition is a versioned choice, not a magic number. This module holds no judgment —
what Tanimoto score counts as a relevant precedent is a Skill decision (G6).
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from chemclaw.config import settings


class FingerprintError(ValueError):
    """A SMILES could not be parsed into a molecule (G4)."""


@lru_cache(maxsize=8)
def _generator(radius: int, n_bits: int) -> rdFingerprintGenerator.FingerprintGenerator64:
    """Cache the Morgan generator per (radius, bits) — constructing it is not free."""
    return rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits)


def _parse(smiles: str) -> Chem.Mol:
    """Parse a SMILES into an RDKit molecule, raising `FingerprintError` on failure."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise FingerprintError(f"unparseable SMILES: {smiles!r}")
    return mol


def ecfp_bitstring(smiles: str) -> str:
    """Return the ECFP4 fingerprint of `smiles` as a `settings.ecfp_bits`-long bitstring.

    The bitstring is the storage form (one char per bit, '0'/'1'), sized to the
    configured width so it inserts straight into the `bit(ecfp_bits)` column.
    """
    fp = _generator(settings.ecfp_radius, settings.ecfp_bits).GetFingerprint(_parse(smiles))
    return str(fp.ToBitString())


def tanimoto(bits_a: str, bits_b: str) -> float:
    """Tanimoto (Jaccard) similarity of two equal-length fingerprint bitstrings.

    `intersection / union` of set bits; two all-zero fingerprints are defined as 0.0
    (no shared structure to speak of). Operates on the stored bitstrings directly, so
    the in-memory backend ranks neighbors without RDKit — the same ordering the
    Postgres backend produces in SQL. (The all-zero case is a guard only: every real
    molecule sets at least one Morgan bit, so a fingerprint from a valid SMILES is never
    empty — where pgvector's Jaccard would return NaN and the two backends could differ.)
    """
    if len(bits_a) != len(bits_b):
        raise FingerprintError("cannot compare fingerprints of different widths")
    a, b = int(bits_a, 2), int(bits_b, 2)
    union = (a | b).bit_count()
    return (a & b).bit_count() / union if union else 0.0
