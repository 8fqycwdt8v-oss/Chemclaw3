"""The `rxnfp` bundle's MCP tool surface (plan step 3.4).

Declaration, not logic: each function delegates to `chemclaw.science.fingerprints.rxnfp` over the
production (Postgres) reaction table, and what this file contributes is the `@server.tool()`
decoration the agent sees. `app.py` serves it over HTTP; `main()` runs the same tools over stdio for
running the capability by hand. Judgment stays out (G6) — see the `molfp` twin for the full note.
"""

from mcp.server.fastmcp import FastMCP

from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions, record_for_reaction
from chemclaw.science.fingerprints.store import (
    FingerprintSearch,
    FingerprintStore,
    Match,
    default_reaction_store,
    log_index_size,
)

server = FastMCP("mcp-rxnfp")
_store: FingerprintStore = default_reaction_store()


@server.tool()
async def similar_reactions(
    reaction_smiles: str, top_k: int | None = None, threshold: float | None = None
) -> FingerprintSearch[Match]:
    """Find stored reactions similar to `reaction_smiles`, most similar first.

    Each hit's `id` is the reaction's **note id**, so it can be passed straight to `expand_note`
    for the full recipe. `top_k` and `threshold` (Tanimoto floor) default to the configured values.

    **Read `verdict` before answering.** Empty `hits` with `index_empty: true` means no reaction
    has been indexed and the question was not answered — never report it as "we have no precedent".
    """
    search = await find_similar_reactions(_store, reaction_smiles, top_k, threshold)
    return search.model_copy(
        update={
            "hits": [
                match.model_copy(update={"id": note_id_for_reaction(match.id)})
                for match in search.hits
            ]
        }
    )


async def report_index_size() -> None:
    """Log this connector's index size at startup (see the `molfp` twin for the full note)."""
    await log_index_size(_store, "reaction")


@server.tool()
async def index_reaction(record_id: str, reaction_smiles: str) -> str:
    """Add or replace a reaction in the fingerprint index; return its id."""
    await _store.add(record_for_reaction(record_id, reaction_smiles))
    return record_id


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
