"""The generic data-source attachment seam (plan Phase F7).

One documented `DataSource` contract unifies the two half-contracts the system already had —
`ElnAdapter` (ingest: fetch + map to the canonical ORD reaction) and `SourceRetriever` (retrieve:
evidence for a query) — plus a config-driven registry. A source implements either or both halves;
the seam is the *composition* and the *registry*, not new DTOs (the existing `RawEntry`/
`OrdReaction`/`EvidenceChunk` types are reused verbatim). Adding a new source — the first live one
being a custom Snowflake ELN connector — becomes one adapter + one registry entry + one config
token, with zero edits to the ingest loop or the evidence gatherer.
"""
