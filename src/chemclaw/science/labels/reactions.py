"""The corpus's reactions as DRFP bits — the half `molecules.py` does for structures.

`corpus_reactions` has the same columns as `reaction_fingerprints`, so `PostgresFingerprintStore`
serves it unchanged; this module is one constant and two functions.

A separate table because the two answer different questions and cite different things:
`reaction_fingerprints` is "have we run this?" resolving to a `reaction-<id>` transcription, this is
literature precedent. Merging would swamp `similar_reactions` with hits whose note id resolves to
nothing.
"""

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.store import PostgresFingerprintStore

CORPUS_REACTIONS_TABLE = "corpus_reactions"


def corpus_reactions() -> PostgresFingerprintStore:
    """Tanimoto search and writes over the corpus's reactions, on the class the ELN corpus uses.

    `source_keyed`, so a hit joins to `reaction_labels (source, reaction_id)` directly.
    """
    return PostgresFingerprintStore(
        CORPUS_REACTIONS_TABLE,
        settings.drfp_bits,
        reaction_definition(),
        source_keyed=True,
    )


def transformation_of(record_smiles: str) -> str:
    """`reactants>agents>products` as `reactants>>products`, or the input if it is not three-part.

    The agent slot is dropped before fingerprinting because `DrfpEncoder` folds agents onto the
    reactants, so the solvent would otherwise dominate similarity; this is what makes
    `reaction_definition()`'s `agents-excluded` token true of these rows. Agents stay queryable as
    `reaction_species` rows with their roles.

    A string that is not three-part is returned unchanged for `drfp_bitstring` to refuse.

    Species are not standardized here: the corpus tier keeps `record_smiles` verbatim so the label
    matches the row it came from. The bits are comparable anyway, because `drfp_bitstring`
    standardizes every species before folding.
    """
    fields = record_smiles.split(">")
    if len(fields) != 3:
        return record_smiles
    return f"{fields[0]}>>{fields[2]}"
