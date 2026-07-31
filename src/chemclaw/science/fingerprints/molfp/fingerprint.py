"""ECFP4 fingerprints — the molecule capability core (plan step 3.1).

Pure, GPU-free, model-free: a SMILES becomes an ECFP4 (Morgan radius 2, 2048-bit)
fingerprint via RDKit, stored as a fixed-width bitstring so it maps directly onto a
Postgres `bit(2048)` column. Radius and width come from config, so the fingerprint
definition is a versioned choice, not a magic number. Ranking (`tanimoto`) and the store
are the domain-neutral `chemclaw.science.fingerprints.store`; this module is only the
molecule-specific "SMILES → bits" step and holds no judgment (G6).
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from chemclaw.core.chem import STANDARDIZATION_VERSION, standardize
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintError


@lru_cache(maxsize=8)
def _generator(radius: int, n_bits: int) -> rdFingerprintGenerator.FingerprintGenerator64:
    """Cache the Morgan generator per (radius, bits) — constructing it is not free."""
    return rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits)


def _parse(smiles: str) -> Chem.Mol:
    """Parse a SMILES into an RDKit molecule, raising `FingerprintError` on failure.

    RDKit parses the empty string to a zero-atom Mol rather than failing; that would
    fingerprint to all zeros — a meaningless query/index entry — so it is rejected here
    too, mirroring rxnfp's empty-fingerprint guard (G4).
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise FingerprintError(f"unparseable SMILES: {smiles!r}")
    if mol.GetNumAtoms() == 0:
        raise FingerprintError(f"empty SMILES (no atoms): {smiles!r}")
    return mol


def ecfp_bitstring(smiles: str) -> str:
    """Return the ECFP4 fingerprint of `smiles` as a `settings.ecfp_bits`-long bitstring.

    The bitstring is the storage form (one char per bit, '0'/'1'), sized to the
    configured width so it inserts straight into the `bit(ecfp_bits)` column.

    **Fingerprinted after standardization**, so similarity answers the "same compound" question the
    index is asked. Without it a hydrochloride and its free base fingerprinted differently and each
    held its own row, so a search for one would rank the other as merely similar — and a chemist
    reading that result cannot tell it from two genuinely different molecules.
    """
    mol = standardize(_parse(smiles))
    fp = _generator(settings.ecfp_radius, settings.ecfp_bits).GetFingerprint(mol)
    return str(fp.ToBitString())


def molecule_definition() -> str:
    """The current ECFP definition signature stored on each molecule row.

    Two ECFP fingerprints of equal width but different radius are the same length yet
    incomparable, so the store records this signature per row and refuses to rank across
    signatures — changing `ecfp_radius`/`ecfp_bits` and re-indexing can't silently mix them.

    The **standardization version** is part of the signature for the same reason, and it is the
    less obvious half: a row indexed before molecule standardization was keyed to the molecule as
    written, so a hydrochloride and its free base held separate rows. Ranking those against rows
    built after standardization would answer a similarity question using two different notions of
    what a molecule *is*. Bumping the definition retires them until a re-index rebuilds them, which
    is the same failure-safe behaviour a changed radius already gets.
    """
    return f"ecfp:r{settings.ecfp_radius}:b{settings.ecfp_bits}:{STANDARDIZATION_VERSION}"
