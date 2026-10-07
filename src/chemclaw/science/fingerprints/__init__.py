"""Structural fingerprinting and Tanimoto search — the engine behind the `molfp`/`rxnfp` bundles.

`store` is the domain-neutral ranking and backends; `molfp`/`rxnfp` define the bits for molecules
and reactions. Tool surfaces are in `connectors/{molfp,rxnfp}`; judging precedent is the
`reaction-search` skill's.
"""
