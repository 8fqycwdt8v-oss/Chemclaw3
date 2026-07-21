"""The agent's cross-source evidence gatherer (plan Phase 5b, generalized).

`gather_evidence` is the one tool that sweeps **every** internal source behind the report
harness's `SourceRetriever` contract and returns cited evidence in a single call — the
substrate for open-ended research questions ("what has been tried / what were the levers /
what matters when a certain group is present"). It is deliberately source-agnostic: today it
unions the knowledge graph (every note type — reactions, campaigns, optimization campaigns,
playbooks, reports) with reaction-fingerprint search; adding a source later (analytics,
external literature) is one more retriever in `_text_retrievers`, not a change here or to the
agent. Every returned chunk carries the id of the note it came from, so the agent can cite it
and `expand_note` for the full recipe/conditions/outcomes.

The judgment — decomposing the question, deciding which anchor to search on, separating
evidenced fact from transferred analogy, and drafting new protocols — lives in the
`deep-research` skill, not here. This tool only gathers.
"""

from agents.framing import frame_untrusted
from chemclaw.config import settings
from mcp_servers.fpstore import default_reaction_store
from report.evidence import EvidenceChunk, SourceRetriever
from report.retrievers import FingerprintReactionRetriever, GraphRetriever

# Test seam: swap the production reaction store for an in-memory one without a database.
_reaction_store = default_reaction_store


def _text_retrievers() -> list[SourceRetriever]:
    """Sources keyed by a free-text query. Extend this list to add a data source (G6)."""
    return [GraphRetriever()]


async def gather_evidence(
    query: str,
    reaction_smiles: str | None = None,
    note_type: str | None = None,
    tag: str | None = None,
) -> list[EvidenceChunk]:
    """Gather cited evidence for a research question from every internal source at once.

    Runs each text source (the knowledge graph, and any future literature/analytics source)
    on `query`, and — when an anchor reaction is given — also pulls structurally similar past
    reactions (DRFP). Results are merged and de-duplicated. Empty is a valid answer (nothing
    on file), never invented.

    Args:
        query: The natural-language question or key terms (matched over note id/tags/body).
        reaction_smiles: Optional `reactants>>products` anchor to also pull similar reactions.
        note_type: Optional graph filter, e.g. "reaction", "optimization-campaign", "playbook".
        tag: Optional graph tag filter (e.g. a project name).

    Returns:
        Evidence chunks, each with its content, the `source_note_id` to cite/expand, and which
        retriever found it. Capped at the configured budget so a broad sweep does not flood the
        context; if you hit the cap, narrow the query (a `note_type`/`tag` filter) rather than
        assume you have seen everything.
    """
    filters: dict[str, str] = {}
    if note_type is not None:
        filters["type"] = note_type
    if tag is not None:
        filters["tag"] = tag

    chunks: list[EvidenceChunk] = []
    for retriever in _text_retrievers():
        chunks.extend(await retriever.retrieve(query, filters))
    if reaction_smiles is not None:
        reaction_retriever = FingerprintReactionRetriever(_reaction_store())
        chunks.extend(await reaction_retriever.retrieve(reaction_smiles, {}))

    seen: set[tuple[str, str]] = set()
    unique: list[EvidenceChunk] = []
    for chunk in chunks:
        key = (chunk.source_note_id, chunk.content)
        if key not in seen:
            seen.add(key)
            unique.append(chunk)
    # Frame each chunk's content as retrieved data before it enters the model context, so a
    # note body carrying adversarial text is read as evidence to cite, not an instruction.
    return [
        chunk.model_copy(
            update={"content": frame_untrusted(chunk.content, note_id=chunk.source_note_id)}
        )
        for chunk in unique[: settings.gather_evidence_max_chunks]
    ]
