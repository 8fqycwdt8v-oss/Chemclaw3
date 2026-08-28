"""The corpus's reactions as DRFP bits — the half `molecules.py` does for structures.

`corpus_reactions` carries the same columns `reaction_fingerprints` does, so similarity needs no
code at all: `PostgresFingerprintStore` is already table-parameterised and `corpus_reactions()` just
points it at the other table. Unlike `CorpusMolecules` there is no extra column and no second search
shape, so there is no class here either — one constant and two functions is the whole module.

**Why a second table rather than more rows in `reaction_fingerprints`.** Not the key: `063` gave
that table a `source` column and an `(source, id)` primary key
(`D-2026-08-27-a-fingerprint-is-keyed-by-its-source`), so it tells two sources apart and a shared
entry id collides with nothing. What is left is the argument `molecules.py` makes, untouched by that
fix: the two answer different questions and cite different things. `reaction_fingerprints` is "have
we run this?" and its hits resolve to a `reaction-<id>` transcription; this is "is there literature
precedent?". Merging them would swamp `similar_reactions` by four orders of magnitude with hits
whose note id resolves to nothing.
"""

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.store import PostgresFingerprintStore

CORPUS_REACTIONS_TABLE = "corpus_reactions"


def corpus_reactions() -> PostgresFingerprintStore:
    """Tanimoto search and writes over the corpus's reactions, on the class the ELN corpus uses.

    `source_keyed`, like `default_reaction_store()`: a corpus row is identified by the registry
    source *and* the source's own reaction id, which is what lets a hit join to
    `reaction_labels (source, reaction_id)` without composing or decomposing a string.
    """
    return PostgresFingerprintStore(
        CORPUS_REACTIONS_TABLE,
        settings.drfp_bits,
        reaction_definition(),
        source_keyed=True,
    )


def transformation_of(record_smiles: str) -> str:
    """`reactants>agents>products` as `reactants>>products`, or the input if it is not three-part.

    **The agent slot is dropped before the bits are taken, and that is the whole point of this
    function.** `DrfpEncoder` folds agents back onto the reactants (`sides[0] += "." + sides[1]`),
    so a fingerprint built from the three-part form encodes the solvent as part of the
    transformation: measured on the ELN path, one coupling in THF against the same coupling in
    2-MeTHF scored 0.82, and 1.00 once the agents were excluded (`ingest/eln/ord.py`). It is also
    what makes `reaction_definition()`'s `agents-excluded` token true of these rows rather than a
    claim about them.

    Nothing is lost by the drop: every agent is a row in `reaction_species` carrying its role, which
    is the index built to answer *which solvent, which ligand, which base*.

    A string that does not split into exactly three fields is returned unchanged, for the reason
    `_standardize_species` gives one field over: it is not a reaction SMILES this module's callers
    write, and `drfp_bitstring` is what should refuse it rather than a guess here about how to
    parse it.

    **The species are not standardized here, and that is not an inconsistency with
    `reaction_fingerprints`.** That table's label comes from `OrdReaction.transformation_smiles`,
    which standardizes because the ELN tier standardizes everything it stores; the corpus tier
    deliberately keeps `record_smiles` verbatim ("what is displayed should be what was recorded"),
    and a label that disagreed with the row it came from would be worse than one that does not
    match the other table's spelling. The *bits* are unaffected either way: `drfp_bitstring`
    standardizes every species itself before folding, which is what makes rows in the two tables
    comparable at all.
    """
    fields = record_smiles.split(">")
    if len(fields) != 3:
        return record_smiles
    return f"{fields[0]}>>{fields[2]}"
