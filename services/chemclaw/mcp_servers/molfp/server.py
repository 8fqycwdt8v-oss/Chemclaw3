"""mcp-molfp: the FastMCP server exposing the molecule capability (plan step 3.1).

A thin transport wrapper — all logic lives in `fingerprint`/`search` and the generic
`fpstore`, so this file just advertises the capability as MCP tools over the production
(Postgres) molecule table. Run as `python -m mcp_servers.molfp.server` (stdio transport).
Judgment stays out: the tools compute and search; when a similarity counts as precedent
is the `reaction-search` skill's call (G6).
"""

from mcp.server.fastmcp import FastMCP

from mcp_servers.fpstore import FingerprintStore, Match, default_molecule_store
from mcp_servers.molfp.search import (
    SubstructureHit,
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)

server = FastMCP("mcp-molfp")
_store: FingerprintStore = default_molecule_store()


@server.tool()
async def similar_molecules(
    smiles: str, top_k: int | None = None, threshold: float | None = None
) -> list[Match]:
    """Find stored molecules structurally similar to `smiles`, most similar first.

    `top_k` and `threshold` (Tanimoto floor) default to the configured values.
    """
    return await find_similar_molecules(_store, smiles, top_k, threshold)


@server.tool()
async def substructure_matches(query: str) -> list[SubstructureHit]:
    """Return stored molecules containing the `query` fragment (SMARTS or SMILES)."""
    return await find_substructure_matches(_store, query)


@server.tool()
async def index_molecule(record_id: str, smiles: str) -> str:
    """Add or replace a molecule in the fingerprint index; return its id."""
    await _store.add(record_for(record_id, smiles))
    return record_id


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
