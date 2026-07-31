"""Shared cheminformatics helpers: the one definition of "the same molecule".

`canonical_smiles` is the single structure-normalizing key used wherever two spellings of one
molecule must collapse to one string — compound identity in the fingerprint index (ingestion),
product↔reactant matching (chain detection), and every calculation cache / workflow-dedup key
(D-011: compute once, never twice). It lived in `chemclaw.ingest.eln.chem` when only the ELN used
it; it moved here once the compute cache and the QM workflow needed the same guarantee, so the
canonicalization that decides "same molecule" exists in exactly one place (DRY).

**RDKit's canonical SMILES is not that definition, and used to be treated as one.** It normalizes
*spelling* — atom ordering, aromaticity perception, ring closures — and nothing else. So a free
base and its hydrochloride, a carboxylate and its sodium salt, and two tautomers of one compound
each produced a different string, and therefore a different `compound_id`, a different calculation
cache key and a different fingerprint row. That is the classic cheminformatics production failure:
identity fragments, the cache misses work D-011 promises never to repeat, and similarity search
answers about a molecule the chemist thinks it has already seen.

`standardize` is the missing step. The pipeline is deliberately the conventional one, in the
conventional order, because a bespoke normalization is a bespoke notion of sameness:

1. `Cleanup` — sanitize, disconnect metals, normalize functional-group spellings (nitro, N-oxide).
2. `FragmentParent` — keep the largest organic fragment, which strips counterions and solvates.
3. `Uncharger` — neutralize what can be neutralized, so a carboxylate meets its acid.
4. `TautomerEnumerator.Canonicalize` — one representative per tautomer set.

**There are two questions here, and conflating them is how this goes wrong in the other
direction.** Applying the pipeline everywhere neutralizes species a chemist meant as ions, and a
calculation submitted for acetate must not silently compute acetic acid — the test suite says so
directly, because `Structure` validates a declared charge against its SMILES. So:

- `canonical_smiles` / `require_canonical_smiles` answer **"is this the same structure?"** —
  spelling only. They key the calculation cache, the QM workflow-dedup id and the prediction
  ledger, where an anion is a different calculation from its conjugate acid and must stay one.
- `standard_smiles` / `require_standard_smiles` answer **"is this the same compound?"** — the full
  pipeline. They key `compound_id`, the fingerprint index, product↔reactant matching in
  `memory.chains` and species grouping in `memory.progression`, where a hydrochloride and its free
  base are one substance and separate rows for them are the fragmentation this fixes.

Two names rather than a flag, so a caller cannot pick the wrong one by leaving an argument out, and
each name says which question it answers.
"""

from functools import lru_cache

from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash

# Bumped whenever the pipeline below changes what it collapses. It is folded into the fingerprint
# `definition` strings, so rows indexed under an older notion of sameness fall out of similarity
# search rather than being silently compared against rows built under a newer one — the guard
# `science/fingerprints/store.py` already applies to a changed radius or bit width, extended to the
# other thing that decides what a row *is*.
STANDARDIZATION_VERSION = "std1"

# One `TautomerEnumerator` for the process. Constructing it parses its transform catalogue, which
# is not free, and this runs per component per ingested reaction.
_TAUTOMERS = rdMolStandardize.TautomerEnumerator()


@lru_cache(maxsize=4096)
def _standardized(smiles: str) -> str | None:
    """The standardized canonical SMILES of `smiles`, or None when it does not parse.

    Cached because the pipeline is materially more expensive than a parse — tautomer
    canonicalization enumerates a transform set — and because the callers are loops: every
    component of every ingested reaction, and every product/reactant pair in chain detection. Pure
    in its argument, so the cache is sound; bounded, so a long-lived worker cannot grow into it.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return str(Chem.MolToSmiles(standardize(mol)))


def standardize(mol: Chem.Mol) -> Chem.Mol:
    """Apply the standardization pipeline to a parsed molecule (see the module docstring).

    Separate from the SMILES helpers so a caller that already holds a molecule — and a test that
    wants to check one stage — does not have to round-trip through a string.
    """
    mol = rdMolStandardize.Cleanup(mol)
    mol = rdMolStandardize.FragmentParent(mol)
    mol = rdMolStandardize.Uncharger().uncharge(mol)
    return _TAUTOMERS.Canonicalize(mol)


class InvalidSmilesError(ChemclawError):
    """A SMILES string that RDKit cannot parse.

    A `ChemclawError`, so a batch boundary catches it as bad data and the Temporal
    retry policy treats it as a fast, non-retryable failure (never a retry loop).
    """


def canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, or the input unchanged if it does not parse.

    A stable, structure-normalized key: two spellings of the same molecule collapse
    to one string, so it is the natural compound id and the product↔reactant match
    key. Lenient by design — the ELN/memory callers key on whatever string they are
    given and never want ingestion to abort on one odd label. Where an unparseable
    structure must instead be rejected, use `require_canonical_smiles`.
    """
    mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else smiles


