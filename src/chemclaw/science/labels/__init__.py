"""The reaction-label index: the derived, queryable view every precedent question is asked of.

DRFP answers "have we run a transformation like this"; facet questions ("which ligands for Buchwald
couplings", "how do we work this up") need per-species roles, a named reaction, conditions in
columns and a substructure index. This package is that second index. It is derived: everything here
is rebuildable from the note or source table it cites, and every answer carries a `CorpusCoverage`
saying what fraction of its scope is labelled.

* `vocabulary` — `SpeciesRole` and the derived role vocabulary.
* `records` — the two-phase row.
* `policy` — the `labels:` manifest block.
* `store` — the index itself, in-memory and Postgres, plus `CorpusCoverage`.

Pure computation and persistence, importing no Temporal, MCP or FastAPI, so a connector bundle may
import it; the I/O halves live in `chemclaw.ingest.labels`.
"""
