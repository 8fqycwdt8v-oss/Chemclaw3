"""Reaction fingerprint capability (plan step 3.4).

Deterministic DRFP reaction fingerprinting + Tanimoto search — the reaction analog of
`chemclaw.science.fingerprints.molfp`, sharing the generic ranking and backends in
`chemclaw.science.fingerprints.store`. The capability *computes*; when a reaction similarity counts
as precedent is the `reaction-search` skill's call (G6).
"""
