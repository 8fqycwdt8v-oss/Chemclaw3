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
)
from chemclaw.science.fingerprints.store import (
    FingerprintSearch,
    FingerprintStore,
    default_molecule_store,
    log_index_size,
)

server = FastMCP("mcp-molfp")
_store: FingerprintStore = default_molecule_store()


@server.tool()
async def similar_molecules(
    smiles: str, top_k: int | None = None, threshold: float | None = None
) -> FingerprintSearch[MoleculeHit]:
    """Find stored molecules structurally similar to `smiles`, most similar first.

    Each hit carries `compound_note_id` — cite that note rather than searching for the SMILES.
    `top_k` and `threshold` (Tanimoto floor) default to the configured values.

    **Read `verdict` before answering.** Empty `hits` with `index_empty: true` means the index
    holds nothing and the question was not answered — it is not a finding of novelty.
    """
    return await find_similar_molecules(_store, smiles, top_k, threshold)


@server.tool()
async def substructure_matches(query: str) -> FingerprintSearch[MoleculeHit]:
    """Return stored molecules containing the `query` fragment (SMARTS or SMILES).

    Each hit carries `compound_note_id` — cite that note rather than searching for the SMILES.

    **Read `verdict` before answering.** Empty `hits` with `index_empty: true` means the index
    holds nothing and the question was not answered — not that no molecule bears the fragment.
    `scan_truncated: true` means only part of the corpus was examined, so an empty result is
    inconclusive; `hits_truncated: true` means the hit count is a lower bound, not a total.
    """
    return await find_substructure_matches(_store, query)


async def report_index_size() -> None:
    """Log this connector's index size at startup — the operator half of the empty-index defect.

    Wired into the app's lifespan (`src/chemclaw/connectors/molfp/server/app.py`), so the pod
    that owns `molecule_fingerprints` says on its first line whether it has anything to search.
    Lives here rather than in the app module because `_store` is this module's, and the transport
    layer has no business reaching for it.
    """
    await log_index_size(_store, "molecule")


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
