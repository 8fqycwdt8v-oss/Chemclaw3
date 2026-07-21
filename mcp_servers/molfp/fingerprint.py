"""ECFP4 fingerprints — the molecule capability core (plan step 3.1).

Pure, GPU-free, model-free: a SMILES becomes an ECFP4 (Morgan radius 2, 2048-bit)
fingerprint via RDKit, stored as a fixed-width bitstring so it maps directly onto a
Postgres `bit(2048)` column. Radius and width come from config, so the fingerprint
definition is a versioned choice, not a magic number. Ranking (`tanimoto`) and the store
are the domain-neutral `mcp_servers.fpstore`; this module is only the molecule-specific
"SMILES → bits" step and holds no judgment (G6).
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator

from chemclaw.config import settings
from mcp_servers.fpstore import FingerprintError


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


def molecule_definition() -> str:
    """The current ECFP definition signature (radius + width) stored on each molecule row.

    Two ECFP fingerprints of equal width but different radius are the same length yet
    incomparable, so the store records this signature per row and refuses to rank across
    signatures — changing `ecfp_radius`/`ecfp_bits` and re-indexing can't silently mix them.
    """
    return f"ecfp:r{settings.ecfp_radius}:b{settings.ecfp_bits}"
