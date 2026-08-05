---
name: knowledge-graph-query
description: >-
  Judgment for answering a question from the Markdown knowledge graph by
  traversing note links (1–2 hops), not by top-k vector similarity.
tools:
  - find_notes
  - expand_note
---

# Knowledge-graph query

Holds the *judgment* for reading the knowledge graph. The capability is
`kg.graph` (build the graph, expand a note's neighborhood); this skill decides how
to use it and how to weigh what it returns.

## How to retrieve

- Find the entry note(s) for the question (by id, `compound_smiles`, or tag), then
  **expand 1–2 hops** with `neighborhood(...)`. Relations are meaningful in both
  directions (a precursor references its product and vice versa), so traversal is
  undirected.
- `find_notes` requires every word you pass to appear *somewhere* in the note (not
  as one exact phrase, but each word can match anywhere) — a natural-language query
  like "what solvent gave the best yield" fails not because the data is missing but
  because no note literally contains "what", "gave", or "best". Query with the
  compound name, reaction type, or a specific term (e.g. "suzuki", "dioxane") instead
  of a full question, and expand from there. A first query returning nothing is a
  cue to retry with a different or narrower term, not to conclude the graph has no
  answer.
- Prefer **graph traversal over vector similarity** (D-004): a linked note is a
  stated relation, not a guess. Do not rank by embedding distance.
- Keep the hop count small (1–2). Going deeper pulls in weakly-related notes and
  dilutes the answer; if 2 hops don't reach the evidence, say what's missing.

## How to weigh what you find

- Respect `confidence` and the `valid_from`/`valid_to` window — do not present a
  low-confidence or expired note as established fact.
- Distinguish `created_by: human` (reviewed) from `created_by: agent` (proposed);
  an unmerged agent note is not yet trusted knowledge.
- **Cite the source note id** for every claim so the answer is traceable. If the
  graph has no note supporting a claim, say so rather than inventing one.
