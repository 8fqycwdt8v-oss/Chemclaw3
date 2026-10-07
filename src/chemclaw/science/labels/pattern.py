"""Substructure screening that scales: an RDKit pattern fingerprint, stored as set-bit indices.

A capped per-record scan is wrong, not just slow, over millions of structures. Screen-then-verify
fixes that: every bit a query pattern sets is also set by any molecule containing it, so a molecule
missing one query bit cannot match and is skipped. ECFP lacks this property, since Morgan hashes
whole circular environments.

Stored as an `INTEGER[]` of set-bit indices because the test is bitwise containment and GIN's `@>`
is the stock Postgres index that answers it; HNSW ranks by distance and cannot.
"""

from collections.abc import Sequence

from rdkit import Chem
from rdkit.Chem import rdmolops

from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.store import FingerprintError

# The pattern fingerprint's width. Not a setting: query and stored bits must come from the same
# folding for containment to mean anything, so changing it is a migration that rebuilds every stored
# array.
PATTERN_BITS = 2048


def pattern_bit_indices(smiles: str) -> list[int]:
    """The set-bit indices of `smiles`'s pattern fingerprint, ascending.

    Ascending for a stable, human-comparable order; `@>` does not care.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise InvalidSmilesError(f"could not parse SMILES: {smiles!r}")
    return _indices(rdmolops.PatternFingerprint(mol, fpSize=PATTERN_BITS))


def query_bit_indices(query: Chem.Mol) -> list[int]:
    """The set-bit indices a compiled SMARTS query demands of any molecule that could match it.

    Takes the compiled query so screen and verify use the same parse. `[]` is not "matches nothing":
    a query that sets no bits screens nothing, and the caller verifies every candidate.
    """
    return _indices(rdmolops.PatternFingerprint(query, fpSize=PATTERN_BITS))


def compile_query(smarts: str) -> Chem.Mol:
    """The SMARTS query, compiled once and bounded in length. Raises `FingerprintError`.

    Compiled once because both screen and verify use it, and the verify loops over candidates.
    Bounded by `substructure_query_max_length` because the string comes from the model and subgraph
    isomorphism is priced in CPU. `MolFromSmarts`, because a query is a pattern, not a molecule.
    """
    ceiling = settings.substructure_query_max_length
    if len(smarts) > ceiling:
        raise FingerprintError(
            f"substructure query exceeds {ceiling} characters ({len(smarts)}); "
            "pass a smaller fragment (or raise CHEMCLAW_SUBSTRUCTURE_QUERY_MAX_LENGTH)"
        )
    query = Chem.MolFromSmarts(smarts)
    if query is None:
        raise FingerprintError(f"could not parse SMARTS query: {smarts!r}")
    return query


def matching(structures: Sequence[str], query: Chem.Mol) -> list[str]:
    """The structures that genuinely contain the compiled `query`, in the order given.

    The exact verification after the sound-but-inexact screen. A stored structure that no longer
    parses is dropped so one bad row cannot fail a search.

    Blocking: RDKit subgraph isomorphism per candidate. Never call it on the event loop;
    `CorpusMolecules.containing` offloads it under a timeout.
    """
    verified = []
    for structure in structures:
        molecule = Chem.MolFromSmiles(structure)
        if molecule is not None and molecule.HasSubstructMatch(query):
            verified.append(structure)
    return verified


def _indices(fingerprint: object) -> list[int]:
    """The ascending set-bit indices of an RDKit `ExplicitBitVect`."""
    return sorted(fingerprint.GetOnBits())  # type: ignore[attr-defined]
