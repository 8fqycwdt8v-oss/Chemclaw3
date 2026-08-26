"""Substructure screening that scales: an RDKit pattern fingerprint, stored as set-bit indices.

`find_substructure_matches` answers "which of our molecules contain this motif" by loading up to
`substructure_scan_max_records` rows and matching each with RDKit. That is right for a corpus of a
few thousand ELN compounds and hopeless for a few million patent structures: the cap does not make
the answer slower, it makes it *wrong*, because a truncated scan reports a lower bound the caller
has to be told about.

The fix is the classical screen-then-verify, and the only interesting choice is the index. A
pattern fingerprint has the property a screen needs and an ECFP does not: **every bit a query
pattern sets is also set by any molecule containing it.** So a molecule missing one of the query's
bits provably cannot contain the pattern and can be skipped without matching it — sound in one
direction, which is exactly what a prefilter may be. `docs/planning/DEFERRED.md` records the other
half: ECFP bits cannot do this, because Morgan hashes whole circular environments and a
substructure's environment is not a subset of a containing molecule's.

Stored as an `INTEGER[]` of set-bit indices rather than a bit string, because the test is bitwise
*containment* and GIN's `@>` is the only index in stock Postgres that answers it. pgvector's HNSW
ranks by distance and cannot express "has at least these bits".
"""

from rdkit import Chem
from rdkit.Chem import rdmolops

from chemclaw.core.chem import InvalidSmilesError
from chemclaw.science.fingerprints.store import FingerprintError

# The pattern fingerprint's width. Not a setting, and that is deliberate: it is not a tuning knob
# but half of a *soundness* contract — a query's bits and a stored molecule's bits must come from
# the same folding or containment means nothing. Changing it means rebuilding every stored array,
# which is a migration, not an environment variable.
PATTERN_BITS = 2048


def pattern_bit_indices(smiles: str) -> list[int]:
    """The set-bit indices of `smiles`'s pattern fingerprint, ascending.

    Ascending because the array is compared with `@>` and read by a person debugging a screen that
    returned nothing; neither cares about order, and a stable one makes two rows for one structure
    comparable by eye.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise InvalidSmilesError(f"could not parse SMILES: {smiles!r}")
    return _indices(rdmolops.PatternFingerprint(mol, fpSize=PATTERN_BITS))


def query_bit_indices(smarts: str) -> list[int]:
    """The set-bit indices a SMARTS query demands of any molecule that could match it.

    The query side of the screen. Built from `MolFromSmarts`, not `MolFromSmiles`, because a query
    is a pattern: `[c,n]1ccccc1` is not a molecule and parsing it as one silently yields `None`.

    An empty result is not an error and must not be treated as "matches nothing": a query so
    generic that it sets no bits screens nothing, and the caller falls back to verifying every
    candidate. Returning `[]` is how that is said.
    """
    query = Chem.MolFromSmarts(smarts)
    if query is None:
        raise FingerprintError(f"could not parse SMARTS query: {smarts!r}")
    return _indices(rdmolops.PatternFingerprint(query, fpSize=PATTERN_BITS))


def contains(smiles: str, smarts: str) -> bool:
    """Whether `smiles` genuinely contains `smarts` — the exact verification after the screen.

    The screen is sound but not exact: it admits molecules whose bits happen to cover the query's
    without the substructure being present. This is what decides, and a stored structure that no
    longer parses answers `False` rather than raising, so one bad row cannot fail a whole search.
    """
    mol = Chem.MolFromSmiles(smiles)
    query = Chem.MolFromSmarts(smarts)
    if mol is None or query is None:
        return False
    return bool(mol.HasSubstructMatch(query))


def _indices(fingerprint: object) -> list[int]:
    """The ascending set-bit indices of an RDKit `ExplicitBitVect`."""
    return sorted(fingerprint.GetOnBits())  # type: ignore[attr-defined]
