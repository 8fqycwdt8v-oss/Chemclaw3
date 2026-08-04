"""A SQL-warehouse ELN whose schema is declared in its manifest, not compiled into an adapter.

The two halves the data-source seam expects — `WarehouseElnAdapter` and `WarehouseVectorRetriever` —
plus the binding they execute. Nothing here is imported eagerly: the seam resolves a half's
`module:callable` only in the process that uses it, so a chat pod never loads the ingest mapper and
a repository with no warehouse client installed still runs the whole suite.

See `README.md` beside this file for the shape of a binding and how to attach one.
"""
