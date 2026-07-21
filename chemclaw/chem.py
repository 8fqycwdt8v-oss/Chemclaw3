"""Shared cheminformatics helpers: SMILES canonicalization for identity keys.

`canonical_smiles` is the single structure-normalizing key used wherever two
spellings of the same molecule must collapse to one string — compound identity in
the fingerprint index (ingestion), product↔reactant matching (chain detection),
and every calculation cache / workflow-dedup key (D-011: compute once, never
twice). It lived in `eln.chem` when only the ELN used it; it moved here once the
compute cache and QM workflow needed the same guarantee, so the canonicalization
that decides "same molecule" exists in exactly one place (DRY).
"""

from rdkit import Chem

from chemclaw.errors import ChemclawError


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
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise InvalidSmilesError(f"invalid SMILES: {smiles!r}")
    return str(Chem.MolToSmiles(mol))
