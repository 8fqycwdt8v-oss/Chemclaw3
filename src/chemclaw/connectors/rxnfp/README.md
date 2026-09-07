# `connectors/rxnfp` — reaction fingerprint similarity search

The reaction analogue of `connectors/molfp`, over DRFP rather than ECFP4. `connector.yaml` declares the
tools and `server/` advertises them; the engine is `science/fingerprints/rxnfp`, which shares the
generic ranking and backends in `science/fingerprints/store`.
