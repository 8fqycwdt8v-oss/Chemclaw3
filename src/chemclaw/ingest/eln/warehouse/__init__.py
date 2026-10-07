"""A SQL-warehouse ELN whose schema is declared in its manifest, not compiled into an adapter.

The data-source seam's two halves (`WarehouseElnAdapter`, `WarehouseVectorRetriever`) plus the
binding they execute. Nothing is imported eagerly, so a chat pod never loads the ingest mapper and
the suite runs without a warehouse client. See `README.md` beside this file for the binding's shape.
"""
