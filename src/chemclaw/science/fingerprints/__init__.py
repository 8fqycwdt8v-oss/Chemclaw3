"""Structural fingerprinting and Tanimoto search — the engine behind the `molfp`/`rxnfp` bundles.

Pure computation with no Temporal, MCP or FastAPI imports. `store` is the domain-neutral ranking and
its backends (in-memory and Postgres/pgvector); `molfp` and `rxnfp` define what a bit-vector means
for a molecule and for a reaction. The tool surfaces live in
`connectors/{molfp,rxnfp}/server/tools.py`. This package computes similarity; whether a match counts
as precedent is the `reaction-search` skill's call.
"""
