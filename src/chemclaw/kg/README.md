# `chemclaw.kg` — layer 4, the knowledge graph

**Responsibility:** "what do we know" (D-004). Interlinked Markdown notes in Git — YAML frontmatter
for the structured, queryable half; a Markdown body whose `[[wikilinks]]` are the relations.
The reasoning path is graph traversal rather than top-k vector similarity, and that is the
*default* rather than the whole capability: since D-062 a deployment can also enter the graph
through a dense or lexical index (`retrieval_mode`, default `graph`). Those are entry points into
the traversal, never a replacement for it.

`note.py` is the schema and parser, `graph.py` the NetworkX indexer, `search.py` what a note's text
*is* for a substring search, `relations.py` and `crosslink.py` the link semantics, `validate.py`
the schema/link checker behind `make kg-validate`, `analytics.py` the derived views, `conflicts.py`
the contradiction detector, `render.py` a `Note` back to Markdown-with-frontmatter, and
`premise.py` whether the knowledge a held-open question rests on still holds (read by the durable
wait when it is asked and when it is answered). `crosslink.cited_calculations` is the one
definition of which calculations a note rests on, counting those it cites only through an artifact.

## Declared but unwired

`graph.related(graph, id, rel, as_of=)` — the *directed, one-relation, date-scoped* query D-134
exists to make possible ("which of this compound's precursors held on that date") — is complete,
tested, and called by nothing in `src/`. No agent tool, route or retriever exposes it;
`agent.graph_tools.expand_note` reports each neighbour's typed edges in both directions, which is
the same question in a less precise form. It is kept as the only read path for a capability a
merged ADR claims.

## One note per id, and one parse per corpus

Three properties of `graph.py` that readers depend on without being able to see them from a call
site, both established in `D-2026-08-16-a-cache-that-lets-every-caller-miss-together`:

- **A duplicate note id resolves to the first file in path order**, in `_parse_notes` and in
  `note_file_fingerprints` alike, so the served graph and the reindex diff name the same file. The
  loser is logged at WARNING and counted (`chemclaw_notes_duplicate_id_total`) — `kg-validate`
  fails a duplicate in the *repository*, which is not the tree a pod is serving.
- **Concurrent misses wait rather than duplicate.** The scan, the parse and the graph assembly for
  one directory happen under one re-entrant lock, so eight threads arriving cold produce one of
  each. Without it eight callers measured 6,219 ms against the 198 ms of the single parse they were
  all repeating.
- **A changed corpus is patched into the cached graph, not reassembled from it**
  (`D-2026-09-07-a-changed-note-is-not-a-changed-corpus`). Only the notes whose files moved are
  detached and re-attached, on a copy, so a note write costs a graph copy rather than re-adding
  every node and edge — measured at 20,000 notes, `build_graph` after touching one file falls from
  1,703 ms to 1,057 ms and the event-loop time it steals under `to_thread` from 1,137 ms to 638 ms.
  `_detach_note` removes a changed note's *out*-edges rather than its node, because
  `networkx.remove_node` takes the in-edges other notes own with it; `tests/test_graph.py` asserts
  the patched graph is identical to the rebuilt one rather than similar to it.

## This package is code; the graph is data

The notes live in `knowledge/` at the repository root — one directory per note type, and
`CHEMCLAW_NOTE_REPO_DIR` can point them at a dedicated checkout. Nothing here holds a note.

## `record.py` is the one write path

`record.py` and `git_writer.py` are the one mechanism by which anything agent-generated becomes
knowledge: the files are written into the tree readers scan, committed, and readable at once. It is
reused everywhere — job results, reports, distilled playbooks — rather than reimplemented per
feature.

**There is no review step, and that is a decision rather than an omission**
(`D-2026-09-05-the-gate-follows-behaviour-not-knowledge`): what stands in for review is provenance
plus correction. A note
carries `created_by: agent`, is served beside its own citations, and is refuted by a `contradicts`
edge or retired by `supersede` — so a wrong machine-written claim is *visible where it is used and
reversible*, which is a different guarantee from one that was checked before it landed. Say the
second thing about this package only if you mean it.

**The write order is part of the contract.** A direct write can be read mid-flight, so no reader
may see a note before what it cites. `_build_write` therefore writes dependencies, then
the subject, then the retirements — each cites the one before it — and its docstring states the
window that ordering accepts.
