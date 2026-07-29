# Retrieval gold corpus (fixture — not live knowledge)

A small, fixed set of knowledge-graph notes the KM-13 retrieval metrics score against
(`evals/retrieval.py`). It is deliberately **not** under `knowledge_dir`: keeping it here makes the
retrieval recall/precision numbers reproducible and independent of whatever is in the live graph,
and keeps `kg-validate` (which scans `knowledge_dir`) from treating these fixtures as real notes.

Each file is a valid `kg.note.Note`. The paired gold queries and their expected source ids live in
`evals/cases/retrieval-*.md`. Edit the two together: a change here that moves what a query surfaces
must be reflected in the expected-source lists (the tests pin the resulting recall/precision).
