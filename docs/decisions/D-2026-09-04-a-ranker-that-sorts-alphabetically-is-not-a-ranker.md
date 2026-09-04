# D-2026-09-04-a-ranker-that-sorts-alphabetically-is-not-a-ranker — what six reviews of the knowledge loop measured

**Status:** accepted · **Date:** 2026-09-04

## Context

Six fresh-context reviews of the knowledge capture → storage → retrieval path, each against a live
Postgres and Temporal, each asked to measure rather than to read. The question behind them was
whether knowledge is automatically captured and reliably used.

The honest one-line answer they converged on: **data is captured automatically; conclusions are
not — and what is captured is retrieved by a ranker that sorts alphabetically and reports no
truncation.** Every mechanism in this loop exists and most are well argued. The loop does not close
because the pieces disagree with each other, and nothing measured the disagreement.

## What was measured

- **The graph leg had no relevance signal.** It ranked `(-coverage, -confidence, note.id)`. On any
  query whose hits all match every term — the ordinary two- or three-word question — coverage is
  constant, and `confidence` is a *trust* signal that ties constantly: over the 38 notes in
  `knowledge/` it takes ten distinct values and 18 notes share one of two. So the ranking fell
  through to the note id. Measured on 5,000 matching notes it returned `reaction-00000..00002`; on a
  fixture where three notes answer the question and 250 merely mention its words, it returned eight
  of the 250 and none of the three. It is the only retriever enabled by default.
- **The lexical reference and Postgres disagreed on numbers.** `to_tsvector` glues a sign onto a
  lexeme, so `charged at -78 C` indexes as `'-78'`: querying `78` misses it, and querying `-78`
  renders as `!'78'` — an exclusion matching nearly every *other* row. A cryogenic temperature was
  reachable by no query at all. Decimals diverged the other way, with the in-memory reference
  over-matching. `core/fulltext.py` had zero test files while measuring 100% coverage.

  **The tokeniser mutant this was found through was closed independently on `main` (#308) while
  this branch was open, and that is worth recording rather than quietly merging.** Both branches
  wrote a `tests/test_fulltext.py`; the merge keeps both, because they are different *kinds* of
  test and each is blind to the other's defect. The offline suite pins the tokeniser against
  mutation and cannot see whether the proxy and the server agree — every one of its assertions
  passed while the numeric divergence above was live. Only a test that asks both backends the same
  question can see that class, which is the argument the module's own docstring has been making
  about this rule since the first time its two halves disagreed.
- **A transient parse failure deleted derived-index rows** — 40 of 100 broken notes retired 40 rows.
- **One oversized note froze both derived legs indefinitely**, because every changed note was
  embedded in one call and the `tsvector` is written in the same `INSERT`.
- **A rejected note could be re-pushed**, leaving a live mergeable branch in no review queue; a
  later merge left the record reading *rejected* for a note serving as evidence.
- **`reaction_records` lost its `reaction_id` index** in migration 056: 0.030 ms against a 17.076 ms
  parallel seq scan at 200,000 rows.
- **Compaction cleared the evidence sweep first** — it is oldest and largest by design — so the
  model was asked to cite what it could no longer see.
- **The starved-source metric was pinned at zero** for every leg but one, because both merge paths
  keep the first occurrence of a note and `chunk.retriever` names only its first finder. On a
  healthy three-leg corpus it read `graph 16, lexical 0, vector 0` — indistinguishable from the
  starvation it exists to detect.

## Decision

Fix each at its root, and pin each with a test that fails on the code it replaces.

Two choices are worth recording because the obvious form of each is wrong:

**Normalise numbers on both sides, never only on the document.** An identifier survives today
*because* the query fragments exactly as the document did — a CAS number indexes as `'108' '-24'
'-7'` and `websearch_to_tsquery` renders the same phrase. Normalising documents alone would have
moved one side and broken every CAS and lot-number lookup that currently works. This was caught by
one review's measured negative after another review had already written the document-only fix.
The cost is stated: a query meaning "exclude the number 78" now reads as "find 78", which on a
corpus of temperatures and equivalents is the reading a chemist intends and the only one under
which a negative quantity is searchable at all.

**Demote trust rather than discard it.** `confidence` decides among *equally relevant* notes, which
is all KM-5 could honestly decide. Relevance is saturating term frequency weighted by inverse
document frequency — BM25 without length normalisation, because `search_text` length tracks how
much a note *records* rather than how padded it is.

**Preserve the citation index through a clearing.** `agent/compaction.py` declined `exclude_tools`
on the grounds that excluding evidence sweeps would exclude the results the edit exists to reclaim.
That premise is right and the conclusion does not follow: it conflates the sweep's *bulk* (chunk
bodies, correctly reclaimable) with its *index* (~60 tokens of note ids that decide whether the
answer can cite anything). Reading one field of one first-party model is not "coupling the context
policy to the shape of every tool's result" — one known shape is not every shape.

## Consequences

- The default profile's context floor moves 42,730 → 43,063 estimated tokens against the 43,500
  ceiling, for the retrieval and capture obligations added to the system prompt. The ceiling is
  **not** raised: a ratchet bumped to accommodate the change that tripped it is a ratchet that
  turns freely.
- `retrieval_top_k` still cuts, and **the cut is still invisible**. Reporting it honestly is a
  contract change across four retrievers, the fan-out, the report harness and their tests; doing it
  statefully on the retriever, or by repeating the count on every chunk, would be worse than not
  doing it. It is a `BACKLOG.md` row carrying its measurement rather than a half-fix here.
- An existing probe that re-measures the starved-source ratio now reads 25 graph / 8 lexical /
  7 dense where the ADR it cites recorded 38 / 0 / 2.

## What this deliberately does not fix

RRF's `k=60` over 8-item lists (a 1.11x within-source spread against a 2.00x agreement bonus, while
two of the three "independent" rankers are the same rule over the same corpus — a tuning decision
plus a design question); the O(corpus) `git worktree add` behind every proposal (2.9 s each at 10k
notes, and the obvious fix replaces a security-relevant containment check); `random_page_cost` and
the dense leg's plan flip (a deployment tuning requirement, not code); the 6-note retrieval gold
corpus, which is smaller than `retrieval_top_k` and therefore structurally cannot see the ranking
defect above; and the capture half — no end-of-turn distillation, no `record_job` for template jobs,
`publish_to_graph` true for 1 of 14 connector jobs. Each is a `BACKLOG.md` row with its measurement.
