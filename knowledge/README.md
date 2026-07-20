# `knowledge/` — Markdown knowledge graph

**Responsibility:** "what do we know" — the persistent knowledge graph as
interlinked Markdown notes in Git. Each note is front-matter (structured,
queryable) plus body, with `[[wikilinks]]` encoding real chemical relations. A
NetworkX indexer builds the graph from this directory; retrieval is **graph
traversal (1–2 hops), not top-k vector similarity** (D-004).

This directory holds **Markdown data, not Python**. Every `created_by: agent`
note enters via the PR-gate (human approves before merge — D-005). Git provides
versioning and the audit trail. See `docs/architektur.md` §4, §10.

Empty until Phase 2 defines the note schema and indexer (plan steps 2.1–2.3).
