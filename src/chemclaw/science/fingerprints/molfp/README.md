# `science/fingerprints/molfp` — molecular fingerprints and structural search

Deterministic ECFP4 fingerprinting (`fingerprint.py`) and search by Tanimoto similarity or
substructure (`search.py`), over the generic ranking and backends in `science/fingerprints/store.py`.
`substructure_index.py` is a pre-parsed, pattern-screened substructure index over one corpus slice,
cached across queries so a repeat search does not re-parse every molecule.

The MCP tools that advertise these functions are in `connectors/molfp/server` — that pair is one of
the two `ARCHITECTURE.md` calls out as looking like a duplicate and not being one.
