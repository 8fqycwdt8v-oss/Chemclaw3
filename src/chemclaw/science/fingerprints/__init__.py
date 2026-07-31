"""Structural fingerprinting and Tanimoto search — the engine behind the `molfp`/`rxnfp` bundles.

Pure computation, in `science/` for the same reason `calc`, `bo` and `safety` are: it imports no
Temporal, no MCP and no FastAPI, so it is importable and testable without an orchestration stack.
`store` is the domain-neutral ranking and its backends (in-memory and Postgres/pgvector); `molfp`
and `rxnfp` are the two domain halves that define what a bit-vector *means* for a molecule and for
a reaction.

Until D-155 this lived in `chemclaw.mcp`, one directory away from `connectors/molfp`, which made
the pair read as a duplication — the very thing `science/` vs `connectors/` exists to distinguish.
The `FastMCP` instances that advertise these functions as tools are now in their bundles, at
`connectors/{molfp,rxnfp}/server/tools.py`, where every other bundle keeps its tool surface.

Capability, not judgment: this package *computes* a similarity; whether a similarity counts as
precedent is the `reaction-search` skill's call (gate G6).
"""
