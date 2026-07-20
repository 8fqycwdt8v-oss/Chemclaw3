"""mcp-rxnfp: the FastMCP server exposing the reaction capability (plan step 3.4).

A thin transport wrapper over the production (Postgres) reaction table — all logic lives
in `fingerprint`/`search` and the generic `fpstore`. Run as
`python -m mcp_servers.rxnfp.server` (stdio transport). Judgment stays out (G6).
"""

from mcp.server.fastmcp import FastMCP

from chemclaw.config import settings
from mcp_servers.fpstore import FingerprintStore, Match, PostgresFingerprintStore
from mcp_servers.rxnfp.search import find_similar_reactions, record_for_reaction

server = FastMCP("mcp-rxnfp")
_store: FingerprintStore = PostgresFingerprintStore("reaction_fingerprints", settings.drfp_bits)


@server.tool()
async def similar_reactions(
    reaction_smiles: str, top_k: int | None = None, threshold: float | None = None
) -> list[Match]:
    """Find stored reactions similar to `reaction_smiles`, most similar first.

    `top_k` and `threshold` (Tanimoto floor) default to the configured values.
    """
    return await find_similar_reactions(_store, reaction_smiles, top_k, threshold)


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
