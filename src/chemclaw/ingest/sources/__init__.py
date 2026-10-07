"""The generic data-source attachment seam.

One `DataSource` contract composes `ElnAdapter` (fetch and map to an ORD reaction) and
`SourceRetriever` (evidence for a query), plus a config-driven registry. A source implements either
or both halves, so adding one is an adapter, a registry entry and a config token, with no edit to
the ingest loop or the evidence gatherer.
"""
