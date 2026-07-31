"""Shared cheminformatics helpers: SMILES canonicalization for identity keys.

`canonical_smiles` is the single structure-normalizing key used wherever two
spellings of the same molecule must collapse to one string — compound identity in
the fingerprint index (ingestion), product↔reactant matching (chain detection),
and every calculation cache / workflow-dedup key (D-011: compute once, never
twice). It lived in `chemclaw.ingest.eln.chem` when only the ELN used it; it moved here once the
compute cache and QM workflow needed the same guarantee, so the canonicalization
that decides "same molecule" exists in exactly one place (DRY).
"""

from rdkit import Chem

from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash


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
    return f"compound-{stable_hash(require_canonical_smiles(smiles), chars=12)}"
