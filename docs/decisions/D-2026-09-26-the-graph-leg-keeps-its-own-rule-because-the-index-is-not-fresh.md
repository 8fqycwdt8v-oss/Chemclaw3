# D-2026-09-26-the-graph-leg-keeps-its-own-rule-because-the-index-is-not-fresh — what `graph` means as a retrieval source

**Status:** accepted · **Date:** 2026-09-26 · Closes the `BACKLOG.md` row *"What `graph` means as a
retrieval source: a lexical rule of its own, or the leg that reads the index"* (issue #465) by
**declining** the second option.

## Context

Two lexical rankers run over the one note corpus: `retrieval/retrievers.py::GraphRetriever` (and
`agent/graph_tools.py::_scan_notes` behind `find_notes`) score `kg.search.term_coverage` plus
`_relevance` in process, and `LexicalRetriever` asks Postgres for `ts_rank` over `note_index`. The
row's evidence, from `make retrieval-arms` on 2026-09-16 (20 probes, 46 labelled pairs, matched slot
budget, one leg at `retrieval_top_k=24`): the Postgres leg alone finds **42** gold notes to the graph
leg's **40**, 24 in the top 3 to 19, and the graph leg finds **no** gold note Postgres misses. The row
offered two answers: `graph` keeps a lexical rule of its own, or it becomes the leg that reads the
index and the reindex stops being conditional on `lexical`/`vector` being enabled.

Those figures were not re-taken for this decision: the gate's shared virtualenv had a broken RDKit
install when the arms were run, and the deciding fact below is one the arms cannot measure at all.

## Decision

**`graph` keeps its own rule. Reading the index is declined.**

**1. The index is not fresh, and `make retrieval-arms` is blind to that by construction.** The only
writer of `note_index` is `NoteReindexWorkflow`, on an hourly Schedule
(`note_reindex_schedule_minutes`); nothing reindexes on a note write — `reindex_notes` has no caller
but `durable/note_index.py` and the arms CLI. The arms rebuild the index in full before measuring, so
their recall is recall over a corpus that is never stale. In production the graph leg reads the files
on disk, so a note the agent just recorded is retrievable on its next call. Read through the index it
would be invisible to `gather_evidence` — and to `find_notes`, which has to move with the graph leg or
re-open `D-2026-08-05` (the model finds a note the evidence sweep cannot cite) — for up to an hour,
and **forever** wherever no Temporal worker runs the Schedule (the CLI, a dev loop).

**2. It would put a hard Postgres dependency on the default retrieval path.** The shipped deployment
is `graph,eln-json`, and today its note leg needs no database. The offline `retrieval_recall` eval
that CI gates on (`make eval-strict`, `evals/retrieval.py` scoring `GraphRetriever` over a fixture
corpus) and the test files that drive `GraphRetriever` over temporary note trees all run without one.

**3. The gain does not pay for either.** It is two gold notes out of 46, at a matched slot budget.

A write-path upsert (`record_note` → incremental reindex of the note it wrote) would answer the first
cost, and is declined as new scope here: it couples every knowledge writer to the embedding endpoint
and the database, with failure modes of its own, for the same two notes.

**Revisit when:** a write-path incremental reindex of `note_index` exists for another reason (a
caller of `retrieval/vector_index.py::reindex_notes` outside `durable/note_index.py` and
`cli/retrieval_arms.py`), **or** `make retrieval-arms` at a matched slot budget shows the Postgres
leg finding at least **4** gold notes (of the labelled set) that the graph leg misses.

## What this leaves true

The two rules stay different rather than better and worse (`tests/test_note_search.py` pins both
directions), both legs ship, `note_reindex_effective` stays derived from the source list, and
`_relevance` stays — removing it alone was already measured as a regression (graph-alone mean gold
rank 4.72 → 5.67, and one gold note lost from the shipped three-leg arm).
