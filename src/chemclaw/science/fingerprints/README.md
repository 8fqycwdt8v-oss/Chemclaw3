# `science/fingerprints` — structural fingerprinting and Tanimoto search

The engine behind the `molfp` and `rxnfp` bundles. Pure computation: no Temporal, no MCP, no
FastAPI, so it is importable and testable without an orchestration stack.

- `store.py` — the domain-neutral ranking and its backends (in-memory, and Postgres/pgvector).
- `molfp/` — what a bit vector means for a *molecule* (ECFP4).
- `rxnfp/` — what one means for a *reaction* (DRFP).

This package keeps the name `science/` while being infrastructure by this repository's own rule, and
that is on the record: retrieval, memory and ELN ingest import it **in process**, which is what
makes it infrastructure rather than an exception. `science/safety` used to sit beside it on the same
grounds until the gate that made the claim true was retired.

Capability, not judgment: this package computes a similarity; whether a similarity counts as
precedent is the `reaction-search` skill's call (gate G6).
