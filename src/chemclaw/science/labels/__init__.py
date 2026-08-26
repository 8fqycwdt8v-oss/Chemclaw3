"""The reaction-label index: the derived, queryable view every precedent question is asked of.

A reaction, in this tree, was three things and none of them queryable by facet: one `OrdReaction`
value in flight, one `reaction_fingerprints` row holding DRFP bits and nothing else, and one
transcription — a `reaction_records` row, and a PR-gated Markdown note before that. That is
enough for "have we run a transformation like this", which is what DRFP answers, and it is enough
for nothing else. "Which ligands did we use for Buchwald couplings",
"what conditions worked for a product like this", "how do we work this up" are all *facet*
questions — they need per-species roles, a named reaction, conditions in columns and a substructure
index — and `skills/reaction-search/SKILL.md` already records what happens when you ask them of a
whole-reaction fingerprint instead: a query built from the two core substrates scored 0.24 against
a real, same-class precedent whose recipe the corpus had encoded as reactants.

So this package is the second index, and it is deliberately *derived*: nothing here is the record
of truth, everything here is rebuildable from the note or the source table it cites, and every
answer carries a `CorpusCoverage` saying what fraction of its own scope has actually been labelled.

* `vocabulary` — `SpeciesRole`, the derived role vocabulary, and why widening `Role` was rejected.
* `records` — the two-phase row, and why the record phase cannot be derived from the fingerprint.
* `policy` — the `labels:` manifest block: what a source carries, what must be derived anyway.
* `store` — the index itself, in-memory and Postgres, plus `CorpusCoverage`.

Pure computation and persistence, in `science/` for the same reason `fingerprints` is: it imports
no Temporal, no MCP and no FastAPI, and a connector bundle may import it where it may not import
`ingest/`. The I/O halves — the MCP labeller client, the drains, the corpus reader — live in
`chemclaw.ingest.labels` for exactly that reason.
"""
