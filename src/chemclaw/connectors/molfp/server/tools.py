"""The `molfp` bundle's MCP tool surface.

Declaration, not logic: every function delegates to `chemclaw.science.fingerprints.molfp` over the
production molecule table, and adds the citation-only ELN records outside that index. What this
file owns is the tool names, defaults and docstrings the agent sees. `app.py` serves it over HTTP;
`main()` runs it over stdio for running one capability by hand.

Judgment stays out: when a similarity counts as precedent is the `reaction-search` skill's call.
"""

from mcp.server.fastmcp import FastMCP

from chemclaw.ingest.eln.records import ReactionRecordStore, default_record_store
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
# The transcription store, for what this index cannot hold: molecules of citation-only ELN records
# are never fingerprinted, so every result says how many such records exist and which list the
# queried structure.
_records: ReactionRecordStore = default_record_store()


@server.tool()
async def similar_molecules(
    smiles: str, top_k: int | None = None, threshold: float | None = None
) -> FingerprintSearch[MoleculeHit]:
    """Find stored molecules structurally similar to `smiles`, most similar first.

    Each hit carries `compound_note_id` — cite that note rather than searching for the SMILES.
    `top_k` and `threshold` (Tanimoto floor) default to the configured values.

    **Read `verdict` before answering.** Empty `hits` with `index_empty: true` means the index
    holds nothing and the question was not answered — it is not a finding of novelty. And
    `hits_truncated: true` means more molecules cleared the threshold than `top_k` could return,
    so the count is a lower bound: raise `top_k` before saying how many analogs exist.
    And `approximate: true` means this deployment searched the index approximately —
    the neighbours returned are the best it proposed, not provably the best on file, so
    a closer one may exist and the list is not a definitive set of precedents. The two
    are independent: a result can be both, and `verdict` says so when it is.
    """
    search = await find_similar_molecules(_store, smiles, top_k, threshold)
    return search.model_copy(update={"unsearched": await _records.citation_only(smiles)})


@server.tool()
async def substructure_matches(query: str) -> FingerprintSearch[MoleculeHit]:
    """Return stored molecules containing the `query` fragment (SMARTS or SMILES).

    Each hit carries `compound_note_id` — cite that note rather than searching for the SMILES.

    **Read `verdict` before answering.** Empty `hits` with `index_empty: true` means the index
    holds nothing and the question was not answered — not that no molecule bears the fragment.
    `scan_truncated: true` means only part of the corpus was examined, so an empty result is
    inconclusive; `hits_truncated: true` means the hit count is a lower bound, not a total.
    """
    search = await find_substructure_matches(_store, query)
    return search.model_copy(update={"unsearched": await _records.citation_only(query)})


async def report_index_size() -> None:
    """Log this connector's index size at startup, so an empty index is visible on the first line.

    Wired into the app's lifespan; lives here because `_store` is this module's.
    """
    await log_index_size(_store, "molecule")


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
