# D-074 — Compared against Google's Open Knowledge Format (OKF v0.1): design reaffirmed, two follow-ups queued

**Context.** OKF (a git-native markdown format Google open-sourced: no cloud account/SDK, an
AI agent as the "wiki librarian" that keeps docs in sync, explicit `[[concept_path]]` links as
a deterministic graph instead of cosine-similarity RAG, a hybrid router splitting core/precise
truths from a wide RAG-searched archive) was checked against the knowledge-graph design already
built here.

**Finding.** D-004/D-005 independently arrived at the same three pillars: git-native Markdown
notes (no graph DB), agent-authored/updated content gated through a PR-gate rather than trusted
blind, and `[[wikilink]]`-driven `NetworkX` graph traversal in place of top-k vector similarity
(D-004's rationale predates and matches OKF's). The hybrid-router split (deterministic bundle for
core truths vs. RAG for wide/archival search) is already our shape too: the graph is the
default retrieval path (D-004), embeddings are only an optional entry point (D-062 hybrid
retrieval — RRF fusion over `vector`/`lexical`/graph, graph traversal stays the reasoning path).
No architecture change follows from this comparison.

**Two OKF conventions queued as backlog, not adopted here.** (1) OKF bundles keep a per-bundle
`log.md` audit trail; we currently only have PR/git history, no explicit per-note-type changelog
— worth a small addition. (2) OKF's format is deliberately untyped bare `[[links]]`; our
frontmatter `type` field is a string with no controlled vocabulary or class hierarchy, so an
agent cannot query by subsumption (e.g. "all electrophilic aromatic substitutions" matching a
`reaction_class: acetylation` note). Rather than building an in-house OWL/RDF ontology (no
second caller yet — KISS), the queued move is to anchor existing external ontology IDs (ChEBI
for compounds, RXNO for reaction classes) as additional frontmatter fields, reusing controlled
vocabularies instead of owning a schema. Both tracked in `BACKLOG.md` under "OKF-inspired
graph polish"; neither is scheduled against a phase yet.
