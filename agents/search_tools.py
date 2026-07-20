"""Agent tools for structural (fingerprint) search (plan steps 3.3, 3.4).

Exposes the molecule/reaction fingerprint capabilities to the conversation agent so it can
answer "what related chemistry have we done?" by structure, not just by text: reaction
similarity (DRFP), molecule similarity (ECFP4), and substructure match (e.g. "reactions on a
substrate bearing a free primary amine"). The capability lives in `mcp_servers`; these are the
thin agent-facing wrappers that inject the production stores and return compact results.

Layer discipline (D-005/G6): these are read-only *capability* — the judgment on which search
to use, what Tanimoto counts as precedent, and how to combine hits lives in the
`reaction-search`/`deep-research` skills, not here. The store factories are module-level so a
test can swap them for in-memory stores without a database.
"""

from pydantic import BaseModel

from mcp_servers.fpstore import default_molecule_store, default_reaction_store
from mcp_servers.molfp.search import find_similar_molecules as _similar_molecules
from mcp_servers.molfp.search import find_substructure_matches as _substructure_matches
from mcp_servers.rxnfp.search import find_similar_reactions as _similar_reactions

# Module-level indirection so tests swap the production Postgres stores for in-memory ones
# (the same seam `workflows.eln_sync` uses).
_reaction_store = default_reaction_store
_molecule_store = default_molecule_store


class ReactionHit(BaseModel):
    """A reaction-similarity hit: the reaction's note id, its SMILES, and the Tanimoto score."""

    reaction_note_id: str
    reaction_smiles: str
    similarity: float


class MoleculeHit(BaseModel):
    """A molecule-search hit: the canonical SMILES and (for similarity) the Tanimoto score."""

    smiles: str
    similarity: float | None = None


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
        Similar molecules with their canonical SMILES and similarity (0–1).
    """
    matches = await _similar_molecules(_molecule_store(), smiles, top_k)
    return [MoleculeHit(smiles=m.label, similarity=m.similarity) for m in matches]


async def find_substructure_matches(pattern: str) -> list[MoleculeHit]:
    """Find indexed molecules that contain a substructure (SMARTS, or a SMILES fragment).

    Use this for functional-group-conditioned questions ("what do we know when a boronic
    acid / a free primary amine is present?"): match the fragment, then bridge to the
    reactions using those molecules with find_notes on each SMILES. Exact RDKit matching,
    not a similarity score.

    Args:
        pattern: The substructure query as SMARTS (a plain SMILES is also valid SMARTS).

    Returns:
        Matching molecules (canonical SMILES; no similarity score).
    """
    records = await _substructure_matches(_molecule_store(), pattern)
    return [MoleculeHit(smiles=r.label) for r in records]
