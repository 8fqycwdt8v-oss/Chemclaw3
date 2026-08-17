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

- `graph.related(graph, id, rel, as_of=)` — the *directed, one-relation, date-scoped* query D-134
  exists to make possible ("which of this compound's precursors held on that date"). No agent tool,
  route or retriever exposes it. What is no longer unwired is the edge data underneath it: since
  `D-2026-08-16-a-cache-that-lets-every-caller-miss-together`, `agent.graph_tools.expand_note`
  reports each neighbour's typed edges in both directions, so a `contradicts` or `supersedes`
  neighbour is legible to the model as one. `related` remains the precise form of the same
  question, and remains uncalled.
- `crosslink.calc_ref_index` / `crosslink.notes_for_calculation` — the reverse lookup from a
  calculation to the notes resting on it (STO-7), for the day a stale calculation needs its
  dependents found.

Both are kept rather than deleted: each is the only read path for a capability a merged ADR
claims, and deleting one deletes the claim with it.

## One note per id, and one parse per corpus

Two properties of `graph.py` that readers depend on without being able to see them from a call
site, both established in `D-2026-08-16-a-cache-that-lets-every-caller-miss-together`:

- **A duplicate note id resolves to the first file in path order**, in `_parse_notes` and in
  `note_file_fingerprints` alike, so the served graph and the reindex diff name the same file. The
  loser is logged at WARNING and counted (`chemclaw_notes_duplicate_id_total`) — `kg-validate`
  fails a duplicate in the *repository*, which is not the tree a pod is serving.
- **Concurrent misses wait rather than duplicate.** The scan, the parse and the graph assembly for
  one directory happen under one re-entrant lock, so eight threads arriving cold produce one of
  each. Without it eight callers measured 6,219 ms against the 198 ms of the single parse they were
  all repeating.

## This package is code; the graph is data

The notes live in `knowledge/` at the repository root — one directory per note type, and
`CHEMCLAW_NOTE_REPO_DIR` can point them at a dedicated checkout. Nothing here holds a note.

## The PR-gate is the review line

`pr_gate.py` and `git_submitter.py` are the one mechanism by which anything agent-generated becomes
knowledge: the agent opens a pull request, a human validates, a merge makes it true. That is "the
agent proposes, a human decides", and it is reused everywhere — job results, reports, distilled
playbooks — rather than reimplemented per feature. An agent never writes to the graph directly.
