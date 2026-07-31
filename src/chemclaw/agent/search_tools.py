"""Agent tools for structural (fingerprint) search (plan steps 3.3, 3.4).

Exposes the molecule/reaction fingerprint capabilities to the conversation agent so it can
answer "what related chemistry have we done?" by structure, not just by text: reaction
similarity (DRFP), molecule similarity (ECFP4), and substructure match (e.g. "reactions on a
substrate bearing a free primary amine"). The capability lives in `chemclaw.science.fingerprints`;
these are the thin agent-facing wrappers that inject the production stores and return compact
results.

Layer discipline (D-005/G6): these are read-only *capability* — the judgment on which search
to use, what Tanimoto counts as precedent, and how to combine hits lives in the
`reaction-search`/`deep-research` skills, not here. The store factories are module-level so a
test can swap them for in-memory stores without a database.

NOT registered on the live agent. The conversational agent reaches structural search over the
the `molfp`/`rxnfp` connector bundles (`similar_reactions`/`similar_molecules`/
`substructure_matches`), not these in-process wrappers — see `chemclaw.agent.chemclaw_agent`. These
exist as the credential-free, subprocess-free in-process seam for the examples and tests; the
MCP path is the production one. Keep the two in sync if the search surface changes.

The molecule half now stays in sync by construction: it re-exports the connector's own
`MoleculeHit` instead of defining a parallel model. The two had already drifted — the connector
grew `compound_note_id` for the citation and a copy here would have kept returning a bare SMILES.
"""

from pydantic import BaseModel

from chemclaw.science.fingerprints.molfp.search import MoleculeHit
from chemclaw.science.fingerprints.molfp.search import find_similar_molecules as _similar_molecules
from chemclaw.science.fingerprints.molfp.search import (
    find_substructure_matches as _substructure_matches,
)
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions as _similar_reactions
from chemclaw.science.fingerprints.store import default_molecule_store, default_reaction_store

__all__ = [
    "MoleculeHit",
    "ReactionHit",
    "find_similar_molecules",
    "find_similar_reactions",
    "find_substructure_matches",
]

# Module-level indirection so tests swap the production Postgres stores for in-memory ones
# (the same seam `workflows.eln_sync` uses).
_reaction_store = default_reaction_store
_molecule_store = default_molecule_store


class ReactionHit(BaseModel):
    """A reaction-similarity hit: the reaction's note id, its SMILES, and the Tanimoto score."""

    reaction_note_id: str
    reaction_smiles: str
    similarity: float


async def find_similar_reactions(
    reaction_smiles: str, top_k: int | None = None
) -> list[ReactionHit]:
    """Find past reactions structurally similar to a query reaction (DRFP Tanimoto).

    Use this to gather what has been tried for a transformation — each hit is a real,
    ingested reaction whose `reaction-<id>` note (retrievable with expand_note) holds the
    full recipe, conditions, and outcomes. Ranked most-similar first.

    Args:
        reaction_smiles: The query reaction as `reactants>>products` SMILES.
        top_k: How many neighbors to return (defaults to the configured value).

    Returns:
        Similar reactions with their note id, SMILES, and similarity (0–1).
    """
    matches = await _similar_reactions(_reaction_store(), reaction_smiles, top_k)
    return [
        ReactionHit(
            reaction_note_id=f"reaction-{m.id}", reaction_smiles=m.label, similarity=m.similarity
        )
        for m in matches
    ]


async def find_similar_molecules(smiles: str, top_k: int | None = None) -> list[MoleculeHit]:
    """Find molecules structurally similar to a query structure (ECFP4 Tanimoto).

    Use this for analogy across substrates ("have we handled a close analog of this
    compound?"). Ranked most-similar first.

    Args:
        smiles: The query molecule SMILES.
        top_k: How many neighbors to return (defaults to the configured value).

    Returns:
        Similar molecules with their compound note id, canonical SMILES and similarity (0–1).
        Cite the note id; it is `null` only for an indexed structure that no longer parses.
    """
    return await _similar_molecules(_molecule_store(), smiles, top_k)


async def find_substructure_matches(pattern: str) -> list[MoleculeHit]:
    """Find indexed molecules that contain a substructure (SMARTS, or a SMILES fragment).

    Use this for functional-group-conditioned questions ("what do we know when a boronic
    acid / a free primary amine is present?"): match the fragment, then expand each hit's
    compound note to reach the reactions that used it. Exact RDKit matching, not a
    similarity score.

    Args:
        pattern: The substructure query as SMARTS (a plain SMILES is also valid SMARTS).

    Returns:
        Matching molecules with their compound note id and canonical SMILES (no similarity).
    """
    return await _substructure_matches(_molecule_store(), pattern)
