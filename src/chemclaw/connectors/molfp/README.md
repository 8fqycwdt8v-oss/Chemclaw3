# `connectors/molfp` — molecule fingerprint similarity and substructure search

The bundle half of the ECFP4 pair. The engine is `science/fingerprints/molfp` — pure computation,
importable with no orchestration stack — and this directory is its MCP wrapper: `connector.yaml`
declares the tools, `server/` advertises them.

`ARCHITECTURE.md` calls out `science/fingerprints` vs `connectors/molfp` as one of the two name
pairs that look like duplicates and are not. The engine computes a similarity; whether a similarity
counts as *precedent* is the `reaction-search` skill's call.
