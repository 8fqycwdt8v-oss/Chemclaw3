# `chemclaw.kg` — layer 4, the knowledge graph

**Responsibility:** "what do we know" (D-004). Interlinked Markdown notes in Git — YAML frontmatter
for the structured, queryable half; a Markdown body whose `[[wikilinks]]` are the relations.
The reasoning path is graph traversal rather than top-k vector similarity, and that is the
*default* rather than the whole capability: since D-062 a deployment can also enter the graph
through a dense or lexical index (`retrieval_mode`, default `graph`). Those are entry points into
the traversal, never a replacement for it — but the sentence here used to state the absolute, and
an absolute with an unnamed condition is how a README stops being true without changing.

`note.py` is the schema and parser, `graph.py` the NetworkX indexer, `search.py` what a note's text
*is* for a substring search, `relations.py` and `crosslink.py` the link semantics, `validate.py`
the schema/link checker behind `make kg-validate`, `analytics.py` the derived views, `conflicts.py`
the contradiction detector.

## Declared but unwired

Two capabilities here are complete, tested, and called by nothing in `src/`. They are named
because a reader who finds them will otherwise assume one of the two wrong things — that they are
dead, or that something depends on them:

- `graph.related(graph, id, rel, as_of=)` — the typed-edge query D-134 exists to make possible
  ("what are this compound's precursors", "what does this note contradict"). No agent tool, route
  or retriever exposes it yet.
- `crosslink.calc_ref_index` / `crosslink.notes_for_calculation` — the reverse lookup from a
  calculation to the notes resting on it (STO-7), for the day a stale calculation needs its
  dependents found.

Both are kept rather than deleted: each is the only read path for a capability a merged ADR
claims, and deleting one deletes the claim with it.

## This package is code; the graph is data

The notes live in `knowledge/` at the repository root — one directory per note type, and
`CHEMCLAW_NOTE_REPO_DIR` can point them at a dedicated checkout. Nothing here holds a note.

## The PR-gate is the review line

`pr_gate.py` and `git_submitter.py` are the one mechanism by which anything agent-generated becomes
knowledge: the agent opens a pull request, a human validates, a merge makes it true. That is "the
agent proposes, a human decides", and it is reused everywhere — job results, reports, distilled
playbooks — rather than reimplemented per feature. An agent never writes to the graph directly.
