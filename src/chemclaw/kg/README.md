# `chemclaw.kg` — layer 4, the knowledge graph

**Responsibility:** "what do we know" (D-004). Interlinked Markdown notes in Git — YAML frontmatter
for the structured, queryable half; a Markdown body whose `[[wikilinks]]` are the relations.
Retrieval is graph traversal, not top-k vector similarity.

`note.py` is the schema and parser, `graph.py` the NetworkX indexer, `relations.py` and
`crosslink.py` the link semantics, `validate.py` the schema/link checker behind `make kg-validate`,
`analytics.py` the derived views, `conflicts.py` the contradiction detector.

## This package is code; the graph is data

The notes live in `knowledge/` at the repository root — one directory per note type, and
`CHEMCLAW_NOTE_REPO_DIR` can point them at a dedicated checkout. Nothing here holds a note.

## The PR-gate is the GxP line

`pr_gate.py` and `git_submitter.py` are the one mechanism by which anything agent-generated becomes
knowledge: the agent opens a pull request, a human validates, a merge makes it true. That is "AI
proposes, human signs off", and it is reused everywhere — job results, reports, distilled
playbooks — rather than reimplemented per feature. An agent never writes to the graph directly.
