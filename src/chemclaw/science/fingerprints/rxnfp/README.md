# `science/fingerprints/rxnfp` — reaction fingerprints and similarity search

The reaction analogue of `science/fingerprints/molfp`: deterministic DRFP fingerprinting (`fingerprint.py`) and
Tanimoto search (`search.py`), sharing the generic ranking and backends in `science/fingerprints/store.py`.

The tool surface over it is `connectors/rxnfp/server`. The capability computes a similarity; when a
reaction similarity counts as precedent is the `reaction-search` skill's call (gate G6).
