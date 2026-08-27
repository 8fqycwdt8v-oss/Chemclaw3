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

from collections.abc import Sequence

from rdkit import Chem
from rdkit.Chem import rdmolops

from chemclaw.core.chem import InvalidSmilesError
from chemclaw.core.config import settings
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


def query_bit_indices(query: Chem.Mol) -> list[int]:
    """The set-bit indices a compiled SMARTS query demands of any molecule that could match it.

    The query side of the screen. Takes the already-compiled query rather than the string, because
    the verify needs the same object and `compile_query` is where the parse and its bound live —
    two parses of one string is two chances for them to disagree about what the query was.

    An empty result is not an error and must not be treated as "matches nothing": a query so
    generic that it sets no bits screens nothing, and the caller falls back to verifying every
    candidate. Returning `[]` is how that is said.
    """
    return _indices(rdmolops.PatternFingerprint(query, fpSize=PATTERN_BITS))


def compile_query(smarts: str) -> Chem.Mol:
    """The SMARTS query, compiled once and bounded in length. Raises `FingerprintError`.

    **Compiled once because it is used twice, and because the verify is a loop.** The screen wants
    the query's pattern bits and the verify wants the query itself, and re-parsing the string per
    candidate row is ~28% of a scan that has up to `substructure_scan_max_records` rows in it.

    **Bounded because the string is written by the model.** SMARTS matching is subgraph
    isomorphism, so a pathological multi-KB pattern is priced in CPU rather than in characters —
    which is what `substructure_query_max_length` exists to stop, and this path was not reading it.
    (`molfp.search.find_substructure_matches` applies the same bound to the other substructure
    surface; the two are one setting and two sentences until one of the paths moves, at which point
    they are worth folding into `science/fingerprints`.)

    `MolFromSmarts` rather than `MolFromSmiles`, because a query is a pattern: `[c,n]1ccccc1` is
    not a molecule and parsing it as one silently yields `None`.
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

    The exact verification after the screen: the screen is sound but not exact — it admits
    molecules whose bits happen to cover the query's without the substructure being present — and
    this is what decides. A stored structure that no longer parses is dropped rather than raising,
    so one bad row cannot fail a whole search.

    **Blocking on purpose, and it must not be called on the event loop.** This is RDKit subgraph
    isomorphism once per candidate, and the pod's one loop serves every session's SSE stream, every
    in-flight turn and every bearer-token validation — measured at 300 rows, an in-line
    comprehension froze all of it for 465 ms, and the record cap is 5,000.
    `CorpusMolecules.containing` is the one caller and offloads it under
    `substructure_match_timeout_seconds`.
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