def require_canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, raising `InvalidSmilesError` if it does not parse.

    Use where an unparseable molecule must not silently pass and where the key must
    not distinguish two spellings of one molecule: the calculation cache keys and
    the QM durable boundary (G4). Canonicalizing before the key means `"CCO"` and
    `"OCC"` share one cache entry / one workflow id, honoring D-011.

    Stricter than RDKit's parser, which would silently truncate `"CCO junk"` at the
    first whitespace (keying a different molecule than the caller submitted) and
    parse `""` to a zero-atom molecule — both are rejected here, since a key for
    the wrong or empty structure is worse than a fast failure at the boundary.
    """
    stripped = smiles.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid SMILES (empty or contains whitespace): {smiles!r}")
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return str(Chem.MolToSmiles(mol))


def standard_smiles(smiles: str) -> str:
    """The **standardized** canonical SMILES, or the input unchanged if it does not parse.

    "Is this the same compound?" — salts stripped, charges neutralized where they can be, one
    tautomer per set. Use it wherever two spellings of one *substance* must reach one record;
    use `canonical_smiles` where an anion is genuinely a different thing to compute.

    Lenient about parse failure for the same reason `canonical_smiles` is: the ELN and memory
    callers key on whatever string they are given, and one odd label must not abort ingestion.
    """
    standardized = _standardized(smiles)
    return standardized if standardized is not None else smiles


def require_standard_smiles(smiles: str) -> str:
    """The standardized canonical SMILES, raising `InvalidSmilesError` if it does not parse.

    The strict counterpart of `standard_smiles`, sharing `require_canonical_smiles`'s rejection of
    the two inputs RDKit would otherwise accept quietly — a string with embedded whitespace (which
    it truncates at the first space, keying a different molecule than the caller submitted) and the
    empty string (which parses to a zero-atom molecule).
    """
    stripped = smiles.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise InvalidSmilesError(f"invalid SMILES (empty or contains whitespace): {smiles!r}")
    mol = Chem.MolFromSmiles(stripped)
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    standardized = _standardized(stripped)
    if standardized is None:  # pragma: no cover - unreachable once the parse above succeeded
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return standardized


def substructure_pattern(query: str) -> Chem.Mol:
    """Compile a substructure query — SMARTS first, then SMILES — or raise `InvalidSmilesError`.

    SMARTS first because every SMILES is also valid SMARTS but not the other way round, and a
    chemist asking for "a carbonyl next to anything aromatic" can only say it in SMARTS. Falling
    back to SMILES is what lets a plain fragment (`"c1ccccc1"`) work without the caller knowing
    which language they typed.

    A zero-atom pattern is rejected rather than run: RDKit matches it against every molecule, so
    the answer to a query that said nothing would be "everything", which reads as a finding.

    Here rather than beside one caller because two subsystems now filter by structure — the
    fingerprint index's substructure search and the calibration ledger's outlier listing — and a
    second copy of "SMARTS or SMILES, and reject the empty one" is exactly the kind of chemistry
    rule that drifts apart unnoticed.
    """
    pattern = Chem.MolFromSmarts(query) or Chem.MolFromSmiles(query)
    if pattern is None:
        raise InvalidSmilesError(f"unparseable substructure query: {query!r}")
    if pattern.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"empty substructure query (no atoms): {query!r}")
    return pattern


def compound_id(smiles: str) -> str:
    """The stable knowledge-graph note id for a molecule, derived from its structure.

    Structure-derived rather than name-derived, so two sources that spell the same molecule
    differently still reach one note — the property that makes a citation from a fingerprint
    hit meaningful at all.

    Lives here, beside the canonicalization it is built on, because the callers span layers
    that share nothing else: the ingest/kg side that *writes* the note
    (`chemclaw.ingest.eln.compound`) and the fingerprint connectors that *cite* it
    (`chemclaw.science.fingerprints.molfp.search`, `chemclaw.connectors.qm.knowledge`). A connector
    must not import the knowledge graph (D-115), and the id is a pure function of the structure — no
    graph needed to derive it, only to confirm the note has been merged.
    """
    return f"compound-{stable_hash(require_standard_smiles(smiles), chars=12)}"
