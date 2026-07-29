"""Getting external records in: the generic `DataSource` seam and the ELN adapters it hosts.

`ingest.sources` is the seam — a new source is one `sources/<name>/datasource.yaml` folder plus
its name in `CHEMCLAW_DATA_SOURCES`, with zero core edits (D-120). `ingest.eln` is the first
concrete family of adapters (free-text JSON exports and ORD), re-hosted behind that seam unchanged.
"""
