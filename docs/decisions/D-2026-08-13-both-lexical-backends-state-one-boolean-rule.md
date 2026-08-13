# D-2026-08-13-both-lexical-backends-state-one-boolean-rule — The note index matches any term and ranks the complete matches first, in both backends

**Status:** accepted · **Date:** 2026-08-13 · Extends `D-138` §4 (the same rule, now on the second entry point into the same corpus).

## Context

`retrieval/vector_index.py` ships two implementations of one `NoteIndex` protocol, and the
in-memory one is called "the reference the tests use" in the module docstring. On the lexical leg
they answered a multi-word question differently:

- `PostgresNoteIndex._lexical` was `websearch_to_tsquery('english', %(q)s)`, which **ANDs** the
  terms: a note had to carry every one of them.
- `InMemoryNoteIndex.search_lexical` scored **any** note sharing a single token.

That is not a cosmetic mismatch, and the consequence only exists in production. A chemist's ordinary
four-word question matched nothing in the durable index, so the lexical leg contributed **zero**
chunks and the Reciprocal Rank Fusion the hybrid mode rests on ran one-legged — while every unit
test passed on the in-memory OR. **A test that cannot see the semantics of the backend it stands in
for is not a reference.**

Measured on a 15,000-note corpus with the four stems of *"amide coupling solvent screen"*: the AND
form matched **0 rows** where partial matches existed. This is the same class of finding as
`D-2026-08-01-a-cap-that-starves-a-source` — a retrieval leg everyone assumed was contributing
turned out to contribute nothing — and it is the second time it has been found on this corpus: `D-138`
§4 found `GraphRetriever` matching the query *verbatim*, so `gather_evidence("the biaryl")` returned
nothing against a corpus whose largest cluster is a biaryl campaign.

The open BACKLOG row named the disagreement and left the semantics undecided: *"decide which
semantics is wanted, then make both backends state it."* This ADR is that decision.

## Decision

**Match any term; rank the notes matching every term above the rest.** Stated in the `NoteIndex`
protocol docstring, implemented identically in both backends, and asserted by
`tests/test_hybrid_rrf.py` against a corpus built so the query's terms are split across notes.

The durable statement expresses it as **one** statement rather than a query-then-retry:

```sql
FROM note_index,
     websearch_to_tsquery('english', %(q)s) AS all_terms,
     (SELECT array_to_string(ARRAY(SELECT quote_literal(term) FROM
        unnest(tsvector_to_array(to_tsvector('english', %(q)s))) AS term), ' | ')::tsquery
     ) AS widened(any_terms)
WHERE lexeme @@ any_terms …
ORDER BY (lexeme @@ all_terms) DESC, score DESC, note_id
```

`ts_rank` over the widened query already ranks a full-coverage note above a partial one; the
explicit `lexeme @@ all_terms` sort key makes that ordering a **guarantee** rather than a tendency.

**The widened query is built from Postgres's own lexemes** (`tsvector_to_array` over the same
`to_tsvector`), not from Python tokens: it must OR exactly the stems the AND form would have
required, including this configuration's stemming and stop-word list. `quote_literal` is what makes
an arbitrary chemist's query safe to splice into a `tsquery` — a lexeme may contain any character
the parser emitted. A stop-word-only question has no lexemes to widen to and still returns nothing,
which is the one way "match any term" could have become "match anything"; that case has its own
test.

**This is the rule `GraphRetriever` already applies to the same corpus** (D-138: match per term,
widen to any term rather than answering "nothing known", let coverage order the result) — so the two
entry points into the knowledge graph now answer a multi-word question the same way.

## Why not the other three options

- **AND everywhere** (make the reference match the durable backend). It makes the tests honest and
  keeps the production defect: a four-term question still returns nothing, and the fusion still runs
  one-legged.
- **Query, then retry widened when the AND returns nothing.** Two round trips on exactly the
  question that is already the slow one, and it produces a *discontinuity*: a corpus with one
  complete match returns one row, and deleting that note suddenly returns fifty. The single
  statement degrades smoothly instead.
- **RRF as the fix.** The review that surfaced this proposed Reciprocal Rank Fusion for "the two
  lexical legs disagree on AND vs OR". Fusing by rank is genuinely what lets a cosine and a
  `ts_rank` combine without agreeing on a score scale — and this repository **already does it**
  (`retrieval/hybrid.py`, k=60). It is not a fix for this defect, because *a leg that returns no
  rows contributes nothing to any fusion rule.* `tests/test_hybrid_rrf.py` makes that distinction
  checkable rather than arguable: with a dense ranking that puts the fully-matching note last, the
  one-legged fusion returns `unrelated` first and the two-legged one returns `complete` first.

## Consequences

- **The cost is measured and it is the price of not returning nothing.** Same corpus, GIN index used
  in both plans (`Bitmap Index Scan`): 3.1 ms matching 5,000 rows (AND) against 12.4 ms matching
  10,000 (widened). The scan is proportional to how many notes share *any* term.
- **A stated residual: the two entry points widen differently, and the difference is bounded by
  `top_k`.** `GraphRetriever` returns complete matches *only* when any exist (`complete or scored`)
  and falls back to partials solely on an empty complete set; the index always returns both, with
  the complete ones ranked first. So the two agree on what ranks highest and can disagree on the
  tail — visible only when there are fewer complete matches than `top_k`. Making the index drop
  partials whenever a complete match exists would reintroduce the discontinuity rejected above, and
  the index feeds a *fusion* that reads a ranking rather than a verdict.
- **Scoped to the note index. The document-chunk index still disagrees.**
  `ingest/documents/index.py:639` is still `websearch_to_tsquery` alone against an in-memory
  reference that scores a shared-token *fraction*, so the identical defect remains open one file
  over. It is not fixed here because that index carries the `_ELIGIBLE` predicate and a citation
  projection, so the widening has to be re-measured against its plan rather than transplanted — and
  a change made on the strength of a measurement taken elsewhere is the thing this repository keeps
  finding. The BACKLOG row stays open, narrowed to that file.
- **The in-memory scores are still a proxy and are still allowed to be.** Tokens stand in for
  Postgres lexemes (no stemming, no stop-word list), so `ts_rank` and a token count will not agree
  on a number. The *ordering intent* and the *boolean rule* are what must match, and only the
  second of those is now a rule rather than a resemblance.
