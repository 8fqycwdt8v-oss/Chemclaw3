"""The `molfp` bundle's MCP tool surface (plan step 3.1).

Declaration, not logic: every function here is a one-line delegation to
`chemclaw.science.fingerprints.molfp`, over the production (Postgres) molecule table. What this file
adds is the `@server.tool()` decoration — the argument names, defaults and docstrings the agent
actually sees — which is why it belongs beside `app.py` in the bundle rather than beside the
engine.

`app.py` serves this `server` over HTTP, which is how the connector seam reaches it. The `main()`
below runs the same tools over stdio, the transport MCP defaults to, and is kept for running one
capability by hand without the FastAPI layer.

Judgment stays out: the tools compute and search; when a similarity counts as precedent is the
`reaction-search` skill's call (G6).
"""

from mcp.server.fastmcp import FastMCP

from chemclaw.science.fingerprints.molfp.search import (
    MoleculeHit,
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from chemclaw.science.fingerprints.store import FingerprintStore, default_molecule_store

server = FastMCP("mcp-molfp")
_store: FingerprintStore = default_molecule_store()


@server.tool()
async def similar_molecules(
    smiles: str, top_k: int | None = None, threshold: float | None = None
) -> list[MoleculeHit]:
    """Find stored molecules structurally similar to `smiles`, most similar first.

    Each hit carries `compound_note_id` — cite that note rather than searching for the SMILES.
    `top_k` and `threshold` (Tanimoto floor) default to the configured values.
    """
    return await find_similar_molecules(_store, smiles, top_k, threshold)


@server.tool()
async def substructure_matches(query: str) -> list[MoleculeHit]:
    """Return stored molecules containing the `query` fragment (SMARTS or SMILES).

    Each hit carries `compound_note_id` — cite that note rather than searching for the SMILES.
    """
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
