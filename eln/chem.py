"""Small shared cheminformatics helper for the ELN and memory layers.

`canonical_smiles` is the single canonicalization used both to key compounds in the
fingerprint index (ingestion) and to link a product of one reaction to a reactant of
another (chain detection). They *must* agree, so the function lives in one place (DRY).
"""

from rdkit import Chem


def canonical_smiles(smiles: str) -> str:
    """RDKit canonical SMILES, or the input unchanged if it does not parse.

    A stable, structure-normalized key: two spellings of the same molecule collapse to
    one string, so it is the natural compound id and the product↔reactant match key.
    """
    mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else smiles
