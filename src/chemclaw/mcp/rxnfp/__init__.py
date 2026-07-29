"""mcp-rxnfp: reaction fingerprint capability (plan step 3.4).

Deterministic DRFP reaction fingerprinting + Tanimoto search — the reaction analog of
`chemclaw.mcp.molfp`, sharing the generic `chemclaw.mcp.fpstore` ranking and backends. The
capability *computes*; when a reaction similarity counts as precedent is the
`reaction-search` skill's call (G6).
"""
